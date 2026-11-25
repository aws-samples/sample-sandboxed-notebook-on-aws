#!/usr/bin/env bash
# ============================================================
# local_stop_app.sh — Close the local Sandboxed Notebook on AWS app
# ============================================================
# The counterpart to local_install.sh. In order:
#
#   1. INVENTORY  every live MicroVM of THIS app's image family
#                 (IMAGE_ARN-<arch>-<size>), classified as notebook VM / pool
#                 spare / orphan, and ask for confirmation.
#   2. DELETE     every RUNNING notebook (one with a live VM) exactly as the UI's
#                 "Delete permanently" does: POST /terminate for its VM (the proxy
#                 checkpoints, then terminates), then DELETE /notebooks/{id}
#                 (cells, versions, DAG, env, chat, and the workbook-intel /
#                 local-file-entity / session-registry rows of every session the
#                 notebook owned; run + VM cost history is kept — SQLite or
#                 Postgres — plus the AgentCore Memory purge for that memory
#                 backend, the chart images under notebook-outputs/<id>/ and
#                 the S3 checkpoint prefix sessions/<sid>/ of every owned
#                 session). If the proxy is down, the same storage call is made
#                 directly after step 3.
#                 Notebooks WITHOUT a live VM (closed/suspended-away) are kept.
#                 Use --keep-notebooks to only terminate their VMs (checkpointed).
#   3. STOP       every local service: proxy (8081), AI agent (8082), pool
#                 manager (8090), web UI (5173), backend (8080), the
#                 SQLite→Aurora sync worker (no port) and the local_install.sh
#                 parent. SIGTERM first, SIGKILL after a grace period.
#   4. TERMINATE  every remaining live VM of this app's image family (pool
#                 spares, orphans, notebook VMs the proxy couldn't handle), then
#                 mark them TERMINATED / remove them from pool_vms in the local DB.
#
# Unlike scripts/teardown.sh it does NOT delete the MicroVM images, IAM roles,
# buckets or data, so the next local_install.sh starts fast. It only touches VMs
# launched from this app's images — never other MicroVMs in the account.
#
# Usage:
#   ./local_stop_app.sh                 # interactive: shows what it will do, asks first
#   ./local_stop_app.sh --yes           # no prompt
#   ./local_stop_app.sh --dry-run       # show the plan, change nothing
#   ./local_stop_app.sh --keep-notebooks # keep running notebooks: VMs are checkpointed
#                                  # + terminated, notebooks stay (offer Restore)
#   ./local_stop_app.sh --keep-vms      # stop local services only, leave VMs + notebooks
#   ./local_stop_app.sh --clear-all-ui-storage # wipe ALL the app's browser data (step 6)
#   ./local_stop_app.sh --no-ui-cleanup # skip step 6
#   ./local_stop_app.sh --ui-cleanup-only [--clear-all-ui-storage]
#                                  # ONLY step 6 (app must already be stopped)
#
#   6. BROWSER    The UI keeps its open notebook tabs in the browser's localStorage
#                 and its autosave RECREATES a notebook it finds missing — so a
#                 deleted notebook still open in a tab would come back. After the
#                 services are down, the script briefly serves a stand-in page on
#                 http://localhost:5173 (loopback only, same origin as the UI) and
#                 opens it in your browser; that page removes the saved tab list
#                 ('microvm-notebooks', 'microvm-active-tab'). An app tab that is
#                 still open reloads onto it by itself (the script answers Vite's
#                 reconnect ping), so its in-memory tabs can't write them back.
#                 --clear-all-ui-storage instead sends the standard
#                 `Clear-Site-Data: "storage"` header, wiping ALL the app's browser
#                 data (theme, panel prefs, Intel card ticks too). Runs when
#                 notebooks were deleted, or whenever --clear-all-ui-storage is given.
# ============================================================
set -uo pipefail   # no -e: every step is best-effort and reports its own failures

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

ASSUME_YES=false; DRY_RUN=false; DELETE_NOTEBOOKS=true; KEEP_VMS=false
CLEAR_ALL_UI=false; UI_CLEANUP=true; UI_CLEANUP_ONLY=false
for arg in "$@"; do
  case "$arg" in
    -y|--yes)               ASSUME_YES=true ;;
    --dry-run)              DRY_RUN=true ;;
    --keep-notebooks)       DELETE_NOTEBOOKS=false ;;
    --keep-vms)             KEEP_VMS=true; DELETE_NOTEBOOKS=false ;;
    --clear-all-ui-storage) CLEAR_ALL_UI=true ;;
    --no-ui-cleanup)        UI_CLEANUP=false ;;
    --ui-cleanup-only)      UI_CLEANUP_ONLY=true ;;
    -h|--help)              sed -n '2,54p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $arg (see --help)"; exit 2 ;;
  esac
done

# shellcheck source=scripts/config.sh
source "$ROOT_DIR/scripts/config.sh" >/dev/null   # AWS_REGION, IMAGE_ARN, ports, aws_mvm
PYTHON="$ROOT_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || { echo "✗ $PYTHON not found — run local_install.sh once first"; exit 1; }
PORTS=("${BACKEND_PORT:-8080}" "${PROXY_PORT:-8081}" "${AI_PORT:-8082}" "${POOL_MANAGER_PORT:-8090}" 5173)
GRACE_SECONDS=10

say()  { echo "$@"; }
step() { echo ""; echo ">> $*"; }
do_or_echo() { if $DRY_RUN; then echo "   [dry-run] $*"; else "$@"; fi; }

# --- Python helper: list this app's live VMs (stdin = list-microvms JSON) and
# classify them against the local DB; or mark VMs terminated in the DB. Uses the
# app's own storage layer so writes match what the proxy would record. ---------
# The code is passed with -c (NOT a heredoc on stdin) so stdin stays free for the
# data piped into the helper (the list-microvms JSON / ids to mark or delete).
db_helper() {
  STORAGE_BACKEND="$STORAGE_BACKEND" STORAGE_CONNECTION="$STORAGE_CONNECTION" \
  MEMORY_BACKEND="${MEMORY_BACKEND:-}" AGENTCORE_MEMORY_ID="${AGENTCORE_MEMORY_ID:-}" \
  AWS_REGION="$AWS_REGION" IMAGE_ARN="$IMAGE_ARN" "$PYTHON" -c "$DB_HELPER_PY" "$@"
}
# read -d '' (not "$(cat <<'PY' …)"): macOS /bin/bash 3.2 mis-parses apostrophes
# in a heredoc nested inside $( ). read returns 1 at EOF — expected, hence || true.
read -r -d '' DB_HELPER_PY <<'PY' || true
import json, logging, os, sys
logging.disable(logging.CRITICAL)
from proxy.storage import storage

mode = sys.argv[1]
if mode == "inventory":
    data = json.load(sys.stdin)
    items = data.get("items") or []
    prefix = os.environ["IMAGE_ARN"] + "-"
    dead = {"TERMINATED", "TERMINATING", "FAILED"}
    sessions = {s["microvm_id"]: s for s in storage.vm_session_list_active()}
    pool = {r["vm_id"] for r in storage.pool_vm_list()}
    for it in items:
        vid, state = it.get("microvmId", ""), it.get("state", "")
        if state in dead or not str(it.get("imageArn", "")).startswith(prefix):
            continue
        s = sessions.get(vid)
        nbid = "-"
        if s:
            kind, sid, name = "notebook", s.get("session_id") or "-", s.get("notebook_name") or "-"
            if s.get("notebook_id") is not None:
                nbid = str(s["notebook_id"])
                if name == "-":
                    nb = storage.notebook_get(nbid) or {}
                    name = nb.get("name") or "-"
        elif vid in pool:
            kind, sid, name = "spare", "-", "-"
        else:
            kind, sid, name = "orphan", "-", "-"
        size = str(it.get("imageArn", "")).rsplit("-", 1)[-1]
        # name LAST: `read` assigns any leftover text to the final variable.
        print("\t".join([vid, kind, state, size, sid, nbid, name.replace("\t", " ")]))
elif mode == "delete-notebooks":
    # Fallback when the proxy is down: the SAME storage.notebook_delete the proxy's
    # DELETE /notebooks/{id} calls (via astorage), so it gets the same cascade:
    # cells, versions, DAG, env, chat, and the workbook-intel / local-file-entity /
    # session-registry rows of every session the notebook owned (run + VM cost
    # history is kept). Plus what that route does after the DB delete: the
    # AgentCore Memory purge (agentcore memory backend), the chart images under
    # notebook-outputs/<id>/, attachments/<id>/, and the S3 prefixes
    # sessions/<sid>/, attachments/<sid>/ and user-data/<sid>/ of every session
    # the notebook owned (storage.notebook_owned_session_ids, read BEFORE the
    # delete: no other surviving notebook references it).
    memory_backend = os.environ.get("MEMORY_BACKEND", "")
    for nbid in (l.strip() for l in sys.stdin):
        if not nbid:
            continue
        try:
            nb = storage.notebook_get(nbid) or {}
            try:
                owned = storage.notebook_owned_session_ids(nbid)
            except Exception as e:
                owned = []
                print(f"   ⚠ owned sessions for notebook {nbid}: {e}", file=sys.stderr)
            ok = storage.notebook_delete(nbid)
            if ok and memory_backend == "agentcore" and nb.get("session_id"):
                try:
                    from proxy.notebook.ai.memory import delete_transcript
                    delete_transcript(nb["session_id"])
                except Exception as e:
                    print(f"   ⚠ AgentCore Memory purge for notebook {nbid}: {e}", file=sys.stderr)
            if ok:
                # Chart PNGs under s3://$ARTIFACT_BUCKET/notebook-outputs/<id>/ (the
                # proxy route's prefix delete). DB first (above); best-effort.
                try:
                    from proxy.notebook.cell_images import delete_notebook_prefix_sync
                    if not os.environ.get("ARTIFACT_BUCKET"):
                        print(f"   ⚠ chart images for notebook {nbid}: ARTIFACT_BUCKET unset, "
                              f"objects under notebook-outputs/{nbid}/ not deleted", file=sys.stderr)
                    else:
                        delete_notebook_prefix_sync(nbid)
                except Exception as e:
                    print(f"   ⚠ chart images for notebook {nbid}: {e}", file=sys.stderr)
            if ok and owned:
                # S3 data of every owned session: sessions/<sid>/ (checkpoints),
                # attachments/<sid>/ and user-data/<sid>/ (the proxy route's
                # session-prefix delete). Best-effort.
                try:
                    from proxy.notebook.session_checkpoints import delete_owned_session_prefixes_sync
                    if not os.environ.get("ARTIFACT_BUCKET"):
                        print(f"   ⚠ session data for notebook {nbid}: ARTIFACT_BUCKET unset, "
                              f"objects under sessions/, attachments/, user-data/<sid>/ of "
                              f"{len(owned)} session(s) not deleted", file=sys.stderr)
                    else:
                        res = delete_owned_session_prefixes_sync(owned, os.environ["ARTIFACT_BUCKET"])
                        for sid, r in res.items():
                            if not isinstance(r, int):
                                print(f"   ⚠ session data of {sid} for notebook {nbid}: {r}",
                                      file=sys.stderr)
                except Exception as e:
                    print(f"   ⚠ session data for notebook {nbid}: {e}", file=sys.stderr)
            if ok:
                # Chat attachments keyed by the notebook id (attachments/<id>/).
                try:
                    from proxy.notebook.session_checkpoints import delete_notebook_data_sync
                    if os.environ.get("ARTIFACT_BUCKET"):
                        delete_notebook_data_sync(nbid, os.environ["ARTIFACT_BUCKET"])
                except Exception as e:
                    print(f"   ⚠ attachments for notebook {nbid}: {e}", file=sys.stderr)
            print(f"   {'✓' if ok else '•'} notebook {nbid} ({nb.get('name') or '?'}): "
                  f"{'deleted from the DB' if ok else 'not found (already deleted)'}")
        except Exception as e:
            print(f"   ✗ notebook {nbid}: delete failed: {e}", file=sys.stderr)
elif mode == "mark":
    for vid in (l.strip() for l in sys.stdin):
        if not vid:
            continue
        try:
            storage.vm_session_mark_terminated(vid)   # no-op if no row / already terminated
        except Exception as e:
            print(f"   ⚠ DB mark {vid}: {e}", file=sys.stderr)
        try:
            storage.pool_vm_delete(vid)               # no-op if not a spare
        except Exception as e:
            print(f"   ⚠ DB pool delete {vid}: {e}", file=sys.stderr)
PY

list_vms_json() { aws_mvm list-microvms --output json 2>/dev/null || echo '{"items": []}'; }

# --- Is the app open in a browser? -------------------------------------------
# Every open app page holds a long-lived connection to the UI dev server (Vite's
# hot-reload websocket), so ESTABLISHED sockets owned by the UI-port listener =
# open browser pages. Prints "<count> <client app names>"; "0" when none / UI down.
ui_open_clients() {
  local vite_pid n names
  vite_pid="$(lsof -nP -tiTCP:5173 -sTCP:LISTEN 2>/dev/null | head -1)"
  [ -z "$vite_pid" ] && { echo 0; return; }
  n="$(lsof -nP -a -p "$vite_pid" -iTCP -sTCP:ESTABLISHED 2>/dev/null | awk 'NR>1' | wc -l | tr -d ' ')"
  names="$(lsof -nP -iTCP:5173 -sTCP:ESTABLISHED 2>/dev/null \
           | awk -v v="$vite_pid" 'NR>1 && $2!=v {print $1}' | sort -u | tr '\n' ' ')"
  echo "${n:-0} ${names}"
}

# --- Step 6 helper: a one-shot stand-in server on the UI origin ------------------
# Serves ONLY a static cleanup page (no input is processed beyond counting
# requests), bound to loopback (127.0.0.1 and ::1) on the UI port, for at most
# UI_CLEAN_TIMEOUT seconds. Same origin as the UI (http://localhost:5173), so the
# page can edit the app's localStorage / the header applies to it. It also
# completes the WebSocket handshake for Vite's "vite-ping" reconnect probe, so an
# app tab that's still open reloads onto this page by itself.
#   argv: <targeted|all> <timeout_s> <grace_s> <port>
#   stdout: one event per line (PING / PAGE / CLEARED ok=0|1); exit 0 if a page
#   reported it cleared the storage, 3 if nothing did before the timeout.
read -r -d '' UI_CLEAN_PY <<'PY' || true
import base64, hashlib, json, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

mode, timeout, grace, port = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
if mode == "all":
    js, what = "localStorage.clear();sessionStorage.clear();", "all of the app's saved data"
else:
    js = "localStorage.removeItem('microvm-notebooks');localStorage.removeItem('microvm-active-tab');"
    what = "the app's saved notebook tabs"
PAGE = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Sandboxed Notebook on AWS - stopped</title><style>"
        "body{margin:0;height:100vh;display:grid;place-items:center;background:#1c1c1e;color:#f5f5f7;"
        "font:15px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
        "main{max-width:34em;text-align:center;padding:24px}p{color:#d1d1d6;line-height:1.5}"
        "</style></head><body><main><h1>Sandboxed Notebook on AWS is stopped</h1>"
        "<p id='s' role='status'>Clearing " + what + " in this browser&hellip;</p></main><script>"
        "var ok=true;try{" + js + "}catch(e){ok=false}"
        "fetch('/__cleared?ok='+(ok?1:0),{method:'POST',cache:'no-store'}).catch(function(){});"
        # json.dumps -> a valid JS string literal (the text contains apostrophes)
        "document.getElementById('s').textContent=ok?"
        + json.dumps("Done: cleared " + what + ". You can close this tab.") + ":"
        + json.dumps("Could not clear the browser storage.") + ";"
        "</script></body></html>").encode()
state = {"cleared": False, "last": time.time()}
lock = threading.Lock()

def event(msg):
    with lock:
        state["last"] = time.time()
        print(msg, flush=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass
    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            # Vite reconnect probe: finish the handshake so the old tab reloads.
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            proto = self.headers.get("Sec-WebSocket-Protocol", "")
            if proto:
                self.send_header("Sec-WebSocket-Protocol", proto.split(",")[0].strip())
            self.end_headers()
            self.close_connection = True
            event("PING")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if mode == "all":
            self.send_header("Clear-Site-Data", '"storage"')
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)
        event("PAGE")
    def do_HEAD(self):
        self.send_response(200); self.send_header("Cache-Control", "no-store"); self.end_headers()
    def do_POST(self):
        ok = "ok=1" in self.path
        self.send_response(204); self.send_header("Cache-Control", "no-store"); self.end_headers()
        if ok:
            state["cleared"] = True
        event(f"CLEARED ok={1 if ok else 0}")

class V6(ThreadingHTTPServer):
    address_family = socket.AF_INET6

servers = []
for cls, host in ((ThreadingHTTPServer, "127.0.0.1"), (V6, "::1")):
    try:
        s = cls((host, port), Handler)
        s.daemon_threads = True
        threading.Thread(target=s.serve_forever, daemon=True).start()
        servers.append(s)
    except OSError as e:
        print(f"BINDFAIL {host}:{port} {e}", flush=True)
if not servers:
    sys.exit(4)
start = time.time()
while time.time() - start < timeout:
    with lock:
        idle = time.time() - state["last"]
    if state["cleared"] and idle >= grace:
        break
    time.sleep(0.2)
for s in servers:
    s.shutdown()
sys.exit(0 if state["cleared"] else 3)
PY

UI_PORT_NUM=5173
UI_CLEAN_TIMEOUT=45
UI_CLEAN_RESULT=""   # cleared | not-confirmed | skipped

clear_browser_state() {
  local mode="$1" url="http://localhost:${UI_PORT_NUM}/" log rc srv_pid opened=false
  if lsof -nP -tiTCP:"$UI_PORT_NUM" -sTCP:LISTEN >/dev/null 2>&1; then
    say "   ⚠ port $UI_PORT_NUM is still in use — can't serve the cleanup page; skipped"
    UI_CLEAN_RESULT="skipped"; return
  fi
  log="$(mktemp -t local_stop_ui.XXXXXX)"
  "$PYTHON" -c "$UI_CLEAN_PY" "$mode" "$UI_CLEAN_TIMEOUT" 4 "$UI_PORT_NUM" >"$log" 2>&1 &
  srv_pid=$!
  # An app tab that's still open polls for the dev server and reloads onto this
  # page by itself (if it's the visible tab) — give it a few seconds first.
  for _ in 1 2 3 4 5 6; do grep -q '^CLEARED ok=1' "$log" && break; sleep 1; done
  if ! grep -q '^CLEARED ok=1' "$log"; then
    if [ "$(uname -s)" = "Darwin" ]; then open "$url" >/dev/null 2>&1 && opened=true
    elif command -v xdg-open >/dev/null 2>&1 && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
      xdg-open "$url" >/dev/null 2>&1 && opened=true
    fi
    if $opened; then say "   Opened $url in your default browser to clear its saved tabs."
    else say "   Open $url in the browser you use for the app (waiting up to ${UI_CLEAN_TIMEOUT}s)…"; fi
    say "   (If the app is open in a DIFFERENT browser, switch to that tab — it reloads onto the page.)"
  fi
  wait "$srv_pid"; rc=$?
  local pings pages
  pings=$(grep -c '^PING' "$log"); pages=$(grep -c '^PAGE' "$log")
  if [ "$rc" -eq 0 ]; then
    UI_CLEAN_RESULT="cleared"
    say "   ✓ browser state cleared ($pages page load(s); $pings open app tab(s) reloaded)"
  else
    UI_CLEAN_RESULT="not-confirmed"
    grep '^BINDFAIL' "$log" | sed 's/^/   ⚠ /'
    say "   ⚠ no browser confirmed the cleanup within ${UI_CLEAN_TIMEOUT}s"
  fi
  rm -f "$log"
}

# --- PIDs of the app's local services (scoped to THIS checkout) ----------------
proc_cwd() {   # cwd of a PID: /proc on Linux, lsof on macOS
  if [ -e "/proc/$1/cwd" ]; then readlink "/proc/$1/cwd" 2>/dev/null
  else lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1; fi
}
service_pids() {
  {
    for p in "${PORTS[@]}"; do lsof -nP -tiTCP:"$p" -sTCP:LISTEN 2>/dev/null; done
    # Port-less or not-yet-listening processes, matched by command AND by running
    # from this checkout (so a second checkout / worktree is never touched).
    for pat in "local_install.sh" "uvicorn proxy.server" "uvicorn pool_manager.server" \
               "ai_assistant.agent_runtime" "sync.sync_to_aurora" "pool_manager.reset_local_pool" \
               "node .*vite"; do
      for pid in $(pgrep -f "$pat" 2>/dev/null); do
        case "$(proc_cwd "$pid")" in "$ROOT_DIR"|"$ROOT_DIR"/*) echo "$pid" ;; esac
      done
    done
  } | grep -vx "$$" | sort -u
}

# =============================================================================
echo "============================================"
echo "  Sandboxed Notebook on AWS — Stop app"
echo "============================================"
echo "  Region:  $AWS_REGION    Image family: ${IMAGE_ARN##*:}-*"
$DRY_RUN && echo "  Mode:    DRY RUN (nothing will be changed)"

# --- --ui-cleanup-only: just step 6, then exit --------------------------------
if $UI_CLEANUP_ONLY; then
  if lsof -nP -tiTCP:"$UI_PORT_NUM" -sTCP:LISTEN >/dev/null 2>&1; then
    say ""; say "✗ The app's UI is still running on port $UI_PORT_NUM — stop the app first (./local_stop_app.sh)."
    exit 1
  fi
  step "Clearing $( $CLEAR_ALL_UI && echo "ALL of the app's browser data" || echo "the app's saved notebook tabs" ) in the browser"
  if $DRY_RUN; then say "   [dry-run] serve the cleanup page on http://localhost:${UI_PORT_NUM}/"; exit 0; fi
  clear_browser_state "$( $CLEAR_ALL_UI && echo all || echo targeted )"
  [ "$UI_CLEAN_RESULT" = "cleared" ] && exit 0 || exit 1
fi

# --- 1. Inventory -------------------------------------------------------------
step "Inventory"
INVENTORY=""
if ! $KEEP_VMS; then
  INVENTORY="$(list_vms_json | db_helper inventory)"
fi
n_kind() { [ -z "$INVENTORY" ] && echo 0 || printf '%s\n' "$INVENTORY" | awk -F'\t' -v k="$1" '$2==k' | wc -l | tr -d ' '; }
N_NB=$(n_kind notebook); N_SPARE=$(n_kind spare); N_ORPHAN=$(n_kind orphan)
SVC_PIDS="$(service_pids)"
N_SVC=$( [ -z "$SVC_PIDS" ] && echo 0 || printf '%s\n' "$SVC_PIDS" | wc -l | tr -d ' ')

PROXY_UP=false
lsof -nP -tiTCP:"${PROXY_PORT:-8081}" -sTCP:LISTEN >/dev/null 2>&1 && PROXY_UP=true

say "   Local service processes: $N_SVC $($PROXY_UP && echo '(proxy is up)' || echo '(proxy is down)')"
if $KEEP_VMS; then
  say "   MicroVMs: left running (--keep-vms)"
else
  say "   Notebook VMs: $N_NB    Pool spares: $N_SPARE    Orphans: $N_ORPHAN"
  if [ -n "$INVENTORY" ]; then
    printf '%s\n' "$INVENTORY" | while IFS=$'\t' read -r vid kind state size sid nbid name; do
      printf '     %-8s %-9s %5s MB  %s%s\n' "$kind" "$state" "$size" "$vid" \
        "$( [ "$kind" = notebook ] && echo "  (notebook $nbid: $name)" )"
    done
  fi
  if [ "$N_NB" -gt 0 ]; then
    if $DELETE_NOTEBOOKS; then
      say "   ⚠ The $N_NB running notebook(s) above will be DELETED — same as the UI's"
      say "     \"Delete permanently\": cells, version history, runs and chat are removed."
      $PROXY_UP || say "     (proxy is down: VMs are terminated WITHOUT a final checkpoint, then the"
      $PROXY_UP || say "      notebooks are deleted directly in the DB)"
      if $UI_CLEANUP; then
        read -r UI_N UI_APPS <<< "$(ui_open_clients)"
        [ "${UI_N:-0}" -gt 0 ] && say "     The app is open in a browser (${UI_APPS% }); at the end its saved tabs are"
        [ "${UI_N:-0}" -gt 0 ] && say "     cleared so the deleted notebooks can't come back (step 6)."
      fi
    elif $PROXY_UP; then
      say "   Running notebooks are KEPT; their VMs are checkpointed to S3, then terminated."
    else
      say "   Running notebooks are KEPT. ⚠ Proxy is down, so their VMs can't be checkpointed"
      say "     now — anything since their last automatic checkpoint is lost."
    fi
  fi
fi

if [ "$N_SVC" -eq 0 ] && { $KEEP_VMS || [ -z "$INVENTORY" ]; }; then
  say ""; say "Nothing to stop."; exit 0
fi

if ! $DRY_RUN && ! $ASSUME_YES; then
  if [ -t 0 ]; then
    if $DELETE_NOTEBOOKS && [ "$N_NB" -gt 0 ]; then
      read -r -p $'\nProceed? Deleting notebooks and terminating VMs cannot be undone. [y/N] ' reply
    else
      read -r -p $'\nProceed? Terminating VMs cannot be undone. [y/N] ' reply
    fi
    case "$reply" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 1 ;; esac
  else
    echo "Not a terminal — re-run with --yes to proceed."; exit 1
  fi
fi

# Notebook ids still to delete in the DB after the services stop (proxy down or a
# proxy DELETE that failed). One id per line.
PENDING_NB_DELETES=""

# --- 2. Running notebooks: the UI's Delete (or keep + checkpoint) via the proxy -
if ! $KEEP_VMS && [ "$N_NB" -gt 0 ]; then
  NB_ROWS="$(printf '%s\n' "$INVENTORY" | awk -F'\t' '$2=="notebook"')"
  if $DELETE_NOTEBOOKS; then
    step "Deleting running notebooks (same as the UI's \"Delete permanently\")"
  else
    step "Checkpointing + terminating notebook VMs (notebooks kept)"
  fi
  TOKEN=""
  if $PROXY_UP; then
    PROXY_PID="$(lsof -nP -tiTCP:"${PROXY_PORT:-8081}" -sTCP:LISTEN 2>/dev/null | head -1)"
    # The proxy's per-run auth token, read from its own environment (never printed).
    TOKEN="$(ps eww -p "$PROXY_PID" 2>/dev/null | tr ' ' '\n' | sed -n 's/^PROXY_AUTH_TOKEN=//p' | head -1)"
    [ -z "$TOKEN" ] && say "   ⚠ couldn't read the proxy auth token — falling back to direct terminate + DB delete"
  fi
  # Process substitution (not a pipe) so PENDING_NB_DELETES survives the loop.
  while IFS=$'\t' read -r vid kind state size sid nbid name; do
    [ -z "$vid" ] && continue
    [ "$sid" = "-" ] && sid=""
    [ "$nbid" = "-" ] && nbid=""
    if $DRY_RUN; then
      echo "   [dry-run] POST /terminate $vid (checkpoint, then terminate)"
      $DELETE_NOTEBOOKS && [ -n "$nbid" ] && echo "   [dry-run] DELETE /notebooks/$nbid ($name)"
      continue
    fi
    if [ -n "$TOKEN" ]; then
      # 1/2 Terminate the SPECIFIC VM (the UI's delete path): the proxy checkpoints
      # to S3 first, then terminates and clears the session routing.
      resp="$(curl -s -m 180 -X POST "http://127.0.0.1:${PROXY_PORT:-8081}/terminate" \
               -H "X-Proxy-Auth: $TOKEN" -H "Content-Type: application/json" \
               ${sid:+-H "X-Session-Id: $sid"} \
               -d "{\"microvmId\": \"$vid\"}" 2>/dev/null)"
      if printf '%s' "$resp" | grep -q '"terminated"'; then
        say "   ✓ $vid: checkpointed + terminated"
      else
        say "   ⚠ $vid: proxy terminate failed — will terminate directly in step 4"
      fi
      # 2/2 Delete the notebook through the same route the UI calls.
      if $DELETE_NOTEBOOKS && [ -n "$nbid" ]; then
        resp="$(curl -s -m 60 -X DELETE "http://127.0.0.1:${PROXY_PORT:-8081}/notebooks/$nbid" \
                 -H "X-Proxy-Auth: $TOKEN" 2>/dev/null)"
        if printf '%s' "$resp" | grep -q '"success": *true'; then
          say "   ✓ notebook $nbid ($name): deleted"
        else
          say "   ⚠ notebook $nbid ($name): proxy delete failed — will delete in the DB directly"
          PENDING_NB_DELETES="${PENDING_NB_DELETES}${nbid}"$'\n'
        fi
      fi
    else
      # No proxy: the VM is terminated in step 4 (no final checkpoint possible),
      # and the notebook row is deleted in the DB once the services are down.
      $DELETE_NOTEBOOKS && [ -n "$nbid" ] && PENDING_NB_DELETES="${PENDING_NB_DELETES}${nbid}"$'\n'
    fi
  done < <(printf '%s\n' "$NB_ROWS")
  unset TOKEN
fi

# --- 3. Stop local services -----------------------------------------------------
step "Stopping local services"
SVC_PIDS="$(service_pids)"
if [ -z "$SVC_PIDS" ]; then
  say "   none running"
else
  # local_install.sh first: its cleanup trap stops its children gracefully.
  for pid in $SVC_PIDS; do
    say "   SIGTERM $pid  $(ps -o command= -p "$pid" 2>/dev/null | cut -c1-90)"
    do_or_echo kill -TERM "$pid" 2>/dev/null
  done
  if ! $DRY_RUN; then
    for _ in $(seq 1 "$GRACE_SECONDS"); do
      [ -z "$(service_pids)" ] && break
      sleep 1
    done
    LEFT="$(service_pids)"
    if [ -n "$LEFT" ]; then
      for pid in $LEFT; do say "   SIGKILL $pid (still running after ${GRACE_SECONDS}s)"; kill -KILL "$pid" 2>/dev/null; done
      sleep 1
    fi
  fi
fi

# --- 4. Terminate remaining VMs + update the local DB --------------------------
if ! $KEEP_VMS; then
  step "Terminating remaining MicroVMs (this app's image family only)"
  REMAINING="$(list_vms_json | db_helper inventory)"
  if [ -z "$REMAINING" ]; then
    say "   none left"
  else
    printf '%s\n' "$REMAINING" | while IFS=$'\t' read -r vid kind state size sid nbid name; do
      if $DRY_RUN; then echo "   [dry-run] terminate $vid ($kind)"; continue; fi
      if aws_mvm terminate-microvm --microvm-identifier "$vid" >/dev/null 2>&1; then
        say "   ✓ $vid ($kind): terminate requested"
      else
        say "   ✗ $vid ($kind): terminate FAILED — retry with ./local_stop_app.sh or scripts/teardown.sh"
      fi
    done
    if ! $DRY_RUN; then
      say "   Waiting for TERMINATED state..."
      for _ in $(seq 1 18); do
        [ -z "$(list_vms_json | db_helper inventory)" ] && break
        sleep 5
      done
    fi
  fi
fi

# Mark EVERY VM this run handled as terminated in the local DB — the ones killed
# above AND the notebook VMs the proxy terminated in step 2 (the proxy leaves its
# vm_sessions row for the next /instances poll to mark, and a granted spare's
# pool_vms row for the next pool reset — neither runs once the services are down).
if ! $KEEP_VMS && ! $DRY_RUN; then
  printf '%s\n%s\n' "$INVENTORY" "${REMAINING:-}" | cut -f1 | grep . | sort -u | db_helper mark
fi

# --- 5. Notebook deletes the proxy couldn't do (direct DB, same storage call) --
# Runs with the services DOWN, so nothing re-saves the notebook behind us. With the
# SQLite backend the delete is tombstoned and reaches Aurora on the next sync run;
# with STORAGE_BACKEND=postgres it's applied to Aurora directly.
if [ -n "$PENDING_NB_DELETES" ]; then
  step "Deleting notebooks in the DB ($STORAGE_BACKEND)"
  if $DRY_RUN; then
    printf '%s' "$PENDING_NB_DELETES" | sed 's/^/   [dry-run] delete notebook /'
  else
    printf '%s' "$PENDING_NB_DELETES" | sort -u | db_helper delete-notebooks
  fi
fi

# --- 6. Clear the app's saved tabs in the browser ----------------------------
# Needed only when notebooks were deleted (so an open/saved tab can't recreate
# them), or when the user asks for a full wipe. Services are down by now, so the
# UI port is free for the stand-in page.
NB_DELETED=false
$DELETE_NOTEBOOKS && [ "${N_NB:-0}" -gt 0 ] && ! $KEEP_VMS && NB_DELETED=true
if $UI_CLEANUP && { $NB_DELETED || $CLEAR_ALL_UI; }; then
  if $CLEAR_ALL_UI; then
    step "Clearing ALL of the app's browser data (Clear-Site-Data: \"storage\")"
  else
    step "Clearing the app's saved notebook tabs in the browser"
  fi
  if $DRY_RUN; then
    say "   [dry-run] serve a cleanup page on http://localhost:${UI_PORT_NUM}/ (loopback only, ≤${UI_CLEAN_TIMEOUT}s)"
    say "   [dry-run] it removes $( $CLEAR_ALL_UI && echo "ALL app storage" || echo "'microvm-notebooks' + 'microvm-active-tab'" ); open tabs reload onto it"
  else
    clear_browser_state "$( $CLEAR_ALL_UI && echo all || echo targeted )"
  fi
fi

# --- Summary -----------------------------------------------------------------
step "Result"
if $DRY_RUN; then say "   dry run — nothing was changed"; exit 0; fi
LEFT_SVC="$(service_pids)"
for p in "${PORTS[@]}"; do
  lsof -nP -tiTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1 && say "   ✗ port $p still in use" || say "   ✓ port $p free"
done
[ -z "$LEFT_SVC" ] && say "   ✓ no app processes left (incl. sync worker)" || say "   ✗ still running: $LEFT_SVC"
if ! $KEEP_VMS; then
  LEFT_VMS="$(list_vms_json | db_helper inventory)"
  if [ -z "$LEFT_VMS" ]; then
    say "   ✓ no live MicroVMs left for this app"
  else
    say "   ⚠ still shutting down: $(printf '%s\n' "$LEFT_VMS" | cut -f1 | tr '\n' ' ')"
  fi
fi
case "$UI_CLEAN_RESULT" in
  cleared) say "   ✓ browser: the app's saved tabs are cleared — deleted notebooks won't come back" ;;
  not-confirmed|skipped)
    say ""
    say "   ⚠ The browser cleanup wasn't confirmed. If a deleted notebook was still open in a"
    say "     tab, it would be recreated when the app next loads. Either re-run:"
    say "       ./local_stop_app.sh --ui-cleanup-only   (with the app's browser tab open and visible)"
    say "     or clear it by hand: http://localhost:5173 → DevTools → Application → Local Storage"
    say "       → delete the 'microvm-notebooks' and 'microvm-active-tab' keys." ;;
esac
say ""
say "Images, IAM roles, buckets and data are kept. Start again with: bash local_install.sh [--AuroraSetup] [--WarmPool]"
say "Full teardown (also deletes images): bash scripts/teardown.sh"
