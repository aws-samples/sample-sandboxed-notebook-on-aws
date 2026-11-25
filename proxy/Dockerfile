# =============================================================================
# VM Proxy — SERVICE container image
# =============================================================================
#
# This Dockerfile builds the container image for the VM Proxy SERVICE — the
# control-plane FastAPI app that fronts every MicroVM (session registry, auth
# token injection, terminal relay, checkpoint orchestration, notebook CRUD,
# metrics). It is NOT the in-VM MicroVM app image: the repo-root Dockerfile is
# the Firecracker / Lambda MicroVM image and must be left untouched. Do NOT use
# the Lambda MicroVM base here — this is an ordinary long-running service
# container (intended for ECS/Fargate behind an ALB).
#
# What it runs:
#   The proxy FastAPI app, exactly as local_install.sh starts it today:
#       uvicorn proxy.server:app --host 0.0.0.0 --port 8081
#   It listens on 8081 (EXPOSE 8081). No --reload (dev-only; never in a service
#   container). /health is suitable for the ALB target-group / ECS health check.
#
# NOT in this image (separate deployables, by design):
#   • The AI chat assistant (agent/ai_assistant/agent_runtime.py) is a SEPARATE
#     service (agent/ai_assistant/Dockerfile, Amazon Bedrock AgentCore Runtime) —
#     it is not started here.
#   • The VM Pool Manager has its own image (pool_manager/Dockerfile).
#
# Build CONTEXT is the repo ROOT (proxy/, app/, batch/, pool_manager/, modelcaps/ all resolve;
# .dockerignore keeps web/, tests/, .git etc. out of the context):
#       docker build -f proxy/Dockerfile -t vm-proxy .
#
# Storage on ECS:
#   For the multi-task ECS target the proxy runs with STORAGE_BACKEND=postgres
#   against shared Aurora. proxy/storage/postgres_db.py imports psycopg2 lazily
#   and raises if it is absent — so proxy/requirements.txt now carries
#   `psycopg2-binary>=2.9` (same pin as pool_manager/requirements.in). The
#   remaining migration work (standing up Aurora, wiring STORAGE_CONNECTION from
#   Secrets Manager, auth) is outside this image.
#
# Operator notes (what the container needs at start-up):
#   • Bind guard: the CMD binds 0.0.0.0, which proxy.platform.auth.assert_loopback_bind
#     refuses outside a container. This image sets DOCKER_CONTAINER=1, which the
#     guard accepts, so no extra flag is needed; the published port is the real
#     network boundary. Outside this image set ALLOW_NON_LOOPBACK_BIND=1.
#   • Set PROXY_AUTH_TOKEN (shared secret for X-Proxy-Auth; the proxy fails closed
#     with 503 when it is unset) and AWS_REGION, give
#     the task an IAM role, and set STORAGE_BACKEND=postgres + STORAGE_CONNECTION
#     for multi-task use (default is a local SQLite file inside the container).
#   • Builds and imports are verified by tests/build/test_service_images_import.py
#     (the image itself was not built or started in CI).
#
# Base image:
#   A stable slim Debian Python, matching pool_manager/Dockerfile. The repo
#   targets a newer Python locally, but slim python:3.13-slim is chosen for a
#   portable service container; nothing the proxy imports requires newer. Bump
#   the tag if a dependency later needs it.
# =============================================================================

FROM python:3.13-slim

# Flush stdout/stderr immediately so logs reach the ECS log driver (awslogs /
# FireLens) -> CloudWatch without buffering.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKER_CONTAINER=1

WORKDIR /app

# Install the proxy SERVICE deps. Copied first so the (heavy) pip layer caches
# across code-only changes. This set is the full proxy/agent/sync host dep list
# (FastAPI/uvicorn, httpx, boto3, the AI/Strands stack, pandas, etc.).
COPY proxy/requirements.txt /app/proxy/requirements.txt
RUN pip install --no-cache-dir -r /app/proxy/requirements.txt

# Copy the proxy package. The proxy imports these repo-root packages besides
# proxy/ and modelcaps/ (the copies below):
#   - pool_manager.config: the stdlib-only grant budget shared with the pool
#     manager (pool_manager/__init__.py resolves its other names lazily, so this
#     import pulls in neither the pool server nor its AWS backend).
#   - app.notebook.{dag_contract,static_analysis,dtypes}: the VM runtime's pure
#     DAG/static-analysis helpers, shared so the proxy and the VM agree on
#     cell-dependency rules (proxy/notebook/dag*.py, proxy/storage/_dag_doc.py,
#     proxy/platform/datasources/*, proxy/platform/routes/sessions.py).
#   - batch.entity_discovery: the entity-discovery job the proxy's AI intel code
#     and the sessions route call (proxy/notebook/ai/intel/*).
# app/__init__.py, app/notebook/__init__.py and batch/__init__.py are empty, so
# importing those modules pulls in only stdlib + packages already in the lockfile
# (no executor/pandas-at-import chain). The whole directories are copied, not just
# the used files, so a new helper import keeps working;
# tests/build/test_service_images_import.py fails if a repo-root package is imported but
# not copied. The image never RUNS the VM server in app/.
COPY proxy/ /app/proxy/
COPY app/ /app/app/
COPY batch/ /app/batch/
COPY pool_manager/ /app/pool_manager/
# Copy the canonical modelcaps package. proxy.notebook.routes.ai and
# proxy.notebook.ai.notebook_agent import the repo-root `modelcaps` package for
# per-model Bedrock inference params + the /ai/models catalog. The build context is
# the repo ROOT, so /modelcaps is available to COPY; placing it at /app/modelcaps
# (WORKDIR /app is on sys.path) makes `import modelcaps` resolve at runtime.
COPY modelcaps/ /app/modelcaps/

# Run as a non-root user (nice-to-have hardening; the service needs no root).
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

# Proxy listens on 8081.
EXPOSE 8081

# Mirror local_install.sh's launch command. No --reload in a service container.
CMD ["uvicorn", "proxy.server:app", "--host", "0.0.0.0", "--port", "8081"]
