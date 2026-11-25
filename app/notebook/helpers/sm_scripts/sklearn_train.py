#!/usr/bin/env python
"""
SageMaker SCRIPT-MODE training entry-point for the `sklearn` algorithm.

This file does NOT run in the notebook VM — it is uploaded to S3 (as the script-mode
source dir) by app.notebook.helpers.sagemaker.sm_train(algorithm="sklearn") and
executed INSIDE AWS's managed `sagemaker-scikit-learn` framework container on the
training instance. So it must be fully self-contained: only the standard library +
scikit-learn + pandas + joblib (all present in that container). It must NOT import
anything from `app.*` — that package doesn't exist on the training instance.

Contract (matches the in-VM train_model + the XGBoost sm_train path so predict() takes
RAW rows everywhere):
  • reads the RAW training CSV (header + the target column) from the `train` channel
    (SM_CHANNEL_TRAIN);
  • builds a Pipeline(ColumnTransformer(one-hot categoricals [+ optional scale on
    numerics]) -> estimator) — the SAME preprocessing shape as train_model;
  • fits it on all rows and joblib.dumps the WHOLE Pipeline to
    {SM_MODEL_DIR}/model.joblib.
The resulting artifact is a stock scikit-learn Pipeline, so (a) sm_load_model can
joblib.load it directly (no custom rewrap class) and (b) the custom serving container's
existing `.joblib` path serves it unchanged.

Hyperparameters (passed by sm_train as SageMaker HyperParameters, read here as
--flags; SageMaker renders every hyperparameter as a CLI arg):
  --target        name of the label column in the CSV            (required)
  --task          'classification' | 'regression'                (required)
  --estimator     fixed menu key (see _ESTIMATORS)               (required)
  --features      comma-separated feature columns, or '' = all-but-target
  --categorical   comma-separated categorical columns, or '' = auto-detect
  --numeric       comma-separated numeric columns, or '' = the rest
  --scale         '1' to StandardScaler the numerics, else '0'
  --n-estimators / --max-depth / --C / --alpha  estimator knobs (menu-dependent)
"""
import argparse
import json
import os

import joblib
import pandas as pd
from pandas.api.types import is_numeric_dtype
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


def _ohe():
    """OneHotEncoder dense, ignoring unseen categories at inference (robust predict).
    sparse_output= (>=1.2) vs sparse= (<1.2) compatibility, same as train_model."""
    try:
        return OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    except TypeError:  # pragma: no cover
        return OneHotEncoder(handle_unknown="ignore", sparse=False)


def _build_estimator(key, task, args):
    """Instantiate one of the FIXED-MENU estimators. The menu mirrors the choices
    train_model would make (RandomForest default), but as named keys since script mode
    can't receive a live estimator object. Each key is task-aware (classifier vs
    regressor). Raises on an unknown key (sm_train validates first, but be safe)."""
    key = (key or "random_forest").lower()
    rs = 42
    clf = task == "classification"

    if key == "random_forest":
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        kw = {"random_state": rs, "n_estimators": args.n_estimators}
        if args.max_depth:
            kw["max_depth"] = args.max_depth
        return RandomForestClassifier(**kw) if clf else RandomForestRegressor(**kw)

    if key == "gradient_boosting":
        from sklearn.ensemble import (GradientBoostingClassifier,
                                      GradientBoostingRegressor)
        kw = {"random_state": rs, "n_estimators": args.n_estimators}
        if args.max_depth:
            kw["max_depth"] = args.max_depth
        return GradientBoostingClassifier(**kw) if clf else GradientBoostingRegressor(**kw)

    if key in ("logistic", "linear"):
        # 'logistic' = LogisticRegression (classification); 'linear' = LinearRegression
        # (regression). Accept either name and resolve by task so a user who says
        # estimator='linear' on a classification target still gets a sane linear model.
        if clf:
            from sklearn.linear_model import LogisticRegression
            return LogisticRegression(max_iter=1000, C=args.C)
        from sklearn.linear_model import LinearRegression
        return LinearRegression()

    if key == "ridge":
        if clf:
            from sklearn.linear_model import RidgeClassifier
            return RidgeClassifier(alpha=args.alpha)
        from sklearn.linear_model import Ridge
        return Ridge(alpha=args.alpha)

    if key == "xgboost":
        # The xgboost Python library's scikit-learn API — a drop-in sklearn estimator,
        # so it fits inside the same Pipeline(ColumnTransformer -> estimator) and the
        # artifact stays a plain .joblib. Requires xgboost in the training container
        # (shipped via the script-mode requirements.txt, pinned to the serving
        # container's major.minor).
        from xgboost import XGBClassifier, XGBRegressor
        kw = {"random_state": rs, "n_estimators": args.n_estimators,
              "tree_method": "hist"}   # CPU hist — matches the CPU serving wheel
        if args.max_depth:
            kw["max_depth"] = args.max_depth
        if clf:
            # Silence the deprecated label-encoder path + keep logloss as the metric.
            return XGBClassifier(eval_metric="logloss", **kw)
        return XGBRegressor(**kw)

    if key == "lightgbm":
        # LightGBM's scikit-learn API — same story as xgboost: a drop-in sklearn
        # estimator inside the Pipeline, artifact stays a plain .joblib. Requires
        # lightgbm in the training container (script-mode requirements.txt, pinned to
        # the serving container's major). verbose=-1 silences its per-iteration chatter.
        from lightgbm import LGBMClassifier, LGBMRegressor
        kw = {"random_state": rs, "n_estimators": args.n_estimators, "verbose": -1}
        if args.max_depth:
            kw["max_depth"] = args.max_depth
        return LGBMClassifier(**kw) if clf else LGBMRegressor(**kw)

    raise ValueError(f"unknown estimator key {key!r}")


def _split(csv_arg):
    """'a,b,c' -> ['a','b','c']; '' / None -> []."""
    if not csv_arg:
        return []
    return [c for c in (s.strip() for s in csv_arg.split(",")) if c]


def _is_channel_noise(name):
    """True for channel entries that are NOT training data: hidden files, the Athena
    UNLOAD manifest/metadata sidecars, and SageMaker's own markers. Skipped so an
    extensionless-but-real data part isn't crowded out and a manifest isn't parsed."""
    low = name.lower()
    if name.startswith(".") or low.endswith(".manifest") or low.endswith("-manifest") \
            or low.endswith("manifest.csv") or low.endswith(".metadata"):
        return True
    return False


def _looks_parquet(path):
    """Sniff a file's first 4 bytes for the Parquet magic 'PAR1'. Athena UNLOAD writes
    EXTENSIONLESS parquet objects (a bare query-id hash), so the extension alone can't
    classify them — the magic bytes can."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"PAR1"
    except OSError:
        return False


def _load_channel(train_dir):
    """Read EVERY data file under the train channel (recursively) and concat into one
    DataFrame. A channel is one VM-uploaded CSV (DataFrame path) OR many part files
    from an S3 prefix / Athena UNLOAD (csv or parquet). Classification is by extension
    when present, else by CONTENT SNIFF — because Athena UNLOAD part files are
    EXTENSIONLESS (a bare query-id hash). Mixed is allowed. Raises if none are found."""
    csvs, parqs = [], []
    for root, _dirs, files in os.walk(train_dir):
        for f in sorted(files):
            if _is_channel_noise(f):
                continue
            low = f.lower()
            full = os.path.join(root, f)
            if low.endswith(".parquet") or low.endswith(".pq"):
                parqs.append(full)
            elif low.endswith(".csv"):
                csvs.append(full)
            # Extensionless (or unknown ext): sniff the bytes. Athena UNLOAD parquet
            # lands here; a non-parquet blob is treated as CSV text.
            elif _looks_parquet(full):
                parqs.append(full)
            else:
                csvs.append(full)
    if not csvs and not parqs:
        raise FileNotFoundError(
            f"No data files found in train channel {train_dir!r}")
    frames = []
    for p in sorted(parqs):
        frames.append(pd.read_parquet(p))
    for p in sorted(csvs):
        frames.append(pd.read_csv(p))
    print(f"[sklearn_train] channel files: {len(parqs)} parquet, {len(csvs)} csv")
    return pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", required=True)
    p.add_argument("--task", required=True, choices=["classification", "regression"])
    p.add_argument("--estimator", default="random_forest")
    p.add_argument("--features", default="")
    p.add_argument("--categorical", default="")
    p.add_argument("--numeric", default="")
    p.add_argument("--scale", default="0")
    p.add_argument("--n-estimators", dest="n_estimators", type=int, default=100)
    p.add_argument("--max-depth", dest="max_depth", type=int, default=0)  # 0 = default
    p.add_argument("--C", dest="C", type=float, default=1.0)
    p.add_argument("--alpha", dest="alpha", type=float, default=1.0)
    # SageMaker injects these dirs via env; argparse fallbacks keep local runs easy.
    p.add_argument("--train", default=os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train"))
    p.add_argument("--model-dir", dest="model_dir",
                   default=os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
    # parse_known_args (not parse_args): SageMaker's script-mode toolkit may inject
    # additional reserved hyperparameters as --flags (e.g. sagemaker_* housekeeping
    # keys the container didn't fully strip). We only care about OUR flags; ignore
    # the rest so an unexpected injected flag can't crash the whole training job.
    args, _unknown = p.parse_known_args()
    if _unknown:
        print(f"[sklearn_train] ignoring unrecognized args: {_unknown}")
    if args.max_depth == 0:
        args.max_depth = None

    # --- Load ALL training files in the channel (CSV and/or Parquet) ---------------
    # The channel may hold ONE file (a VM-uploaded CSV from the DataFrame path) or
    # MANY (an S3 prefix / Athena UNLOAD output — part files written in parallel, in
    # csv or parquet). Walk the channel dir recursively, read every data file, and
    # concat into one frame. This is where the data finally lands in memory — on the
    # TRAINING instance (sized via instance=), never in the notebook VM.
    df = _load_channel(args.train)
    print(f"[sklearn_train] loaded {df.shape[0]} rows x {df.shape[1]} cols from "
          f"channel {args.train!r}")

    target = args.target
    if target not in df.columns:
        raise KeyError(f"target column {target!r} not in CSV columns {list(df.columns)}")
    df = df.dropna(subset=[target])

    # --- Resolve feature columns + the categorical/numeric split -------------------
    feat_cols = _split(args.features) or [c for c in df.columns if c != target]
    feat_cols = [c for c in feat_cols if c != target]
    if not feat_cols:
        raise ValueError("no feature columns after excluding the target")

    X = df[feat_cols]
    categorical = _split(args.categorical)
    if not categorical:
        categorical = [c for c in feat_cols
                       if X[c].dtype == bool or not is_numeric_dtype(X[c])]
    else:
        categorical = [c for c in categorical if c in feat_cols]
    numeric = _split(args.numeric) or [c for c in feat_cols if c not in categorical]
    numeric = [c for c in numeric if c in feat_cols]

    num_step = StandardScaler() if str(args.scale) in ("1", "true", "True") else "passthrough"
    pre = ColumnTransformer(
        transformers=[("cat", _ohe(), categorical), ("num", num_step, numeric)],
        remainder="drop",
    )

    estimator = _build_estimator(args.estimator, args.task, args)
    model = Pipeline([("pre", pre), ("model", estimator)])
    # Same raw-row marker train_model stamps, so save_model / the serving container's
    # schema probe recognize this as a raw-row Pipeline.
    model._microvm_rawrow = True

    y = df[target]
    if args.task == "classification":
        # Keep labels as-is (strings/ints) — sklearn classifiers carry classes_ natively,
        # which the serving container + model_info read back for proba columns.
        y = y.astype("category") if y.dtype == object else y
    else:
        y = pd.to_numeric(y, errors="coerce")

    print(f"[sklearn_train] fitting {type(estimator).__name__} "
          f"(task={args.task}, {len(categorical)} cat / {len(numeric)} num features)")
    model.fit(X, y)

    os.makedirs(args.model_dir, exist_ok=True)
    out = os.path.join(args.model_dir, "model.joblib")
    joblib.dump(model, out)
    print(f"[sklearn_train] wrote {out}")

    # A tiny sidecar for debuggability (not required by the loader). Best-effort.
    try:
        meta = {
            "task": args.task,
            "estimator": type(estimator).__name__,
            "feat_cols": feat_cols,
            "categorical": categorical,
            "numeric": numeric,
            "classes": [str(c) for c in getattr(estimator, "classes_", [])]
            if args.task == "classification" else None,
        }
        with open(os.path.join(args.model_dir, "microvm_meta.json"), "w") as fh:
            json.dump(meta, fh)
    except Exception as e:  # pragma: no cover
        print(f"[sklearn_train] meta sidecar skipped: {e}")


if __name__ == "__main__":
    main()
