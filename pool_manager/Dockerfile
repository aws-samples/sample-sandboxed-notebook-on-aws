# =============================================================================
# VM Pool Manager — SERVICE container image
# =============================================================================
#
# This Dockerfile builds the container image for the VM Pool Manager SERVICE
# (the standalone process that pre-boots + maintains the warm spare pool). It is
# NOT the in-VM MicroVM app image: the repo-root Dockerfile is the Firecracker /
# Lambda MicroVM image and must be left untouched. Do NOT use the Lambda MicroVM
# base here — this is an ordinary long-running service container (intended for
# ECS/Fargate, desiredCount=1).
#
# What it runs:
#   The pool manager FastAPI app, the same module local_install.sh starts
#   (there bound to POOL_BIND_HOST, 127.0.0.1; here on all interfaces of the
#   container):
#       uvicorn pool_manager.server:app --host 0.0.0.0 --port 8090
#   It listens on 8090 (EXPOSE 8090). No --reload (that is a dev-only convenience
#   and must never run in a service container).
#
# ⚠ PROXY COUPLING GOTCHA (important — read before trimming the COPY lines):
#   The pool manager HARD-IMPORTS proxy code. pool_manager/server.py does
#       from proxy.storage import storage as default_storage
#   so importing the server pulls in proxy.storage (pool_manager/__init__ itself
#   is lazy and imports nothing). A naive image that COPYs only pool_manager/
#   therefore fails at import time (ModuleNotFoundError: proxy). This image
#   deliberately COPYs BOTH the pool_manager/ AND the whole proxy/ package so the
#   import graph resolves. (A cleaner long-term fix is to factor proxy.storage out
#   into a shared library that both services depend on — NOT done here; that is a
#   larger refactor out of scope for this packaging change.)
#
#   Because both packages must be copied, the build CONTEXT is the repo ROOT:
#       docker build -f pool_manager/Dockerfile -t pool-mgr .
#
# Operator notes: the CMD binds 0.0.0.0; the bind guard (assert_loopback_bind in
#   pool_manager/server.py) accepts it because this image sets DOCKER_CONTAINER=1
#   (outside a container set ALLOW_NON_LOOPBACK_BIND=1). Set AWS_REGION, an IAM role,
#   POOL_AUTH_TOKEN (the proxy must send the same one), and STORAGE_BACKEND=postgres +
#   STORAGE_CONNECTION. Imports are verified by tests/build/test_service_images_import.py;
#   the image itself was not built or started in CI.
#
# Base image:
#   A stable slim Debian Python. The repo targets Python 3.14 locally (.venv is
#   3.14.5), but a slim python:3.13-slim is chosen here deliberately: it is more
#   portable and widely available for a service container, and nothing the pool
#   manager imports requires 3.14. Bump the tag if a dependency later needs it.
# =============================================================================

FROM python:3.13-slim

# Flush stdout/stderr immediately so logs reach CloudWatch without buffering.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKER_CONTAINER=1

WORKDIR /app

# Install the pool-manager SERVICE deps from the hash-pinned proxy/requirements.txt
# lockfile. There is deliberately NO pool_manager/requirements.txt: per
# pool_manager/requirements.in and the header of proxy/requirements.txt, the lock is
# compiled from proxy/requirements.in + pool_manager/requirements.in, so it covers
# the pool manager's deps (fastapi, uvicorn, httpx, boto3, psycopg2-binary) as a
# strict subset. It is a superset image-wise (the pool manager imports proxy.storage,
# so it needs the proxy's storage deps anyway). Copied first so the pip layer
# caches across code-only changes.
COPY proxy/requirements.txt /app/proxy/requirements.txt
RUN pip install --no-cache-dir -r /app/proxy/requirements.txt
# Copy the packages the import graph needs — see the PROXY COUPLING GOTCHA above.
# pool_manager hard-imports proxy.storage, and the proxy package in turn imports
# app.notebook.{dag_contract,static_analysis,dtypes} and batch.entity_discovery
# (empty package __init__ files, so only stdlib + lockfile packages are pulled in).
# See proxy/Dockerfile for details. tests/build/test_service_images_import.py checks this.
COPY pool_manager/ /app/pool_manager/
COPY proxy/ /app/proxy/
COPY app/ /app/app/
COPY batch/ /app/batch/
# The proxy package imports the repo-root `modelcaps` package (per-model Bedrock
# inference params + /ai/models catalog); it must be at /app/modelcaps.
COPY modelcaps/ /app/modelcaps/
# Run as a non-root user (nice-to-have hardening; the service needs no root).
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

# Pool manager listens on 8090.
EXPOSE 8090

# Mirror local_install.sh's launch command. No --reload in a service container.
CMD ["uvicorn", "pool_manager.server:app", "--host", "0.0.0.0", "--port", "8090"]
