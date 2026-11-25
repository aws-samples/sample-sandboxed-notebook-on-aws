"""Custom SageMaker serving app — ONE container for sklearn (.joblib), ONNX (.onnx),
serverless-compatible. (A SageMaker-trained model is itself a sklearn Pipeline, so it
takes the .joblib path — xgboost/lightgbm estimators included, since this image ships
those libraries.)

Why custom (not the stock SKLearn/Triton/XGBoost containers):
  - Stock SKLearn AND stock XGBoost serving containers write /etc/sagemaker-nginx.conf,
    which SageMaker SERVERLESS forbids (read-only root fs) → PermissionError, endpoint
    fails the ping health check. (Both verified failing on serverless.)
  - Stock Triton -py3 images dropped the onnxruntime backend (24.02+), and the older
    image that has it is ~18GB > the 10GB serverless image limit.
  This app sidesteps all of them: gunicorn binds :8080 DIRECTLY (no nginx, no /etc
  writes), everything transient lives under /tmp. It's tiny, so it fits serverless.

SageMaker serving contract: respond 200 on GET /ping (health) and run inference on
POST /invocations. SageMaker unpacks model.tar.gz into /opt/ml/model (read-only).

Model dispatch (single image, two formats):
  - a *.joblib → joblib.load, call .predict (+ predict_proba). The sklearn Pipeline
                 (from train_model OR sm_train) carries its own preprocessing, so it
                 takes RAW rows; an xgboost/lightgbm estimator inside it just works.
  - a *.onnx   → onnxruntime.InferenceSession, run the graph (multi-input raw-row
                 graphs supported: each named input fed its column)

Request/response (JSON):
  POST /invocations {"rows": [ {col: val, ...}, ... ]}  (raw rows / feature dicts)
    OR {"instances": [[...], ...]}                       (positional rows)
  → {"predictions": [...], "probabilities": [[...], ...]}  (proba omitted if N/A)

DEBUGGING: every stage logs to stderr (→ CloudWatch) with a "[serve]" prefix — an
import-time environment banner, model discovery + load details/timings, and per-request
shape + timing + full tracebacks. So a startup or inference failure is diagnosable from
the endpoint's log stream WITHOUT rebuilding the image. Set SERVE_DEBUG=0 to quiet the
per-request logs (startup + errors always log).
"""
import glob
import json
import os
import sys
import time
import traceback

import numpy as np
from flask import Flask, Response, request

MODEL_DIR = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")
_DEBUG = os.environ.get("SERVE_DEBUG", "1") not in ("0", "false", "False", "")

app = Flask(__name__)

# Loaded once at import (gunicorn worker boot). _KIND is "sklearn" or "onnx".
_MODEL = None
_KIND = None
_ONNX_INPUTS = None   # cached session input specs for the onnx path
_ONNX_CLASSES = None  # class labels from metadata_props (onnx classifiers)
_LOAD_ERROR = None    # last load traceback string (surfaced in /ping body for quick triage)


def _log(msg, *, always=False):
    """Timestamped line to stderr → CloudWatch. Startup/errors always log; per-request
    logs honour SERVE_DEBUG."""
    if always or _DEBUG:
        print(f"[serve] {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def _log_exc(context):
    """Log a full traceback to stderr with a context label (always emitted)."""
    print(f"[serve] {time.strftime('%H:%M:%S')} ERROR in {context}:", file=sys.stderr, flush=True)
    traceback.print_exc()
    sys.stderr.flush()


def _env_banner():
    """Log the runtime environment once at import — versions + model dir contents.
    This alone explains most 'works locally, fails on the endpoint' cases (version
    skew, a missing lib, an unexpected artifact layout) at a glance in CloudWatch."""
    _log(f"boot: python={sys.version.split()[0]} cwd={os.getcwd()} "
         f"MODEL_DIR={MODEL_DIR} HOME={os.environ.get('HOME')} "
         f"SERVE_DEBUG={_DEBUG}", always=True)
    # Library versions — a mismatch vs the artifact's writer is the classic failure.
    for mod in ("numpy", "pandas", "scikit-learn", "joblib", "xgboost", "onnxruntime", "flask"):
        try:
            import importlib.metadata as md
            _log(f"  lib {mod}=={md.version(mod)}", always=True)
        except Exception:  # noqa: BLE001
            _log(f"  lib {mod}==<not installed>", always=True)
    # What's actually in the model dir (recursive) — surfaces artifact layout issues.
    try:
        found = sorted(glob.glob(os.path.join(MODEL_DIR, "**", "*"), recursive=True))
        if found:
            _log(f"  MODEL_DIR contents ({len(found)}):", always=True)
            for f in found[:40]:
                try:
                    sz = os.path.getsize(f) if os.path.isfile(f) else "-"
                except OSError:
                    sz = "?"
                _log(f"    {f}  ({sz} bytes)", always=True)
        else:
            _log(f"  MODEL_DIR is EMPTY (or missing): {MODEL_DIR}", always=True)
    except Exception:  # noqa: BLE001
        _log_exc("_env_banner listing")


def _load_model():
    global _MODEL, _KIND, _ONNX_INPUTS, _LOAD_ERROR
    t0 = time.time()
    joblibs = sorted(glob.glob(os.path.join(MODEL_DIR, "**", "*.joblib"), recursive=True))
    onnxs = sorted(glob.glob(os.path.join(MODEL_DIR, "**", "*.onnx"), recursive=True))
    _log(f"load: discovered joblib={joblibs} onnx={onnxs}", always=True)

    if joblibs:
        import joblib
        _log(f"load: sklearn from {joblibs[0]}", always=True)
        _MODEL = joblib.load(joblibs[0])
        _KIND = "sklearn"
        _log(f"load: sklearn model={type(_MODEL).__name__} "
             f"n_features_in_={getattr(_MODEL, 'n_features_in_', '?')} "
             f"has_predict_proba={hasattr(_MODEL, 'predict_proba')}", always=True)
    elif onnxs:
        import onnxruntime
        _log(f"load: onnx from {onnxs[0]}", always=True)
        _MODEL = onnxruntime.InferenceSession(onnxs[0], providers=["CPUExecutionProvider"])
        _ONNX_INPUTS = _MODEL.get_inputs()
        _KIND = "onnx"
        # Class labels stamped into metadata_props at deploy time (see
        # sagemaker._stamp_onnx_classes) so we can label proba columns like the
        # sklearn/xgboost paths. onnxruntime surfaces them via custom_metadata_map.
        global _ONNX_CLASSES
        try:
            meta = _MODEL.get_modelmeta().custom_metadata_map or {}
            if "microvm_classes" in meta:
                _ONNX_CLASSES = json.loads(meta["microvm_classes"])
        except Exception:  # noqa: BLE001
            _ONNX_CLASSES = None
        _log(f"load: onnx inputs=[" + ", ".join(
            f"{i.name}:{i.type}{i.shape}" for i in _ONNX_INPUTS) + f"] classes={_ONNX_CLASSES}",
            always=True)
    else:
        raise RuntimeError(f"No .joblib / .onnx model found under {MODEL_DIR}")
    _log(f"load: OK kind={_KIND} in {time.time() - t0:.2f}s", always=True)
    _LOAD_ERROR = None
    return _MODEL


def _ensure_loaded():
    global _LOAD_ERROR
    if _MODEL is None:
        try:
            _load_model()
        except Exception:  # noqa: BLE001
            _LOAD_ERROR = traceback.format_exc()
            raise


def _rows_to_frame(rows):
    """List of {col: val} dicts → pandas DataFrame (preserves column names so an
    sklearn model with feature_names_in_ lines up)."""
    import pandas as pd
    return pd.DataFrame(rows)


def _json_safe(v):
    """Coerce a class label (possibly a numpy scalar) to a JSON-serializable value."""
    return v.item() if hasattr(v, "item") else v


def _json_default(o):
    """json.dumps default hook: coerce numpy scalars/arrays to native Python so a
    stray numpy int64/float32 (e.g. a class label from sm_train's factorized
    classes_) never 500s the endpoint with 'Object of type int64 is not JSON
    serializable'. Belt-and-suspenders on top of per-field _json_safe.

    Order matters: a numpy ARRAY also has .item() (which raises on size>1), so check
    ndim first and use .tolist() for arrays; .item() only for true 0-d scalars."""
    if hasattr(o, "ndim") and getattr(o, "ndim", 0) > 0:   # numpy array (1-d+)
        return o.tolist()
    if hasattr(o, "item"):                                  # numpy scalar (0-d)
        return o.item()
    if hasattr(o, "tolist"):                                # anything else array-like
        return o.tolist()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _json_response(payload, status=200):
    """Build a JSON Response, numpy-safe (see _json_default)."""
    return Response(json.dumps(payload, default=_json_default), status=status,
                    mimetype="application/json")


@app.route("/ping", methods=["GET"])
def ping():
    # Healthy only once the model is loadable — SageMaker gates traffic on this.
    try:
        _ensure_loaded()
        return Response(json.dumps({"status": "ok", "kind": _KIND}),
                        status=200, mimetype="application/json")
    except Exception as e:  # noqa: BLE001
        # Log the FULL traceback to stderr so it lands in CloudWatch — otherwise the
        # error is trapped in the response body (which SageMaker discards) and a
        # startup failure looks like an opaque 180s ping timeout.
        _log_exc("/ping model load")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, mimetype="application/json")


def _describe_payload(rows, instances):
    """Short human description of the invoke payload shape for the request log."""
    if rows is not None:
        cols = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else "?"
        return f"rows n={len(rows)} cols={cols}"
    if instances is not None:
        try:
            arr = np.asarray(instances)
            return f"instances shape={arr.shape}"
        except Exception:  # noqa: BLE001
            return f"instances n={len(instances)}"
    return "no rows/instances"


@app.route("/invocations", methods=["POST"])
def invocations():
    t0 = time.time()
    try:
        _ensure_loaded()
    except Exception as e:  # noqa: BLE001
        _log_exc("/invocations ensure_loaded")
        return Response(json.dumps({"error": f"model not loaded: {e}"}), status=500,
                        mimetype="application/json")
    try:
        body = request.get_data(as_text=True)
        payload = json.loads(body) if body else {}
    except Exception as e:  # noqa: BLE001
        _log(f"invoke: bad JSON: {e}", always=True)
        return Response(json.dumps({"error": f"bad JSON: {e}"}), status=400,
                        mimetype="application/json")

    # Schema request (sentinel): return the model's I/O contract instead of running
    # inference, so the notebook's Data Catalog can show "feed these columns → get
    # this prediction" WITHOUT a real scoring call. The key is namespaced so it can't
    # collide with a real feature column. This is the only route reachable via
    # InvokeEndpoint, so schema has to ride on /invocations. Foreign containers won't
    # implement it — the caller treats a missing `microvm_schema` marker as "no schema".
    if isinstance(payload, dict) and payload.get("__microvm_schema__"):
        _log(f"invoke: schema request kind={_KIND}")
        try:
            return _json_response(_model_schema())
        except Exception as e:  # noqa: BLE001
            _log_exc("/invocations schema")
            return Response(json.dumps({"error": f"schema unavailable: {e}"}),
                            status=500, mimetype="application/json")

    rows = payload.get("rows")
    instances = payload.get("instances")
    _log(f"invoke: kind={_KIND} {_describe_payload(rows, instances)}")

    try:
        if _KIND == "sklearn":
            resp = _invoke_sklearn(rows, instances)
        else:
            resp = _invoke_onnx(rows, instances)
    except Exception as e:  # noqa: BLE001
        # Never let an inference error surface as an opaque container crash — log the
        # full traceback and return a 500 with the message so it's visible both in
        # CloudWatch and to the caller.
        _log_exc("/invocations inference")
        return Response(json.dumps({"error": f"{type(e).__name__}: {e}"}), status=500,
                        mimetype="application/json")
    _log(f"invoke: OK in {time.time() - t0:.3f}s")
    return resp


def _bad_request(msg):
    return Response(json.dumps({"error": msg}), status=400, mimetype="application/json")


def _model_schema() -> dict:
    """Describe the loaded model's I/O contract for the notebook Data Catalog:
        {microvm_schema: True, kind, task, feat_cols[], classes[]?, n_features}
    feat_cols are the RAW input columns you feed (encoding is baked into the model),
    classes are the classifier labels (omitted for a regressor). All best-effort —
    a field we can't determine is simply omitted. The `microvm_schema` marker tells
    the caller this is OUR reply (a foreign container won't set it)."""
    out = {"microvm_schema": True, "kind": _KIND}

    if _KIND == "sklearn":
        # A raw-row Pipeline records the input columns as feature_names_in_.
        names = getattr(_MODEL, "feature_names_in_", None)
        if names is not None:
            out["feat_cols"] = [str(n) for n in np.asarray(names).tolist()]
            out["n_features"] = len(out["feat_cols"])
        else:
            n = getattr(_MODEL, "n_features_in_", None)
            if n is not None:
                out["n_features"] = int(n)
        classes = getattr(_MODEL, "classes_", None)
        if classes is not None:
            out["task"] = "classification"
            out["classes"] = [_json_safe(c) for c in np.asarray(classes).tolist()]
        else:
            out["task"] = "regression" if getattr(_MODEL, "_estimator_type", "") == "regressor" else "unknown"
        return out

    # onnx: the graph's named inputs ARE the raw columns for a per-column mixed-type
    # graph (the fixed to_onnx export). Report their names/types/shapes + stamped
    # classes. n_features = input count for a multi-input graph, else last input dim.
    inputs = _ONNX_INPUTS or []
    def _shape(s):
        try:
            return [d if isinstance(d, int) else (d or "?") for d in s]
        except Exception:  # noqa: BLE001
            return []
    out["inputs"] = [{"name": i.name, "type": i.type, "shape": _shape(i.shape)} for i in inputs]
    # For a per-column graph each input is one raw column → surface them as feat_cols.
    if len(inputs) > 1:
        out["feat_cols"] = [i.name for i in inputs]
        out["n_features"] = len(inputs)
    elif inputs:
        shp = _shape(inputs[0].shape)
        if shp and isinstance(shp[-1], int):
            out["n_features"] = shp[-1]
    if _ONNX_CLASSES is not None:
        out["task"] = "classification"
        out["classes"] = [_json_safe(c) for c in _ONNX_CLASSES]
    return out


def _invoke_sklearn(rows, instances):
    if rows is not None:
        X = _rows_to_frame(rows)
    elif instances is not None:
        X = np.asarray(instances, dtype=float)
    else:
        return _bad_request("provide 'rows' or 'instances'")
    preds = np.asarray(_MODEL.predict(X)).tolist()
    out = {"predictions": preds}
    if hasattr(_MODEL, "predict_proba"):
        try:
            out["probabilities"] = np.asarray(_MODEL.predict_proba(X)).tolist()
            # Emit the class labels so the caller can label proba columns exactly like
            # a local predict_proba (else it falls back to class_0/class_1). Column
            # order of probabilities matches classes_ by sklearn contract.
            classes = getattr(_MODEL, "classes_", None)
            if classes is not None:
                out["classes"] = [_json_safe(c) for c in np.asarray(classes).tolist()]
        except Exception:  # noqa: BLE001
            _log("invoke: sklearn predict_proba failed (omitting probabilities)")
    return _json_response(out)


def _invoke_onnx(rows, instances):
    # onnx path — supports BOTH graph shapes:
    #   • MULTI-INPUT (raw-row graph from the fixed to_onnx): one named tensor per
    #     original column (string/float). Feed via "rows" (list of feature dicts),
    #     routing each column to its matching graph input.
    #   • SINGLE-INPUT (legacy X[?,N] graph, or any bare-estimator export): one input
    #     tensor. Feed via "instances" (positional matrix) OR "rows" (dicts, whose
    #     values are stacked in graph-input order into the single tensor).
    feed = {}
    if len(_ONNX_INPUTS) == 1:
        inp = _ONNX_INPUTS[0]
        if instances is not None:
            mat = np.asarray(instances)
        elif rows is not None:
            # Stack each row's values into a matrix (dict insertion order preserved).
            mat = np.asarray([list(r.values()) for r in rows])
        else:
            return _bad_request("onnx model expects 'rows' or 'instances'")
        if "string" not in inp.type:
            mat = mat.astype(np.float32)
        feed[inp.name] = mat
    else:
        # Multi-input: requires named columns → "rows".
        if rows is None:
            return _bad_request("this ONNX model has multiple named inputs; "
                                "provide 'rows' (feature dicts)")
        for inp in _ONNX_INPUTS:
            col = inp.name
            try:
                vals = [r[col] for r in rows]
            except KeyError:
                return _bad_request(f"ONNX input '{col}' missing from rows; "
                                    f"expected columns: {[i.name for i in _ONNX_INPUTS]}")
            arr = np.array(vals).reshape(-1, 1)
            if "string" not in inp.type:
                arr = arr.astype(np.float32)
            feed[col] = arr

    outputs = _MODEL.run(None, feed)
    result = {"predictions": np.asarray(outputs[0]).ravel().tolist()}
    if len(outputs) > 1:
        try:
            probs = np.asarray(outputs[1])
            result["probabilities"] = probs.tolist()
            # Label proba columns with the real class labels (stamped at deploy time),
            # matching the sklearn/xgboost responses. Only when the count lines up.
            if _ONNX_CLASSES is not None and probs.ndim == 2 and len(_ONNX_CLASSES) == probs.shape[1]:
                result["classes"] = list(_ONNX_CLASSES)
        except Exception:  # noqa: BLE001
            _log("invoke: onnx probabilities output not array-able (omitting)")
    return _json_response(result)


# Log the environment at import (gunicorn worker boot) so the very first thing in the
# endpoint's CloudWatch stream is a full picture of the runtime + artifact.
_env_banner()


if __name__ == "__main__":
    # Local debug only; in the container gunicorn serves this app on :8080.
    # Bind to loopback for the local-debug path (threat-model T001); the container
    # entrypoint uses gunicorn, not this __main__ block, so the served port is
    # unaffected.
    _ensure_loaded()
    app.run(host="127.0.0.1", port=8080)
