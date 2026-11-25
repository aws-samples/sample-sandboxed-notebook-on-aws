# Security

This is a **proof-of-concept** that runs on your own machine against your own AWS
account. It is intended for a single operator experimenting locally, not as a
multi-tenant or production service. This document describes the security model, the
controls that are in place today, the boundaries you should understand, and where
each control lives in the code.

## Contents

- [Reporting a vulnerability](#reporting-a-vulnerability)
- [Threat model](#threat-model)
- [Controls in place](#controls-in-place)
- [Where each control lives](#where-each-control-lives)
- [Boundaries you should understand](#boundaries-you-should-understand)
- [Network egress](#network-egress)
- [Before exposing this beyond your laptop](#before-exposing-this-beyond-your-laptop)

## Reporting a vulnerability

If you discover a potential security issue in this project, please notify AWS
Security via the [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
or email <aws-security@amazon.com>. Please do **not** create a public GitHub issue
for security reports.

## Threat model

Each notebook session runs untrusted user code inside its **own Firecracker MicroVM**.
Hardware-level VM isolation is the primary security boundary: a compromise inside a
notebook is confined to that user's own session and VM. The host-side control plane
(proxy, warm-pool manager, AI agent) runs on your laptop under your own AWS
credentials and is **loopback-only** by design.

## Controls in place

### Local-only binding

The proxy and the warm-pool manager bind `127.0.0.1`. The installer sets
`PROXY_BIND_HOST` and `POOL_BIND_HOST` to `127.0.0.1`. At startup each service calls a
guard that refuses to run on a non-loopback host unless you set
`ALLOW_NON_LOOPBACK_BIND=1`, or the process runs in a container (`/.dockerenv` present
or `DOCKER_CONTAINER` set), where the published port is the boundary. The pool manager
`__main__` entry point additionally hardcodes `127.0.0.1`. Container `CMD`s bind
`0.0.0.0` inside the container boundary and are reachable only through the proxy.

### Authenticated local APIs

- Every proxy route requires the `X-Proxy-Auth` header. The check is an app-level
  dependency, so a route cannot opt out. The only exempt route is `GET /health`.
- The warm-pool manager requires the same header, checked against a separate token, on
  `POST /grant` and `POST /admin/refill` and `POST /admin/drain`. `/status`, `/health`
  and `/ready` are unauthenticated, which is one reason the pool manager stays on
  loopback.
- Tokens are compared in constant time (`hmac.compare_digest`). If the server-side
  token is unset or empty, the proxy and pool manager answer 503 and authenticate no
  one (fail closed). A missing or wrong header gets 401.
- `./local_install.sh` mints a fresh 32-byte hex `PROXY_AUTH_TOKEN` and
  `POOL_AUTH_TOKEN` on every run (`openssl rand`, falling back to Python `secrets`) and
  passes them through process environment variables. The installer does not write them
  to disk. The web UI receives the proxy token as `VITE_PROXY_TOKEN` in the Vite dev
  server environment, so it is present in the JavaScript the browser loads.
- This is a local, single-operator mechanism. It is not an identity system.

### Server-to-server calls carry the shared secret

Calls between the services use the same header. The proxy sends `POOL_AUTH_TOKEN` to
the pool manager `/grant` endpoint. The proxy-side AI tools and the Notebook
Intelligence context fetch send `PROXY_AUTH_TOKEN` when they call back into the proxy.
The AI agent does the same for its execution tools.

### Scoped CORS, and WebSocket origin and token checks

- The proxy CORS allowlist contains only `http://localhost:<UI_PORT>` and
  `http://127.0.0.1:<UI_PORT>` (default port 5173). Credentialed cross-origin access is
  off, methods and headers are enumerated, and there is no wildcard.
- The terminal WebSocket checks the `Origin` header (a missing origin is rejected) and
  the shared secret, and closes with code 4403 on failure. Both checks run before the
  server resolves the session to a VM or wakes it. The token is accepted as an
  `x-proxy-auth.<token>` subprotocol, with a `?token=` query parameter as a fallback.

### Session ownership on terminate

`POST /terminate` requires a `microvmId`. When the request also names a session and
the VM belongs to a different session, the proxy returns **409** before any checkpoint,
DAG flush or termination. A stale browser tab therefore cannot checkpoint over, or
terminate, another notebook's VM. The Connect dialog lists only the VMs that belong to
the tab's own session.

### Environment variables encrypted at rest

Notebook environment-variable values are sealed before they are written to the
database and unsealed on read:

- AES-256-GCM, a fresh random 12-byte nonce per value, and a per-notebook key derived
  with HKDF-SHA256 (salt is the notebook id) from a 32-byte master key.
- The master key is stored in AWS Secrets Manager as `microvm-internal/env-master-key`,
  outside the `microvm/` prefix. The VM execution role can read only `microvm/*`
  secrets, so a VM cannot read the master key.
- Encryption fails closed: if the master key cannot be loaded, the proxy refuses to
  save the env var instead of writing plaintext. Decryption is best-effort per value,
  so one unreadable legacy value does not break a notebook launch.
- Protects against someone who can read only the database (the SQLite file or the
  Aurora replica). It does not protect against a compromised proxy host, because the
  proxy must decrypt values to inject them into the VM.
- Secrets Manager entries (the other half of the Secrets and Env panel) are stored as
  references only, so no secret value is written to the database. The dropdowns list
  only `microvm/*` secrets.

### SSRF guard on import-from-URL

The import endpoint (`POST /import-from-url` in
`proxy/notebook/routes/notebooks.py`) fetches a user-supplied URL, so it validates the
URL first. Only `http` and `https` are allowed. The host must resolve, and every
resolved address must be public: private, loopback, link-local (including the instance
metadata address), reserved, multicast and unspecified addresses are rejected. The
check runs again after the GitHub blob-to-raw rewrite, and redirects are not followed.
Residual risk: the name is resolved again at connect time, so a hostile DNS server
could return a different address (DNS rebinding). There is no connection-level IP pinning.

### S3 delete guards

- **Data-source panel deletes** are allowed only when the object is in the artifact
  bucket and its key starts with a prefix in `DATASOURCE_S3_DELETABLE_PREFIXES`
  (default `user-data/`). The route returns 403 otherwise, so sample data and arbitrary
  keys cannot be deleted from the UI.
- **Prefix deletes** (session, notebook and chart-image cleanup) are built only from
  validated ids: session ids must match a strict pattern and notebook ids must be
  positive integers. An invalid id yields no prefix, never a broader one. Listed keys
  are also re-checked to start with the prefix before deletion.
- **Notebook delete** removes the notebook's chart images (`notebook-outputs/<id>/`),
  its attachments (`attachments/<id>/`), and for each session the notebook owned the
  checkpoint (`sessions/<sid>/`), attachments (`attachments/<sid>/`) and promoted
  files (`user-data/<sid>/`). The shared `user-data/` area is not touched. S3 cleanup
  is best-effort and never fails the database delete.
- **Orphan chart images** are deleted only after the proxy re-reads the notebook's
  referenced keys and skips any key that is referenced again, still uploading, or
  uploaded within a short grace window.

### Logging redaction in the pool manager

When a grant fails, the error text can echo the request body. The pool manager masks
every env-var value and every secret reference value from the grant body (values of
four or more characters) with `***` before logging or returning the message. Grant
log lines carry only the session id, architecture, size and notebook id.

### Least-privilege VM execution role

The in-VM execution role (`iam/exec-role-permissions.json`) is scoped by resource:

- S3 write and delete is limited to the `sessions/`, `user-data/`, `models/`,
  `unload/`, `sagemaker/`, `attachments/` and `athena-results/` prefixes of the
  artifact bucket. Read is bucket-wide, so the VM can read but not overwrite the
  prefixes it boots from.
- Athena is limited to the `microvm-demo` workgroup. The role includes
  `athena:StopQueryExecution` so a timeout or Stop in the notebook can cancel the
  running query natively instead of leaving it to run and bill.
- Secrets Manager read is limited to `microvm/*`. Amazon DynamoDB read is limited to
  the demo table name patterns. SageMaker job, model and endpoint actions are limited
  to `microvm-` named resources.
- Some discovery actions use `*` because the services do not support resource-level
  scoping for them (for example `dynamodb:ListTables`, Amazon Bedrock discovery, and
  `lakeformation:GetDataAccess`).

### Confirm-before-execute AI assistant

With `AI_AGENT_AUTO_EXECUTE` off (the default in `scripts/config.sh`), the assistant
proposes an action and waits for you to approve it in the UI. Nothing the model
suggests executes without a click. Setting it to `true` removes that gate.

### Hash-pinned host dependencies

`proxy/requirements.txt` is a generated lockfile with a hash for every package, and
`local_install.sh` installs it with `--require-hashes`. The AI agent environment is
exported with hashes and installed the same way. To change a dependency, edit the
`.in` source and regenerate the lock (see the header of `proxy/requirements.txt`).

## Where each control lives

| Control | Location |
|---|---|
| Loopback guard, token check, CORS allowlist | `proxy/platform/auth.py` |
| Proxy app-level auth and CORS | `proxy/server.py` |
| Pool manager auth, loopback bind | `pool_manager/server.py` |
| Token minting, bind hosts | `local_install.sh` |
| Terminal WebSocket origin and token check | `proxy/platform/routes/terminal.py` |
| Session ownership 409 on terminate | `proxy/platform/routes/microvm.py` |
| Env-var encryption | `proxy/platform/env_crypto.py` |
| Secrets Manager name filter | `proxy/platform/routes/sessions.py` |
| SSRF guard | `proxy/notebook/routes/_url_guard.py`, used by `proxy/notebook/routes/notebooks.py` |
| Panel delete prefix guard | `proxy/platform/datasources/s3.py` |
| Session and notebook prefix deletes | `proxy/notebook/session_checkpoints.py` |
| Chart image deletes | `proxy/notebook/cell_images.py` |
| Grant log redaction | `pool_manager/manager.py` |
| Pool grant auth header | `proxy/platform/microvm_manager.py` |
| VM execution role | `iam/exec-role-permissions.json` |
| Confirm-before-execute | `agent/ai_assistant/tools/execution_tools.py` |

## Boundaries you should understand

These are deliberate properties of a local, single-operator POC, not oversights:

- **Code runs with full capability inside the VM.** Executing arbitrary Python and SQL
  is the product. Isolation comes from the Firecracker VM, not from sandboxing within it.
- **The host services use your AWS credentials.** A local process cannot assume a role
  without a credential source, so it acts as you. Run it in a sandbox or development
  account, not a production admin account.
- **Anything with the shared secret can control the proxy.** The proxy token is in the
  UI's JavaScript and in the process environment, so another process running as your
  user can read it.
- **No multi-user authentication, per-user quotas, or audit attribution.** Adding real
  authentication is a prerequisite before exposing the services beyond localhost.
- **The local database is not encrypted at rest.** Only env-var values are sealed
  (see above). Notebook content, outputs and chat history are stored in plaintext.
  Rely on full-disk encryption on the laptop.
- **Pool manager `/status`, `/health` and `/ready` are unauthenticated.**
- **Import-from-URL is exposed to DNS rebinding** as described above.

## Network egress

By default MicroVMs have unrestricted outbound internet access through the
`INTERNET_EGRESS` connector, so `pip`, `git` and public APIs work in notebooks.
No egress filtering ships in this POC. Two Layer-7 approaches for restricting
destinations by domain are documented in [NETWORK_EGRESS.md](NETWORK_EGRESS.md).

## Before exposing this beyond your laptop

Do not put these services on a network, a shared host, or the public internet without
first adding real authentication, authorization and transport security appropriate to
that context. The loopback bind and the local shared secret are safeguards for the
single-operator case. They are not a substitute for an identity system.
