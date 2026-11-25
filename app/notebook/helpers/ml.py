"""MACHINE LEARNING — classic model I/O + inference (scikit-learn, joblib, ONNX).

Pre-loaded helpers for the "bring your own trained model" workflow: pull a fitted
model artifact from S3 (or /tmp), run predictions on a DataFrame, and inspect what
the model expects — all as one-liners that handle the download, deserialization,
and DataFrame<->array marshaling for you.

This complements the Bedrock LLM helpers in llm.py (invoke_llm, llm_map, ...). Those
call foundation models over an API; the helpers here run a CLASSIC model (a fitted
scikit-learn / xgboost / lightgbm estimator, or an ONNX graph) IN-PROCESS inside the
MicroVM.

What's baked into the image: scikit-learn + joblib + numpy + scipy, so joblib/pickle
models load and predict with no setup. ONNX inference (onnxruntime) is installed on
demand the first time you call load_onnx() — it's a larger dependency, so we don't
carry it in every VM.

Usage (in any cell):
    model = load_model("my-bucket", "models/churn.joblib")   # or an s3:// URI
    preds = predict_batch(model, df, output_col="churn_pred")
    model_info(model)          # what features/classes does it expect?

Security / sandbox notes:
    • Deserializing a pickle/joblib artifact EXECUTES arbitrary code from the file.
      Only load models from artifacts you trust (your own training runs). This is an
      inherent property of pickle, not specific to these helpers.
    • Downloads land in /tmp (the sandbox's only writable path), same as download_s3.
"""

import io
import os
import sys
import subprocess

import boto3
import numpy as np
import pandas as pd

from ._common import _log_read, _log_write, logger
from ._common import _REGION
from ..aws_clients import aws_region as _aws_region
from . import _s3
from .readers import _resolve_s3_target


# Model artifact extensions we know how to (de)serialize. Anything else falls back
# to joblib.load (which also reads plain pickles), with a clear error if that fails.
_JOBLIB_EXTS = (".joblib", ".jbl")
_PICKLE_EXTS = (".pkl", ".pickle", ".sav")
_ONNX_EXTS = (".onnx",)


def _ext_of(key: str) -> str:
    """Lower-cased file extension (incl. the dot) of an S3 key / local path."""
    base = str(key).rsplit("/", 1)[-1]
    return ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""


def _is_rawrow_pipeline(model) -> bool:
    """True if `model` is a RAW-ROW scikit-learn Pipeline — one that does its OWN
    preprocessing (one-hot/scale) internally, so callers feed it raw rows (mixed
    string + numeric columns) instead of a pre-encoded matrix. This is exactly what
    train_model() returns.

    Detected by EITHER a train_model marker (`_microvm_rawrow`) OR the structural
    signature: an sklearn Pipeline that contains a ColumnTransformer step (the seam
    that maps named raw columns → encoded features). A bare estimator
    (RandomForestClassifier, etc.) or a Pipeline without a ColumnTransformer is NOT
    raw-row — it expects a pre-encoded matrix."""
    if getattr(model, "_microvm_rawrow", False):
        return True
    try:
        from sklearn.pipeline import Pipeline
        from sklearn.compose import ColumnTransformer
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(model, Pipeline):
        return False
    return any(isinstance(step, ColumnTransformer) for _, step in model.steps)


def _warn_if_not_rawrow_pipeline(model) -> None:
    """Warn (do NOT raise) when saving a scikit-learn model that is NOT a raw-row
    Pipeline. A pre-encoded model (bare estimator expecting a one-hot matrix) is
    fragile to ship: whoever loads it — including a SageMaker endpoint — must
    reproduce the EXACT encoding (same columns, same order), which the artifact
    doesn't carry. train_model() returns a raw-row Pipeline that bakes preprocessing
    in, so it's callable with raw rows anywhere (mirrors the guarantee to_onnx gives
    for ONNX). Only warns for actual sklearn-style estimators (has .predict); other
    objects pass through untouched."""
    if _is_rawrow_pipeline(model):
        return
    # Only nag about things that look like a model (have .predict); leave anything
    # else (a dict, a custom object) alone — save_model is still a general serializer.
    if not hasattr(model, "predict"):
        return
    import warnings
    warnings.warn(
        f"Saving a {type(model).__name__} that is NOT a raw-row Pipeline. It expects "
        "a PRE-ENCODED feature matrix, so anyone loading it (including a SageMaker "
        "endpoint via sm_deploy_*) must reproduce the exact one-hot encoding by hand — "
        "the artifact doesn't carry it. Prefer train_model(df, target), which returns "
        "a Pipeline that one-hots internally so it takes RAW rows. Saving anyway.",
        stacklevel=3,
    )


def _deserialize_model(data: bytes, ext: str):
    """Turn raw artifact bytes into a live model object, choosing the loader by
    extension. joblib.load reads BOTH joblib dumps and plain pickles, so it's the
    default; we only special-case nothing else here (ONNX has its own path). Raises
    a clear, actionable error when the backing library for a pickled object isn't
    installed (e.g. an xgboost model but xgboost isn't in this VM)."""
    import joblib
    buf = io.BytesIO(data)
    try:
        return joblib.load(buf)
    except ModuleNotFoundError as e:
        # The pickle referenced a library that isn't installed in this VM (e.g.
        # xgboost, lightgbm). Tell the user exactly how to fix it.
        missing = e.name or "the model's library"
        raise ModuleNotFoundError(
            f"This model was built with '{missing}', which isn't installed in this "
            f"sandbox. Install it first, e.g. run in a cell:  "
            f"install_package('{missing}')  — then reload the model."
        ) from e


# =============================================================================
# MACHINE LEARNING / LOCAL — model I/O (load / save a trained model, S3 + local)
# =============================================================================

@_log_read("s3")
def load_model(bucket: str, key: str = None):
    """
    Load a trained model from S3 into a live object you can call .predict() on.

    Accepts either (bucket, key) or a single full S3 URI / path:
        model = load_model("my-bucket", "models/churn.joblib")
        model = load_model("s3://my-bucket/models/churn.joblib")

    Format is detected from the extension:
        .joblib/.jbl/.pkl/.pickle/.sav  → joblib.load (reads joblib dumps AND plain
                                          pickles — the common scikit-learn case)
        .onnx                           → use load_onnx() instead (returns a runtime
                                          session, not a Python estimator)

    Deserializing executes code from the artifact — only load models you trust.
    Returns the model object (e.g. a fitted scikit-learn estimator).
    """
    bucket, key = _resolve_s3_target(bucket, key)
    ext = _ext_of(key)
    if ext in _ONNX_EXTS:
        raise ValueError(
            "This is an ONNX model — call load_onnx(bucket, key) instead, which "
            "returns an inference session (use onnx_predict to run it)."
        )
    data = _s3.get_bytes(bucket, key, region=_aws_region())
    return _deserialize_model(data, ext)


def load_model_local(path: str):
    """
    Load a trained model from a local /tmp file (e.g. one you downloaded earlier
    with download_s3, or trained in a previous cell and saved with save_model_local).

        model = load_model_local("/tmp/churn.joblib")

    Format detected from the extension (see load_model). Returns the model object.
    """
    ext = _ext_of(path)
    if ext in _ONNX_EXTS:
        raise ValueError("This is an ONNX model — use load_onnx / onnx_predict.")
    with open(path, "rb") as f:
        data = f.read()
    return _deserialize_model(data, ext)


def _serialize_model(model, ext: str) -> bytes:
    """Serialize a model to bytes, choosing the format by target extension so
    save_model / save_model_local are symmetric with load_model / load_onnx:
        .onnx  → the ONNX graph's own bytes (an onnx.ModelProto via
                 SerializeToString(), or bytes/bytearray passed through)
        else   → joblib (the portable scikit-learn/pickle format)."""
    if ext in _ONNX_EXTS:
        # Already-serialized bytes → pass through; an onnx ModelProto → its bytes.
        if isinstance(model, (bytes, bytearray)):
            return bytes(model)
        if hasattr(model, "SerializeToString"):
            return model.SerializeToString()
        raise TypeError(
            "Saving to a .onnx key expects an ONNX model (onnx.ModelProto, e.g. "
            "from skl2onnx.to_onnx) or its serialized bytes. For a scikit-learn "
            "estimator, save to a .joblib key instead."
        )
    import joblib
    # PORTABILITY: the artifact must load where dill ISN'T installed (a SageMaker
    # serving container, a plain-sklearn consumer). The VM uses dill (checkpoint/
    # restore), and importing dill patches the global pickle reduce path so joblib
    # would otherwise emit dill opcodes for builtin types like `slice` (a
    # ColumnTransformer's output_indices_) — making the artifact unloadable without
    # dill (observed: endpoint /ping 500, "No module named 'dill'", 180s timeout).
    # The process-wide fix is dill.extend(use_dill=False) at VM startup (app/server.py),
    # which reverts pickle/joblib to stdlib behaviour while keeping dill's own Pickler
    # working. We ALSO assert it here defensively (idempotent, cheap) so a save is
    # dill-free even if this code runs in a process that imported dill another way.
    _ensure_dill_free_pickling()
    buf = io.BytesIO()
    joblib.dump(model, buf)
    return buf.getvalue()


def _ensure_dill_free_pickling() -> None:
    """Idempotently revert dill's global pickle patch (dill.extend(use_dill=False)) so
    joblib/pickle emit STANDARD opcodes — artifacts stay loadable without dill. No-op
    if dill isn't importable, and safe to call repeatedly. This is the documented fix
    for the dill↔joblib interaction (dill registers reducers into pickle's dispatch on
    import); it leaves dill's OWN Pickler intact, so checkpoint/restore is unaffected."""
    try:
        import dill
        dill.extend(use_dill=False)
    except Exception:  # noqa: BLE001 — dill absent or API changed: nothing to do.
        pass


@_log_write("s3")
def save_model(model, bucket: str, key: str = None) -> str:
    """
    Serialize a trained model and upload it to S3 (the inverse of load_model).

    Accepts either (model, bucket, key) or (model, full_s3_uri):
        save_model(clf, "my-bucket", "models/churn.joblib")     # sklearn → joblib
        save_model(clf, "s3://my-bucket/models/churn.joblib")
        save_model(onx, "my-bucket", "models/churn.onnx")       # ONNX graph → .onnx

    Format is chosen from the key's extension: .joblib/.jbl/.pkl/.pickle/.sav use
    joblib (portable scikit-learn/pickle); .onnx stores the ONNX graph's own bytes
    (pass an onnx.ModelProto from skl2onnx.to_onnx, or its serialized bytes). Load
    it back with load_model (joblib/pickle) or load_onnx (.onnx). Returns the S3 URI.

    RAW-ROW CONTRACT: for the sklearn path, prefer a RAW-ROW Pipeline (train_model's
    output) — one that one-hots/scales internally so it's callable with raw rows
    anywhere, including a SageMaker endpoint. Saving a bare, pre-encoded estimator
    WARNS (it still saves) because the caller would have to reproduce the encoding by
    hand. (ONNX from to_onnx already bakes preprocessing in, so it's exempt.)
    """
    bucket, key = _resolve_s3_target(bucket, key)
    ext = _ext_of(key)
    if ext not in _ONNX_EXTS:
        _warn_if_not_rawrow_pipeline(model)   # nudge toward a raw-row Pipeline
    body = _serialize_model(model, ext)
    _s3.put_bytes(bucket, key, body, content_type="application/octet-stream",
                  region=_aws_region())
    return f"s3://{bucket}/{key}"


def save_model_local(model, path: str) -> str:
    """
    Serialize a trained model to a local /tmp file. Format is chosen from the
    extension: .joblib/.pkl/... use joblib; .onnx stores the ONNX graph's bytes.

        save_model_local(clf, "/tmp/churn.joblib")   # sklearn estimator
        save_model_local(onx, "/tmp/churn.onnx")     # ONNX model

    Returns the local path. (Only /tmp is writable in the sandbox.)
    """
    ext = _ext_of(path)
    if ext not in _ONNX_EXTS:
        _warn_if_not_rawrow_pipeline(model)   # nudge toward a raw-row Pipeline
    with open(path, "wb") as f:
        f.write(_serialize_model(model, ext))
    return path


# =============================================================================
# MACHINE LEARNING / LOCAL — inference (run a loaded model on your data)
# =============================================================================

def _as_features(data, feature_cols=None):
    """Coerce `data` into the 2-D feature matrix a model expects, and return
    (X, index) where index is the pandas index to reattach to predictions (or None).

    Accepts a DataFrame (optionally narrowed to feature_cols), a Series (treated as
    one row / a single column), or a numpy array / list. This is the DataFrame<->array
    bridge so callers can pass the frame they already have."""
    if isinstance(data, pd.DataFrame):
        X = data[feature_cols] if feature_cols else data
        return X, data.index
    if isinstance(data, pd.Series):
        return data.to_frame().T, None
    arr = np.asarray(data)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return arr, None


def _resolve_model(model):
    """Return a live predictor for `model`. If it's a SageMakerJob handle (from
    sm_train) rather than a loaded model, resolve it with sm_load_model() first —
    which WAITS for training to finish and downloads the artifact. This is what lets
    predict / predict_proba / predict_batch each accept a job OR a model, so there's
    ONE prediction surface for local and SageMaker models alike.

    Duck-typed (a job has .job_name + .model_s3_uri but no .predict) so this module
    doesn't import helpers.sagemaker at load time — that module imports from THIS one,
    and a top-level import would be circular. The import is done lazily, only when a
    job is actually passed."""
    if not hasattr(model, "predict") and hasattr(model, "job_name") \
            and hasattr(model, "model_s3_uri"):
        from .sagemaker import sm_load_model
        return sm_load_model(model)
    return model


def _is_endpoint(model) -> bool:
    """True if `model` is a SageMakerEndpoint handle (a deployed remote endpoint),
    duck-typed by .endpoint_name. predict()/predict_batch()/predict_proba() route
    these to the remote sm_predict path instead of a local .predict(). Only the
    HANDLE auto-routes — a bare endpoint-name string is ambiguous, so call
    sm_predict('name', df) explicitly for that."""
    return hasattr(model, "endpoint_name") and not hasattr(model, "job_name")


def predict(model, data, feature_cols=None):
    """
    Run a model (or a SageMakerJob) .predict() on your data and return the predictions.

        preds = predict(model, df)                       # all columns are features
        preds = predict(model, df, feature_cols=["age", "tenure", "balance"])

    `data` may be a DataFrame, Series, numpy array, or list of rows. When it's a
    DataFrame the result is returned as a pandas Series aligned to the input index;
    otherwise a numpy array. To append predictions as a new column instead, use
    predict_batch. `model` may also be a SageMakerJob handle (resolved via
    sm_load_model, which waits for the job and downloads the model) OR a
    SageMakerEndpoint handle (invoked remotely via sm_predict — a billable call).
    """
    if _is_endpoint(model):
        from .sagemaker import sm_predict
        return sm_predict(model, data if feature_cols is None
                          else _as_features(data, feature_cols)[0])
    model = _resolve_model(model)
    X, index = _as_features(data, feature_cols)
    y = model.predict(X)
    y = np.asarray(y)
    if index is not None and y.ndim == 1 and len(y) == len(index):
        return pd.Series(y, index=index, name="prediction")
    return y


def predict_proba(model, data, feature_cols=None):
    """
    Class probabilities from a classifier (a model or a SageMakerJob), as a
    DataFrame with one column per class (labeled with the model's classes_ when
    available) and aligned to the input index.

        probs = predict_proba(model, df)   # columns: the class labels

    Raises a clear error if the model has no predict_proba (e.g. a plain regressor
    or an SVM without probability=True). `model` may also be a SageMakerJob handle
    (resolved via sm_load_model first) OR a SageMakerEndpoint handle (probabilities
    fetched remotely via sm_predict(..., proba=True)).
    """
    if _is_endpoint(model):
        from .sagemaker import _sm_invoke
        out = _sm_invoke(model, data)
        probs = out.get("probabilities")
        if probs is None:
            raise AttributeError(
                "This endpoint did not return probabilities (the deployed model is "
                "likely a regressor). Use predict() for the point prediction.")
        proba = np.asarray(probs, dtype=float)
        index = data.index if isinstance(data, pd.DataFrame) else None
        # Label columns with the real class labels the endpoint returned (matches a
        # local predict_proba), falling back to class_<i> only if absent.
        classes = out.get("classes")
        if classes is not None and proba.ndim == 2 and len(classes) == proba.shape[1]:
            cols = [str(c) for c in classes]
        else:
            cols = [f"class_{i}" for i in range(proba.shape[1])] if proba.ndim == 2 else None
        return pd.DataFrame(proba, columns=cols, index=index)
    model = _resolve_model(model)
    if not hasattr(model, "predict_proba"):
        raise AttributeError(
            f"{type(model).__name__} has no predict_proba. It's likely a regressor "
            "or a classifier trained without probability estimates — use predict()."
        )
    X, index = _as_features(data, feature_cols)
    proba = np.asarray(model.predict_proba(X))
    classes = getattr(model, "classes_", None)
    cols = [str(c) for c in classes] if classes is not None and len(classes) == proba.shape[1] \
        else [f"class_{i}" for i in range(proba.shape[1])]
    return pd.DataFrame(proba, columns=cols, index=index if index is not None else None)


def predict_batch(model, df: pd.DataFrame, feature_cols=None,
                  output_col: str = "prediction", proba: bool = False) -> pd.DataFrame:
    """
    Score a whole DataFrame with a model (or a SageMakerJob) and return a COPY of df
    with the predictions appended as a new column — the natural way in a notebook.

        scored = predict_batch(model, df, output_col="churn_pred")
        scored = predict_batch(model, df, feature_cols=["age", "tenure"], output_col="risk")

    `model` may be a loaded model (from train_model / load_model / sm_load_model), a
    SageMakerJob handle (from sm_train — resolved with sm_load_model(), which WAITS
    for training and downloads the model), OR a SageMakerEndpoint handle (from
    sm_deploy_serverless/sm_deploy_instance — scored REMOTELY via sm_predict_batch, a
    billable call). So the ONE batch_predict works for local models, SageMaker jobs,
    and deployed endpoints:

        job = sm_train(df, "returned")
        scored = predict_batch(job, df, output_col="will_return")   # resolves the job, then scores
        ep = sm_deploy_serverless(model)
        scored = predict_batch(ep, df, output_col="will_return")    # invokes the endpoint

    proba=True (classification only) appends class PROBABILITIES instead of the hard
    label — what you want to RANK by likelihood rather than see a mostly-one-class
    column. For a BINARY classifier `output_col` holds P(positive class) as one
    column; for MULTICLASS it appends one column per class named "<output_col>_<class>".

        scored = predict_batch(model, df, output_col="churn_prob", proba=True)
        scored.sort_values("churn_prob", ascending=False).head(20)   # riskiest first

    feature_cols: which columns feed the model (default: all columns). The original
    frame is not modified. Returns df + [output_col] (or the per-class columns).
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("predict_batch expects a DataFrame; use predict() for arrays.")
    # A deployed endpoint scores remotely (sm_predict_batch handles the InvokeEndpoint
    # + proba column shaping), so route to it before any local-model resolution.
    if _is_endpoint(model):
        from .sagemaker import sm_predict_batch
        return sm_predict_batch(model, df, output_col=output_col, proba=proba)
    # Accept a SageMakerJob handle too (resolved to a live model via sm_load_model).
    model = _resolve_model(model)
    X = df[feature_cols] if feature_cols else df
    out = df.copy()

    if proba:
        if not hasattr(model, "predict_proba"):
            raise AttributeError(
                f"{type(model).__name__} has no predict_proba — it's likely a "
                "regressor or a classifier without probability estimates. Drop "
                "proba=True to append the predicted value instead."
            )
        p = np.asarray(model.predict_proba(X), dtype=float)
        classes = getattr(model, "classes_", None)
        if p.shape[1] == 2:
            # Binary: append P(positive class) as a single, sort-ready column
            # (the positive class is the 2nd column).
            out[output_col] = p[:, 1]
        else:
            labels = classes if classes is not None else range(p.shape[1])
            for i, cls in enumerate(labels):
                out[f"{output_col}_{cls}"] = p[:, i]
    else:
        out[output_col] = np.asarray(model.predict(X))
    return out


def _final_estimator(model):
    """Unwrap a scikit-learn Pipeline to the estimator that actually predicts —
    its last step — so hyperparameters like n_estimators surface unprefixed instead
    of hiding under 'forest__n_estimators'. Recurses through nested Pipelines and
    returns the model unchanged when it isn't a Pipeline."""
    est = getattr(model, "steps", None)
    if est:  # a Pipeline exposes .steps as [(name, estimator), ...]
        last = model.steps[-1][1]
        return _final_estimator(last)
    return model


def model_info(model) -> dict:
    """
    Inspect any model — a scikit-learn estimator/Pipeline, an ONNX InferenceSession (from load_onnx), or a deployed SageMaker endpoint (handle or 'microvm-ep-*' name) — printing a "what do I feed this?" summary and returning it as a dict.
    For sklearn: type/estimator/task/features/classes/hyperparameters. For ONNX: graph
    inputs/outputs (name/type/shape) + stamped classes. For an endpoint: a cheap
    describe-only summary (status/kind/serverless memory+concurrency/region) — it does
    NOT invoke the endpoint. Parallels the *_schema helpers for data sources.

        model_info(model)

    Returns a dict with (when available):
        type            top-level class name (e.g. "Pipeline" or "RandomForestClassifier")
        estimator       the underlying estimator that predicts — for a Pipeline this
                        is the final step (e.g. "RandomForestClassifier"); same as
                        `type` for a bare estimator
        task            "classifier" | "regressor" | "transformer" | "onnx" | "unknown"
        n_features      number of input features expected
        feature_names   the training feature names (if the model recorded them)
        classes         target class labels (classifiers only)
        params          the predicting estimator's hyperparameters (unprefixed, so
                        n_estimators/max_depth surface even inside a Pipeline)

    Also understands an ONNX InferenceSession (from load_onnx): since an ONNX graph
    has no scikit-learn attributes, it reports the graph's INPUTS (name/type/shape),
    OUTPUTS, and — when to_onnx stamped them — the target classes, so you still get a
    "what do I feed this?" summary for a framework-free model.

    And a deployed SageMaker endpoint — a SageMakerEndpoint handle (from sm_deploy_*) or
    a 'microvm-ep-*' endpoint NAME string: reports a CHEAP control-plane summary
    (status, kind, serverless memory/concurrency, region) via describe_endpoint. It
    does NOT invoke the endpoint (no request cost) — score it with sm_predict(name, df).
    """
    # A deployed SageMaker endpoint — a REMOTE model you invoke, not a local object.
    # Detect a SageMakerEndpoint handle (by class name, to avoid importing the sm
    # helpers here) or a microvm-ep-* endpoint NAME string, and report a cheap
    # control-plane summary (status/kind/serverless shape) via the sm helper. Handled
    # FIRST so it never falls through to the sklearn probes (which would be empty).
    if _looks_like_sm_endpoint(model):
        from .sagemaker import _endpoint_info
        info = _endpoint_info(model)
        _print_model_info(info)
        return info

    # ONNX InferenceSession — a compiled graph, not a scikit-learn estimator, so none
    # of the getattr(...) probes below apply. Detect it by its onnxruntime API surface
    # (get_inputs/get_outputs/get_modelmeta) without importing onnxruntime here, and
    # summarize the graph instead. Handled FIRST so the sklearn path never sees it.
    if _looks_like_onnx_session(model):
        info = _onnx_model_info(model)
        _print_model_info(info)
        return info

    info = {"type": type(model).__name__}
    # For a Pipeline, the estimator that actually predicts is the last step. Report
    # its class name and read hyperparameters from IT, so params like n_estimators
    # aren't hidden behind the step-prefixed 'forest__n_estimators' Pipeline keys.
    final = _final_estimator(model)
    info["estimator"] = type(final).__name__
    # Task heuristic from scikit-learn's estimator tags / duck typing.
    est_type = getattr(model, "_estimator_type", None)
    if est_type == "classifier" or hasattr(model, "classes_"):
        info["task"] = "classifier"
    elif est_type == "regressor":
        info["task"] = "regressor"
    elif hasattr(model, "transform"):
        info["task"] = "transformer"
    else:
        info["task"] = "unknown"
    n_feat = getattr(model, "n_features_in_", None)
    if n_feat is not None:
        info["n_features"] = int(n_feat)
    names = getattr(model, "feature_names_in_", None)
    if names is not None:
        info["feature_names"] = [str(n) for n in names]
    classes = getattr(model, "classes_", None)
    if classes is not None:
        info["classes"] = [str(c) for c in classes]
    if hasattr(final, "get_params"):
        try:
            # Read params from the predicting estimator (Pipeline's final step),
            # not the Pipeline wrapper — otherwise n_estimators/max_depth are missing.
            # Keep it JSON-friendly: stringify non-scalar param values.
            params = {}
            for k, v in final.get_params(deep=False).items():
                params[k] = v if isinstance(v, (int, float, str, bool, type(None))) else str(v)
            info["params"] = params
        except Exception:
            pass
    # PRINT a readable summary as a side effect, THEN return the dict. The notebook
    # cell executor only auto-displays DataFrames/plots/display() — a bare returned
    # dict renders nothing — so a user who ends a cell with `model_info(model)` would
    # otherwise see no output. Printing here makes the inspector useful inline while
    # still returning the dict for programmatic use (info = model_info(model)).
    _print_model_info(info)
    return info


def _looks_like_sm_endpoint(model) -> bool:
    """True if `model` is a deployed SageMaker endpoint reference: either a
    SageMakerEndpoint handle (detected by class name, so we don't import the sm
    helpers) or a microvm-ep-* endpoint NAME string (the prefix deploy stamps and
    the panel/IAM filter on). A plain string that isn't an endpoint name is left for
    the normal path (there is none for str, so it just won't match model_info's other
    branches — acceptable)."""
    if type(model).__name__ == "SageMakerEndpoint":
        return True
    return isinstance(model, str) and model.startswith("microvm-ep-")


def _looks_like_onnx_session(model) -> bool:
    """True if `model` is an onnxruntime InferenceSession — detected by its API
    surface (get_inputs/get_outputs/get_modelmeta) plus class name, so we don't import
    onnxruntime just to isinstance-check. A scikit-learn estimator has none of these."""
    return (
        type(model).__name__ == "InferenceSession"
        and all(hasattr(model, m) for m in ("get_inputs", "get_outputs", "get_modelmeta"))
    )


def _onnx_model_info(session) -> dict:
    """Build a model_info()-style dict for an ONNX InferenceSession. Reports the
    graph's inputs (name/type/shape), outputs, and — when to_onnx/_stamp_onnx_classes
    stored them — the target classes, plus any producer metadata. All best-effort so a
    partial graph still yields a useful summary."""
    info = {"type": "InferenceSession", "estimator": "ONNX graph", "task": "onnx"}

    def _shape(s):
        # onnxruntime shapes mix ints and symbolic dims (e.g. [None, 11]); stringify.
        try:
            return [d if isinstance(d, int) else (d or "?") for d in s]
        except Exception:
            return []

    try:
        inputs = session.get_inputs()
        info["inputs"] = [
            {"name": i.name, "type": i.type, "shape": _shape(i.shape)} for i in inputs
        ]
        # Feature count: the last dim of the first input, when concrete. A per-column
        # mixed-type graph has one input per column, so use the input COUNT there.
        if len(inputs) > 1:
            info["n_features"] = len(inputs)
        elif inputs:
            shp = _shape(inputs[0].shape)
            if shp and isinstance(shp[-1], int):
                info["n_features"] = shp[-1]
    except Exception:
        pass

    try:
        info["outputs"] = [
            {"name": o.name, "type": o.type, "shape": _shape(o.shape)}
            for o in session.get_outputs()
        ]
    except Exception:
        pass

    # Target classes, if to_onnx/_stamp_onnx_classes stamped them into the graph
    # metadata (custom_metadata_map['microvm_classes'] — a JSON list). This mirrors
    # what the serving container reads, so model_info agrees with the endpoint.
    try:
        meta = session.get_modelmeta()
        cmap = dict(getattr(meta, "custom_metadata_map", {}) or {})
        raw = cmap.get("microvm_classes")
        if raw:
            import json as _json
            try:
                info["classes"] = _json.loads(raw)
            except Exception:
                info["classes"] = [c for c in str(raw).split(",") if c]
        producer = getattr(meta, "producer_name", "") or ""
        if producer:
            info["params"] = {"producer": producer}
    except Exception:
        pass

    return info


def _print_model_info(info: dict) -> None:
    """Pretty-print a model_info() dict as a compact, aligned summary. Best-effort:
    any formatting error falls back to a plain dict print so output is never lost."""
    try:
        # A deployed endpoint is a remote resource — render its control-plane summary
        # (status/kind/serverless shape), not sklearn/onnx fields.
        if info.get("endpoint_kind") == "endpoint":
            _print_endpoint_info(info)
            return
        # ONNX sessions carry inputs/outputs instead of sklearn feature_names/params
        # — render those so an ONNX summary is as informative as the sklearn one.
        if info.get("task") == "onnx":
            _print_onnx_info(info)
            return
        lines = ["Model summary"]
        # Headline fields in a stable, readable order.
        for label, field in (
            ("type", "type"), ("estimator", "estimator"), ("task", "task"),
            ("n_features", "n_features"),
        ):
            if field in info:
                lines.append(f"  {label:<13} {info[field]}")
        classes = info.get("classes")
        if classes is not None:
            shown = ", ".join(str(c) for c in classes[:10])
            if len(classes) > 10:
                shown += f", … (+{len(classes) - 10} more)"
            lines.append(f"  {'classes':<13} [{shown}]")
        names = info.get("feature_names")
        if names:
            shown = ", ".join(str(n) for n in names[:12])
            if len(names) > 12:
                shown += f", … (+{len(names) - 12} more)"
            lines.append(f"  {'features':<13} {shown}")
        params = info.get("params") or {}
        if params:
            # Surface the hyperparameters people actually tune first; then a few more.
            key_order = ["n_estimators", "max_depth", "learning_rate", "C", "alpha",
                         "kernel", "num_round"]
            picked = [(k, params[k]) for k in key_order if k in params]
            for k, v in params.items():
                if len(picked) >= 6:
                    break
                if k not in dict(picked):
                    picked.append((k, v))
            if picked:
                lines.append("  params        " +
                             ", ".join(f"{k}={v}" for k, v in picked))
        print("\n".join(lines))
    except Exception:
        print(info)


def _print_onnx_info(info: dict) -> None:
    """Pretty-print an ONNX-session model_info() dict: type + inputs/outputs (name,
    type, shape) + classes, since an ONNX graph has no sklearn params/feature_names."""
    try:
        lines = ["Model summary (ONNX graph)"]
        lines.append(f"  {'type':<13} InferenceSession")
        if "n_features" in info:
            lines.append(f"  {'n_features':<13} {info['n_features']}")
        classes = info.get("classes")
        if classes is not None:
            shown = ", ".join(str(c) for c in classes[:10])
            if len(classes) > 10:
                shown += f", … (+{len(classes) - 10} more)"
            lines.append(f"  {'classes':<13} [{shown}]")

        def _fmt_io(io):
            # "name: type shape" per input/output; shape like [?, 11].
            shp = io.get("shape") or []
            shp_s = "[" + ", ".join(str(d) for d in shp) + "]" if shp else ""
            t = (io.get("type") or "").replace("tensor(", "").rstrip(")")
            return f"{io.get('name')}: {t} {shp_s}".strip()

        inputs = info.get("inputs") or []
        for n, io in enumerate(inputs[:12]):
            label = "inputs" if n == 0 else ""
            lines.append(f"  {label:<13} {_fmt_io(io)}")
        if len(inputs) > 12:
            lines.append(f"  {'':<13} … (+{len(inputs) - 12} more)")
        outputs = info.get("outputs") or []
        for n, io in enumerate(outputs[:6]):
            label = "outputs" if n == 0 else ""
            lines.append(f"  {label:<13} {_fmt_io(io)}")
        producer = (info.get("params") or {}).get("producer")
        if producer:
            lines.append(f"  {'producer':<13} {producer}")
        print("\n".join(lines))
    except Exception:
        print(info)


def _print_endpoint_info(info: dict) -> None:
    """Pretty-print a deployed SageMaker endpoint's summary from _endpoint_info():
    name/status/kind + serverless shape + region, plus — when the endpoint runs OUR
    serving container — the model's I/O contract (task, feature columns, classes) and
    how to score it. The schema line comes from a tiny schema probe (see
    _endpoint_schema); it's simply absent for a foreign endpoint."""
    try:
        lines = ["Endpoint summary"]
        lines.append(f"  {'name':<13} {info.get('name','')}")
        status = info.get("status", "")
        if info.get("failure_reason"):
            status = f"{status} — {info['failure_reason']}"
        lines.append(f"  {'status':<13} {status}")
        if info.get("kind"):
            lines.append(f"  {'kind':<13} {info['kind']}")
        if info.get("model_task"):
            lines.append(f"  {'task':<13} {info['model_task']}")
        # Serverless (or provisioned) shape.
        etype = info.get("endpoint_type")
        if etype == "serverless":
            shape = "serverless"
            bits = []
            if info.get("memory_mb") is not None:
                bits.append(f"{info['memory_mb']} MB")
            if info.get("max_concurrency") is not None:
                bits.append(f"max_concurrency={info['max_concurrency']}")
            if info.get("provisioned_concurrency") is not None:
                bits.append(f"provisioned_concurrency={info['provisioned_concurrency']}")
            if bits:
                shape += " (" + ", ".join(bits) + ")"
            lines.append(f"  {'type':<13} {shape}")
        elif etype in ("instance", "provisioned"):
            shape = "instance"
            bits = []
            if info.get("instance_type"):
                bits.append(str(info["instance_type"]))
            if info.get("instance_count") is not None:
                cnt = info["instance_count"]
                bits.append(f"×{cnt}" if cnt and cnt != 1 else "×1")
            if bits:
                shape += " (" + ", ".join(bits) + ")"
            lines.append(f"  {'type':<13} {shape}")
        if info.get("region"):
            lines.append(f"  {'region':<13} {info['region']}")
        # Model I/O contract (only present when it's our serving container).
        if info.get("n_features") is not None:
            lines.append(f"  {'n_features':<13} {info['n_features']}")
        names = info.get("feature_names")
        if names:
            shown = ", ".join(str(n) for n in names[:12])
            if len(names) > 12:
                shown += f", … (+{len(names) - 12} more)"
            lines.append(f"  {'features':<13} {shown}")
        classes = info.get("classes")
        if classes is not None:
            shown = ", ".join(str(c) for c in classes[:10])
            if len(classes) > 10:
                shown += f", … (+{len(classes) - 10} more)"
            lines.append(f"  {'classes':<13} [{shown}]")
        # How to actually use it.
        name = info.get("name", "<name>")
        lines.append(f"  {'score with':<13} sm_predict('{name}', df)  ·  "
                     f"sm_predict_batch('{name}', df, output_col='pred')")
        print("\n".join(lines))
    except Exception:
        print(info)


# =============================================================================
# MACHINE LEARNING / LOCAL — training (fit a scikit-learn model from a DataFrame)
# =============================================================================

def _infer_task(y: "pd.Series") -> str:
    """Guess 'classification' vs 'regression' from the target column. Non-numeric
    (object/bool/category) is always classification; a numeric column with few
    distinct values relative to its length looks categorical (e.g. a 0/1 label or a
    small rating scale), so it's classification too — everything else is regression.
    Overridable via the explicit task= argument."""
    from pandas.api.types import is_numeric_dtype
    # Non-numeric target (str/object/category/bool) is always classification. Use
    # is_numeric_dtype so newer pandas' 'str' string dtype is handled (an == object
    # check misses it and would wrongly treat a text label as a regression target).
    if y.dtype == bool or not is_numeric_dtype(y):
        return "classification"
    nunique = y.nunique(dropna=True)
    if nunique <= 2:
        return "classification"
    # Few distinct integer-ish values (<=20 and <5% of rows) → treat as labels.
    if nunique <= 20 and nunique < max(1, len(y)) * 0.05 and \
            (y.dropna() % 1 == 0).all():
        return "classification"
    return "regression"


def train_model(
    df: "pd.DataFrame",
    target: str,
    *,
    features=None,
    task: str = None,
    estimator=None,
    categorical=None,
    numeric=None,
    test_size: float = 0.2,
    stratify: bool = True,
    scale: bool = False,
    cv: int = None,
    random_state: int = 42,
    return_report: bool = True,
    **estimator_kwargs,
):
    """
    Fit a scikit-learn model from a DataFrame in ONE call — the end-to-end version
    of the usual get_dummies → train_test_split → fit → score boilerplate. Every
    default is overridable, so it's a one-liner for the common case and a full knob
    board when you need control.

        model = train_model(df, "returned")                       # infers everything
        model = train_model(df, "price", task="regression")       # force the task
        model = train_model(df, "churn", estimator=XGBClassifier(max_depth=4))
        model = train_model(df, "churn", n_estimators=500, max_depth=8)  # → default estimator

    What it does, all overridable:
      • task        'classification' | 'regression'. Default: inferred from the
                    target column (dtype + cardinality; see _infer_task).
      • features    columns to train on. Default: every column except `target`.
      • categorical/numeric  how to split those features. Default: object/bool/
                    category columns are one-hot encoded, the rest passed as numeric.
                    Pass explicit lists to override the auto-detection.
      • estimator   any scikit-learn-style estimator INSTANCE. Default:
                    RandomForest{Classifier,Regressor}(random_state=...). Extra
                    **estimator_kwargs (e.g. n_estimators=, max_depth=) are forwarded
                    to the DEFAULT estimator (ignored if you pass your own instance).
      • scale       wrap numeric features in a StandardScaler (default False; useful
                    for linear/SVM/KNN estimators).
      • test_size / stratify / random_state  the holdout split (stratify applies to
                    classification only). cv=k runs k-fold cross-val for the score
                    instead of a single split.

    Returns a FITTED estimator (a Pipeline: preprocessing + the model) that plugs
    straight into the other ML helpers — save_model(model, ...), predict_batch(
    model, df), model_info(model). The Pipeline one-hots/scales new data the SAME
    way at inference, so you feed it raw rows (no manual get_dummies at predict time).

    When return_report=True (default) it also prints a short fit report (holdout
    metrics + feature count) so you see how it did without extra code. Set
    return_report=False to stay silent (e.g. in a loop).
    """
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler
    from sklearn.model_selection import train_test_split, cross_val_score

    if not isinstance(df, pd.DataFrame):
        raise TypeError("train_model expects a pandas DataFrame as the first argument.")
    if target not in df.columns:
        raise KeyError(
            f"target column {target!r} is not in the DataFrame. "
            f"Available columns: {', '.join(map(str, df.columns[:20]))}"
            + (" …" if len(df.columns) > 20 else "")
        )

    # Rows with a missing target can't supervise training — drop them up front.
    data = df.dropna(subset=[target])
    if len(data) == 0:
        raise ValueError(f"No rows left after dropping missing {target!r} values.")

    feat_cols = list(features) if features is not None else [c for c in data.columns if c != target]
    feat_cols = [c for c in feat_cols if c != target]
    if not feat_cols:
        raise ValueError("No feature columns — pass features=[...] or add columns to the DataFrame.")

    X = data[feat_cols]
    y = data[target]

    task = (task or _infer_task(y)).lower()
    if task not in ("classification", "regression"):
        raise ValueError("task must be 'classification' or 'regression'.")

    # Auto-split features into categorical (one-hot) vs numeric (passthrough/scaled),
    # unless the caller pinned them explicitly. "Categorical" = anything NOT numeric
    # (object/str/category/bool) — checked via is_numeric_dtype so it's robust across
    # pandas string-dtype changes (newer pandas reports string columns as 'str', not
    # 'object', so an == object check silently misses them and the raw strings reach
    # the estimator → "could not convert string to float").
    from pandas.api.types import is_numeric_dtype
    if categorical is None:
        categorical = [c for c in feat_cols
                       if X[c].dtype == bool or not is_numeric_dtype(X[c])]
    else:
        categorical = [c for c in categorical if c in feat_cols]
    if numeric is None:
        numeric = [c for c in feat_cols if c not in categorical]
    else:
        numeric = [c for c in numeric if c in feat_cols]

    # Preprocess: one-hot the categoricals (ignore unseen categories at inference),
    # optionally scale the numerics. handle_unknown='ignore' keeps prediction robust
    # to categories not present at fit time. sparse_output=False so the matrix stays
    # dense (simpler; these are notebook-scale frames).
    try:
        ohe = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    except TypeError:
        # scikit-learn < 1.2 used sparse= instead of sparse_output=.
        ohe = OneHotEncoder(handle_unknown="ignore", sparse=False)
    num_step = StandardScaler() if scale else "passthrough"
    pre = ColumnTransformer(
        transformers=[("cat", ohe, categorical), ("num", num_step, numeric)],
        remainder="drop",
    )

    # Default estimator: RandomForest (a strong, low-config baseline for both tasks).
    # Forward **estimator_kwargs to it. A caller-supplied estimator instance wins and
    # ignores estimator_kwargs (configure it yourself when you pass it).
    if estimator is None:
        if task == "classification":
            from sklearn.ensemble import RandomForestClassifier
            estimator = RandomForestClassifier(random_state=random_state, **estimator_kwargs)
        else:
            from sklearn.ensemble import RandomForestRegressor
            estimator = RandomForestRegressor(random_state=random_state, **estimator_kwargs)
    elif estimator_kwargs:
        logger.info("train_model: estimator= supplied, so **%s were ignored "
                    "(configure the estimator instance directly).",
                    list(estimator_kwargs))

    model = Pipeline([("pre", pre), ("model", estimator)])
    # Mark this as a RAW-ROW Pipeline: preprocessing (one-hot/scale) lives INSIDE the
    # Pipeline, so it's called with raw rows (mixed str+numeric) — no client-side
    # get_dummies. save_model/save_model_local and sm_deploy_* rely on this contract
    # (and _is_rawrow_pipeline detects it); the ColumnTransformer step also makes it
    # structurally detectable, but the explicit marker is unambiguous + future-proof.
    model._microvm_rawrow = True

    report = {"task": task, "n_features_in": len(feat_cols),
              "n_categorical": len(categorical), "n_numeric": len(numeric),
              "n_rows": int(len(data)), "estimator": type(estimator).__name__}

    # SCORE: k-fold cross-val if cv= given, else a single holdout split. Either way
    # the RETURNED model is refit on ALL rows (max data for downstream inference).
    if cv and cv > 1:
        scoring = "roc_auc" if (task == "classification" and y.nunique() == 2) else \
                  ("accuracy" if task == "classification" else "r2")
        scores = cross_val_score(model, X, y, cv=cv, scoring=scoring)
        report["cv"] = int(cv)
        report["cv_metric"] = scoring
        report["cv_mean"] = float(scores.mean())
        report["cv_std"] = float(scores.std())
        model.fit(X, y)
    else:
        strat = y if (task == "classification" and stratify) else None
        try:
            X_tr, X_te, y_tr, y_te = train_test_split(
                X, y, test_size=test_size, random_state=random_state, stratify=strat)
        except ValueError:
            # Stratify can fail when a class has too few members — retry unstratified.
            X_tr, X_te, y_tr, y_te = train_test_split(
                X, y, test_size=test_size, random_state=random_state)
        model.fit(X_tr, y_tr)
        report.update(_score_holdout(model, X_te, y_te, task))
        # Refit on the FULL dataset so the returned model uses every row.
        model.fit(X, y)

    if return_report:
        _print_train_report(report)
    return model


def _score_holdout(model, X_te, y_te, task: str) -> dict:
    """Compute task-appropriate holdout metrics into a plain dict (best-effort;
    a metric that can't be computed is simply omitted rather than raising)."""
    out = {}
    try:
        if task == "classification":
            from sklearn.metrics import accuracy_score, f1_score
            preds = model.predict(X_te)
            out["holdout_accuracy"] = float(accuracy_score(y_te, preds))
            try:
                out["holdout_f1"] = float(f1_score(y_te, preds, average="weighted"))
            except Exception:
                pass
            # Binary AUC when the model exposes probabilities.
            if hasattr(model, "predict_proba") and y_te.nunique() == 2:
                from sklearn.metrics import roc_auc_score
                proba = model.predict_proba(X_te)[:, 1]
                out["holdout_auc"] = float(roc_auc_score(y_te, proba))
        else:
            from sklearn.metrics import r2_score, mean_absolute_error
            preds = model.predict(X_te)
            out["holdout_r2"] = float(r2_score(y_te, preds))
            out["holdout_mae"] = float(mean_absolute_error(y_te, preds))
        out["n_test"] = int(len(y_te))
    except Exception as e:
        logger.info("train_model: holdout scoring skipped (%s)", e)
    return out


def _print_train_report(report: dict):
    """Print a compact one-block fit summary, so the common case needs no extra
    metric code. Purely informational (train_model returns the model regardless)."""
    lines = [f"✓ trained {report.get('estimator','model')} "
             f"({report.get('task','?')}) on {report.get('n_rows','?')} rows, "
             f"{report.get('n_features_in','?')} feature cols "
             f"({report.get('n_categorical',0)} categorical → one-hot, "
             f"{report.get('n_numeric',0)} numeric)"]
    if "cv_mean" in report:
        lines.append(f"  {report['cv']}-fold CV {report['cv_metric']}: "
                     f"{report['cv_mean']:.3f} ± {report['cv_std']:.3f}")
    else:
        bits = []
        for k, label in (("holdout_auc", "AUC"), ("holdout_accuracy", "accuracy"),
                         ("holdout_f1", "F1"), ("holdout_r2", "R²"),
                         ("holdout_mae", "MAE")):
            if k in report:
                bits.append(f"{label} {report[k]:.3f}")
        if bits:
            lines.append(f"  holdout ({report.get('n_test','?')} rows): " + ", ".join(bits))
    lines.append("  → save_model(model, bucket, key) · predict_batch(model, df) · model_info(model)")
    print("\n".join(lines))


# =============================================================================
# MACHINE LEARNING / LOCAL — ONNX inference (framework-free; onnxruntime installed on demand)
# =============================================================================

def _pip_install_on_demand(pip_name: str, import_name: str = None):
    """Import a package, pip-installing it on first use if absent. Shared lazy-install
    seam so ONNX export/inference (and any helper whose OWN code imports a non-baked
    package) never makes the user run install_package() by hand — the helper that
    needs the dependency owns installing it, then it's cached for the VM's lifetime.
    Returns the imported module."""
    import importlib
    mod = import_name or pip_name
    try:
        return importlib.import_module(mod)
    except ImportError:
        logger.info(f"📦 {pip_name} not present — installing on demand (first use)...")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", pip_name],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to install {pip_name}: "
                + (proc.stderr or proc.stdout or "").strip()[:300]
            )
        return importlib.import_module(mod)


def _ensure_onnxruntime():
    """Import onnxruntime, installing it on first use. ONNX is intentionally NOT
    baked into the image (it's ~21MB); this pays that cost lazily the first time a
    notebook actually needs ONNX inference, then it's cached for the VM's lifetime."""
    return _pip_install_on_demand("onnxruntime")


def _ensure_skl2onnx():
    """Import skl2onnx, installing it on first use. Like onnxruntime it's NOT baked
    into the image, so the to_onnx() helper below installs it lazily — making the
    WHOLE ONNX round-trip (export → save → load → predict) install-free, matching how
    load_onnx auto-installs onnxruntime."""
    return _pip_install_on_demand("skl2onnx")


def _pipeline_column_types(model, sample_df):
    """For a Pipeline/ColumnTransformer over a DataFrame, build skl2onnx
    per-column `initial_types`: a StringTensorType for each categorical column and a
    FloatTensorType for each numeric one, so string features (one-hot encoded inside
    the graph) convert correctly. Returns a list[(name, TensorType)] or None if the
    model isn't column-typed (→ caller uses the plain single-tensor path).

    The categorical/numeric split is read from the fitted ColumnTransformer when
    present (authoritative — it's exactly what the model was trained on); otherwise
    it falls back to the sample DataFrame's own dtypes. Columns are emitted in the
    DataFrame's order, each as an [None, 1] tensor (skl2onnx's per-column convention).
    """
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    from pandas.api.types import is_numeric_dtype
    from skl2onnx.common.data_types import FloatTensorType, StringTensorType

    if not isinstance(sample_df, pd.DataFrame):
        return None

    # Find a fitted ColumnTransformer inside the model (Pipeline step or bare).
    ct = None
    if isinstance(model, ColumnTransformer):
        ct = model
    elif isinstance(model, Pipeline):
        for _, step in model.steps:
            if isinstance(step, ColumnTransformer):
                ct = step
                break

    categorical = set()
    if ct is not None:
        # Read the actual column groups the transformer was fitted with.
        for _name, _trans, cols in getattr(ct, "transformers_", getattr(ct, "transformers", [])):
            if _trans == "drop" or cols is None:
                continue
            col_list = list(cols) if isinstance(cols, (list, tuple, np.ndarray, pd.Index)) else [cols]
            # A column group is "categorical" if its transformer isn't a plain
            # passthrough AND the columns are non-numeric in the sample.
            for c in col_list:
                if c in sample_df.columns and not is_numeric_dtype(sample_df[c]):
                    categorical.add(c)

    # If no ColumnTransformer (or it revealed no string cols), fall back to dtypes.
    if not categorical:
        categorical = {c for c in sample_df.columns if not is_numeric_dtype(sample_df[c])}

    # No string columns at all → this is an all-numeric model; use the simple path.
    if not categorical:
        return None

    initial_types = []
    for c in sample_df.columns:
        if c in categorical:
            initial_types.append((str(c), StringTensorType([None, 1])))
        else:
            initial_types.append((str(c), FloatTensorType([None, 1])))
    return initial_types


def to_onnx(model, sample, target_opset: int = None):
    """
    Convert a fitted scikit-learn model to an ONNX graph — the framework-free export
    format. Installs skl2onnx on first use (not baked into the image), so you do NOT
    need to install_package() it yourself; this makes the full ONNX round-trip
    install-free:

        onx = to_onnx(risk_model, X_train)        # sklearn estimator/Pipeline → ONNX
        save_model(onx, bucket, "models/m.onnx")  # a .onnx key stores the graph bytes
        sess = load_onnx(bucket, "models/m.onnx") # installs onnxruntime on first use
        preds = onnx_predict(sess, new_rows)

    `sample` is a small batch of INPUT rows used to infer the graph's input signature
    — pass a DataFrame (recommended), numpy array, or list (a few rows is plenty).

    TWO conversion modes, chosen automatically:
      • MIXED-TYPE Pipeline (a train_model output with a ColumnTransformer that
        one-hots STRING columns): the graph gets a PER-COLUMN input signature — one
        named tensor per DataFrame column, StringTensorType for categoricals and
        FloatTensorType for numerics — so raw string rows feed straight in and the
        one-hot encoding runs INSIDE the graph. This mirrors train_model's "feed raw
        rows" contract server-side. (Casting such a Pipeline to one float32 tensor —
        the old behaviour — raises "could not convert string to float".)
      • ALL-NUMERIC estimator/Pipeline: a single float32 input tensor (the simple,
        original path).

    For classifiers, ZipMap is DISABLED (`zipmap=False`) so the probability output is
    a plain float tensor (a matrix of per-class probs) instead of a sequence-of-maps
    — friendlier for onnxruntime, Triton, and downstream tensor handling.

    Returns an onnx.ModelProto (feed it straight to save_model with a .onnx key).

    NOTE: converts SCIKIT-LEARN estimators (train_model output, plain sklearn). An
    XGBoost/LightGBM booster needs onnxmltools instead — out of scope here.
    """
    skl2onnx = _ensure_skl2onnx()

    X, _ = _as_features(sample)

    # `options` disables ZipMap for classifiers → plain proba tensor. Harmless for
    # regressors/transformers (they have no ZipMap), so we always pass it.
    common = {"options": {"zipmap": False}}
    if target_opset is not None:
        common["target_opset"] = target_opset

    # Mixed-type path: a Pipeline/ColumnTransformer over a DataFrame with string cols
    # needs per-column typed inputs (else skl2onnx tries to make one float tensor and
    # fails on the strings). Detect from the fitted model + the sample's dtypes.
    initial_types = _pipeline_column_types(model, X if isinstance(X, pd.DataFrame) else sample)
    if initial_types is not None:
        return skl2onnx.convert_sklearn(model, initial_types=initial_types, **common)

    # All-numeric path: one float32 tensor (the original simple behaviour).
    if isinstance(X, pd.DataFrame):
        X = X.to_numpy()
    X = np.asarray(X, dtype=np.float32)
    return skl2onnx.to_onnx(model, X, **common)


@_log_read("s3")
def load_onnx(bucket: str, key: str = None):
    """
    Load an ONNX model into an inference session — run classic-model inference
    WITHOUT the original training framework installed.

        sess = load_onnx("my-bucket", "models/model.onnx")   # from S3
        sess = load_onnx("s3://my-bucket/models/model.onnx")  # S3 URI
        sess = load_onnx("/tmp/models/model.onnx")            # local sandbox file
        preds = onnx_predict(sess, df)

    Installs onnxruntime on first use (not baked into the image). Accepts (bucket,
    key), a single s3:// URI, OR a single local /tmp path. Returns an
    onnxruntime.InferenceSession.
    """
    ort = _ensure_onnxruntime()
    # A single local filesystem path (no bucket/key split, not an s3:// URI) is
    # loaded straight off disk — mirrors load_model_local for the ONNX case.
    if key is None and isinstance(bucket, str) and not bucket.startswith("s3://") \
            and (bucket.startswith("/") or os.path.exists(bucket)):
        return ort.InferenceSession(bucket, providers=["CPUExecutionProvider"])
    bucket, key = _resolve_s3_target(bucket, key)
    data = _s3.get_bytes(bucket, key, region=_aws_region())
    return ort.InferenceSession(data, providers=["CPUExecutionProvider"])


def onnx_predict(session, data, feature_cols=None, input_name: str = None):
    """
    Run inference with an ONNX session (from load_onnx) and return the model's first
    output as a numpy array (or a Series aligned to a DataFrame's index).

        preds = onnx_predict(sess, df)

    `data` may be a DataFrame, Series, array, or list.

    Handles BOTH graph shapes automatically:
      • SINGLE-input graph (all-numeric model): the features are cast to float32 and
        fed as one tensor (the common case). `input_name` overrides the input name.
      • MULTI-input graph (a mixed-type Pipeline exported by to_onnx with per-column
        string/float inputs): each declared input is fed the matching DataFrame
        column — strings as-is, numerics as float32 — so you pass RAW rows and the
        graph's baked-in encoding runs server-side. Requires a DataFrame whose columns
        cover the graph's input names.
    """
    inputs = session.get_inputs()
    index = data.index if isinstance(data, pd.DataFrame) else None

    # Multi-input graph → build a per-column feed keyed by the graph's input names.
    if len(inputs) > 1:
        if not isinstance(data, pd.DataFrame):
            raise TypeError(
                "This ONNX graph has multiple named inputs (a mixed-type Pipeline). "
                "Pass a DataFrame with columns matching the graph inputs: "
                f"{[i.name for i in inputs]}."
            )
        feed = {}
        for inp in inputs:
            col = inp.name
            if col not in data.columns:
                raise KeyError(
                    f"ONNX graph expects input {col!r} but it's not in the DataFrame "
                    f"columns {list(data.columns)}."
                )
            arr = data[col].to_numpy().reshape(-1, 1)
            # String inputs stay object/str; numeric inputs cast to float32.
            if "string" not in inp.type:
                arr = arr.astype(np.float32)
            feed[col] = arr
        outputs = session.run(None, feed)
        y = np.asarray(outputs[0])
        if index is not None and y.ndim == 1 and len(y) == len(index):
            return pd.Series(y, index=index, name="prediction")
        return y

    # Single-input graph → one float32 tensor (original behaviour).
    X, index = _as_features(data, feature_cols)
    if isinstance(X, pd.DataFrame):
        X = X.to_numpy()
    X = np.asarray(X, dtype=np.float32)
    name = input_name or inputs[0].name
    outputs = session.run(None, {name: X})
    y = np.asarray(outputs[0])
    if index is not None and y.ndim == 1 and len(y) == len(index):
        return pd.Series(y, index=index, name="prediction")
    return y


# =============================================================================
# MACHINE LEARNING / LOCAL — evaluate a fitted model on a labeled test set
# =============================================================================

def evaluate_model(model, X, y, feature_cols=None, task: str = None) -> dict:
    """
    Score a FITTED model on a labeled test set and return the metrics as a dict —
    the standalone version of the holdout report train_model prints, so you can
    evaluate a model you LOADED (load_model / sm_load_model) or re-check an existing
    model on fresh data without retraining.

        model = load_model("my-bucket", "models/churn.joblib")
        metrics = evaluate_model(model, test_df, test_df["churn"],
                                 feature_cols=["age", "tenure", "balance"])
        # {'holdout_accuracy': 0.91, 'holdout_f1': 0.89, 'holdout_auc': 0.95, 'n_test': 400}

    Args:
      model        a fitted scikit-learn estimator/Pipeline, OR a SageMakerJob /
                   SageMakerEndpoint handle (resolved the same way predict() does).
      X            the test features: a DataFrame (optionally narrowed by
                   feature_cols), Series, array, or list — same contract as predict().
      y            the true labels, aligned to X (Series / array / list).
      feature_cols optional list to select the feature columns from a DataFrame X.
      task         'classification' | 'regression'. Default: inferred from `y`
                   (same rule train_model uses). Classification →
                   accuracy / F1 / AUC (binary, if the model has predict_proba);
                   regression → R² / MAE.

    Returns the metrics dict (keys: holdout_accuracy/holdout_f1/holdout_auc or
    holdout_r2/holdout_mae, plus n_test). A metric that can't be computed is omitted
    rather than raising.
    """
    predictor = _resolve_model(model)
    X_eval, _ = _as_features(X, feature_cols)
    y_true = y if isinstance(y, pd.Series) else pd.Series(np.asarray(y).ravel())
    resolved_task = task or _infer_task(y_true)
    report = _score_holdout(predictor, X_eval, y_true, resolved_task)
    # Friendly one-line summary, mirroring _print_train_report's holdout line.
    bits = []
    for k, label in (("holdout_auc", "AUC"), ("holdout_accuracy", "accuracy"),
                     ("holdout_f1", "F1"), ("holdout_r2", "R²"), ("holdout_mae", "MAE")):
        if k in report:
            bits.append(f"{label} {report[k]:.3f}")
    print(f"✓ evaluated ({resolved_task}) on {report.get('n_test', len(y_true))} rows"
          + (": " + ", ".join(bits) if bits else " — no metrics computable"))
    return report
