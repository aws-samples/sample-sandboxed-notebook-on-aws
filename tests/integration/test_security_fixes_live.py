#!/usr/bin/env python3
# test-kind: manual
"""
Live end-to-end security-fix verification — run against a RUNNING stack.

This exercises the ACTUAL request path (proxy on :8081, pool manager on :8090)
rather than unit-testing the mechanism in isolation. It exists because BOTH
regressions we shipped during the security work only surfaced on the live path
and passed the unit suites:

  * the cell-execute 401 (callers not sending X-Proxy-Auth), and
  * the terminal WebSocket `TypeError` (require_proxy_auth couldn't be injected
    on a WS route).

It covers every finding the fixes touched: C1 (loopback bind + auth gate),
C2 (CORS allowlist), C3 (WebSocket Origin+token gate), H1 (env values
write-only), H3 (/secrets/keys prefix), plus the pool-manager auth split and
the two regressions above. H2 (IAM narrowing) is not exercisable over HTTP —
it is an IAM policy — so this script only STATICALLY asserts the policy file is
narrowed (no `images/` write), and prints a note.

Design:
  * stdlib only (urllib + the `websockets` lib IF present; WS checks skip with a
    clear SKIP if it's absent). No new dependencies required for the HTTP checks.
  * Non-destructive: GET/OPTIONS and a single harmless POST to a gated route to
    observe the 401 (no VM is launched, nothing is deleted).
  * Discovers the per-run shared secret from the running proxy process's env, so
    you don't have to pass it. Override with PROXY_AUTH_TOKEN=... if needed.

Run (stack must be up, e.g. via ./local_install.sh):
    .venv/bin/python tests/integration/test_security_fixes_live.py

Exit code 0 = all non-skipped checks passed; 1 = at least one failed.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

PROXY = os.environ.get("PROXY_BASE", "http://127.0.0.1:8081")
POOL = os.environ.get("POOL_BASE", "http://127.0.0.1:8090")
UI_PORT = os.environ.get("UI_PORT", "5173")
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

AUTH_HEADER = "X-Proxy-Auth"
GOOD_ORIGIN = f"http://localhost:{UI_PORT}"
BAD_ORIGIN = "http://evil.example"

_results = []  # (name, "PASS"|"FAIL"|"SKIP", detail)


def record(name, status, detail=""):
    _results.append((name, status, detail))
    icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "–"}[status]
    print(f"  {icon} [{status}] {name}" + (f"  — {detail}" if detail else ""))


# ── token discovery ──────────────────────────────────────────────────────────
def discover_proxy_token():
    """Read PROXY_AUTH_TOKEN from env, else from the running proxy process's env
    (macOS `ps eww`). Returns the token string or '' if not found."""
    tok = os.environ.get("PROXY_AUTH_TOKEN", "")
    if tok:
        return tok
    try:
        # find the uvicorn proxy pid listening on 8081
        pid = subprocess.check_output(
            ["bash", "-lc", "lsof -ti :8081 | head -1"], text=True
        ).strip()
        if not pid:
            return ""
        env_blob = subprocess.check_output(["ps", "eww", pid], text=True)
        for part in env_blob.replace("\n", " ").split(" "):
            if part.startswith("PROXY_AUTH_TOKEN="):
                return part.split("=", 1)[1]
    except Exception:
        pass
    return ""


def discover_pool_token():
    tok = os.environ.get("POOL_AUTH_TOKEN", "")
    if tok:
        return tok
    try:
        pid = subprocess.check_output(
            ["bash", "-lc", "lsof -ti :8090 | head -1"], text=True
        ).strip()
        if not pid:
            return ""
        env_blob = subprocess.check_output(["ps", "eww", pid], text=True)
        for part in env_blob.replace("\n", " ").split(" "):
            if part.startswith("POOL_AUTH_TOKEN="):
                return part.split("=", 1)[1]
    except Exception:
        pass
    return ""


# ── tiny HTTP helper (returns (status, headers, body_text)) ──────────────────
def http(method, url, headers=None, body=None, timeout=15):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, {}, f"__error__ {type(e).__name__}: {e}"


# ============================================================================
# C1 — auth gate: unauth rejected, authed passes, /health exempt, fail-closed
# ============================================================================
def test_c1_auth_gate(token):
    print("\n== C1: proxy auth gate ==")
    if not token:
        record("C1 token discovered", "SKIP", "no PROXY_AUTH_TOKEN found; is the stack up with auth?")
        return

    # 1. Unauthenticated request to a gated route → 401
    s, _, _ = http("GET", f"{PROXY}/notebooks")
    record("C1 GET /notebooks WITHOUT token → 401", "PASS" if s == 401 else "FAIL", f"got {s}")

    # 2. Authenticated request → 200 (not 401)
    s, _, _ = http("GET", f"{PROXY}/notebooks", {AUTH_HEADER: token})
    record("C1 GET /notebooks WITH token → 200", "PASS" if s == 200 else "FAIL", f"got {s}")

    # 3. Wrong token → 401
    s, _, _ = http("GET", f"{PROXY}/notebooks", {AUTH_HEADER: "deadbeef-not-the-token"})
    record("C1 GET /notebooks with WRONG token → 401", "PASS" if s == 401 else "FAIL", f"got {s}")

    # 4. /health exempt → 200 WITHOUT a token (liveness)
    s, _, _ = http("GET", f"{PROXY}/health")
    record("C1 GET /health WITHOUT token → 200 (exempt)", "PASS" if s == 200 else "FAIL", f"got {s}")

    # 5. A gated POST without token → 401 (observe the gate BEFORE any handler
    #    logic; non-destructive — the request is rejected before doing anything).
    s, _, _ = http("POST", f"{PROXY}/launch", {"Content-Type": "application/json"}, "{}")
    record("C1 POST /launch WITHOUT token → 401 (rejected before handler)", "PASS" if s == 401 else "FAIL", f"got {s}")


# ============================================================================
# C1 regression — the EXECUTE path must carry auth (this is the one that 401'd)
# ============================================================================
def test_c1_execute_regression(token):
    print("\n== C1 regression: /proxy/execute* auth ==")
    if not token:
        record("execute-path checks", "SKIP", "no token")
        return
    # Without token → 401 (gate applies to the forwarder route too)
    s, _, _ = http("POST", f"{PROXY}/proxy/execute-batch",
                   {"Content-Type": "application/json"}, '{"cells":[]}')
    record("/proxy/execute-batch WITHOUT token → 401", "PASS" if s == 401 else "FAIL", f"got {s}")
    # With token → NOT 401 (reaches the handler; may 4xx/5xx for missing session,
    # but the point is the AUTH gate no longer blocks it — the regression was 401).
    s, _, _ = http("POST", f"{PROXY}/proxy/execute-batch",
                   {"Content-Type": "application/json", AUTH_HEADER: token,
                    "X-Session-Id": "nonexistent-session-for-test"}, '{"cells":[]}')
    ok = s is not None and s != 401
    record("/proxy/execute-batch WITH token → not 401 (regression fixed)", "PASS" if ok else "FAIL",
           f"got {s} (any non-401 is fine; session may be missing)")


# ============================================================================
# C2 — CORS allowlist: loopback origin allowed, foreign origin NOT reflected,
#      credentials off
# ============================================================================
def test_c2_cors(token):
    print("\n== C2: CORS allowlist ==")
    # Preflight (OPTIONS) from the ALLOWED UI origin → ACAO echoes it (or is set)
    s, h, _ = http("OPTIONS", f"{PROXY}/notebooks", {
        "Origin": GOOD_ORIGIN,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": AUTH_HEADER,
    })
    acao = h.get("access-control-allow-origin") or h.get("Access-Control-Allow-Origin")
    record("C2 preflight from UI origin allowed", "PASS" if acao in (GOOD_ORIGIN, "*") and acao != "*" else ("FAIL" if acao == "*" else "PASS" if acao == GOOD_ORIGIN else "FAIL"),
           f"ACAO={acao!r}")

    # Foreign origin → must NOT be reflected (no ACAO, or not the evil origin)
    s, h, _ = http("OPTIONS", f"{PROXY}/notebooks", {
        "Origin": BAD_ORIGIN,
        "Access-Control-Request-Method": "GET",
    })
    acao = h.get("access-control-allow-origin") or h.get("Access-Control-Allow-Origin")
    record("C2 foreign origin NOT reflected", "PASS" if acao != BAD_ORIGIN else "FAIL", f"ACAO={acao!r}")

    # Wildcard must be gone (the C2 bug was allow_origins=['*'])
    record("C2 wildcard '*' not used", "PASS" if acao != "*" else "FAIL", f"ACAO={acao!r}")

    # Credentials must be OFF (C2 dropped allow_credentials)
    acac = (h.get("access-control-allow-credentials") or
            h.get("Access-Control-Allow-Credentials") or "")
    record("C2 allow-credentials not 'true'", "PASS" if acac.lower() != "true" else "FAIL", f"ACAC={acac!r}")


# ============================================================================
# C3 — WebSocket terminal relay: Origin + token gate, and the require_proxy_auth
#      WS regression (handshake must NOT 500 with TypeError)
# ============================================================================
def test_c3_terminal_ws(token):
    print("\n== C3: terminal WebSocket gate ==")
    try:
        import asyncio
        import websockets  # noqa
    except Exception:
        record("C3 WebSocket checks", "SKIP",
               "`websockets` lib not importable; run inside the venv to exercise WS")
        return

    import asyncio
    import websockets
    from websockets.exceptions import InvalidStatus, InvalidStatusCode

    ws_base = PROXY.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_base}/ws/terminal?session_id=nonexistent-session"

    async def _try(headers=None, subprotocols=None):
        """Attempt a handshake. Return ('open'|'closed:<code>'|'rejected:<status>'|'error:<e>')."""
        try:
            async with websockets.connect(url, additional_headers=headers or {},
                                          subprotocols=subprotocols or [],
                                          open_timeout=8) as ws:
                # If it opened, close immediately — we only care that the gate
                # decision happened (open vs rejected), not the shell itself.
                await ws.close()
                return "open"
        except (InvalidStatus, InvalidStatusCode) as e:
            code = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", "?")
            return f"rejected:{code}"
        except websockets.exceptions.ConnectionClosed as e:
            return f"closed:{e.code}"
        except Exception as e:
            return f"error:{type(e).__name__}:{e}"

    loop = asyncio.new_event_loop()

    # 1. The REGRESSION check: a handshake must NOT produce a 500 (the TypeError).
    #    Missing Origin → the C3 gate closes 4403 (or rejects), which is fine; a
    #    500/InternalServerError is the regression and must NOT happen.
    r_noorigin = loop.run_until_complete(_try(headers={}))
    is_500 = "500" in r_noorigin or "InternalServerError" in r_noorigin
    record("C3 handshake does NOT 500 (require_proxy_auth WS regression)", "PASS" if not is_500 else "FAIL", r_noorigin)

    # 2. Missing Origin / bad origin → rejected (4403 close or non-open)
    record("C3 missing Origin → rejected (not open)", "PASS" if r_noorigin != "open" else "FAIL", r_noorigin)

    # 3. Allowed Origin but NO token → rejected (4403)
    r_notoken = loop.run_until_complete(_try(headers={"Origin": GOOD_ORIGIN}))
    record("C3 allowed Origin, NO token → rejected", "PASS" if r_notoken != "open" else "FAIL", r_notoken)

    # 4. Bad Origin even WITH token → rejected
    if token:
        r_badorigin = loop.run_until_complete(
            _try(headers={"Origin": BAD_ORIGIN}, subprotocols=[f"x-proxy-auth.{token}"]))
        record("C3 bad Origin + token → rejected", "PASS" if r_badorigin != "open" else "FAIL", r_badorigin)
    else:
        record("C3 bad Origin + token", "SKIP", "no token")

    loop.close()


# ============================================================================
# H1 — notebook env values are WRITE-ONLY: GET returns presence, never plaintext
# ============================================================================
def test_h1_env_write_only(token):
    print("\n== H1: env values write-only ==")
    if not token:
        record("H1 checks", "SKIP", "no token")
        return
    # Find a notebook id to probe. Prefer notebook 1..N; use /notebooks list.
    s, _, body = http("GET", f"{PROXY}/notebooks", {AUTH_HEADER: token})
    nb_id = None
    try:
        data = json.loads(body)
        items = data if isinstance(data, list) else data.get("notebooks", data)
        if isinstance(items, list) and items:
            nb_id = items[0].get("id")
    except Exception:
        pass
    if nb_id is None:
        nb_id = 1  # fall back to a likely id; the shape check still applies

    s, _, body = http("GET", f"{PROXY}/notebooks/{nb_id}/env", {AUTH_HEADER: token})
    if s != 200:
        record("H1 GET /notebooks/{id}/env reachable", "SKIP", f"got {s} for nb {nb_id}")
        return
    try:
        data = json.loads(body)
    except Exception:
        record("H1 env response is JSON", "FAIL", body[:120])
        return
    env = data.get("env_vars", {})
    # Every env entry must be a presence marker {"set": bool}, NOT a raw string value.
    bad = [k for k, v in env.items() if not (isinstance(v, dict) and "set" in v)]
    if not env:
        record("H1 env values write-only (no entries to check)", "PASS", "notebook has no stored env vars")
    else:
        record("H1 env entries are {set:bool} presence markers, not values",
               "PASS" if not bad else "FAIL",
               f"{len(env)} keys; offending={bad[:3]}")
    # Secrets must still be value-free (unchanged behavior)
    secrets = data.get("secrets", [])
    leaked = [x for x in secrets if isinstance(x, dict) and "value" in x]
    record("H1 secrets carry no value field", "PASS" if not leaked else "FAIL", f"{len(secrets)} secrets")


# ============================================================================
# H3 — GET /secrets/keys enforces the microvm/ prefix
# ============================================================================
def test_h3_secret_keys_prefix(token):
    print("\n== H3: /secrets/keys prefix guard ==")
    if not token:
        record("H3 checks", "SKIP", "no token")
        return
    # Non-microvm/ secret id → rejected with the prefix error, no AWS leak.
    s, _, body = http("GET", f"{PROXY}/secrets/keys?secret_id=prod-db-credentials", {AUTH_HEADER: token})
    rejected = False
    try:
        d = json.loads(body)
        rejected = (d.get("keys") == [] and "prefix" in (d.get("error") or "").lower())
    except Exception:
        pass
    record("H3 non-microvm/ secret_id rejected by prefix guard", "PASS" if rejected else "FAIL",
           f"status {s}, body {body[:120]}")

    # The env-master-key (the one the architecture protects hardest) must also be rejected.
    s, _, body = http("GET", f"{PROXY}/secrets/keys?secret_id=microvm-internal/env-master-key", {AUTH_HEADER: token})
    try:
        d = json.loads(body)
        rejected2 = (d.get("keys") == [] and "prefix" in (d.get("error") or "").lower())
    except Exception:
        rejected2 = False
    record("H3 env-master-key (microvm-internal/) rejected", "PASS" if rejected2 else "FAIL",
           f"body {body[:120]}")


# ============================================================================
# Pool manager auth split: liveness exempt, privileged routes gated
# ============================================================================
def test_pool_auth(pool_token):
    print("\n== Pool manager auth split ==")
    # Liveness/readiness/status must be reachable WITHOUT a token.
    for path in ("/health", "/status", "/ready"):
        s, _, _ = http("GET", f"{POOL}{path}")
        ok = s in (200, 503)  # /ready may 503 if below target; both mean "reached, unauth-allowed"
        record(f"Pool GET {path} without token → reachable ({s})", "PASS" if ok else "FAIL", f"got {s}")
    # A privileged route WITHOUT token → 401/403 (do NOT send a valid token; we
    # don't want to actually drain/refill the pool).
    s, _, _ = http("POST", f"{POOL}/admin/drain", {"Content-Type": "application/json"}, "{}")
    record("Pool POST /admin/drain WITHOUT token → 401 (gated, not executed)",
           "PASS" if s in (401, 403) else "FAIL", f"got {s}")


# ============================================================================
# H2 — static assertion only (IAM policy can't be exercised over HTTP)
# ============================================================================
def test_h2_iam_static():
    print("\n== H2: exec-role S3 write grant (static policy check) ==")
    path = os.path.join(REPO, "iam", "exec-role-permissions.json")
    try:
        policy = json.load(open(path))
    except Exception as e:
        record("H2 policy file loads", "FAIL", str(e))
        return
    write_stmt = None
    for st in policy.get("Statement", []):
        acts = st.get("Action", [])
        acts = acts if isinstance(acts, list) else [acts]
        if "s3:PutObject" in acts:
            write_stmt = st
            break
    if not write_stmt:
        record("H2 S3 write statement found", "FAIL", "no s3:PutObject statement")
        return
    res = write_stmt.get("Resource", [])
    res = res if isinstance(res, list) else [res]
    # No whole-bucket wildcard write, and images/ must NOT be writable.
    whole_bucket = any(r.rstrip("/").endswith("artifacts-*/*") or r.endswith("artifacts-*") for r in res)
    images_writable = any("/images/" in r or r.endswith("/images/*") for r in res)
    record("H2 write grant is NOT whole-bucket wildcard", "PASS" if not whole_bucket else "FAIL",
           f"resources={len(res)}")
    record("H2 images/ is NOT writable by the exec role", "PASS" if not images_writable else "FAIL",
           "boot artifact protected")
    expected = {"sessions/", "user-data/", "models/", "unload/", "sagemaker/", "attachments/", "athena-results/"}
    present = {p for p in expected if any(f"/{p}" in r for r in res)}
    record("H2 the 7 notebook-writable prefixes are present",
           "PASS" if present == expected else "FAIL", f"missing={sorted(expected - present)}")


def main():
    print("=" * 70)
    print("LIVE SECURITY-FIX VERIFICATION (C1/C2/C3, H1/H2/H3, pool auth)")
    print(f"  proxy={PROXY}  pool={POOL}  ui_port={UI_PORT}")
    print("=" * 70)

    # Preflight: is the proxy even up?
    s, _, _ = http("GET", f"{PROXY}/health")
    if s is None:
        print(f"\n❌ Proxy not reachable at {PROXY}. Start the stack (./local_install.sh) first.")
        sys.exit(2)

    proxy_token = discover_proxy_token()
    pool_token = discover_pool_token()
    if not proxy_token:
        print("\n⚠  Could not discover PROXY_AUTH_TOKEN (auth-dependent checks will SKIP).")
        print("   Pass it explicitly:  PROXY_AUTH_TOKEN=<tok> .venv/bin/python <this>")

    test_c1_auth_gate(proxy_token)
    test_c1_execute_regression(proxy_token)
    test_c2_cors(proxy_token)
    test_c3_terminal_ws(proxy_token)
    test_h1_env_write_only(proxy_token)
    test_h3_secret_keys_prefix(proxy_token)
    test_pool_auth(pool_token)
    test_h2_iam_static()

    passed = sum(1 for _, st, _ in _results if st == "PASS")
    failed = sum(1 for _, st, _ in _results if st == "FAIL")
    skipped = sum(1 for _, st, _ in _results if st == "SKIP")
    print("\n" + "=" * 70)
    print(f"RESULT: {passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        print("\nFAILURES:")
        for name, st, detail in _results:
            if st == "FAIL":
                print(f"  ✗ {name} — {detail}")
    print("=" * 70)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
