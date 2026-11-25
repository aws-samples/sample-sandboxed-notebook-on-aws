# Setup & Operations

How to install, run, configure, and tear down Sandboxed Notebook on AWS on your
machine. For what the project is and how it works, see the [README](README.md).

**Contents:** [Quick Start](#quick-start) · [Local-only by design](#security-local-only-by-design) · [Prerequisites](#prerequisites) · [Aurora (opt-in)](#aurora-is-optional-opt-in) · [Warm Pool (opt-in)](#warm-pool-is-optional-opt-in) · [S3 Tables & Bedrock KB](#s3-tables-apache-iceberg--the-bedrock-knowledge-base--always-installed) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [Stopping the app](#stopping-the-app) · [Teardown](#teardown)

> **Platform: macOS.** `local_install.sh` is written and tested for macOS (Apple
> Silicon or Intel). It uses macOS/BSD conventions (`lsof`, the AWS CLI `.pkg`
> installer, Homebrew install hints) and builds **arm64 and x86_64** MicroVM
> images by default. It may run on Linux with minor tweaks, but that path is unsupported.

## Quick Start

```bash
./local_install.sh        # builds images, seeds all data sources, starts proxy + AI agent + UI (SQLite, no Aurora)
./local_stop_app.sh       # when you're done: closes the app — stops every service and terminates its MicroVMs
```

Use **`./local_stop_app.sh`** to close the app (see [Stopping the app](#stopping-the-app)).
Ctrl+C in the `local_install.sh` terminal only stops the local services and leaves
the MicroVMs (notebook VMs and warm-pool spares) running and billing.

That's the whole setup — the script is self-checking and self-contained. On every
run it:

1. **Reaps stale processes** from a previous run (frees ports 8081 / 8082 / 5173
   and any leftover sync worker).
2. **Preflights your tools** and fails fast with one actionable list if anything is
   missing: the CLI tools `jq`, `curl`, `unzip`, `git` (with a `brew install …`
   hint), **AWS CLI ≥ 2.35.10** (needed for the `lambda-microvms` API), **Python ≥ 3.13**,
   and **Node.js ≥ 20.19 / 22.12** (for the Vite 8 UI). The Python check tries
   `python3.14`, `python3.13`, `python3` and `python` in that order and uses the first
   one that is 3.13 or newer. If none qualifies it prints the version it found and
   where to download Python. An existing `.venv` built by an older interpreter is
   rejected too, with the command to recreate it (`rm -rf .venv && ./local_install.sh`).
3. **Creates an isolated project virtualenv at `.venv`** (reused on later runs) and
   installs all proxy/agent/sync Python deps into it — nothing is installed into, or
   run from, your system Python. This sidesteps the modern-macOS "externally managed
   environment" (PEP 668) error and the old `No module named 'boto3'` trap: every
   child script and service uses the same `.venv` interpreter.
4. **Provisions/reuses the AWS resources** (IAM roles, S3 bucket, env-var master
   key) and **builds the MicroVM images**, then **starts the proxy + AI agent + UI**.

A single run provisions the full demo environment: the sample tables (Athena/DynamoDB/S3), the **S3 Tables (Apache Iceberg)** demo tables, and the **Bedrock Knowledge Base** (S3 Vectors) — all idempotent, all reused on later runs. **Aurora and the warm pool are the two opt-ins** (see below).

Opens at http://localhost:5173. Each notebook tab auto-launches a MicroVM. Closing a tab suspends its VM (reopening the notebook resumes it); deleting a notebook terminates its VM.

**Requirements:** macOS, AWS CLI 2.35.10+, Python 3.13+, Node.js 20.19+ (or 22.12+), the `jq`/`curl`/`unzip`/`git` CLI tools, and configured AWS credentials. The installer checks all of these up front. See [Prerequisites](#prerequisites) for details.

---

## Security: local-only by design

This stack is a local developer POC and the installer runs it accordingly. The full
posture, including what is and is not protected, is in [SECURITY.md](SECURITY.md).
What the installer does:

- **Loopback-only binds.** The host-side proxy and warm-pool manager bind
  `127.0.0.1` only (`PROXY_BIND_HOST` / `POOL_BIND_HOST`), so nothing is exposed
  to other machines on your network. The container `CMD`s still bind `0.0.0.0`
  inside the MicroVM/container boundary, which is correct: they are only
  reachable through the proxy, never directly.
- **Per-run shared secret.** Each run mints fresh random `PROXY_AUTH_TOKEN` and
  `POOL_AUTH_TOKEN` values (via `openssl rand`, falling back to Python's
  `secrets`) and exports them to every process it launches. Every proxy and
  warm-pool call must present the token in the `X-Proxy-Auth` header; the UI
  receives it as `VITE_PROXY_TOKEN`. Liveness probes (`/health`, pool `/status`)
  stay unauthenticated so the installer can poll readiness. The tokens live only
  in process environment for that run and are never written to disk.

Exposing any of these services beyond `127.0.0.1` requires adding real
authentication and transport security first; the loopback bind and shared secret
are a local safety floor, not production auth.

---

## Prerequisites

**Operating system: macOS** (Apple Silicon or Intel). The launcher is built and
tested on macOS; it assumes macOS/BSD tooling and Homebrew for install hints.

### AWS MicroVM Mode

`local_install.sh` **checks every item below up front** and prints exactly what to
install (with a `brew install …` command when Homebrew is present) before it does
any work — so a fresh Mac gets one clear list rather than a mid-run failure.

| Requirement | Version | Notes |
|------------|---------|-------|
| AWS CLI | 2.35.10+ | Required for the `lambda-microvms` API |
| Python | 3.13+ | Only used to **bootstrap** `.venv`; deps are installed into the venv, never your system Python |
| Node.js | 20.19+ or 22.12+ | Vite 8 (the UI build) requires this; older Node starts the proxy/VMs but not the UI |
| CLI tools | `jq`, `curl`, `unzip`, `git` | `jq` ships with macOS Sequoia 15+ but **not** Sonoma 14 and earlier — `brew install jq` |
| AWS credentials | — | Configured via `~/.aws/credentials` — see [AWS identity & permissions](#aws-identity--permissions-the-profile-you-run-as) for the profile and permissions you need |

```bash
# One-shot install of the CLI tools on macOS (Homebrew)
brew install jq git node awscli      # curl + unzip are built into macOS

# Or install/upgrade just the AWS CLI from Amazon's package (macOS)
curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o /tmp/AWSCLIV2.pkg
sudo installer -pkg /tmp/AWSCLIV2.pkg -target /
```

### AWS identity & permissions (the profile you run as)

Everything the installer provisions is created under **one local AWS CLI
profile** — set by `AWS_CLI_PROFILE` in `scripts/config.sh` (default `default`;
change it to a named profile if you don't use `default`). The account is
auto-detected from that profile (`aws sts get-caller-identity`), and the region
is fixed by `AWS_REGION` in `scripts/config.sh` (`us-west-2`), independent of the
profile's own default region.

That profile's identity must be allowed to **create and manage** resources — this
is a demo installer, so it needs near-admin breadth, not read-only access. On a
first run it creates/updates:

- **IAM** — roles + inline policies (`MicroVMSandboxBuildRole`,
  `MicroVMSandboxExecRole`, and, if used, the SageMaker and Bedrock-KB roles)
- **S3** — the artifacts bucket + bucket policy
- **Secrets Manager** — the env-var master key
- **Lambda MicroVMs** — build/run/suspend/terminate via the `lambda-microvms` API
- **DynamoDB / Athena / Glue / CloudWatch Logs** — sample data, query DB, log
  groups + metric filters
- **S3 Vectors + Bedrock** — the Knowledge Base (always installed)
- **RDS + Secrets Manager** — only with `--AuroraSetup`

A correctly-installed but under-privileged profile (read-only, or missing
`iam:CreateRole`) will **pass every preflight check and then fail mid-provision**.
If you're unsure, run the installer with a profile that has administrative access
to a sandbox/dev account.

> **The `.venv` is managed for you.** The first run creates `.venv` at the repo
> root and installs all Python dependencies there; later runs reuse it. You never
> need to create or activate it. To rebuild it from scratch, delete it and re-run:
> `rm -rf .venv && ./local_install.sh`.

> **Regenerating the dependency lock.** The host-side Python deps are hash-pinned.
> `proxy/requirements.txt` is a GENERATED, fully hash-pinned lockfile — do **not**
> hand-edit it. The human-edited SOURCES are `proxy/requirements.in` and
> `pool_manager/requirements.in` (the pool-manager deps are a strict subset of the
> proxy set and ship from the same merged lock). After editing a `.in`, regenerate
> the lock:
>
> ```bash
> (cd agent/ai_assistant && uv export --no-emit-project --no-dev --format requirements-txt) > /tmp/h5-agent-constraints.txt
> uv pip compile --generate-hashes -c /tmp/h5-agent-constraints.txt \
>     proxy/requirements.in pool_manager/requirements.in -o proxy/requirements.txt
> ```
>
> The agent `uv.lock` export is passed as a constraint so the shared `.venv` resolves
> jointly across the proxy and the AI-assistant agent leg. `local_install.sh` installs
> both legs with `--require-hashes`, so every direct and transitive dependency is
> verified against its sha256.

### IAM Roles (auto-created)
- `MicroVMSandboxBuildRole` — S3 read during image build
- `MicroVMSandboxExecRole` — S3, DynamoDB, Athena (including `athena:StopQueryExecution`, used to cancel a running query when a cell times out or is stopped), Glue, STS for running VMs. The policy is `iam/exec-role-permissions.json`.

### AI Features (optional)
Amazon Bedrock access with model access enabled for the model in `BEDROCK_MODEL_ID` (default Claude Haiku 5.5, see [Configuration](#configuration)). Models in `BEDROCK_EXCLUDE_MODELS` are left out of the model dropdown. If Bedrock is not configured, AI buttons are hidden and everything else works.

---

## Aurora is optional (opt-in)

The stack runs fully on local **SQLite** by default. Aurora PostgreSQL — and the
SQLite→Aurora sync worker that feeds its curated `v_nb_*` views — is entirely
opt-in via a flag. Pick the command that matches what you want:

| You want… | Command | What runs | Aurora |
|---|---|---|---|
| **No Aurora** (fully local) | `./local_install.sh` | proxy + AI agent + UI (SQLite) | untouched; sync worker **not** started |
| **Aurora + sync** | `./local_install.sh --AuroraSetup` | proxy + AI agent + UI + sync worker | ensured (created if missing, else reused) |
| **Reset Aurora schema** | `./local_install.sh --AuroraReset` | same as `--AuroraSetup` | **drops + recreates** the schema (destructive) |

**First-time user:**
- **Without Aurora** — just run `./local_install.sh`. No Aurora resources are
  created or referenced; the sync worker is skipped. This is the simplest path.
- **With Aurora** — run `./local_install.sh --AuroraSetup`. On the first run this
  **provisions** the Aurora Serverless v2 cluster (control plane) and creates the
  roles, tables, indexes, and curated `v_nb_*` views **over the RDS Data API**
  (works from a laptop — no VPC/psycopg2 needed). The resolved ARNs are written to
  `scripts/.aurora_env` and wired into the proxy (VM read path) and the sync worker.

**Subsequent runs:**
- `./local_install.sh` — same fully-local run every time.
- `./local_install.sh --AuroraSetup` — detects the existing cluster and **reuses it
  as-is**: it only re-resolves the ARNs from your account (no schema DDL, no data
  loss) and starts all four services. This is the everyday Aurora command.
- `./local_install.sh --AuroraReset` — only when you deliberately want to wipe and
  rebuild the Aurora schema (also resets the local SQLite DB).

> Aurora ARNs are **not** hardcoded in `scripts/config.sh` — they are discovered
> from your own AWS account at launch and cached in `scripts/.aurora_env`
> (gitignored). A blank Aurora config simply means "no Aurora data source", which
> is the correct default for the no-flag run.

---

## Warm Pool is optional (opt-in)

Launching or restoring a notebook normally pays a cold-provision floor (~8–12s)
while AWS boots a brand-new MicroVM. The **VM Pool Manager** removes that floor by
keeping a small set of pre-booted, suspended spare VMs ready to hand out — a launch
becomes *resume + rebind* (~1–2s). It is **off by default**; the whole stack runs
without it, and with it off launch/restore behavior is exactly as before. (For the
design and the throttling trade-off, see the [README › Warm Pool Manager](README.md#warm-pool-manager-optional).)

| You want… | Command | What runs |
|---|---|---|
| **No warm pool** (default) | `./local_install.sh` | proxy + AI agent + UI; pool manager **not** started |
| **Warm pool** | `./local_install.sh --WarmPool` | the above + VM Pool Manager on `:8090`, grant-from-pool enabled |

`--WarmPool` sets `POOL_ENABLED=true`, which (1) starts the `pool_manager` service
and (2) tells the proxy to grant VMs from the pool. A pre-set `POOL_ENABLED=true`
env var is honored too — the flag just forces it on. The flag composes with the
others, e.g. `./local_install.sh --WarmPool --AuroraSetup`.

**What a `--WarmPool` run does, in order:**

1. **Clean slate (terminate-then-wipe).** Before starting, it terminates every
   MicroVM recorded in `pool_vms` via the real control plane and then deletes its
   row (per-row, so an interrupted run never orphans a live billing VM). Each start
   begins from a known-empty pool. Fail-closed: if the reset can't confirm it ran,
   the install aborts rather than start over un-terminated VMs.
2. **Starts the pool manager** on `http://localhost:8090` (`POOL_MANAGER_PORT`).
3. **Waits up to 180s for the pool to fill** — each spare is a real MicroVM boot
   (~10–15s), minted up to `POOL_FILL_CONCURRENCY` at a time — then prints the
   per-tier counts. On timeout it continues; the pool keeps filling in the
   background.

> **Pooled VMs have fixed lifecycle.** A spare is pre-booted with the configured
> idle-timeout and max-lifetime, so for a pool-granted VM those two controls are
> **frozen at their config values and locked in the UI**. A spare also carries its
> true mint time, so the "Terminates in" countdown reflects the real AWS clock.

A grant has a fixed time budget set in `pool_manager/config.py`: resume (30 s) + rebind (10 s) + restore (120 s) = 160 s. The pool reconcile sweep and the proxy both derive their timeouts from that budget, so a grant that is still within budget is not reaped.

**Tuning** lives in `scripts/config.sh` (all honored only on a `--WarmPool` run):

| Variable | Default | Meaning |
|---|---|---|
| `POOL_SIZE_PER_IMAGE` | `2` | Default spares per `(arch, size)` tier |
| `POOL_SIZE_OVERRIDES_ARM64` | `{"1024":1,"2048":2,"4096":2,"8192":1}` | Per-size overrides (JSON); unset sizes fall back to `POOL_SIZE_PER_IMAGE` |
| `POOL_SIZE_OVERRIDES_X86_64` | `{"1024":0,"2048":1,"4096":0,"8192":0}` | Same, for x86_64: 1 spare for the default 2 GB size, none for the others (`0` = no spares) |
| `POOL_MAX_AGE_HOURS` | `2` | Recycle spares older than this; **clamped to a max of 2** so a granted spare keeps most of the AWS 8h lifetime |
| `POOL_FILL_CONCURRENCY` | `4` | Bounded parallel launches at fill time |
| `POOL_MINT_HEALTH_TIMEOUT_SECONDS` | `90` | How long a mint waits for a fresh spare's `/health` before discarding it |
| `POOL_MANAGER_PORT` | `8090` | Port the pool manager listens on (proxy reaches it via `POOL_MANAGER_URL`) |

> Lower the pool size (`POOL_MAX_AGE_HOURS` is already at its 2 h ceiling) to reduce `ListMicrovms`
> churn if you hit throttling — see the trade-off note in the README. The warm
> pool is a latency optimization, not a free one; leave it off unless fast
> launch/restore matters for your use case.

---

## S3 Tables (Apache Iceberg) & the Bedrock Knowledge Base — always installed

Two richer data sources ship as part of **every** `./local_install.sh` run (no flag
needed) — they need no Aurora and add a few minutes only on first provision:

- **S3 Tables (Apache Iceberg)** — `scripts/setup_s3_tables.sh` provisions a real
  Amazon S3 Table bucket + namespace (`microvm_demo_iceberg`) and creates/seeds two
  Iceberg tables via Athena's federated `s3tablescatalog`: `order_events` (order
  lifecycle log) and `product_price_history` (SCD-2 price history), correlated to
  the existing `orders`/`products`. They surface through the existing **Athena**
  data source — query them by `database.table` (e.g.
  `read_athena("SELECT * FROM microvm_demo_iceberg.order_events LIMIT 100")`); the
  federated catalog is auto-resolved, and Iceberg **time-travel** (`FOR TIMESTAMP
  AS OF` / snapshots) works out of the box. Idempotent; reused on later runs.

- **Bedrock Knowledge Base** (Amazon S3 Vectors) — `scripts/setup_bedrock_kb.sh`
  provisions an S3 Vectors bucket + index, a KB role, the Knowledge Base, and a
  data source ingesting the product-spec PDFs under `scripts/demoproductspec/`,
  then polls until ingestion completes. The PDFs are not in the repo: on the first
  install the folder is empty, so the script generates them (plus `.metadata.json`
  sidecars and `manifest.json`) with `scripts/generate_product_specs.py --no-llm`,
  the built-in template, from the Athena `products`/`orders` demo tables. No Bedrock
  calls are made for this, and the content is deterministic. The generator runs in
  its own venv, `scripts/.specgen-venv` (exact pins in `scripts/kb_specs_lib.sh`).
  Later installs reuse the local PDFs and only upload files that changed. To
  regenerate with Bedrock-authored product prose (model `BEDROCK_MODEL_ID`), run
  `bash scripts/setup_bedrock_kb.sh --regen`; add `--no-llm` to go back to the
  template. Subsequent runs reuse the existing KB/bucket/index/role by name. The resolved KB id is written to
  `scripts/.kb_env` (gitignored) and wired into the proxy and each VM, so notebooks
  can call the pre-loaded `read_bedrock_kb(query, filter=...)` (retrieved chunks)
  and `ask_bedrock_kb(query, filter=...)` (a cited answer) helpers. `BEDROCK_KB_ID`
  accepts a comma-separated list, so multiple KBs can be surfaced at once.

Both are **non-fatal**: if provisioning fails (e.g. the S3 Tables ↔ analytics
integration isn't enabled, or a KB permission is missing) the launch warns and
continues, and the rest of the stack is unaffected. Neither id is hardcoded in
`scripts/config.sh` — they're created/discovered at launch and cached in
`scripts/.kb_env`.

---

## Configuration

Settings live in `scripts/config.sh`, which `local_install.sh` sources. Most use the
`${VAR:-default}` form, so you can override one for a single run from the shell, for
example `DEFAULT_MEMORY_MIB=4096 ./local_install.sh`. Defaults below are the values in
that file. Warm-pool tuning has its own table under [Warm Pool](#warm-pool-is-optional-opt-in).

| Variable | Default | Meaning |
|---|---|---|
| `AWS_REGION` | `us-west-2` | Region for everything the installer creates (set in the file, not overridable from the shell) |
| `AWS_CLI_PROFILE` | `default` | Local AWS CLI profile the installer runs as (set in the file) |
| `SESSION_PERSISTENCE_MODE` | `checkpoint` | `checkpoint` (default: state stays in S3, restore on the next open), `eternal` (reactive recovery: a request that finds the VM dead launches a new one and replays), or `none` (ephemeral, nothing saved). Any other value is treated as `none` |
| `MAX_LIFETIME_SECONDS` | `28800` | Max VM lifetime (8 h, the AWS maximum) |
| `IDLE_TIMEOUT_SECONDS` | `60` | Idle time before AWS suspends a VM (60 s is the AWS minimum) |
| `IMAGE_ARCHES` | `arm64 x86_64` | Architectures to build and offer. `IMAGE_ARCHES=arm64` opts out of x86_64 |
| `DEFAULT_IMAGE_ARCH` | `arm64` | Architecture pre-selected in the UI (the AWS default, cheaper) |
| `IMAGE_SIZES_ARM64` | `1024 2048 4096 8192` | arm64 memory tiers (MiB) |
| `IMAGE_SIZES_X86_64` | `1024 2048 4096 8192` | x86_64 memory tiers (MiB), set independently |
| `DEFAULT_MEMORY_MIB` | `2048` | Memory tier used when a launch names no size. Must be one of the tiers above for every built arch |
| `EXECUTION_TIMEOUT_SECONDS` | `60` | Max seconds one cell may run before it is stopped. Applies to Python and SQL cells (set in the file, not overridable from the shell) |
| `NOTEBOOK_MAX_DISPLAY_ROWS` | `1000` | Max DataFrame or SQL result rows sent to the UI; larger results show "first N of M rows" |
| `PROXY_FORWARD_CONNECT_TIMEOUT` | `5.0` | Seconds to connect from the proxy to a VM before failing (fast failure for an unreachable VM) |
| `PROXY_FORWARD_READ_TIMEOUT` | `120.0` | Seconds the proxy waits for a VM's reply to a forwarded cell request |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-haiku-5-5` | AI Assistant default model (see below) |
| `BEDROCK_REGION` | `$AWS_REGION` | Region for Amazon Bedrock calls |
| `BEDROCK_EXCLUDE_MODELS` | `anthropic.claude-sonnet-4-5-20250929-v1:0` | Comma-separated model ids left out of the dropdown and the fallback chain. Empty excludes nothing |
| `PROXY_PORT` / `AI_PORT` / `POOL_MANAGER_PORT` | `8081` / `8082` / `8090` | Ports of the proxy, AI agent and pool manager. `PROXY_PORT` is fixed in the file; `AI_PORT` and `POOL_MANAGER_PORT` can be overridden. The UI runs on 5173 |

Two more variables are read by the proxy code and are not set in `scripts/config.sh`:

| Variable | Default | Meaning |
|---|---|---|
| `STORAGE_DB_PATH` | `proxy/data/microvm.db` | Path of the local SQLite database. When set, the database directory follows the path you give (`proxy/storage/sqlite_db.py`) |
| `ALLOW_NON_LOOPBACK_BIND` | unset | Set to `1` to let the proxy or pool manager start with a non-loopback bind host. Without it they refuse to start, unless running in a container. See [SECURITY.md](SECURITY.md) |

The proxy, pool manager, VMs and the UI all read `DEFAULT_MEMORY_MIB` from `scripts/config.sh`.
Memory tiers are per-architecture (arm64 and x86_64 need not match; today both
offer 1-8 GB at 2 GB : 1 vCPU). Images are named `notebook-microvm-{arch}-{mem}`.
Both arches build from the same AWS-managed base image (`al2023-1`); the arch is
set per image with `create-microvm-image --cpu-configurations`. The UI shows an
arch picker when more than one arch is built, with arm64 pre-selected. x86_64
compute is priced ~18% higher than arm64 (rates in the README's Cost section).

- **Build time:** the first install builds 8 images (4 per arch) in parallel
  instead of 4, so it takes longer; each image has a 10-minute build timeout.
  Later runs skip images that already exist. `IMAGE_ARCHES=arm64 ./local_install.sh`
  builds and offers arm64 only.
- **Regions:** x86_64 MicroVMs are not available in `ap-east-1` (Hong Kong),
  `ap-southeast-5` (Malaysia), `ap-southeast-7` (Thailand) or `eu-south-2` (Spain).
  When `AWS_REGION` is one of them, `scripts/config.sh` drops x86_64 from
  `IMAGE_ARCHES` and the installer prints a warning; only arm64 is built and offered.
- **Restores** default to the arch the notebook was saved on (and its memory tier),
  whatever arch is picked in the launch form. To restore onto another arch or tier,
  open Restore on a different instance type in the Connect dialog; the choice is sent as
  `restoreOverride: true` with `arch` and `memoryMiB`.
- **pip installs are per-arch.** The baked-in `requirements.txt` stack is the same
  on both arches, but a package installed at runtime needs a wheel for the VM's arch
  (`aarch64` or `x86_64`); without one pip builds from source in the VM, which can
  time out (120s) or fail for lack of a compiler. The Packages panel and the AI's
  `install_package` tool report this plainly ("No prebuilt wheel for X on arm64;
  building from source timed out after 120s …") with pip's output under
  **Details**, and suggest the other arch only when it is offered. The message
  logic runs inside the VM, so it needs images rebuilt for both arches.

Override for testing, for example to check recovery after an AWS max-lifetime kill:
`MAX_LIFETIME_SECONDS=180 ./local_install.sh`

AI model config:
- `BEDROCK_MODEL_ID` in `scripts/config.sh` (default Claude Haiku 5.5) is the AI Assistant's default model (chat, explain, fix, lineage, tags) and the in-VM LLM helpers. The code has no other default. To revert to Claude Sonnet 4.6, set `BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6`.
- `INTEL_MODEL_ID` in `proxy/notebook/ai/constants.py` is Claude Haiku 4.5 for Workbook Intelligence (faster, 2x speedup with near-equal quality). The `INTEL_MODEL_ID` env var overrides it.
- Temperature, token limits and truncation constants are in `proxy/notebook/ai/constants.py`.

**Outbound HTTP proxy env vars are not honored for VM traffic.** The proxy and
the pool manager reuse shared `httpx` clients with a custom transport (connect
retries + keep-alive), and with a custom transport `httpx` ignores
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`. This affects proxy →
MicroVM, proxy → local agent / PyPI (`proxy/platform/http_clients.py`), and pool
manager → MicroVM (`pool_manager/aws_backend.py`, `_shared_http`). AWS SDK calls
(boto3) are not affected; they read the proxy env vars themselves. It only
matters on networks that require an outbound HTTP proxy. The symptom there: AWS
calls (launch, S3, Bedrock) work, but calls to the VM endpoints fail to connect.

---

## Troubleshooting

**Installer says Python 3.13 or newer is required.** The installer tries `python3.14`,
`python3.13`, `python3` and `python`. Install Python 3.13+ from python.org or Homebrew
and re-run. If it reports that the existing `.venv` uses an older Python, recreate it:
`rm -rf .venv && ./local_install.sh`.

**Run All button is disabled and says "Loading data sources".** Run and Run All wait
for the light-weight data source list, because SQL cells need the source type to route.
The button enables when the list has loaded, or at once when the VM is suspended.

**A cell stops with "Execution timed out after 60s".** Every cell, Python or SQL, has the
`EXECUTION_TIMEOUT_SECONDS` budget (default 60). The Stop button ends a cell the same
way. When the cell was waiting on an Amazon Athena query (`read_athena`, UNLOAD or a SQL
cell), the query is cancelled too. That needs `athena:StopQueryExecution` on the exec role (`iam/exec-role-permissions.json`).

**Terminating a VM returns 409 "microvmId belongs to a different session".** The proxy
refuses a terminate request that names a session other than the one that owns the VM.
The Connect dialog lists only the VMs of the current browser tab for the same reason.
Connecting means restoring the notebook onto a new VM or launching a new VM; there is no
attach to an existing one.

**A request to a VM returns 409 "VM is no longer available".** The VM is terminated or
failed. In `checkpoint` mode reopen the notebook and choose Restore. In `eternal` mode the
next request launches a replacement itself.

**Packages could not be installed after a restore on another architecture.** A restore
re-installs the notebook's pip packages by name for the VM's own arch. A package without a
build for that arch is skipped; the restore itself still succeeds. The banner after the
restore names the packages that failed (action Open Packages), and the Packages panel marks
each one with the pip reason. Install a version that has a build, or restore on the original
architecture, where the failed names are tried again. A banner saying packages were "not
verified" means the VM could not report (for example an older image); check the Packages
panel.

**Restore shows a memory warning.** The warning appears for the chosen tier, smaller,
larger or the saved one, when the estimated need (2x the checkpoint size) is 50 percent or
more of that tier (a strong warning above 90 percent). The restore is still allowed. If the notebook then runs out of memory, terminate
with save, and restore on a larger tier from Restore on a different instance type.

**pip install fails on one CPU architecture.** The baked-in `requirements.txt` stack is
identical on arm64 and x86_64, but a package you install later is resolved for the
VM's own arch. pip uses a prebuilt wheel (`aarch64` or `x86_64`) when the package
publishes one. Otherwise it builds from source inside the VM, which can time out or fail
because the image has no compiler. A package can have a wheel on one arch and not the other.

A failed install through the Packages panel or the AI's `install_package` tool says why
instead of returning raw pip output. Examples:

- "No prebuilt wheel for X on arm64; building from source timed out after 120s. Try a
  version that has an arm64 wheel."
- "No version of X is available for Python 3.11 on x86_64 (pip found no matching
  distribution)."
- "No version of pandas matches '==99' for Python 3.11 (available: ...)". A version pin
  that does not exist is not blamed on the architecture.
- "...timed out ... while downloading its source package (no prebuilt wheel for arm64;
  the source build hadn't started yet)".

For the no-wheel cases only, and only when the deployment offers the other arch, the
Packages panel adds "Running the notebook on x86_64 may help: it may have a prebuilt
wheel." (or ARM64). A **Details** expander with pip's output stays open until you
dismiss it. These messages come from `app/` code baked into the images, so rebuild the
images for both arches (`scripts/build_all_images.sh`) for a change to take effect.

**AWS calls work but calls to VM endpoints fail to connect.** See the note on outbound
HTTP proxy variables at the end of [Configuration](#configuration).

---

## Stopping the app

```bash
./local_stop_app.sh                 # shows what it will do, asks once, then closes the app
./local_stop_app.sh --dry-run       # preview only — changes nothing
```

`local_stop_app.sh` is the counterpart to `local_install.sh`. It touches only VMs
launched from this app's images (`notebook-microvm-*`) and only processes running
from this checkout. In order, it:

1. **Takes stock** of the live MicroVMs — notebook VMs, warm-pool spares, orphans —
   and asks for confirmation.
2. **Deletes the running notebooks** (those with a live VM) exactly as the UI's
   *Delete permanently* does: the VM is checkpointed to S3 and terminated, then the
   notebook's cells, version history, runs and chat are removed from the DB (SQLite
   or Postgres). Its S3 data goes too: cell images and attachments, the checkpoints of
   each session it owns, and the files promoted to `user-data/<session_id>/`. Shared
   `samples/` and the flat `user-data/` area are never touched. Notebooks without a
   live VM are kept.
3. **Stops every local service** — proxy (8081), AI agent (8082), pool manager
   (8090), UI (5173), the SQLite→Aurora sync worker and the `local_install.sh`
   parent.
4. **Terminates the remaining MicroVMs** (pool spares, orphans) and marks them
   terminated in the DB.
5. **Clears the app's saved tabs in your browser**, so a deleted notebook that was
   still open in a tab isn't recreated by the UI's autosave next time. It briefly
   serves a cleanup page on `http://localhost:5173` (loopback only) and opens it; an
   app tab that's still open reloads onto it by itself — keep that tab visible
   while the script runs.

Images, IAM roles, buckets and data are kept, so the next `./local_install.sh`
starts fast.

| Option | Effect |
|---|---|
| `--yes` / `-y` | Skip the confirmation prompt (required when not run from a terminal) |
| `--dry-run` | Show the plan, change nothing |
| `--keep-notebooks` | Keep running notebooks: their VMs are checkpointed + terminated, the notebooks stay (they offer *Restore* on reopen) |
| `--keep-vms` | Stop the local services only; leave all VMs and notebooks |
| `--clear-all-ui-storage` | Wipe **all** the app's browser data (theme, panel prefs, Intel card ticks too), via the standard `Clear-Site-Data: "storage"` header |
| `--no-ui-cleanup` | Skip the browser step |
| `--ui-cleanup-only` | Run only the browser step (the app must already be stopped) |

To also delete the MicroVM **images** (and drop their standing storage cost), use
the full teardown below — it runs `local_stop_app.sh --yes` first.

---

## Teardown

```bash
bash scripts/teardown.sh
```

First closes the local app with `./local_stop_app.sh --yes`: open notebooks (with a
live VM) are checkpointed and deleted as the UI's *Delete* does, every local service
(incl. the pool manager and sync worker) is stopped and the warm-pool spares are
terminated, so nothing re-creates VMs from images that are about to be deleted.
Closed notebooks are kept. Then it terminates all MicroVMs, deletes the image tiers,
removes the demo `microvm/` secrets, and sweeps the S3 ML artifacts (`models/`,
`sagemaker/`, `unload/`).

**Left intact** (so re-installs stay fast and your data survives): the artifacts S3
bucket (`samples/`, `user-data/`, `knowledge-base/`), the IAM roles, the ECR serving
repo, the DynamoDB tables, the Athena database, and — when provisioned — the Aurora
cluster, the S3 Table bucket, and the Bedrock KB. Remove those by hand if you want a
truly clean account.
