# =============================================================================
# AI Chat Assistant — SERVICE container image (Amazon Bedrock AgentCore Runtime)
# =============================================================================
# Builds the container for the extracted chat-agent service — the SAME artifact
# the AgentCore CLI (`agentcore dev` / `agentcore deploy`) builds and runs. It is
# NOT the in-VM MicroVM image (repo-root Dockerfile) and NOT the proxy image.
#
# What it runs: the BedrockAgentCoreApp from ai_assistant/agent_runtime.py, under
# OpenTelemetry auto-instrumentation (AgentCore Runtime observability), exposing
# POST /invocations + GET /ping on 8080 (the AgentCore service-contract port).
#
# BUILD CONTEXT = `codeLocation` from agentcore.json, i.e. THIS directory
# (agent/ai_assistant/).
#   `agentcore dev`/`deploy` set the Docker build context to the runtime's
#   `codeLocation` (here `ai_assistant/`), NOT the project root and NOT the repo
#   root. So every COPY path here is relative to agent/ai_assistant/ — the package
#   files sit at the context ROOT (`COPY pyproject.toml uv.lock ./`, `COPY . .`),
#   exactly like the CLI-generated scaffold Dockerfile. Docker cannot COPY from
#   OUTSIDE the context, so this image is deliberately SELF-CONTAINED: it never
#   reaches up to the sibling proxy/ package (which lives above the context).
#
# WHY NO proxy/ (unlike pool_manager/Dockerfile):
#   The agent imports proxy.storage ONLY on the local SQLite memory path
#   (ai_assistant/memory.py's lazy `from proxy.storage import storage`). On
#   AgentCore the agent runs with MEMORY_BACKEND=agentcore (AgentCore Memory),
#   which never reaches that import — verified: the package imports cleanly with
#   no proxy/ present under the agentcore backend. The local SQLite dev path is
#   served by local_install.sh running the agent as a plain `python -m
#   ai_assistant.agent_runtime` from the shared repo .venv (where proxy/ IS
#   importable) — that path does NOT use this image. So the container is the
#   AgentCore artifact (AgentCore Memory), and proxy/ stays out of it.
#
# Base: python:3.13-slim. Deps resolved with uv from the frozen uv.lock.
# =============================================================================

FROM python:3.13-slim

# DOCKER_CONTAINER=1 is REQUIRED (not cosmetic): BedrockAgentCoreApp.run() binds
# 0.0.0.0 ONLY if it detects a container via /.dockerenv OR the DOCKER_CONTAINER
# env — otherwise it binds 127.0.0.1, reachable only INSIDE the container, so the
# host port-forward (and the `agentcore dev` CLI's fetch) fails with "fetch failed"
# even though the server is healthy in-container. finch does NOT create /.dockerenv
# (that's Docker-specific), so this env is what forces the 0.0.0.0 bind under finch.
# The CLI-generated scaffold Dockerfile sets it for the same reason.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKER_CONTAINER=1

WORKDIR /app

# uv — the dependency resolver/installer used for the frozen sync below.
RUN pip install uv

# Resolve deps first from the lockfile (this layer caches across code changes).
# --no-install-project: install only the locked dependencies, not the ai-assistant
# project itself (the code is COPYd below and run as `python -m ai_assistant.*`).
# Build context is agent/ai_assistant/, so pyproject.toml + uv.lock are at the
# context root. The in-image package dir is /app/ai_assistant/ (the package name
# the code imports), so we copy INTO that subdir.
COPY pyproject.toml uv.lock /app/ai_assistant/
# --group tracing: the container (unlike local dev) runs under
# `opentelemetry-instrument` (see CMD), so it needs the CONTAINER-ONLY `tracing`
# group (aws-opentelemetry-distro). That group is NOT installed into the shared
# local .venv — local_install.sh uses `uv export --no-dev` with no --group, which
# excludes it, letting opentelemetry-api float up to satisfy fastapi there.
RUN cd /app/ai_assistant && uv sync --frozen --no-dev --group tracing --no-install-project

# Copy the agent package contents (context root = the package dir) INTO
# /app/ai_assistant/ so `python -m ai_assistant.agent_runtime` resolves. Self-
# contained — no sibling proxy/ (see the header).
#
# VENDORED modelcaps: ai_assistant/modelcaps/ is a GENERATED, on-disk vendored copy
# of the repo-root /modelcaps runtime trio (params.py, __init__.py, model_caps.json),
# produced by scripts/sync_modelcaps.sh. Because it lives physically under this build
# context it is swept in by `COPY . /app/ai_assistant/` here (and by `agentcore
# deploy`), so notebook_agent.py's `from .modelcaps import ...` (i.e. ai_assistant.
# modelcaps) resolves with no sibling proxy/ needed. Never hand-edit it; edit
# /modelcaps and re-run sync.
COPY . /app/ai_assistant/

# Finalize the environment now that the project code is present.
# Keep --group tracing here too so the finalize sync doesn't prune the
# container-only observability distro installed in the layer above.
RUN cd /app/ai_assistant && uv sync --frozen --no-dev --group tracing

# uv installs into /app/ai_assistant/.venv — put it on PATH so opentelemetry-instrument
# and python resolve to the synced environment.
ENV PATH="/app/ai_assistant/.venv/bin:$PATH"

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

# Chat agent listens on 8080 — the AgentCore Runtime HTTP service-contract port
# (/invocations, /ping). In the container AI_PORT is unset, so agent_runtime binds
# 8080 (BedrockAgentCoreApp.run's default), which is what the AgentCore CLI/runtime
# connects to. (Local non-container dev uses 8082 via AI_PORT; see agent_runtime.py.)
EXPOSE 8080

# Run the Runtime artifact under OpenTelemetry auto-instrumentation.
# WORKDIR /app keeps the dotted name `ai_assistant.agent_runtime` resolvable.
CMD ["opentelemetry-instrument", "python", "-m", "ai_assistant.agent_runtime"]
