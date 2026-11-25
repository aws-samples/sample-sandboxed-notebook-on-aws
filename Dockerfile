# =============================================================================
# Lambda MicroVM Sandbox — Application Image
# =============================================================================
#
# This Dockerfile defines the application image that runs inside each Lambda
# MicroVM. It is NOT a traditional container — Lambda builds a Firecracker
# memory snapshot from this image, and new VMs restore from that snapshot
# in milliseconds (no boot, no init, no cold start).
#
# Architecture:
#   1. Base image: Amazon Linux 2023 minimal (provided by Lambda MicroVMs)
#   2. System packages: Python 3.11, git, bash, tar, gzip
#   3. Python venv with pre-installed data science packages (pandas, numpy, etc.)
#   4. FastAPI app serving on port 8080 (code execution + lifecycle hooks)
#
# Build process (handled by AWS):
#   - Lambda pulls this Dockerfile + code from S3 (notebook-microvm.zip)
#   - Runs the Dockerfile to create the filesystem
#   - Boots the image, waits for the /ready hook to return 200
#   - Takes a Firecracker memory snapshot (all imports pre-loaded in RAM)
#   - New VMs restore from this snapshot — Python + packages already in memory
#
# CPU architecture (arm64 / x86_64):
#   This Dockerfile is ARCH-NEUTRAL — there is NO per-arch Dockerfile and no
#   arch baked in here. Both arches share one AWS-managed base image (al2023-1);
#   the target architecture is selected by `create-microvm-image
#   --cpu-configurations architecture=ARM_64|X86_64` (see
#   scripts/build_all_images.sh and cpu_arch_api_value_for in scripts/config.sh),
#   NOT by the FROM tag below. `al2023-minimal` is a multi-arch manifest, so the
#   same FROM resolves to the arm64 or x86_64 variant of the image being built.
#   Every pin in requirements.txt is pure Python or ships manylinux wheels for
#   both aarch64 and x86_64, and nothing here downloads an arch-specific binary,
#   so one Dockerfile builds correctly for every arch in IMAGE_ARCHES.
#
# Terminal access:
#   The SHELL_INGRESS network connector provides interactive shell access.
#   It requires /bin/bash to exist (hence the explicit `bash` install).
#   The venv PATH is injected into /root/.bashrc so that `python3`, `pip`,
#   `git`, and all installed packages are available in the terminal.
#
# Adding packages:
#   Add Python packages to requirements.txt. They'll be pre-installed in the
#   venv and available instantly on VM launch (no pip install at runtime).
#   System packages go in the `dnf install` line below.
#
# Port 8080:
#   Lambda MicroVM ingress routes all inbound traffic to port 8080 by default.
#   The FastAPI app listens here and handles code execution, lifecycle hooks,
#   file uploads, metrics, and variable introspection.
# =============================================================================

# Multi-arch base manifest — resolves to arm64 or x86_64 to match the
# --cpu-configurations architecture selected at build time. Do NOT hardcode an arch here.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# System packages:
#   python3.11   — Runtime for the sandbox app and user code
#   python3.11-pip — Package installer (used to bootstrap venv)
#   git          — Clone repos from the terminal
#   tar, gzip    — Extract archives (wget'd datasets, etc.)
#   bash         — Required by SHELL_INGRESS for interactive terminal access
#   libgomp      — OpenMP runtime (libgomp.so.1). Required by lightgbm, which a
#                  user can lazy-install when loading a SageMaker estimator='lightgbm'
#                  model (sm_load_model). The lightgbm wheel does NOT bundle OpenMP
#                  (unlike xgboost-cpu), so without this the import fails with
#                  "libgomp.so.1: cannot open shared object file".
RUN dnf install -y python3.11 python3.11-pip git tar gzip bash findutils libgomp && dnf clean all

# Symlink python3 globally so any shell (including platform shell) can find it
RUN ln -sf /usr/bin/python3.11 /usr/bin/python3 && \
    ln -sf /usr/bin/python3.11 /usr/bin/python

# Use a venv to keep packages isolated from system Python
RUN python3.11 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH" \
    DOCKER_CONTAINER=1

# Make the venv PATH available in terminal sessions (SHELL_INGRESS spawns bash)
# Without this, `python3` in the terminal finds the bare system Python (no packages)
# Also set a useful prompt showing the current directory, and start interactive
# terminals in /tmp — that's where user files live (uploads land in /tmp and the
# notebook's file model is /tmp-scoped), so the SHELL_INGRESS terminal opens right
# where the user's data is instead of the app's /app working dir. The `[[ $- == *i* ]]`
# guard limits the cd to INTERACTIVE shells so non-interactive `bash -c` calls that
# source .bashrc aren't affected.
RUN echo 'export PATH="/app/venv/bin:$PATH"' >> /root/.bashrc && \
    echo 'export PS1="\[\e[36m\]\w\[\e[0m\] \$ "' >> /root/.bashrc && \
    echo '[[ $- == *i* ]] && cd /tmp 2>/dev/null' >> /root/.bashrc

# Install sandbox server + pre-baked data science packages
# These are captured in the Firecracker snapshot — zero cold-start import latency
# requirements.txt lives inside app/ (moved from the repo root). The build
# context is the app.zip root, which contains app/ + this Dockerfile, so the
# file is at app/requirements.txt here.
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application code (FastAPI app with execution engine + lifecycle hooks)
WORKDIR /app
COPY app/ ./app/

# Pre-generate the autocomplete completion catalog by introspecting the installed
# libraries (pandas, numpy, plotly, boto3, builtins, stdlib). This runs at BUILD
# time — the venv is on PATH and every lib from requirements.txt is present — and
# bakes app/notebook/data/completion_catalog.json into the image, which the editor
# fetches via GET /completions-catalog for signature-rich, zero-runtime-cost
# completions. Non-fatal: if generation fails the app still serves an empty catalog.
RUN python -m app.notebook.completion_catalog || echo "WARN: completion catalog generation failed; editor will use live introspection only"

# Lambda MicroVM ingress routes to this port by default
EXPOSE 8080

# Start the FastAPI server (uvicorn)
# This process is snapshotted after /ready returns — on restore it's already running
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8080"]
