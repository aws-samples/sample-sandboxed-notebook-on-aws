# =============================================================================
# Custom SageMaker serving container — ONE image for sklearn (.joblib) + ONNX (.onnx)
# =============================================================================
# Serverless-compatible by design:
#   - gunicorn binds :8080 directly (NO nginx) → never writes /etc, so it survives
#     SageMaker Serverless's read-only root filesystem (the stock SKLearn container
#     fails there writing /etc/sagemaker-nginx.conf).
#   - small footprint (slim base + a handful of wheels) → well under the 10GB
#     serverless image limit (unlike the ~18GB Triton image that bundles onnxruntime).
#   - MUST be built for linux/amd64: SageMaker Serverless runs on x86 CPU.
#
# SageMaker BYO-container hosting contract:
#   - image has an executable `serve` on PATH (SageMaker runs `docker run <img> serve`)
#   - the app answers GET /ping (health) and POST /invocations (inference) on :8080
#   - the model artifact (model.tar.gz) is unpacked read-only into /opt/ml/model
# =============================================================================
FROM public.ecr.aws/docker/library/python:3.11-slim

# System deps: certs, plus libgomp1 (OpenMP runtime, libgomp.so.1) required by
# lightgbm — a .joblib Pipeline with estimator='lightgbm' imports lightgbm at load,
# and its wheel does NOT bundle OpenMP (unlike xgboost-cpu). No build toolchain
# needed (all Python deps are prebuilt wheels).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/program
COPY requirements.txt /opt/program/requirements.txt
RUN pip install --no-cache-dir -r /opt/program/requirements.txt

COPY serve_app.py /opt/program/serve_app.py
COPY serve /opt/program/serve
RUN chmod +x /opt/program/serve

# `serve` must be found on PATH (SageMaker invokes it as the container command),
# and Python must import serve_app from here.
ENV PATH="/opt/program:${PATH}" \
    PYTHONPATH="/opt/program:${PYTHONPATH}" \
    PYTHONUNBUFFERED=1

# SERVERLESS write-path hardening (image-level defaults; `serve` re-asserts these).
# Serverless mounts the root fs READ-ONLY with only /tmp writable, so point HOME and
# every cache/temp location at /tmp. Prevents startup writes to /root, /etc, etc. —
# the failure mode that broke the stock SKLearn container on serverless.
ENV HOME=/tmp \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/.mpl \
    PYTHONPYCACHEPREFIX=/tmp/.pycache
# /tmp is world-writable so the container works whether SageMaker runs it as root or
# a non-root uid.
RUN chmod 1777 /tmp

EXPOSE 8080
