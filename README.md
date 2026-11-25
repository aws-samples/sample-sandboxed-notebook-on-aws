# Sandboxed Notebook on AWS

**Sandboxed Notebook on AWS** is a Python and SQL notebook that runs on **AWS Lambda MicroVMs**. Each session gets its own Firecracker VM with persistent state, VM-level isolation, and automatic suspend and resume.

> Proof-of-concept demonstrating Lambda MicroVMs as stateful code execution sandboxes. Extensible to other runtimes (R, Node.js, Julia) by swapping the executor and image.

## Disclaimer

This is sample code for demonstration and informational purposes only and is not intended for production use. It is provided “as is,” without warranties of any kind, as set out in the MIT-0 license. This sample has not been hardened for production security, and AWS makes no guarantee that it will isolate or contain any code you run. You should ensure that it meets your organizational security, regulatory, and compliance requirements before deployment. You are responsible for all AWS charges incurred by deploying and using this sample.

This sample includes an AI assistant, enabled by default, that uses foundation models available through Amazon Bedrock, including third-party models. AI-generated code, queries, and text may be inaccurate, incomplete, insecure, or unsuitable for your use case. The assistant will ask for your approval before running code. You are responsible for reviewing any proposed code and for validating all AI-generated output before relying on it. AWS makes no guarantee as to the performance, accuracy, or results of the AI assistant or any output it generates. Your use of Amazon Bedrock and any model is subject to the AWS Customer Agreement, the AWS Service Terms, the AWS Responsible AI Policy, and any applicable third-party model provider terms, and will incur charges in your AWS account.

> **Runs on macOS** (Apple Silicon or Intel). The one-click installer targets macOS tooling. See **[SETUP.md](SETUP.md)** for install, prerequisites, and configuration.

## Contents

- [Disclaimer](#disclaimer)
- [Demo Videos](#demo-videos)
- [Why Lambda MicroVMs for Notebooks?](#why-lambda-microvms-for-notebooks)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
  - [Session-Based Routing](#session-based-routing)
  - [Execution Model](#execution-model)
  - [Persistence Modes](#persistence-modes)
  - [Lifecycle Hooks](#lifecycle-hooks)
  - [Warm Pool Manager (optional)](#warm-pool-manager-optional)
- [Features](#features)
  - [Notebook](#notebook), [Multiple Outputs per Cell](#multiple-outputs-per-cell), [SQL Engine](#sql-engine-duckdb--athena--aurora--dynamodb)
  - [AI Assistant](#ai-assistant-strands-agents--bedrock-agentcore-runtime), [Workbook Intelligence](#workbook-intelligence-ai-powered-data-analysis)
  - [Snippets Library](#snippets-library), [Sample Notebooks](#sample-notebooks)
  - [Sidebar](#sidebar), [Outline and Cell Map](#outline-and-cell-map), [Run History](#run-history)
  - [Secrets and Environment Variables](#secrets-and-environment-variables), [Interactive Terminal](#interactive-terminal), [Logs Panel](#logs-panel), [MicroVM Management](#microvm-management), [pip and CPU Architecture](#pip-and-cpu-architecture)
- [Data Sources and Configuration](#data-sources-and-configuration)
  - [Data Source Connectivity](#data-source-connectivity), [Configuration](#configuration)
- [Security](#security)
- [Testing](#testing)
- [Project Structure](#project-structure)
- [Cost](#cost)
- [References](#references)
- [License](#license)

Other documents: [SETUP.md](SETUP.md) (install, configuration, troubleshooting, teardown), [SECURITY.md](SECURITY.md) (security model), [NOTEBOOK_STATE.md](NOTEBOOK_STATE.md) (checkpoint and restore design), [NETWORK_EGRESS.md](NETWORK_EGRESS.md) (Layer-7 egress control), [CONTRIBUTING.md](CONTRIBUTING.md).

## Demo Videos

<table>
<tr>
<td width="33%">
<a href="https://share.descript.com/view/8BAokQd9eTy" target="_blank" rel="noopener">
<img src="https://img.shields.io/badge/▶_Video_1-Building_a_Data_Science_MicroVM_Image-blue?style=for-the-badge&labelColor=1a1a2e" width="100%"/>
</a>
<br/><sub>How to create a Docker image for a data science environment and deploy it as Lambda MicroVM images</sub>
</td>
<td width="33%">
<a href="https://share.descript.com/view/AAPf8FAOKcl" target="_blank" rel="noopener">
<img src="https://img.shields.io/badge/▶_Video_2-Running_Notebooks_on_Lambda_MicroVMs-blue?style=for-the-badge&labelColor=1a1a2e" width="100%"/>
</a>
<br/><sub>Running Notebooks on Lambda MicroVMs</sub>
</td>
<td width="33%">
<a href="https://share.descript.com/view/7WSjbHaxXKb" target="_blank" rel="noopener">
<img src="https://img.shields.io/badge/▶_Video_3-AI_Assistant_with_Full_VM_Context-blue?style=for-the-badge&labelColor=1a1a2e" width="100%"/>
</a>
<br/><sub>AI Assistant with Full MicroVM Context</sub>
</td>
</tr>
</table>

## Why Lambda MicroVMs for Notebooks?

Traditional notebook platforms run kernels as containers on shared Kubernetes nodes. Lambda MicroVMs offer a fundamentally better primitive for this workload:

| | EKS Pods (containers) | Lambda MicroVMs |
|---|---|---|
| **Isolation** | Shared host kernel (namespaces + cgroups) | Dedicated Firecracker guest kernel per session, hardware VM boundary |
| **Idle cost** | 5-min eviction tail + 24/7 EBS volumes | Suspends to ~$0 instantly, snapshot only |
| **Resume** | Kill pod → recreate → reattach EBS (~10-30s) | Snapshot restore with memory + disk intact (~1-2s) |
| **Infra overhead** | Cluster autoscaler, node pools, PDBs, etcd tuning | None, fully managed, no cluster to operate |
| **Blast radius** | Runaway kernel affects co-located pods on same node | Confined to its own VM, terminated cleanly |

**Key benefits for notebook use cases:**

- **VM-level tenant isolation**: Customer-supplied Python runs in its own Firecracker VM with a dedicated guest kernel. Container escapes, fork bombs, and malicious dependencies cannot cross the boundary. Materially stronger for SOC 2 and enterprise security reviews.

- **Instant suspend, zero idle cost**: No eviction timers, no EBS volumes running 24/7. The VM freezes the moment the user stops typing and resumes in 1-2 seconds when they return. You pay only for active compute.

- **Eliminates control-plane churn**: Notebook kernels stop being Kubernetes pods, so they generate zero scheduling, eviction, and etcd write load. The control-plane stability problem disappears for this workload.

- **Simpler operations**: No cluster autoscaler, node drain logic, or EBS reattach choreography. Lifecycle is a single API: `run → suspend → resume → terminate`.

**Cost:** directionally **~43% cheaper per user** than evict-on-idle EKS + EBS
(~$3.87 vs ~$6.79 per user/month for a 2 vCPU / 4 GB kernel), with no 24/7 EBS volume
and no flat per-cluster control-plane fee. Full rate card, the per-op snapshot
read/write breakdown, and the scale comparison are in **[Cost](#cost)**.

## Quick Start

On **macOS**, clone the repository (it clones into a `sample-sandboxed-notebook-on-aws`
directory) and run the installer from the repo root:

```bash
git clone https://github.com/aws-samples/sample-sandboxed-notebook-on-aws.git
cd sample-sandboxed-notebook-on-aws
./local_install.sh        # preflight checks → .venv → build images → start proxy + AI agent + UI
./local_stop_app.sh       # close the app: stop all services + terminate its MicroVMs
```

Opens at http://localhost:5173. Each notebook tab auto-launches a MicroVM; closing a
tab suspends its VM, deleting a notebook terminates it. The first install builds
**8 MicroVM images** (4 memory tiers for each of arm64 and x86_64) and offers both
arches; `IMAGE_ARCHES=arm64 ./local_install.sh` builds and offers arm64 only. x86_64 is
skipped automatically in `ap-east-1`, `ap-southeast-5`, `ap-southeast-7` and
`eu-south-2`, where it isn't available. The run is fully local on SQLite by default.
Aurora (`--AuroraSetup`) and the warm pool are the two opt-ins
(`--WarmPool`, see [Warm Pool Manager](#warm-pool-manager-optional)).

The installer is self-checking. It preflights your tools (AWS CLI, Python 3.13 or newer,
Node, `jq`/`curl`/`unzip`/`git`), creates an isolated `.venv`, and provisions or reuses
all AWS resources before starting the services. Ctrl+C in the installer's terminal stops
only the local services and leaves the VMs running.

**To close the app, use `./local_stop_app.sh`.** It deletes the running notebooks
(as the UI's *Delete* does, checkpointing each VM first), stops every local service
including the sync worker, terminates the remaining MicroVMs (warm-pool spares
included) and clears the app's saved tabs in your browser. Options
(`--dry-run`, `--yes`, `--keep-notebooks`, ...) are in
[SETUP.md → Stopping the app](SETUP.md#stopping-the-app).

**Local-only by design.** This is a local developer POC, not a hardened multi-user
service. The host-side services bind `127.0.0.1` and every call into the proxy carries
a per-run shared secret. See [Security](#security) and [SECURITY.md](SECURITY.md).

Full setup, prerequisites, Aurora and Knowledge Base options, configuration,
troubleshooting and teardown are in **[SETUP.md](SETUP.md)**.

---

## Architecture

### Session-Based Routing

All callers interact with the proxy using only an `X-Session-Id` header. The proxy hides all VM internals, endpoints, auth tokens, VM IDs, recovery state. The AI chat agent runs as its **own local process** (`agent/ai_assistant/agent_runtime.py`, a BedrockAgentCoreApp on `:8082`); the proxy forwards `/ai/chat` to it:

```
┌────────────────────┐         ┌─────────────────────────────────────┐
│  Browser / Client  │  HTTP   │         Smart Proxy (:8081)         │
│                    ├────────►│                                     │
│  X-Session-Id: uuid│         │  • Session registry (sid → VM)      │
│                    │         │  • Auth token injection (JWE)       │
│                    │  WS     │  • Reactive recovery (eternal mode) │
│  Terminal (xterm)  ├────────►│  • Terminal relay (WS → SHELL_INGRESS)│
│                    │         │  • Checkpoint orchestration         │
│                    │         │  • Notebook CRUD, metrics, sessions │
└────────────────────┘         └───────┬──────────────────┬──────────┘
                                       │                  │ HTTP forward
                                       │                  │ (POST /invocations)
                                       │                  ▼
                                       │   ┌─────────────────────────────────────┐
                                       │   │  AI Chat Agent (:8082)              │
                                       │   │  agent/ai_assistant/agent_runtime.py│
                                       │   │  BedrockAgentCoreApp                │
                                       │   │  • Strands agent + Bedrock          │
                                       │   │  • tools call back to proxy ────────┼──┐
                                       │   │    (execute_code, variables…)       │  │
                                       │   └─────────────────────────────────────┘  │
                                       │        ▲ tools (HTTP, X-Session-Id)        │
                                       │        └───────────────────────────────────┘
                                       │ HTTPS + auth (HTTP_INGRESS)
                                       │ WSS + subprotocols (SHELL_INGRESS)
                                       ▼
                        ┌────────────────────────-──┐
                        │  Lambda MicroVM           │
                        │  (Firecracker, ARM64/x86) │
                        │  FastAPI + SandboxExecutor│
                        │  Platform Shell (bash PTY)│
                        └─────────────────────-─────┘
```

**Why this design:**
- Transparent recovery, same session ID, different VM underneath (eternal mode relinks after a VM dies)
- No credential leakage, caller never handles auth tokens
- Mode-agnostic, same API works in eternal and checkpoint mode
- Simplified clients, just track session IDs, nothing else
- One error shape: every error response from the proxy is JSON with an `error` field (`json_error` in `proxy/platform/errors.py`), and a terminate request that names a session other than the one that owns the VM is refused with `409`, as is one whose pre-terminate save failed (`"saveFailed": true`, the VM keeps running)
- **Agent as a separate service**: the chat agent is the SAME artifact deployable to AWS Bedrock AgentCore Runtime. Running it as its own local process (`:8082`) mirrors that boundary: the proxy forwards `/ai/chat` over HTTP, and the agent's tools call back into the proxy with `X-Session-Id` (so agent-run code goes through the exact same VM path as the user's). Iterating on or restarting the agent never touches the proxy or the VMs.

### Execution Model

**Run guard.** Every cell, Python or SQL, runs under a run guard
(`app/notebook/run_guard.py`: a shared `_RunGuard` base with a Python and a SQL
variant). The guard enforces one time budget, `EXECUTION_TIMEOUT_SECONDS` (default 60,
set in `scripts/config.sh`), and it is what the **Stop** button reaches. A cell that
runs too long ends with "Execution timed out after Ns"; a stopped cell ends with
"Execution interrupted by user". A guard fires at most once per cell and cannot leak a
pending interrupt into the next cell.

- **Python cells:** the guard injects `KeyboardInterrupt` into the cell's thread
  (`PyThreadState_SetAsyncExc`), which reliably stops runaway pure-Python loops.
  Cells blocked in a C-level call (`time.sleep`, socket receive, C extensions) cannot
  be killed in-process. That would need per-cell process isolation, so the sandbox
  runs one cell at a time and treats this as a documented limitation.
- **SQL cells:** the guard calls a native cancel when the engine registered one
  (DuckDB `interrupt()`, Amazon Athena `StopQueryExecution`), so the engine raises on
  its own. Otherwise (Aurora Data API, Amazon DynamoDB, result shaping) it injects
  `KeyboardInterrupt` like a Python cell.
- **Athena cancel:** `read_athena()`, `unload_athena_to_s3()` and SQL cells share one
  Athena runner (`app/notebook/helpers/_athena.py`). On timeout or Stop it cancels the
  running query. This needs `athena:StopQueryExecution` on the VM execution role
  (`iam/exec-role-permissions.json`).
- **Last expression:** if the last statement of a cell is a bare expression, it is
  evaluated once (its value is shown), not twice.

**Burst model.** VMs get 4x baseline resources pre-allocated from boot. Usage above
baseline incurs burst billing at the same vCPU and memory rates. Exceeding 4x is an
out-of-memory crash.

### Persistence Modes

Lambda MicroVMs have an 8-hour max lifetime (AWS limit), and a VM can also be
reclaimed when it hits its idle or suspend timeout. State is saved to S3 so a new VM
can pick it up. Set the mode with `SESSION_PERSISTENCE_MODE` (see
[SETUP.md → Configuration](SETUP.md#configuration)).

| | Checkpoint (default) | Eternal |
|---|---|---|
| How a new VM is provisioned | On the user's next launch or open | **Reactively**, on the next request after the VM is found dead |
| What happens when the VM dies | State stays in S3; the VM is gone until the user returns | Proxy launches a fresh VM, restores from S3, relinks the session and serves the request (one-time cold start, about 9-12 s) |
| User experience | Click "Restore" (or launch a new VM) next time | Seamless: same session ID, no manual step |
| Scheduler or timers | None | None: no rotation timer or polling loop |
| Best for | Intermittent, cost-sensitive use | Always-on notebooks |

A third value, `none`, keeps nothing (ephemeral VMs).

**Connecting a notebook.** A notebook connects only by restoring its state onto a
**new** VM or by launching a **new** VM. There is no attach to an existing VM.
**Restore options.** By default a restore lands on the saved CPU architecture and memory
tier, so nobody ends on the wrong architecture by accident. The Connect dialog has a
collapsed section, Restore on a different instance type, to choose another architecture or
tier (the request carries `restoreOverride: true` with `arch` and `memoryMiB`). The memory
check compares the saved checkpoint size with the chosen tier, whatever its size relative to
the saved tier (the default restore included): an estimated need of 2x the checkpoint gives a
normal warning from 50 percent of the tier and a strong one above 90 percent. The proxy runs
the same check at `/launch` on the real S3 checkpoint size and returns it as
`restore_memory_check`; it only warns and never blocks. Another architecture shows an explicit warning, and after the
restore a banner lists the packages that could not be re-installed (or confirms all were).
None of the warnings blocks a restore. Details in [NOTEBOOK_STATE.md](NOTEBOOK_STATE.md).

**What survives a restore:** variables, DataFrames, `/tmp/` files, pip packages,
variable provenance and the notebook DAG copy (`dag.json`), **and imports**: the
executor records every successful `import` and replays it on restore, so
`import pandas as pd` keeps working on the new VM. Functions, lambdas and classes
defined in cells are saved with a reference to the notebook namespace instead of a
copy of every variable, so they stay small and see the live variables after a
restore. Other variables refer to a cell-defined class or function by name instead of
carrying a copy, so after a restore `type(c) is C`, `isinstance` and subclass checks,
shared class state (`C.count`), Enum members (`c.color is Color.RED`) and aliases
(`D is C`) behave as before the save. The restore loads class variables before the
variables that use them. A class defined inside a function or deleted from the
namespace is still copied into each variable that uses it. Data shared between
variables (`b = a`, a dict held in another dict, a DataFrame bound under several names,
an object attribute holding a top-level list) is stored once and restores as one
object, and two functions that share a closure cell (`inc, get = make_counter()`) share
it again after a restore. A numpy view still restores as a copy. Eternal recovery also carries the
notebook's environment variables and secret references onto the replacement VM.

**What is excluded:** objects that cannot be pickled, such as matplotlib figures and
open handles (their module bindings, e.g. `plt`, are replayed from the import ledger),
and the injected helper names such as `display()`, which the VM re-creates on every
start.

How the save, the dirty flag, the delta format and the 16-way parallel restore work,
and how the database half of a notebook fits in, is in
**[NOTEBOOK_STATE.md](NOTEBOOK_STATE.md)** (see
[Persistence modes](NOTEBOOK_STATE.md#persistence-modes) and
[Eternal recovery](NOTEBOOK_STATE.md#eternal-recovery)).

### Lifecycle Hooks

| Hook | When | Purpose |
|------|------|---------|
| `/run` | VM starts | Initialize the session; optionally restore from S3 (namespace, files, packages, `dag.json`, imports) |
| `/suspend` | Going idle | Save state to S3 if dirty (**both modes**). This is the primary save point: a suspended VM reclaimed by AWS never fires `/terminate`. The save has a 45 s budget (hook timeout 60 s) and the hook never fails: on a timeout or error it keeps the previous checkpoint, stays dirty and suspends with memory intact |
| `/resume` | Traffic arrives | Validate state |
| `/terminate` | Shutting down (VM RUNNING at kill time) | Save state to S3 if dirty (**both modes**) |

Saves are gated by a dirty flag (a fingerprint of the provenance clock, installed
packages and `/tmp` file signatures), so unchanged state is never re-serialized.
Concurrent requests during an Eternal recovery are queued and replayed in FIFO order,
and per-phase timings are available at `GET /recovery-timings`.

### Warm Pool Manager (optional)

Launching or restoring a notebook normally pays a **cold-provision floor**, AWS
has to boot a brand-new MicroVM before anything else can happen (~8–12s). The
**VM Pool Manager** removes that floor by keeping a small set of **pre-booted,
suspended spare VMs** ready to hand out. On a launch, the proxy asks the pool for a
spare, which is **resumed and rebound** to the real session in ~1–2s instead of
cold-launched. It's an **optional, opt-in service**, the whole platform runs
without it, and when it's off launch/restore behavior is exactly as before.

```
┌──────────────┐   /grant (arch,size)   ┌───────────────────────┐   run / suspend
│  Smart Proxy │ ─────────────────────► │   VM Pool Manager     │ ───────────────►  AWS Lambda
│  (:8081)     │ ◄───────────────────── │   (:8090, optional)   │   MicroVMs API
└──────────────┘   {vm_id, endpoint,    │                       │
                     launched_at}       │  pre-booted, SUSPENDED│
     grant-or-fall-through:             │  spares per tier:     │
     a miss/empty/timeout →             │   1GB×1 2GB×2 4GB×2 8GB×1
     normal cold launch                 └───────────────────────┘
```

**Why it exists:** make new-notebook launches and session restores feel instant by
taking the VM-provisioning time off the critical path. A spare is already booted and
healthy, so a grant is just *resume + rebind* (and, for a restore, pull state from
S3 onto the already-warm VM).

**How it works (design highlights):**
- **Grant-then-fallback.** The proxy tries the pool first; on any miss, empty-pool
  (409), timeout, or error it falls straight through to a normal cold launch, the
  pool is a *fast path*, never a dependency. With `POOL_ENABLED=false` (default) the
  pool manager isn't even started.
- **Self-refilling + bounded.** A background loop keeps each memory tier at its
  configured target, trims over-target spares (oldest first), and recycles spares
  older than `POOL_MAX_AGE_HOURS` (clamped to 2h, so a granted spare still has most
  of the AWS 8h lifetime left).
- **Pool ledger in the same store as the proxy** (`pool_vms` table; SQLite locally,
  Aurora/Postgres for a hosted deploy). An atomic claim (`FOR UPDATE SKIP LOCKED` on
  Postgres) makes concurrent grants race-safe, and the mutating maintenance sweeps
  are advisory-locked so **multiple pool-manager instances can safely share one
  ledger** (for a future multi-task deployment).
- **Honest lifetime + cost.** A granted spare carries its true **mint time** (not the
  adopt time), so the panel's "Terminates in" countdown and the suspended-snapshot
  cost reflect the real AWS clock, a spare that sat warm for an hour shows ~7h left,
  not ~8h.

Enable it with the installer flag:

```bash
./local_install.sh --WarmPool          # start the pool manager + grant-from-pool
```

Tuning lives in `scripts/config.sh` (`POOL_SIZE_PER_IMAGE`, `POOL_SIZE_OVERRIDES_ARM64`,
`POOL_SIZE_OVERRIDES_X86_64` (default: 1 x86_64 spare at 2 GB, the default size, and 0
for the other x86 sizes), `POOL_MAX_AGE_HOURS`, `POOL_FILL_CONCURRENCY`, `POOL_MINT_HEALTH_TIMEOUT_SECONDS`,
`POOL_MANAGER_PORT`). On every `--WarmPool` run the installer first **terminates any
old pooled VMs and wipes `pool_vms`** (terminate-then-wipe) so each start begins from
a clean, known pool.

> **Trade-off: the pool increases `ListMicrovms` churn.** The pool continuously
> mints and recycles VMs (a mostly-idle 6-spare pool with a 2h recycle ceiling turns
> over ~70 VMs/day even with no users). AWS's `lambda-microvms` **`ListMicrovms`**
> control-plane call returns **every VM including recently-TERMINATED ones** (it has
> **no state filter**, and terminated records are retained, undocumented, observed
> on the order of hours), so heavy pool churn makes that list grow large. Combined
> with `ListMicrovms`'s **account-level rate limit**, a large list + frequent polling
> can trigger **ThrottlingException** (and, under a request pileup, SigV4
> `InvalidSignatureException` from signatures expiring while queued). This project
> mitigates it, the proxy pages `ListMicrovms` at the API-max `maxResults=50` (far
> fewer round-trips), the frontend polls `/instances` at 15s and only when a notebook
> is open, and the pool manager's own reconcile runs on a 60s loop, but the
> fundamental tension remains: **more pool churn ⇒ a larger VM list ⇒ more
> `ListMicrovms` pressure.** Lower the pool size (`POOL_MAX_AGE_HOURS` is already at its 2 h ceiling) to
> reduce churn if you hit throttling. The warm pool is a latency optimization, not a
> free one, leave it **off** unless fast launch/restore matters for your use case.

---

## Features

### Notebook
- **Three cell types:** Python (blue), SQL (orange) and Markdown (purple in every theme)
- **Sequential execution:** `Shift+Enter` runs cells in order, no race conditions
- **Run All:** runs every cell in order. While it runs, the execution-time tint of the
  Outline map is off and the previous ticks and times are cleared; when it finishes the
  tint comes back with fresh values. Run and Run All wait until the data source list has
  loaded (SQL cells need it to route), except for a suspended VM, which a run resumes
- **Timeout and Stop:** see [Execution Model](#execution-model)
- **Rich output:** DataFrames as styled HTML tables, matplotlib inline, syntax highlighting
- **File upload:** CSV, Excel, Parquet, JSON are auto-loaded as pandas DataFrames
- **Multi-tab:** each tab is a separate notebook with its own MicroVM. Connecting a tab
  restores its state onto a new VM or launches a new VM
- **Save and open:** `.notebook.json` preserves code, output, charts, AI explanations
- **How was this computed?** Every App-tab output and notebook cell output has a lineage drawer: data sources (catalog vs ad-hoc), the `@param` inputs and upstream steps that produced it, a trust badge (green, amber or red with reasons) and an on-demand AI summary. The Notebook tab can highlight the upstream cells. Only reads made through the notebook reader helpers and SQL cells are recorded (identifiers only), so a direct `boto3` or `pd.read_csv('s3://...')` read does not appear as a source. The AI summary sends each step's cell code, the output's shape and the source identifiers to Amazon Bedrock (like the other AI features); nothing from it is persisted.

### Multiple Outputs per Cell

A cell can show several tables, Plotly charts and matplotlib/seaborn figures, with printed text between them.

- **VM** (`/execute`): returns `outputs: [...]` next to the legacy `output`, `html` (first table/chart) and `image` (last figure). Items: `{"t":"text","text"}`, `{"t":"table","payload":{__df_table__…},"label"}`, `{"t":"plotly","html","label"}`, `{"t":"image","src":"data:image/png;base64,…","key"}`. `display()`, `plt.show()` and Plotly `fig.show()` are recorded with their stdout position; open matplotlib figures are added in figure-number order. Everything is rendered after the cell ran (the state at the end of the run, so `execution_time_ms` is unchanged). At most 20 rich items and about 5 MB per cell, then a "… N more outputs not shown" text item. Outputs shown before an error are kept. SQL cells stay single-table.
- **UI**: the first table/chart/image is open, later ones start collapsed behind a one-line button (`▸ Table 2 · upstream · 12 rows × 2 cols`) and are not mounted until opened. Copy and Export act on the whole cell output (all items, static, with captions).

### SQL Engine (DuckDB + Athena + Aurora + DynamoDB)
Native SQL cells with intelligent auto-routing, write standard SQL, engine chosen transparently:

| Source | Syntax | Engine |
|--------|--------|--------|
| DataFrame in memory | `SELECT * FROM df_name` | DuckDB |
| Local file | `SELECT * FROM '/tmp/file.csv'` | DuckDB |
| Amazon S3 file | `SELECT * FROM read_csv('s3://...')` | DuckDB + httpfs |
| Amazon S3 Parquet prefix | `SELECT * FROM read_parquet('s3://.../prefix/*')` | DuckDB + httpfs |
| Amazon DynamoDB table | `SELECT * FROM dynamodb."table"` | PartiQL (or scan → DuckDB) |
| Amazon Athena table | `SELECT * FROM db.table` | Athena |
| Amazon S3 Tables (Iceberg) | `SELECT * FROM ns.table` (+ time-travel) | Athena (federated `s3tablescatalog`) |
| Amazon Aurora PostgreSQL | via `read_aurora("...")` | RDS Data API |
| Mixed JOIN | Any combination above | Materialize remote → DuckDB |

A SQL cell on Athena (including S3 Tables) returns the same typed frame as `read_athena()`: a SQL `NULL` is `None` and column types follow Athena's declared types (`bigint` → integer, `double` → float, `boolean`, `date`/`timestamp` → datetime). Test for NULL with `isna()`, not `== ""`. SQL cells share the Python-cell run guard: they stop at `EXECUTION_TIMEOUT_SECONDS` and on Stop, and a running Athena query is cancelled (also for `read_athena()` and `unload_athena_to_s3()`). See [Execution Model](#execution-model).

### AI Assistant (Strands Agents + Bedrock AgentCore Runtime)
- **Chat panel**: conversational agent with notebook context awareness
- **Explain**: one-click plain-English explanation of any cell
- **Fix**: AI-suggested fixes for error cells with one-click apply
- **NLP-to-Code**: type natural language, get Python
- **Auto-Annotate**: document all cells with AI in one click
- **Built for Amazon Bedrock AgentCore Runtime**: the chat agent is a
  `BedrockAgentCoreApp` (`agent/ai_assistant/agent_runtime.py`) that implements the
  AgentCore Runtime contract (`/invocations` + `/ping`). Locally it runs as its own
  process on `:8082`; the same artifact deploys unchanged to AgentCore Runtime
  (`agent/agentcore/`, via the `@aws/agentcore` CLI), and the proxy only needs
  `AGENT_URL` repointed. It runs NOT inside the MicroVM (no image bloat) and NOT in
  the proxy (restart/iterate independently); the proxy forwards `/ai/chat` to it.
- Auto-detects Bedrock credentials; hides AI buttons if not configured
- Built on **Amazon Bedrock** via the **Converse API**, the single, model-agnostic
  surface that makes model switching possible (see below)

#### Switching models

The chat panel has a **model picker**: pick a different foundation model for the next
turn and the conversation continues on it, same thread, same notebook context. Nothing
is hardcoded, the dropdown is driven end-to-end by a generated **model-capability
catalog**:

- **Where the list comes from.** A top-N shortlist of the strongest Converse-capable
  models is derived from the catalog (`GET /ai/models` → `modelcaps.top_models_for_agent`),
  ranked and deduped per provider, region-scoped to what's actually invocable. The
  catalog (`modelcaps/model_caps.json`) is a *generated* artifact, regenerated by
  `modelcaps/refresh.py`, never hand-edited, so the picker never drifts from reality.
- **How the switch works, the Converse API.** Amazon Bedrock's **Converse API** gives
  every model one uniform request/response shape (messages, `inferenceConfig`,
  tool use, streaming), so changing models is just changing the `modelId` on the next
  `converse`/`converse_stream` call, no per-provider request reshaping. The selected
  `model_id` rides along with the chat request and the agent **switches at a turn
  boundary** (mid-conversation, keeping history).
- **Capability-aware, not one-size-fits-all.** Per-model quirks are read from the
  catalog and applied automatically so a switch never sends an invalid request:
  `inferenceConfig` clamps `maxTokens` to the model's output cap, `temperature` is
  sent only to models that accept it (newer models reject it), and a model's
  "minimal-thinking" override goes into `additionalModelRequestFields` when supported.
  Attachments are gated too, the proxy blocks an image on a non-vision model or a
  PDF/Office doc on a model without document support, with a clear message to switch.
- **Defaults + overrides.** The default model is `BEDROCK_MODEL_ID` (default `us.anthropic.claude-haiku-5-5` in `scripts/config.sh`);
  the picker overrides it per conversation. Workbook Intelligence runs its own model
  via `INTEL_MODEL_ID`, independent of the chat picker.

#### Resilience: retries and automatic model fallback

Two layers keep a chat turn from failing on a bad moment or a bad model choice
(`agent/ai_assistant/notebook_agent.py`):

- **Transient errors → boto3 retries, same model.** The Bedrock client is built with
  boto3's `standard` retry mode (`CHAT_BEDROCK_MAX_RETRIES` total attempts, default 2) plus explicit
  connect/read timeouts. Throttling, 5xx, and timeouts are retried on the **same**
  model, because switching models on a throttle would be the wrong fix.
- **Hard model-unavailability → fall back to the next model by score.** If the selected
  model fails deterministically (access not granted to the account, not available in
  the region, or an invalid model id), the agent doesn't retry it. It walks a
  **fallback chain** (`modelcaps.fallback_chain`) down the same score ranking the picker
  uses, ending at the configured default. On each hop it rebuilds the agent on the next
  model with the same session manager, so **conversation history is preserved**, and it
  shows a visible "switched to …" note in the chat. Operator-excluded models
  (`BEDROCK_EXCLUDE_MODELS`) never re-enter through the chain. If the whole chain is
  exhausted, the error is surfaced.
- **Rejected parameter → retry once without it.** If a model rejects `temperature`, the
  turn is retried once on the same model without it.

The error classifier is deliberately narrow: only deterministic model errors trigger a
switch, and transient ones stay with boto3's retry.

#### Confirm-before-execute (default)

The standalone AI assistant defaults to **confirm-before-execute**. When the agent
wants to run code (`execute_code`) or add a dependency (`install_package`), the chat
panel shows the proposed code or package name with inline **Approve** / **Reject**
buttons; nothing touches the connected MicroVM until you approve. This is the
fail-safe default (`AI_AGENT_AUTO_EXECUTE=false`).

Why it matters: the assistant reads from your connected data sources (S3 objects,
query results, logs, files), and that content is **untrusted input to the model**. A
crafted record or file can carry text that tries to steer the agent into running code
you did not ask for (prompt injection). The confirmation step puts a human in the loop
before any such tool call executes, and the **MicroVM remains the containment
boundary**, anything approved still runs inside the isolated VM, never on the proxy or
your host.

To opt into immediate auto-run (today's behavior, no prompt), set the opt-in before
starting the stack:

```bash
export AI_AGENT_AUTO_EXECUTE=true
./local_install.sh
```

This flag gates only the interactive chat assistant; the in-proxy Fix loop is
unaffected.

### Workbook Intelligence (AI-Powered Data Analysis)
Automatic data profiling and intelligence generation when data sources are connected:

- **Two-phase generation**: structured tab cards appear fast (~25-30s), full prose report generates in background (~35-45s)
- **4 insight tabs**: Suggested Analyses, Visualizations, Investigations, Alerts, each card is clickable (sends prompt to AI chat)
- **Entity discovery**: automatic schema profiling for all connected S3, DynamoDB, Athena, and local files
- **Cross-source verification**: agent validates referential integrity (FK overlaps) and flags issues
- **Incremental updates**: uploading a new file triggers a fast delta (only new findings, ~16s)
- **Deletion pruning**: removing a file auto-prunes related insights (~6s)
- **Full report modal**: comprehensive markdown report with relationships, join paths, and data quality analysis
- **Data quality alerts**: PII detection, null rates, type mismatches, cardinality anomalies
- Model: Claude Haiku 4.5 (optimized for speed; the `INTEL_MODEL_ID` env var overrides it)
- Prompt caching enabled for multi-turn agent loop (reduces latency on subsequent tool calls)

### Snippets Library
~100 pre-loaded helper functions available in every cell, no imports needed. The
panel is driven by the SAME introspection the `/helpers` endpoint and editor
autocomplete use, so it never drifts from the real helpers:
- **Data Loading**: `read_s3_*`, `read_dynamodb` / `read_dynamodb_query`, `read_athena`, `read_aurora`, `read_url`, `read_local` / `read_local_prefix`, `read_bedrock_kb` / `ask_bedrock_kb`, `read_logs` / `grep_logs` / `tail_logs`, `sample_data`, `query_df`
- **Data Export**: `to_s3_csv` / `to_s3_parquet` / `to_s3_json` / `to_s3_excel`, `to_local`, `to_local_prefix`, `upload_s3`, `save_model`
- **Visualization**: `plot_line`, `plot_bar`, `plot_scatter`, `plot_histogram`, `plot_heatmap`, `plot_box`, `plot_pie`, `plot_area`, `plot_corr`
- **Generative AI**: `invoke_llm`, `llm_classify` / `llm_extract` / `llm_sentiment` / `llm_summarize` / `llm_translate` / `llm_map`, `embed_texts`, `semantic_search`, `count_tokens`, `list_bedrock_models`
- **Machine Learning**: in-VM sklearn (`train_model`, `model_info`, `predict_batch`, `save_model` / `load_model`) and remote SageMaker (`sm_train`, `sm_deploy`, `sm_predict`, …)
- **Utilities**: `profile`, `whoami`, `compare_df`, `list_s3`, `head_s3`, `@timer`, `get_env` / `get_secret`, `list_env` / `list_secret`, `estimate_session_cost` / `session_cost`, `this_notebook`

Each entry carries a curated example (hand-authored in `curated_snippets.json`);
click it to insert the example into the current cell. The panel also hosts
multi-step **Recipes** and your own saved snippets (localStorage).

> **Regenerating the catalog:** adding or renaming a helper surfaces it everywhere
> automatically, but the AI prompt's reference block and the cold-start fallback
> list are generated artifacts, re-run `python -m scripts.gen_helpers_block` (CI
> guards it with `--check`).

### Sample Notebooks
The **Samples** panel lists the bundled notebooks (`web/public/samples/`, indexed by
`index.json`) and loads one into a new tab. Each entry shows a short description and
**showcased-feature pills**, for example the AWS services or platform features the
sample demonstrates (`showcasedFeatures` in `index.json`, drawn from the vocabulary in
`web/src/services/sampleFeatures.js`). A search box, like the one in Snippets, filters
by sample name or pill label and highlights the match; Esc clears it. Samples that need
Aurora are only offered when Aurora is set up.

### Sidebar
The activity bar on the left (resizable, collapsible, grouped with dividers):
- **Group 1:** Notebooks
- **Group 2:** Outline, Data Sources, Variables, Packages, Secrets & Env
- **Group 3:** Terminal, Logs (both open as bottom panels)
- **Group 4:** Snippets, Sample Notebooks
- **Bottom of the bar:** MicroVMs, About

Workbook Intelligence (Intel) lives in the AI chat panel, not in the activity bar.

### Outline and Cell Map
The Outline panel lists the notebook's cells and shows a **cell map** on its right edge.
The map is always shown for a non-empty notebook, however few cells it has.
- **Proportional ticks:** each cell owns a segment of the strip proportional to the
  height of its content (with a small minimum so tiny cells stay visible). Ticks are
  coloured by cell type; code and SQL cells that have not run are dimmed.
- **Section bands:** a Markdown heading is drawn as a wider purple band, with the cells
  of its section as narrower ticks under it.
- **Status lane:** a narrow lane on the right edge marks exceptions only: running,
  error or stale. Successful and unrun cells leave it empty.
- **Viewport window:** an outlined rectangle shows which part of the notebook is on
  screen. Scrolling the notebook moves it; dragging it scrolls the notebook.
- **Click and hover:** click a tick to jump to that cell, click an empty part of the
  strip to centre the notebook there. Hovering a tick shows its label and status.
  Ticks are buttons, so the keyboard works too.
- **Execution-time tint:** the heat-map tint on the outline rows is separate from the
  map and can be toggled; Run All resets it as described under [Notebook](#notebook).

### Run History
Each notebook in the Notebooks panel can be expanded to a **Run History** card, a raised
card under the notebook row. It shows a duration sparkline and a reverse-chronological
list of recent runs (the latest 15), with a per-run drill-down: the cells of the run,
their parameters (collapsed behind a count until opened) and a per-cell metric view. The
history is fetched when the notebook row is expanded and refreshes when a new run of
that notebook is recorded.

### Secrets and Environment Variables
Inject secrets and config into MicroVMs at launch time, from the **Secrets & Env** panel:
- **AWS Secrets Manager**: Browse available secrets from your account, select which to inject. Values are fetched inside the VM by its execution role (proxy never sees them).
- **Direct env vars**: Key-value pairs injected via `runHookPayload`. Values masked in the UI and **sealed at rest** by the proxy (`env_crypto.py`, backed by a Secrets Manager master key); the proxy fails *closed* on env-var writes if the key is missing.
- **Read in code with helpers**: no `os.environ`/`json.loads` boilerplate:
  - `get_env("DB_PORT", 5432, as_type=int)` / `get_secret("DB_CREDS")`: smart typing (a JSON secret returns a dict; plaintext stays a string; opt into int/float/bool with `as_type`).
  - `list_env()` lists your **direct env vars**, `list_secret()` lists your **secrets**, a clean, non-overlapping split, keys only (values never printed). `list_env(all=True)` dumps everything including platform vars.
- **Security**: the proxy only lists secret *names* (never values); secrets resolve inside the VM. Same trust boundary as Lambda + Secrets Manager.
- **More:** see [SECURITY.md](SECURITY.md#environment-variables-encrypted-at-rest) for how env-var values are sealed. Eternal recovery re-injects them on the replacement VM.

### Interactive Terminal
- **Full shell access**: bash terminal inside the MicroVM via AWS SHELL_INGRESS connector
- **Platform-managed PTY**: no custom shell server needed, uses Lambda's built-in shell endpoint
- **Resizable bottom panel**: drag to resize, appears below the notebook
- **Idle auto-disconnect**: WebSocket closes after 60s of no input, allowing VM to suspend
- **Auto-reconnect on type**: typing in a disconnected terminal reconnects transparently (resumes VM if suspended)
- **Session-aware**: switches to the correct VM when you switch notebook tabs
- **Pre-installed tools**: `python3`, `pip`, `git`, `tar`, `gzip` available out of the box
- **Package access**: all packages from `requirements.txt` on PATH (pandas, numpy, boto3, etc.)

### Logs Panel
- **Real-time VM logs:** Amazon CloudWatch Logs stream from the MicroVM, live-updating
- **Execution traces:** stdout/stderr from code execution, agent tool calls
- **Intel tracing:** per-tool-call trace of the Workbook Intel agent (which tools called, inputs, results, timing)
- **Session-aware:** automatically follows the active notebook tab's VM

### MicroVM Management
- 4 memory tiers: 1 GB (0.5 vCPU) through 8 GB (4 vCPU), burst to 4x. The tier used when a launch names none is `DEFAULT_MEMORY_MIB` (default 2048)
- Configurable idle suspend (1 min to 2 hr), auto-resume on traffic (about 1 s)
- Configurable max duration (30 min to 8 hr), which controls the absolute VM lifetime
- Real-time cost tracking (running, suspended, burst, snapshot read and write)
- Connection status pill (running, suspended, terminated)
- Instance panel: specs, lifecycle, resources, cost breakdown per VM
- A terminate request must come from the session that owns the VM; any other session gets `409`
- Terminate & Save keeps the VM running when the save fails and shows "Save failed: <reason>. The VM was kept running so you can free memory or save again.", with a **Terminate without saving** choice. A variable too large to save in the VM's memory or disk is left out with that reason, everything else is saved, and the VM card lists it under "Not saved"; its previously saved copy, if any, is kept. Large variables are streamed to disk while they are saved, so a save needs about 1x the variable's size in free memory (see [NOTEBOOK_STATE.md](NOTEBOOK_STATE.md))

### pip and CPU Architecture
The images bake in the same pinned `requirements.txt` for both arches, so the default
stack is identical on arm64 and x86_64. Packages you install yourself (Packages panel,
a `!pip` or terminal install, or the AI's `install_package` tool) are resolved for the
VM's own arch: pip uses a prebuilt wheel when the package publishes one, else it tries
to build from source inside the VM, which can be slow or fail. A failed install says
why instead of returning raw pip output, and the Packages panel can suggest the other
arch. The message catalogue is in
[SETUP.md → Troubleshooting](SETUP.md#troubleshooting).

---

## Data Sources and Configuration

### Data Source Connectivity

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Lambda MicroVM (Firecracker)                                                   │
│                                                                                 │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │  Notebook Code (Python / SQL)                                             │  │
│  │                                                                           │  │
│  │  • pandas, numpy, polars, matplotlib, scipy                               │  │
│  │  • DuckDB (in-process SQL engine)                                         │  │
│  │  • boto3 (AWS SDK)                                                        │  │
│  └───┬───────────┬──────────────┬────────────────┬───────────────────────────┘  │
│      │           │              │                │                              │
│      ▼           │              │                │                              │
│  ┌────────┐      │              │                │                              │
│  │ /tmp/  │      │              │                │                              │
│  │ Local  │      │              │                │                              │
│  │ Files  │      │              │                │                              │
│  └────────┘      │              │                │                              │
│                  │              │                │                              │
└──────────────────┼──────────────┼────────────────┼──────────────────────────────┘
                   │              │                │
    ┌──────────────┼──────────────┼────────────────┼───────────────────────────┐
    │              ▼              ▼                ▼                           │
    │  ┌─────────────────────────────────────────────────────────────────────┐ │
    │  │              IAM Execution Role (auto-injected credentials)         │ │
    │  └──┬──────────┬───────────┬──────────────┬───────────────┬──────────-─┘ │
    │     │          │           │              │               │              │
    │     ▼          ▼           ▼              ▼               ▼              │
    │  ┌──────┐  ┌────────┐  ┌────────┐  ┌──────────┐  ┌───────────┐           │
    │  │  S3  │  │DynamoDB│  │ Athena │  │   Glue   │  │    STS    │           │
    │  │      │  │        │  │        │  │ (Catalog)│  │           │           │
    │  │Bucket│  │ Tables │  │Workgrp │  │  Tables  │  │  Assume   │           │
    │  └──────┘  └────────┘  └────────┘  └──────────┘  └───────────┘           │
    │                                                                          │
    │                 AWS Account (IAM-based access)                           │
    └──────────────────────────────────────────────────────────────────────────┘

                   │
    ┌──────────────┼───────────────────────────────────────────────────────────┐
    │              ▼                                                           │
    │  ┌─────────────────────────────────────────────────────────────────────┐ │
    │  │           VPC Egress Connector (ENI in customer subnets)            │ │
    │  └──┬──────────┬───────────┬──────────────┬──────────────────────────-─┘ │
    │     │          │           │              │                              │
    │     ▼          ▼           ▼              ▼                              │
    │  ┌──────┐  ┌────────┐  ┌────────-──┐  ┌───────────────┐                  │
    │  │ RDS  │  │Redshift│  │ElastiCache│  │  On-premises  │                  │
    │  │      │  │        │  │           │  │  (Direct      │                  │
    │  │Postgres││  DWH   │  │  Redis    │  │   Connect)    │                  │
    │  │MySQL │  │        │  │           │  │               │                  │
    │  └──────┘  └────────┘  └────────-──┘  └───────────────┘                  │
    │                                                                          │
    │                 Customer VPC (private subnet access)                     │
    └──────────────────────────────────────────────────────────────────────────┘

                   │
    ┌──────────────┼───────────────────────────────────────────────────────────┐
    │              ▼                                                           │
    │  ┌─────────────────────────────────────────────────────────────────────┐ │
    │  │              Internet Egress (default, no VPC needed)               │ │
    │  └──┬──────────┬───────────┬───────────────────────────────────-───────┘ │
    │     │          │           │                                             │
    │     ▼          ▼           ▼                                             │
    │  ┌──────┐  ┌────────┐  ┌────────────┐                                    │
    │  │Public│  │  pip   │  │ SaaS APIs  │                                    │
    │  │ APIs │  │install │  │ (partner & │                                    │
    │  │      │  │        │  │  3rd-party)│                                    │
    │  └──────┘  └────────┘  │            │                                    │
    │                        └────────────┘                                    │
    │                 Public Internet                                          │
    └──────────────────────────────────────────────────────────────────────────┘
```

| Access Pattern | Mechanism | Data Sources |
|----------------|-----------|--------------|
| Local | In-VM filesystem | `/tmp/*.csv`, `.parquet`, `.json`, `.xlsx` (+ uploaded model artifacts) |
| IAM Role | Auto-injected credentials | S3, DynamoDB, Athena, Glue (Catalog), STS, Aurora PostgreSQL (RDS Data API), S3 Tables / Iceberg (federated `s3tablescatalog`), Amazon Bedrock Knowledge Bases (S3 Vectors), Amazon SageMaker AI endpoints, Amazon CloudWatch Logs |
| VPC Connector | ENI in private subnets | RDS, Redshift, ElastiCache, OpenSearch, on-prem (DX) |
| Internet | Default egress | Public APIs, pip packages, SaaS and third-party APIs |

**Discovered + profiled automatically** by entity discovery (`batch/entity_discovery.py`)
and surfaced in the Data Sources panel: S3, DynamoDB, Athena, Aurora, Bedrock KB,
CloudWatch Logs, SageMaker endpoints, and local `/tmp` files. Each provider lives in
`proxy/platform/datasources/` (see [Project Structure](#project-structure)).

Sample data auto-provisioned: DynamoDB table, 4 S3 CSVs, Athena database with 4 tables,
S3 Tables (Iceberg) demo tables, and a Bedrock KB (S3 Vectors) over the product-spec PDFs.
The PDFs are not committed: the first install generates them from the Athena tables with the
built-in template (`generate_product_specs.py --no-llm`, no Bedrock calls). Run
`bash scripts/setup_bedrock_kb.sh --regen` to regenerate them with Bedrock-authored content
(see [SETUP.md](SETUP.md#s3-tables-apache-iceberg--the-bedrock-knowledge-base--always-installed)).

### Configuration

All settings live in `scripts/config.sh`, which `local_install.sh` sources. The
variable table (persistence mode, lifetime and idle timeouts, architectures and memory
tiers, `DEFAULT_MEMORY_MIB`, `EXECUTION_TIMEOUT_SECONDS`, forward timeouts, Amazon
Bedrock models, ports, `STORAGE_DB_PATH`, `ALLOW_NON_LOOPBACK_BIND`) with defaults is in
**[SETUP.md → Configuration](SETUP.md#configuration)**, and the warm pool settings are
in [SETUP.md → Warm Pool](SETUP.md#warm-pool-is-optional-opt-in). Frequently used:

| Setting | Default | Effect |
|---|---|---|
| `SESSION_PERSISTENCE_MODE` | `checkpoint` | `checkpoint`, `eternal` or `none` |
| `DEFAULT_MEMORY_MIB` | `2048` | Memory tier when a launch names none |
| `EXECUTION_TIMEOUT_SECONDS` | `60` | Per-cell time budget (Python and SQL) |
| `IMAGE_ARCHES` | `arm64 x86_64` | Architectures built and offered |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-haiku-5-5` | Default AI Assistant model |
| `STORAGE_DB_PATH` | `proxy/data/microvm.db` | Location of the local SQLite database |

---

## Security

This is a local developer POC, not a hardened multi-user service. In short:

- The proxy and the pool manager bind `127.0.0.1` only (the proxy refuses a non-loopback bind unless `ALLOW_NON_LOOPBACK_BIND` is set).
- Each installer run mints a fresh shared secret and every call into the proxy carries it in `X-Proxy-Auth`. It is passed to the processes through their environment, not written to disk.
- Notebook environment variables are sealed at rest; secrets resolve inside the VM; the AI assistant asks for approval before running code; import-from-URL has an SSRF guard; S3 deletes are prefix-guarded.
- Outbound internet access from a MicroVM is unrestricted by default. [NETWORK_EGRESS.md](NETWORK_EGRESS.md) documents two Layer-7 options (AWS Network Firewall, or an in-VM transparent proxy) for restricting domains.
- If you ever expose a service beyond `127.0.0.1`, add real authentication and transport security first.

The full model, each control with the code that implements it, the boundaries to
understand, and how to report a vulnerability are in **[SECURITY.md](SECURITY.md)**.

---

## Testing

`tests/` has one folder per component under test. Each test file is a self-running
script (pytest is not installed); run it with the project virtualenv's interpreter,
`.venv/bin/python` (your system Python lacks the deps). Tests fall into three groups:

- **Offline**: no AWS and no running proxy; finish in seconds. Most of `tests/`.
- **Live**: need the running local stack (`./local_install.sh`) and launch real MicroVMs.
- **Opt-in live-AWS**: touch real AWS and **skip cleanly** unless their env gate is set.

### How to tell offline from live
Every test file has a marker in its first lines, for example `# test-kind: offline`.
The marker travels with the file (folders mix kinds), and `tests/offline_tests.sh --check`
fails when a file has none, so it cannot go stale.

| `# test-kind:` | Meaning | Run by |
|---|---|---|
| `offline` | No AWS, no proxy | `tests/offline_tests.sh` |
| `both` | Offline part runs with `PROXY_URL=http://127.0.0.1:9`; a live section runs against a proxy | `offline_tests.sh` (offline part) and `run_tests.sh` |
| `live` | Needs the running stack | `tests/run_tests.sh` (every `live`/`both` file in any folder, except that `eternal/` and `checkpoint/` run only in the matching proxy mode) |
| `gated` | Touches AWS only when its env gate is set, otherwise skips cleanly | by hand with the gate set |
| `manual` | Benchmark or by-hand harness (`bench_*.py`, `integration/`, `sagemaker/`, `sync/test_sync_e2e.py`); `run_tests.sh` does not run them | by hand |

List by kind with `bash tests/offline_tests.sh --list [--kind live]`. New test files must carry the marker in the first five lines (normally line 2, after the shebang).

### Layout

| Folder | Component under test | Kind |
|---|---|---|
| `tests/vm/` | VM app (`app/`): executor, Python/SQL cell engines, run guards (timeout/Stop for Python and SQL cells, incl. the interrupt-race regression), the single Athena runner (`read_athena`, UNLOAD, cancel on timeout/Stop), DynamoDB reader and S3 module (pagination), cached AWS client factory, helper public-API snapshot, `boto3` lint (no direct clients outside the allow-list), variable/provenance tracking, lineage capture, pip diagnostics, env helpers, local file discovery, `/status` | offline |
| `tests/dag/` | Notebook DAG: builder (`proxy/notebook/dag.py`), capture/delta, `cell_code_hash` contract, VM `/dag` endpoint, rebuild benchmark | offline |
| `tests/proxy/` | Proxy control plane (`proxy/`): auth, CORS, bind guard, terminal WS, secrets, session/notebook delete cascades, cell images, VM detail/metrics, burst/cost, URL guard, datasource cache | offline |
| `tests/ai/` | AI assistant + model catalog: explain/lineage routes, prompts and helper block, model params/defaults/capabilities, chat throttle fallback, approval hook, orphan-variable marking | offline |
| `tests/storage/` | Storage backends (SQLite always, Postgres when `TEST_PG_DSN` is set), cost views, handoff safety, transcripts, notebook DAG storage | offline |
| `tests/checkpoint/` | Checkpoint save/restore: delta core, S3 round-trip (fake S3), dirty flag, DAG checkpoint | offline; `test_auto_checkpoint`, `test_s3_restore`, `test_suspend_checkpoint_timing` are **live**; `bench_restore_parallel` is **manual** |
| `tests/pool/` | Warm Pool Manager (`pool_manager/`), via `_poolkit.py` (fake VM backend + temp SQLite); proxy launch/recover with pool | offline; `test_pool_e2e`, `test_notebook_lifecycle_e2e`, `test_restore_timing`, `test_pool_grant_load`, `test_terminates_in_mint_time` are **gated** (env-gated live) |
| `tests/sync/` | SQLite → Aurora sync worker | offline: `test_sync_dag_unit`, `test_sync_cell_image_unit`; **manual**: `test_sync_e2e` (needs `--AuroraSetup`) |
| `tests/build/` | Build and config: `scripts/config.sh` arch block, service-image import graph, KB spec generation | offline |
| `tests/web/` | Web export: `.ipynb` export validated with `nbformat` | offline (needs `tests/requirements-dev.txt`) |
| `tests/common/` | Mode-agnostic live tests run by `run_tests.sh`: lifecycle, SQL routing, interrupt, burst, chat pending-approval auto-decline | **live** |
| `tests/eternal/` | Eternal mode: reactive recovery | **live** (run by `run_tests.sh` in eternal mode) |
| `tests/integration/` | Live end-to-end checks run by hand: model switch, AI interrupt/resume, security fixes, chart persistence, mutation lineage | **manual** (live) |
| `tests/sagemaker/` | Custom Amazon SageMaker AI serving container | **manual** (AWS endpoints) |
| `tests/NetworkEgress/` | Layer-7 egress control test + teardown ([NETWORK_EGRESS.md](NETWORK_EGRESS.md)) | **gated** (opt-in live AWS) |
| `tests/fixtures/` | Shared fixtures (`dag_*.json`, `pip_install/`, SQL cases); `gen_dag_fixtures.py` regenerates `dag_*.json` | data |

Tests locate the repo root from their own path (`tests/<folder>/x.py` → `../..`), so
they run from any working directory.

### Offline tests

Install the test-only dependencies once. They live in `tests/requirements-dev.txt`
(`nbformat`, `matplotlib`, `duckdb`, `dill`, `plotly`) and are not installed by `local_install.sh`:

```bash
uv pip install --python .venv/bin/python -r tests/requirements-dev.txt
```

Run the whole offline set, or one file:

```bash
bash tests/offline_tests.sh                  # every offline/both file, one at a time, AWS creds unset
bash tests/offline_tests.sh --list           # show the selected files
bash tests/offline_tests.sh --check          # fail if a test file lacks a valid marker
bash tests/offline_tests.sh -k pool          # only files whose path contains "pool"
.venv/bin/python tests/dag/test_dag_builder.py
```

`offline_tests.sh` runs each file with `AWS_*` credentials unset and
`PROXY_URL=http://127.0.0.1:9`, so a `both` file such as
`tests/common/test_chat_pending_interrupt_autodecline.py` skips its live section. It
prints PASS / SKIP (passed with a skipped section, e.g. Postgres without `TEST_PG_DSN`) /
FAIL per file, writes logs to `${OFFLINE_LOG_DIR:-/tmp/offline_tests}`, and exits 1 on any failure.
Each file prints PASS/FAIL per check and exits non-zero on failure. Contributor detail (adding a test, the marker rule, the web node tests) is in [CONTRIBUTING.md](CONTRIBUTING.md#running-tests).

Other offline commands:

```bash
bash tests/build/test_kb_spec_autogen.sh                    # KB spec-PDF detection + offline --no-llm generation
.venv/bin/python tests/dag/bench_dag_rebuild.py             # DAG RMW p95 + payload bytes (benchmark)
.venv/bin/python tests/fixtures/gen_dag_fixtures.py         # regenerate tests/fixtures/dag_*.json
node --test web/src/services/__tests__/lineage.test.mjs     # web: lineage model
node --test web/src/services/__tests__/depGraph.test.mjs    # web: DAG traversal
node --test web/src/services/__tests__/dagDelta.test.mjs    # web: dag_delta apply + graphHash parity
node --test web/src/services/__tests__/chatTurns.test.mjs   # web: AI chat turns
```

Both CPU arches are covered by the offline suites with no flag (per-arch config and
pricing in `tests/build/test_arch_config.py` and `tests/storage/test_pricing_by_arch.py`;
x86_64 grants, image names and recover in `tests/pool/`; pip no-wheel/timeout
classification in `tests/vm/test_pip_diagnostics.py` and `tests/vm/test_install_package_pip.py`).

### Live tests (need a running proxy)

`run_tests.sh` health-checks the proxy, reads its persistence mode, then runs every file
marked `live` or `both`, selected by marker across all of `tests/`. The mode-specific
folders are the exception: `tests/eternal/` runs only in eternal mode and
`tests/checkpoint/` only in checkpoint mode. A new live test in any other folder is
picked up without editing the script. It does not run offline files; use `offline_tests.sh`:

```bash
./local_install.sh        # in one terminal (starts the proxy)
bash tests/run_tests.sh   # in another
```

All live tests use `X-Session-Id` only; no VM internals are referenced. They launch on
the proxy's default arch (arm64), so a normal run starts no x86_64 VMs. To cover
x86_64 too (real x86 VMs, extra cost):

```bash
LIVE_TEST_X86=1 bash tests/run_tests.sh                                     # default pass, then the same suites with TEST_ARCH=x86_64
TEST_ARCH=x86_64 .venv/bin/python tests/common/test_microvm_lifecycle.py   # one test on x86_64
```

With `TEST_ARCH` set, each live test sends `"arch"` in its `/launch` body and asserts
the launch reports that arch; the x86_64 pass logs to `/tmp/test_<name>_x86_64.log`
and its results are counted separately. The env-gated pool suites (`LIFECYCLE_ARCH`,
`RESTORE_TIMING_ARCH`, `TERMINATES_IN_ARCH`, `LOAD_ARCH`, `POOL_E2E_ARCH`) and
`NetworkEgressTest.py --arch` default to `TEST_ARCH` when it is set.

Live suites that `run_tests.sh` does not launch (run them directly):

```bash
.venv/bin/python tests/integration/test_model_switch_live.py          # switch AI model mid-session
.venv/bin/python tests/integration/test_ai_interrupt_resume_live.py   # interrupt + resume an AI turn
.venv/bin/python tests/integration/test_security_fixes_live.py        # live security-fix regressions
.venv/bin/python tests/sagemaker/test_serving_endpoints.py            # custom serving container: /ping + /invocations
.venv/bin/python tests/sync/test_sync_e2e.py                          # SQLite → Aurora sync worker (needs --AuroraSetup)
```

### Opt-in live-AWS tests (env-gated)

These launch real VMs or touch real AWS and skip cleanly unless their env gate is set,
so they never fail a normal run:

- **`tests/pool/`**: `test_pool_e2e` (`POOL_E2E=1`), `test_notebook_lifecycle_e2e`
  (`POOL_LIFECYCLE_E2E=1`), `test_restore_timing` (`POOL_RESTORE_TIMING=1`,
  `RESTORE_SLA_SECONDS`), `test_pool_grant_load` (`POOL_GRANT_LOAD=1`, concurrent-grant stress), and
  `test_terminates_in_mint_time` (`POOL_TERMINATES_IN=1`). They need the warm-pool stack running.
- **`tests/NetworkEgress/`**: `NetworkEgressTest.py` / `NetworkEgressTeardown.py`; see
  **[NETWORK_EGRESS.md](NETWORK_EGRESS.md)**.

---

## Project Structure

```
local_install.sh             # One-click launcher (macOS): preflight checks → .venv → AWS setup →
                             #   build images → start proxy + AI chat agent + UI.
                             #   ALWAYS installs (idempotent): sample data, S3 Tables (Iceberg) demo
                             #                   tables, the Bedrock KB (S3 Vectors), the SageMaker
                             #                   serving image, and the env-var master key.
                             #   No flag = fully local (SQLite, no sync worker).
                             #   --AuroraSetup = also ensure Aurora + start sync worker (non-destructive,
                             #                   provisions if missing; schema DDL via the RDS Data API).
                             #   --AuroraReset = --AuroraSetup + drop/recreate Aurora schema & reset SQLite.
local_stop_app.sh            # Close the app (counterpart to local_install.sh): delete running notebooks
                             #   (checkpoint + terminate their VMs, as the UI's Delete), stop every local
                             #   service incl. the sync worker, terminate warm-pool spares/orphans, clear
                             #   the app's saved tabs in the browser. Keeps images, roles, buckets, data.
                             #   --dry-run, --yes, --keep-notebooks, --keep-vms, --clear-all-ui-storage.
Dockerfile                   # MicroVM image (data-science Python stack baked in; COPYs app/requirements.txt)
.dockerignore                # Build-context excludes for the proxy/ and pool_manager/ service images
SETUP.md, NETWORK_EGRESS.md, NOTEBOOK_STATE.md  # Setup guide, Layer-7 egress control, checkpoint/restore design
CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md, LICENSE
.venv/                       # Host virtualenv the installer creates/reuses (gitignored)
iam/                         # IAM policy/trust JSON for the build + exec roles (+ proxy-role and
                             #   pool-manager-role task-role policies — documentation for a hosted deploy)
modelcaps/                    # Bedrock model capability catalog: model_caps.json + params.py resolver,
                              #   price_overrides.json, refresh.py (regenerates the catalog). The runtime
                              #   trio is vendored into app/ and agent/ by scripts/sync_modelcaps.sh.

app/                          # Runs INSIDE the MicroVM
├── requirements.txt          # In-VM Python deps (baked into the image; COPYd by the root Dockerfile)
├── server.py                 # FastAPI entrypoint, pre-loaded libs
├── platform/
│   ├── hooks.py              # Lifecycle hooks: /ready, /validate (build) + /run, /suspend, /resume, /terminate
│   ├── errors.py             # In-VM error formatting
│   └── checkpoint/           # DELTA checkpoint package (see NOTEBOOK_STATE.md)
│       ├── manager.py        # Orchestrates save/restore (dirty-flag gating, 16-way parallel restore, import-ledger replay)
│       ├── delta.py          # Per-variable + per-file change detection (SHA-256 vars, (size,mtime) files; ≤64KB batched)
│       ├── archive.py        # Safe /tmp path-join + tar extraction helpers (_safe_join / _safe_extractall)
│       └── namespace_filter.py # Module + non-picklable-object exclusion
└── notebook/
    ├── executor.py           # SandboxExecutor (stateful Python engine, import ledger, interrupt)
    ├── run_guard.py          # Run guards: timeout + Stop for ONE cell (_RunGuard base, _PythonRunGuard,
    │                         #   _SqlRunGuard; native cancels such as Athena stop_query_execution)
    ├── run_limits.py         # Shared run limits (EXECUTION_TIMEOUT_SECONDS, timeout/interrupt texts)
    ├── code_engine.py        # /execute, /execute-batch, /set-env, /refresh-secrets
    ├── sql_engine.py         # /execute-sql with auto-routing
    ├── aws_clients.py        # Cached AWS client factory for VM-side code (reset on credential refresh)
    ├── dag_contract.py, dag_store.py # Notebook DAG contract (stdlib only) + the VM-held DAG document
    ├── read_capture.py, http_capture.py # Per-cell capture of data sources / outbound HTTP read (lineage "Sources")
    ├── var_inspect.py        # Read-only variable inspector for the AI agents (live DataFrame schemas)
    ├── scratch.py            # Agent exploration scratch scopes
    ├── pip_diagnostics.py    # pip install failure diagnostics (no wheel for this arch, timeouts, …)
    ├── vm_status.py          # Read-only VM status for the Live MicroVMs card
    ├── data_catalog.py       # /data-catalog endpoint (S3, DynamoDB, Athena, CloudWatch discovery)
    ├── completion_catalog.py # Build-time helper + builtin/library introspection (feeds /helpers + autocomplete)
    ├── static_analysis.py    # Lightweight static checks surfaced in the editor
    ├── dtypes.py             # DataFrame type detection utilities
    ├── routes.py             # /install, /variables, /helpers, /completions-catalog, /health, /metrics, /upload
    ├── data/                 # Generated completion_catalog.json (completion catalog)
    └── helpers/              # The pre-loaded helper library (available in every cell, no imports)
        ├── readers.py        # read_s3_*, read_dynamodb*, read_athena, read_url, read_local*, sample_data, …
        ├── _athena.py        # The ONE Athena runner (SQL cells, read_athena, UNLOAD): typed results, cancel on timeout/Stop
        ├── _dynamo.py        # The ONE DynamoDB reader (scan/query/PartiQL, paginated, stops on timeout/Stop)
        ├── _s3.py            # The ONE S3 object module (paginated list, get/put bytes, delete_prefix)
        ├── export.py         # to_s3_*, to_local, to_local_prefix, upload_s3, save_model
        ├── aurora.py         # read_aurora + aurora_tables/aurora_schema
        ├── bedrock_kb.py     # read_bedrock_kb / ask_bedrock_kb (retrieve-only KB)
        ├── llm.py            # invoke_llm, llm_* per-row helpers, embed_texts, semantic_search
        ├── ml.py             # train_model, model_info, predict_batch, save_model/load_model (in-VM sklearn)
        ├── sagemaker.py      # sm_train / sm_deploy / sm_* (remote SageMaker training + serving)
        ├── sm_scripts/       # SageMaker training entrypoint (sklearn_train.py) + its requirements
        ├── logs.py           # read_logs/grep_logs/tail_logs/ask_logs/log_groups (CloudWatch Logs Insights)
        ├── env.py            # get_env/get_secret + list_env/list_secret (env + Secrets Manager)
        ├── cost.py           # estimate_session_cost / session_cost / this_notebook
        ├── profiling.py      # profile, compare_df, list_s3, head_s3, @timer, whoami
        ├── packages.py       # list_packages + runtime registration
        ├── viz.py            # plot_line/bar/scatter/histogram/heatmap/box/pie/area/corr
        ├── modelcaps/        # Vendored model capability catalog (scripts/sync_modelcaps.sh)
        └── _common.py        # Shared decorators (read/write logging), region, S3 target resolution

batch/                        # Background jobs (entity discovery)
└── entity_discovery.py       # Auto-profile all data sources (S3, DynamoDB, Athena, Aurora, Bedrock KB,
                              #   CloudWatch, local files). KB profile centers on its filterable metadata.

serving/                      # Custom SageMaker serving container (ONE image: sklearn .joblib + ONNX .onnx)
├── serve_app.py              # gunicorn app: GET /ping + POST /invocations (serverless-safe, no nginx)
├── serve                     # Entrypoint
├── Dockerfile                # Serving image
└── requirements.txt          # Serving deps

proxy/                        # Runs on your machine (hides all VM internals)
├── requirements.txt          # Proxy/agent/sync deps (installed into .venv on the host; also used by proxy/Dockerfile)
├── Dockerfile                # Service container image (copies proxy/ app/ batch/ pool_manager/ modelcaps/, port 8081)
├── server.py                 # FastAPI entrypoint, lifespan (startup reconcile), health, router wiring
├── platform/
│   ├── microvm_manager.py    # Session registry (write-through to DB), tokens, reactive recovery,
│   │                         #   startup registry reconcile, AWS client
│   ├── cost_tracker.py       # Baseline + burst + snapshot read/write cost tracking
│   ├── env_crypto.py         # Seals notebook env-var VALUES at rest (Secrets Manager master key)
│   ├── package_classifier.py # PyPI-based package category detection
│   ├── token_cache.py        # Auth-token cache (in-process; the redis backend is not implemented)
│   ├── errors.py             # Centralized error handling, json_error, metric lines
│   ├── defaults.py           # Product defaults (DEFAULT_MEMORY_MIB)
│   ├── vm_detail.py          # VM card detail payload (GET /instances/{id}/detail)
│   ├── vm_metrics_history.py # Per-run metrics history (GET /instances/{id}/metrics-history)
│   ├── burst_time.py         # Burst-time summary from stored metrics
│   ├── auth.py               # Shared-secret auth, CORS origins, bind guard
│   ├── aws_clients.py, http_clients.py # Cached boto3 clients, shared httpx clients
│   ├── datasources/          # Schema discovery providers + registry
│   │   ├── registry.py       # Single source of truth for source types (order/labels/docs)
│   │   ├── interface.py      # DataSourceProvider abstraction
│   │   ├── bedrock_kb.py     # Bedrock Knowledge Base (S3 Vectors) — retrieve-only, multi-KB
│   │   ├── cloudwatch.py     # CloudWatch Logs groups/streams discovery
│   │   ├── sagemaker_endpoint.py # Live SageMaker endpoints as a data source
│   │   └── s3.py, dynamodb.py, athena.py, aurora.py, local.py
│   └── routes/
│       ├── microvm.py        # /launch, /terminate (session-optional), /suspend, /resume, /proxy/{path}, /instances
│       ├── sessions.py       # /sessions, /datasources (+ /catalog, /schema), /secrets
│       ├── metrics.py        # /image-tiers (+ image versions for the VM detail card)
│       └── terminal.py       # WebSocket terminal relay → VM shell
├── notebook/
│   ├── format_engine.py      # Notebook format/transform helpers
│   ├── dag.py, dag_service.py # Persisted per-notebook DAG: pure builder + proxy orchestration
│   ├── cell_images.py        # Chart (matplotlib PNG) persistence for cells (notebook-outputs/ in S3)
│   ├── session_checkpoints.py # S3 cleanup of a deleted notebook's session data
│   ├── var_orphans.py        # Flags variables whose defining cell was deleted (DAG-aware)
│   ├── ai/
│   │   ├── constants.py      # Model IDs, token limits, CHAT_AGENT_ID, SOURCE_TYPE_LABELS (chat subset copied to ai_assistant/)
│   │   ├── prompts.py        # Intel + fix/annotate/terminal prompts + the chat group (SINGLE SOURCE; scripts/gen_prompts.py
│   │   │                     #   generates agent/ai_assistant/prompts.py from it) — no proxy.* import
│   │   ├── helpers_block.py  # AUTO-GENERATED helper reference block for the prompt (gen_helpers_block.py)
│   │   ├── notebook_agent.py # Strands one-shot helpers (fix, annotate, suggest-tag) + Workbook Intel agent wiring (chat moved to ai_assistant/)
│   │   ├── memory.py         # Chat transcript READ/DELETE seam (bubbles + agentcore purge); write seam moved to ai_assistant/
│   │   ├── workbook_intel.py # Workbook Intel entrypoint (delegates to intel/)
│   │   ├── explain_cell.py, explain_context.py, explain_prompt.py, explain_sections.py
│   │   │                     #   "Explain this code": orchestrator, context builder, prompt, section parsing
│   │   ├── intel/            # Workbook Intel pipeline
│   │   │   ├── generate.py   # Phase 1 (Strands agent) + Phase 2 (direct converse) full generation
│   │   │   ├── delta.py      # Incremental (file upload) + deletion pruning
│   │   │   ├── context.py    # Entity-doc assembly, source counts
│   │   │   ├── parsing.py    # Intel JSON/delimited response parsing + salvage
│   │   │   └── store.py      # Per-session FIFO scheduler + S3 read/write
│   │   └── tools/            # Agent tools (execution_tools, notebook_tools) — all HTTP to proxy
│   └── routes/
│       ├── ai.py             # /ai/chat (forwards to agent), /ai/chat/sync, /ai/annotate, /ai/fix, /ai/suggest-tag
│       ├── intel.py          # /workbook-intel (GET/POST) endpoints
│       ├── notebooks.py      # Notebook CRUD + versions (+ import-from-url)
│       ├── _url_guard.py     # SSRF guard for user-supplied fetch URLs
│       ├── format.py         # Notebook formatting endpoint
│       └── logs.py           # CloudWatch log streaming
├── storage/                  # Abstract storage seam (proxy code never touches a DB driver directly)
│   ├── interface.py          # Abstract StorageBackend (notebooks, vm_sessions, session_registry, …)
│   ├── sqlite_db.py          # SQLite implementation (local default)
│   ├── postgres_db.py        # Postgres/Aurora implementation (cloud)
│   ├── async_storage.py      # Async wrapper (asyncio.to_thread) over the sync backend
│   ├── versioning.py         # Notebook version/document helpers
│   ├── _timestamps.py        # Shared ISO-UTC timestamp normalization (both backends return the same shape)
│   ├── _cell_fields.py, _dag_doc.py, _metrics_fields.py # Backend-neutral row/field helpers
│   └── _transcript.py        # Agent-message → UI-bubble transcript translation (shared)
└── data/                     # Local SQLite database (microvm.db) + package_categories.json

agent/                        # AgentCore project root — a PLAIN directory (NO __init__.py). Launch the
│                             #   agent from inside it (cwd=agent or PYTHONPATH=agent) so `ai_assistant.*`
│                             #   resolves (import strategy B1).
├── ai_assistant/             # AI CHAT assistant as its own service (BedrockAgentCoreApp on :8082;
│   │                         #   /invocations + /ping). Deployable to Amazon Bedrock AgentCore Runtime.
│   │                         #   Imports proxy.storage ONLY on the local SQLite memory path (image COPYs proxy/).
│   ├── agent_runtime.py      # AgentCore entrypoint (dispatch chat|clear); started as `python -m ai_assistant.agent_runtime`
│   ├── notebook_agent.py     # The Strands CHAT Agent: chat_stream/new_thread/get_or_create_agent + per-turn context build
│   ├── prompts.py            # Chat system prompt — GENERATED from proxy/notebook/ai/prompts.py (scripts/gen_prompts.py)
│   ├── helpers_block.py      # AUTO-GENERATED helper reference block for the prompt
│   ├── constants.py          # Chat-only subset (agent/attachment/source-label constants)
│   ├── memory.py             # Chat WRITE seam: MEMORY_BACKEND factory (sqlite DbSessionRepository ↔ AgentCore Memory)
│   ├── sessions.py           # In-process agent cache (keyed by session_id)
│   ├── tools/                # Agent tools (execution_tools, notebook_tools) — all HTTP back to the proxy;
│   │                         #   approval_hook.py = confirm-before-execute gate (Strands hook)
│   ├── modelcaps/            # Vendored model capability catalog (scripts/sync_modelcaps.sh)
│   ├── pyproject.toml        # uv-managed SERVICE deps (strands-agents, bedrock-agentcore, boto3, httpx, psycopg2-binary, aws-opentelemetry-distro)
│   ├── uv.lock               # Pinned dependency lockfile (generated by `uv lock`)
│   ├── .dockerignore         # Excludes __pycache__/.venv/Dockerfile from the build context
│   └── Dockerfile            # SELF-CONTAINED service image (build context = this dir / codeLocation;
│                             #   copies ONLY ai_assistant/, NOT proxy/; port 8080 = AgentCore contract;
│                             #   DOCKER_CONTAINER=1 so the SDK binds 0.0.0.0; CMD opentelemetry-instrument)
└── agentcore/                # AgentCore CLI project (@aws/agentcore — CDK-driven; NOT the starter-toolkit)
    ├── agentcore.json        # Runtime manifest: build=Container, codeLocation=ai_assistant/, protocol=HTTP,
    │                         #   networkMode=PUBLIC (VPC is a deploy-time override), runtimeVersion=PYTHON_3_13,
    │                         #   one short-term memories[] entry. Validated by `agentcore status`.
    ├── aws-targets.json      # Deployment targets (account+region) — [] until a human adds one
    ├── .llm-context/         # CLI-shipped schema/type reference (agentcore.ts, aws-targets.ts)
    ├── .cli/                 # CLI run state (gitignored; deployed-state.json is written by `agentcore deploy`)
    └── cdk/                  # CDK app `agentcore deploy` runs (generic: reads agentcore.json at synth).
                              #   Needs `npm install` + `cdk bootstrap` before first deploy (human follow-up).

pool_manager/                 # OPTIONAL warm-pool service (own process on :8090; see "Warm Pool Manager").
│                             #   Started only with --WarmPool / POOL_ENABLED=true. Shares the proxy's
│                             #   storage (pool_vms table) — imports proxy.storage, no DB driver of its own.
├── server.py                 # FastAPI app factory + lifespan. Endpoints: /grant, /status, /health,
│                             #   /ready (DB reachable + pool at target), /admin/refill, /admin/drain
├── manager.py                # PoolManager: fill/recycle/reconcile loops (advisory-locked for multi-instance
│                             #   safety), grant() (claim-youngest → resume → rebind → optional restore),
│                             #   over-target trim, stale-'granting' reconcile
├── config.py                 # PoolConfig + load_config() (reads POOL_* env from scripts/config.sh)
├── vm_backend.py             # VmBackend / VmHandle interfaces (the backend seam)
├── aws_backend.py            # Real lambda-microvms backend (run/get/list/suspend/resume/terminate + auth token)
├── fake_backend.py           # In-memory backend for offline tests (no AWS, no real VMs)
├── backend_factory.py        # build_backend() — side-effect-free backend selection (aws | fake)
├── reset_local_pool.py       # One-shot: terminate pooled VMs then wipe pool_vms (local clean slate,
│                             #   run by local_install.sh before the pool starts; terminate-then-wipe)
├── requirements.in           # Minimal SERVICE deps (no own lockfile; covered by proxy/requirements.txt)
└── Dockerfile                # Service container image (copies pool_manager/ proxy/ app/ batch/ modelcaps/, port 8090)

sync/                         # LOCAL-DEV-ONLY SQLite → Aurora bridge (throwaway)
└── sync_to_aurora.py         # Standalone worker: replays local SQLite writes/deletes into Aurora
                              #   over the RDS Data API (generic FK-topo introspection). Unneeded once
                              #   the proxy runs directly on Postgres (STORAGE_BACKEND=postgres).

web/src/                      # React UI (Vite)
├── App.jsx / AppView.jsx     # Main app, tab management, intel watching
├── components/
│   ├── panels/               # Sidebar panels
│   │   ├── NotebooksPanel.jsx, OutlinePanel.jsx, DataSourcesPanel.jsx, VariablesPanel.jsx
│   │   ├── OutlineMap.jsx    # Outline cell map: proportional ticks, section bands, status lane, notebook viewport window
│   │   ├── VmMetricsHistory.jsx # VM card Resources history chart
│   │   ├── PanelState.jsx, PanelExpandButton.jsx, highlightMatch.jsx # Shared panel pieces (empty/loading/error, search highlight)
│   │   ├── PackagesPanel.jsx, TerminalPanel.jsx, LogsPanel.jsx
│   │   ├── SnippetsPanel.jsx # Helper library + user snippets + Recipes (uses the two JSON files below)
│   │   ├── curated_snippets.json          # Hand-authored example code per helper (source of truth)
│   │   ├── helpersFallback.generated.json # AUTO-GENERATED cold-start helper list (gen_helpers_block.py)
│   │   ├── SamplesPanel.jsx, MicroVMsPanel.jsx, EnvPanel.jsx (Secrets & Env), AboutPanel.jsx
│   │   └── datasources/      # Per-source browser sub-components
│   ├── Cell.jsx / CellEditor.jsx / CellOutput.jsx / MarkdownCell.jsx   # Cell + CodeMirror editor + output
│   ├── CellFocusOverlay.jsx / ExplainPanel.jsx # Cell focus mode + "Explain this code"
│   ├── LineageDrawer.jsx     # "How was this computed?" lineage drawer
│   ├── ExecutionHud.jsx      # Run progress / resource HUD
│   ├── CellDeleteConfirm.jsx, FixVerification.jsx, DiffCell.jsx, PersistedCellImage.jsx
│   ├── Notebook.jsx / NotebookToolbar.jsx      # Cell list, execution, Run All
│   ├── Sidebar.jsx           # Activity bar with grouped icons
│   ├── AiChatPanel.jsx / AiFab.jsx / ChatTurn.jsx / ChatTurnRail.jsx # AI chat
│   ├── IntelAccordion.jsx / IntelReportModal.jsx  # Workbook Intelligence
│   ├── ConnectionPanel.jsx   # VM connect/restore (Option A restore, optionally on another arch/tier / Option B fresh launch)
│   ├── DataGrid.jsx / DataChart.jsx / JsonTree.jsx / ParamWidgets.jsx  # Rich output + @param widgets
│   ├── VersionHistoryDrawer.jsx / VariableDetailModal.jsx
│   └── Icons.jsx, Modal.jsx, Markdown.jsx, ErrorBoundary.jsx, WelcomeScreen.jsx
├── hooks/                    # React hooks (useNotebookCells, useSamplesImport, …)
├── services/                 # Pure logic + API client (all calls use X-Session-Id): apiFetch/apiError, vmLaunch,
│                             #   runRequest, cellReconcile, execTint, outlineMap, sampleFeatures, chart/export helpers, …
│                             #   (node tests in services/__tests__/)
├── theme.css / theme-light.css # Theme tokens (light/dark/ember) + shared light+dark rules
└── constants.js / config.js  # Frontend constants + proxy URL

web/public/samples/           # Sample notebooks (lazy-loaded via index.json, with showcasedFeatures per sample), e.g.
                              #   aws_data_sources, helper_functions, cloudwatch_logs, ml_inference,
                              #   sagemaker_training, sagemaker_bigdata, notebook_analytics, time_series, …

scripts/                      # Setup and operations
├── config.sh                 # All configuration (region, bucket, IAM, ports, models, Aurora/KB/SageMaker, …)
├── setup_iam.sh              # Create IAM roles
├── setup_env_master_key.sh   # Ensure the env-var encryption master key (Secrets Manager) — every run
├── setup_sample_data.sh      # Seed e-commerce data (S3, DynamoDB, Athena)
├── setup_s3_tables.sh        # Provision/seed the S3 Tables (Iceberg) demo tables (order_events, price history)
├── setup_bedrock_kb.sh       # Provision/reuse a Bedrock KB on S3 Vectors (generates + ingests demoproductspec/ PDFs)
├── kb_specs_lib.sh           # Spec-PDF helpers for setup_bedrock_kb.sh (completeness check, pinned specgen venv)
├── setup_sagemaker.sh        # Build/push the custom SageMaker serving image (serving/) to ECR
├── setup_aurora_postgres.sh  # Provision Aurora PostgreSQL: cluster + roles + schema + v_nb_* views (RDS Data API)
├── setup_agentcore_memory.sh # STANDALONE: create-or-get an AgentCore Memory resource (+ .py helper)
├── generate_product_specs.py # Generate the product-spec PDFs (+ metadata sidecars + manifest) for the KB corpus
├── gen_helpers_block.py      # Generate helpers_block.py + helpersFallback.generated.json from live introspection
├── gen_prompts.py            # Generate agent/ai_assistant/prompts.py from the proxy's prompt source (--check)
├── sync_modelcaps.sh         # Vendor the modelcaps runtime trio into app/ and agent/
├── benchmark_intel_models.py # LLM model benchmark (Phase1/2, incremental, deletion, judge)
├── bench_models.py           # Notebook-assistant model benchmark across Claude models (optional LLM-as-judge)
├── build_all_images.sh       # Build MicroVM images: 4 memory tiers per arch in IMAGE_ARCHES (8 by default)
├── teardown.sh               # Clean up AWS resources (images, demo secrets, S3 ML artifacts)
└── demoproductspec/          # Generated on first install (gitignored): product-spec PDFs + .metadata.json sidecars (KB corpus)

tests/                        # Tests + fixtures, one folder per component (see the Testing section)
├── run_tests.sh              # Auto-detect proxy mode; run every live/both-marked file (eternal/ or checkpoint/ only in their mode; needs a running proxy)
├── offline_tests.sh          # Run every file marked `# test-kind: offline|both` with no AWS and no proxy (--list, --check, -k)
├── _select.sh                # Marker helpers shared by the two scripts
├── requirements-dev.txt      # Test-only deps for the offline suites (nbformat, matplotlib, duckdb, dill, plotly)
├── product_returns.csv       # Test fixture for deletion-flow testing
├── vm/                       # (offline) VM app: executor, cell engines, run guards + interrupt race, Athena runner, DynamoDB/S3
│                             #   readers, AWS client factory, helper-API snapshot, boto3 lint, provenance, lineage, pip, /status
├── dag/                      # (offline) notebook DAG builder, capture/delta, hash contract, VM /dag endpoint, bench_dag_rebuild.py
├── proxy/                    # (offline) proxy control plane: auth/CORS/bind guard/terminal WS, delete cascades, VM detail/metrics, URL guard
├── ai/                       # (offline) AI assistant: explain/lineage routes, prompts, model params/caps, throttle fallback, approval hook
├── storage/                  # (offline) storage backends (SQLite; Postgres with TEST_PG_DSN) + cost views
├── checkpoint/               # Checkpoint mode + DELTA checkpoint  (see NOTEBOOK_STATE.md)
│   ├── test_auto_checkpoint.py, test_s3_restore.py, test_suspend_checkpoint_timing.py, bench_restore_parallel.py  # (live)
│   └── test_delta_core.py, test_delta_s3_roundtrip.py, test_file_delta_core.py, test_dirty_flag_file_delete.py,
│       test_dag_checkpoint.py, test_checkpoint_total_size.py, test_injected_shims_not_saved.py,
│       test_small_batch_unpicklable.py   # (offline)
├── pool/                     # Warm Pool Manager (most OFFLINE via a fake backend; live ones are env-gated and skip cleanly)
│   ├── _poolkit.py                     # Shared harness (FakeVmBackend, temp SQLite, config/manager builders)
│   ├── test_pool_*.py, test_grant_*.py, test_fake_backend.py, test_aws_backend.py, test_config_wiring.py,
│   │   test_multi_manager_shared_db.py, test_rebind_identity.py, test_reset_*.py, test_ready_endpoint.py,
│   │   test_proxy_launch_*.py, test_proxy_recover_*.py, ...   # (offline)
│   └── test_pool_e2e, test_notebook_lifecycle_e2e, test_restore_timing, test_pool_grant_load,
│       test_terminates_in_mint_time   # (gated: live, env-gated)
├── sync/                     # SQLite → Aurora sync worker
│   └── test_sync_dag_unit.py, test_sync_cell_image_unit.py (offline), test_sync_e2e.py (manual, needs --AuroraSetup)
├── build/                    # (offline) scripts/config.sh arch block, service-image imports, KB spec generation
├── web/                      # (offline) .ipynb export validated with nbformat
├── common/                   # (live) mode-agnostic: lifecycle, SQL routing, interrupt, burst, chat auto-decline
├── eternal/                  # (live) Eternal mode: reactive recovery
├── integration/              # (manual) end-to-end by hand: model switch, AI interrupt/resume, security fixes
├── sagemaker/                # (manual) SageMaker serving: test_serving_endpoints.py
├── NetworkEgress/            # (gated, opt-in live AWS) Layer-7 egress-control test + teardown (see NETWORK_EGRESS.md)
└── fixtures/                 # DAG/pip/SQL fixtures; gen_dag_fixtures.py regenerates dag_*.json
```

> Each test file's `# test-kind:` marker says which group it belongs to (see Testing).
> **Offline** tests (`vm/`, `dag/`, `proxy/`, `ai/`, `storage/`, `build/`, `web/`, the
> offline files in `checkpoint/`, `pool/` and `sync/`) need no AWS or proxy; run them
> with `.venv/bin/python`. **Live** suites need a running proxy (`./local_install.sh`);
> **opt-in live-AWS** suites skip cleanly unless their env gate is set. See the
> [Testing](#testing) section for how each group runs.

---

## Cost

**Pricing rates** (from [AWS Lambda MicroVM pricing](https://aws.amazon.com/lambda/pricing/)
and the AWS Price List API, on-demand; us-west-2 and us-east-1 have the same rates). vCPU and
memory are priced per CPU architecture; the snapshot and image rates are the same for both:

| Component | Graviton (arm64) | x86_64 |
|-----------|------------------|--------|
| vCPU (compute) | $0.0000276944 / vCPU-second | $0.0000326557 / vCPU-second |
| Memory | $0.0000036667 / GB-second | $0.0000043235 / GB-second |
| Snapshot storage (suspended) | ~$0.08 / GB-month | ~$0.08 / GB-month |
| Snapshot data read (launch/resume) | $0.00155 / GB | $0.00155 / GB |
| Snapshot data write (suspend) | $0.00380 / GB | $0.00380 / GB |
| MicroVM image storage | ~$0.08 / GB-month (1-week minimum retention) | same |

Each VM is priced at its own architecture's rates (burst included), from
`MICROVM_PRICING_JSON` in `scripts/config.sh`. x86_64 compute costs ~18% more than arm64:
a 2 GB / 1 vCPU VM running for an hour is ~$0.126 on arm64 and ~$0.149 on x86_64. The
examples below use arm64, the default.

**Note:** CPU is allocated at 2 GB : 1 vCPU. A 4 GB kernel = 2 vCPU. Snapshot **data
read** is billed each time a VM launches or resumes (reading the snapshot back into
memory); snapshot **data write** is billed on each suspend (writing memory+disk out).
Both are charged per GB of the snapshot (≈ the memory tier), distinct from snapshot
**storage**, which is the per-GB-month charge while a VM sits SUSPENDED.

**MicroVM image storage** is a separate, standing charge from suspended-VM snapshots:
each built image tier (e.g. `notebook-microvm-arm64-2048`) is billed at ~$0.08/GB-month
on the image's on-disk size (filesystem content, not the memory tier), with a **1-week
minimum retention period**. The default build is 8 image tiers (4 arm64 + 4 x86_64, ~2 GB
each), so it is **~$1.28/month** standing cost even with zero VMs running (~$0.64/month with
`IMAGE_ARCHES=arm64`), the price of not rebuilding
images on every launch. The cost tracker does **not** meter this (it only prices running
compute + suspended snapshots); run `scripts/teardown.sh` to delete the images and drop
it to $0.

**Example breakdown**: 2 vCPU / 4 GB kernel, ~2.5 hr/day session of which ~30 min is active cell execution (~11 hr/month compute, 22 workdays). Each day the session launches once and suspends/resumes ~2× (an average workday: step away, resume, step away, resume) → per day **3 reads** (1 launch + 2 resumes) and **2 writes** (2 suspends); over 22 days that's **66 reads + 44 writes**:

| Component | Calculation | Monthly Cost |
|-----------|-------------|-------------|
| Compute (running), vCPU term | 2 vCPU × 39,600s × $0.0000276944 | $2.19 |
| Compute (running), memory term | 4 GB × 39,600s × $0.0000036667 | $0.58 |
| Snapshot storage (suspended) | 4 GB × 158,400s × ($0.08/GB-month ÷ 2,592,000s) | $0.02 |
| Snapshot data read (launch/resume) | 66 × 4 GB × $0.00155 | $0.41 |
| Snapshot data write (suspend) | 44 × 4 GB × $0.00380 | $0.67 |
| Burst (usage over tier) | none in this baseline | $0.00 |
| **Total** | | **~$3.87** |

The cost tracker (`proxy/platform/cost_tracker.py::price_quantities`) bills **one blended running
cost** while the VM is RUNNING (the two "Compute" rows above are the internal vCPU + memory terms of
that single figure, there is no separate vCPU-only or memory-only bill), a **snapshot storage charge
prorated per suspended-second** while SUSPENDED (not a flat GB-month), a **snapshot data read charge**
on each launch/resume and a **snapshot data write charge** on each suspend (each per GB of the
snapshot ≈ the memory tier), and a **burst charge** only for usage above the memory tier. The
~158,400s suspended = the ~2.5 hr/day session window minus the ~30 min/day of active compute, over 22
workdays. Snapshot data read/write adds ~$1.08/mo here, modest in absolute terms, and the all-in
MicroVM figure (~$3.87) still lands ~43% under the EKS per-pod cost (~$6.79), because EKS has no
equivalent per-operation snapshot charge and carries a 24/7 EBS volume plus a flat per-cluster
control-plane fee the MicroVM model has no counterpart for.

### Comparison vs EKS + EBS (at scale)

Same 2 vCPU / 4 GB kernel, ~2.5 hr/day session (~30 min active), snapshot read/write
included:

| Scale | Lambda MicroVMs | EKS + EBS (evict-on-idle) | Saving |
|-------|----------------|---------------------------|--------|
| Per user | ~$3.87 | ~$6.79 | ~43% |
| 1,000 users | ~$3,868 | ~$6,790 | ~$35K/yr |
| 2,000 users | ~$7,736 | ~$13,580 | ~$70K/yr |

EKS per-pod cost is Fargate compute (2 vCPU / 4 GB at us-east-1 on-demand rates
$0.04048/vCPU-hr + $0.004445/GB-hr, warm ~55 hr/mo = $5.43) + a 24/7 gp3 EBS volume
($1.36). **Not included:** the EKS **control-plane** fee of **$0.10/cluster-hour
(~$73/mo)**, a flat, per-cluster charge that does not scale with pods, so it's left
out of the per-user figure rather than amortized across an assumed fleet size. It is
real overhead EKS carries and Lambda MicroVMs do not (fully managed, no control
plane); at 1,000+ users it amortizes to a few cents per user, but at small scale it
dominates.

### Warm Pool Manager cost (optional)

When the [warm pool](#warm-pool-manager-optional) is enabled, the pool itself has a
standing cost on top of per-user sessions: its spares sit **SUSPENDED 24/7** (billed
at the snapshot-storage rate), and every time a spare is minted or recycled it pays a
brief **running** window plus one snapshot **read** and one snapshot **write**.

**Per mint** of a spare with G GB of memory (G/2 vCPU), with the arch's vCPU and
memory rates:

```
per mint  = 60 s × (G/2 × vCPU rate + G × memory rate)   # boot + health check, RUNNING
          + G × $0.00155                                 # 1 snapshot read (launch into RUNNING)
          + G × $0.00380                                 # 1 snapshot write (suspend into the pool)
per month = per mint × 12 mints/day/spare (POOL_MAX_AGE_HOURS=2) × 30 days
```

The cost tracker counts one snapshot read on every transition into RUNNING, cold
launch included, and one write on every suspend, so a mint is one of each. A 2 GB
spare costs ~$0.0128 per mint on arm64 and ~$0.0132 on x86_64; ~84% of that is the
snapshot read + write, not compute.

Directional, for the **default 7-spare pool** (arm64: 1 GB×1, 2 GB×2, 4 GB×2,
8 GB×1 = 6 spares, 21 GB, 10.5 vCPU; x86_64: 2 GB×1; **23 GB** of standing snapshot in
total), idle 24/7 with the 2 h recycle ceiling (arm64 72 mints/day, x86_64 12 mints/day):

| Component | Calculation (per month, 30 days) | Monthly Cost |
|-----------|----------------------------------|-------------|
| Snapshot storage, arm64 spares (idle, 24/7) | 21 GB × $0.08/GB-month | ~$1.68 |
| Mint compute, arm64 | 60 s × 12/day × 30 × (10.5 vCPU × $0.0000276944 + 21 GB × $0.0000036667) = 7,560 vCPU-s + 15,120 GB-s per day | ~$7.94 |
| Mint snapshot read, arm64 | 21 GB × 12/day × 30 × $0.00155 (252 GB/day) | ~$11.72 |
| Mint snapshot write, arm64 | 21 GB × 12/day × 30 × $0.00380 (252 GB/day) | ~$28.73 |
| Snapshot storage, x86_64 spare (idle, 24/7) | 2 GB × $0.08/GB-month | ~$0.16 |
| Mint compute, x86_64 | 60 s × 12/day × 30 × (1 vCPU × $0.0000326557 + 2 GB × $0.0000043235) | ~$0.89 |
| Mint snapshot read, x86_64 | 2 GB × 12/day × 30 × $0.00155 (24 GB/day) | ~$1.12 |
| Mint snapshot write, x86_64 | 2 GB × 12/day × 30 × $0.00380 (24 GB/day) | ~$2.74 |
| **Total (whole pool, not per user)** | arm64 ~$50.07 (churn ~$48.39) + x86_64 ~$4.90 (churn ~$4.74) | **~$54.97/mo** |

Notes:
- This is a **fixed pool overhead**, independent of how many users are active, it's
  the price of keeping launches/restores instant. It is a floor: each grant also
  triggers one backfill mint (~$0.0128 for a 2 GB arm64 spare).
- The per-mint snapshot write/read dominate (~81% of the total), then mint compute
  (~16%); standing snapshot storage is ~3%. Shrinking the pool
  (`POOL_SIZE_PER_IMAGE` / `POOL_SIZE_OVERRIDES_ARM64` / `POOL_SIZE_OVERRIDES_X86_64`)
  cuts every row. `POOL_MAX_AGE_HOURS` is already at its 2 h ceiling (the pool manager
  clamps anything higher), so it can't be raised to cut churn further; lowering it
  raises churn in proportion (1 h doubles the mint rows).
  `POOL_SIZE_OVERRIDES_X86_64='{"2048":0}'` (or `IMAGE_ARCHES=arm64`) drops the x86 rows.
- The x86_64 spare's churn is priced at x86 rates, like any x86 VM.
- The same churn drives the `ListMicrovms` pressure noted
  [above](#warm-pool-manager-optional).
- A granted spare's suspended time **before** the grant is attributed to the user's
  session cost on grant (the panel's "Suspended (snapshot storage)" line), not
  double-counted here, this table is only the *unclaimed* pool overhead.
- Rough rule of thumb: **~$2.40/GB-month of standing pool memory** at the 2 h recycle
  (~$2.30 of mint churn, ~$2.37 on x86_64, plus $0.08 of snapshot storage). Leave the
  pool off and this cost is $0.

See [Comparison vs EKS + EBS](#comparison-vs-eks--ebs-at-scale) above for the ~43%
per-user saving (the EKS control-plane fee is noted separately, not amortized into the
per-user figure).

---

## References

- [AWS Lambda MicroVMs Docs](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html)
- [Launch Blog Post](https://aws.amazon.com/blogs/aws/run-isolated-sandboxes-with-full-lifecycle-control-aws-lambda-introduces-microvms/)
- [MicroVM Pricing](https://aws.amazon.com/lambda/pricing/)

---

## License

This sample code is made available under the MIT-0 license (MIT No Attribution). See the [LICENSE](LICENSE) file.
