"""
WebSocket terminal relay — interactive shell access to MicroVMs.

Part of: proxy.platform (Smart MicroVM Service layer)

Architecture:
  Browser (xterm.js) ←WS→ Proxy (this relay) ←WSS→ Lambda MicroVM (SHELL_INGRESS)

The SHELL_INGRESS connector provides a platform-managed bash PTY inside the VM.
Auth is passed via WebSocket subprotocols (not HTTP headers) per the Lambda
MicroVM protocol spec.

Protocol:
  1. Browser connects: ws://<proxy host:port>/ws/terminal?session_id=<uuid>,
     offering the subprotocol x-proxy-auth.<PROXY_AUTH_TOKEN> (a ?token= query
     param is also accepted for clients that cannot set a subprotocol).
  2. Before accept(): the Origin must be on the UI allowlist and the token must
     match; a rejected caller never gets a socket and never wakes a VM.
  3. Proxy resolves session → VM and wakes a suspended VM (waits for RUNNING),
     then gets a shell auth token.
  4. Proxy connects to VM: wss://<endpoint>/ws/shell with subprotocol auth
     (retried while the shell comes up).
  5. VM sends session_init JSON → forwarded to browser
  6. Bidirectional relay: browser keystrokes → VM, PTY output → browser
  7. On disconnect (browser or VM): cancel other direction, close both
"""

import asyncio
import functools
import logging
import ssl

import certifi
import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from proxy.platform import resume_wait
from proxy.platform.auth import (
    WS_SUBPROTOCOL_PREFIX,
    allowed_origins,
    is_token_valid,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])

# Retry config for connecting to VM shell (VM may be resuming from suspend)
MAX_CONNECT_RETRIES = 3
FIRST_MSG_TIMEOUT = 5  # seconds to wait for session_init or session_error
# Seconds between shell connect attempts: base * attempt (2 s, 4 s) after a
# session_error from the VM, a flat base (2 s) after a connection exception.
RETRY_BACKOFF_BASE = 2

# Proactive-resume config: how long we wait for a suspended VM to reach RUNNING
# before attaching the shell. Reads follow the shared resume schedule
# (proxy/platform/resume_wait.py: 0.3 s, 0.25 s steps to 3 s, then 1 s).
RESUME_WAIT_BUDGET_SEC = 30.0


async def _ensure_vm_awake(vm_manager, microvm_id: str, browser_ws: WebSocket) -> bool:
    """Proactively resume a SUSPENDED VM and wait until it is RUNNING before we
    attach the interactive shell.

    Opening the terminal used to rely purely on the SHELL_INGRESS connection
    triggering AWS auto-resume, plus a shallow error-gated retry — a race that
    intermittently attached the shell to a not-yet-resumed guest (keystrokes hit
    a suspended VM). This mirrors what the execute path and the /resume route do:
    read authoritative control-plane state, resume if needed, and poll to RUNNING.

    Returns True if the VM is (or became) RUNNING/alive enough to attach, False if
    it's genuinely dead/gone (caller should bail). A status line is streamed to the
    browser terminal while we wait, so the user sees "resuming" instead of a hang.
    """
    try:
        state = await vm_manager.get_vm_state(microvm_id)
    except Exception as e:
        # State read inconclusive — don't block; let the shell-connect retry handle it.
        logger.warning(f"Terminal: could not read state for {microvm_id}: {e}")
        return True

    if state in ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"):
        return False  # genuinely gone — nothing to attach to

    if state not in ("SUSPENDED", "SUSPENDING", "RESUMING", "PENDING"):
        return True  # RUNNING (or UNKNOWN) — proceed straight to attach

    # Tell the user we're waking the VM (dim text; the real banner comes later).
    try:
        await browser_ws.send_text('\x1b[2m  Waking the notebook sandbox…\x1b[0m\r\n')
    except Exception:
        pass

    # SUSPENDED gets an explicit resume call; RESUMING/PENDING are moving to
    # RUNNING, so we just poll them. SUSPENDING settles at SUSPENDED, so the
    # shared wait resumes once it reads SUSPENDED (also after a failed resume).
    st = await resume_wait.wake_and_wait(
        state, lambda: vm_manager.get_vm_state(microvm_id),
        lambda: vm_manager.resume_vm(microvm_id), RESUME_WAIT_BUDGET_SEC,
        dead_states=("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"), retry_errors=True,
        vm_id=microvm_id, who="Terminal", log=logger)
    if st == "RUNNING":
        return True
    if st in ("TERMINATED", "TERMINATING", "FAILED", "NOT_FOUND"):
        return False
    # Didn't reach RUNNING in time — attach anyway and let the shell-connect retry
    # ride out the last bit of resume (better than closing on the user).
    return True


@functools.lru_cache(maxsize=1)
def _create_ssl_context() -> ssl.SSLContext:
    """Create SSL context with proper CA verification (Amazon Trust CAs).

    Built once (loading the certifi bundle is ~10-30 ms of blocking CPU) and
    reused for every terminal connect; verification settings are unchanged."""
    return ssl.create_default_context(cafile=certifi.where())


async def _connect_to_vm_shell(
    ws_url: str,
    subprotocols: list[str],
    ssl_context: ssl.SSLContext,
    browser_ws: WebSocket,
) -> websockets.WebSocketClientProtocol | None:
    """
    Connect to the VM's shell endpoint with retry logic.
    
    Retries handle the case where the VM is resuming from suspend —
    the shell isn't available immediately after auto-resume.
    
    Returns the connected VM WebSocket, or None if all retries failed.
    The first message (session_init) is forwarded to the browser.
    """
    vm_ws = None

    for attempt in range(MAX_CONNECT_RETRIES):
        try:
            vm_ws = await websockets.connect(
                ws_url, subprotocols=subprotocols, ssl=ssl_context, open_timeout=10
            )

            # Read first message — either session_init (success) or session_error
            first_msg = await asyncio.wait_for(vm_ws.recv(), timeout=FIRST_MSG_TIMEOUT)

            if isinstance(first_msg, str) and '"session_error"' in first_msg:
                logger.warning(
                    f"Shell not ready (attempt {attempt + 1}/{MAX_CONNECT_RETRIES}): "
                    f"{first_msg[:100]}"
                )
                await vm_ws.close()
                vm_ws = None

                if attempt < MAX_CONNECT_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF_BASE * (attempt + 1))
                    continue
                else:
                    # Final retry failed — forward error to browser
                    await browser_ws.send_text(first_msg)
                    return None
            else:
                # Success — forward session_init to browser
                if isinstance(first_msg, bytes):
                    await browser_ws.send_bytes(first_msg)
                else:
                    await browser_ws.send_text(first_msg)
                return vm_ws

        except Exception as e:
            logger.warning(f"Shell connect attempt {attempt + 1}/{MAX_CONNECT_RETRIES} failed: {e}")
            if vm_ws:
                await vm_ws.close()
                vm_ws = None
            if attempt < MAX_CONNECT_RETRIES - 1:
                await asyncio.sleep(RETRY_BACKOFF_BASE)
            else:
                raise

    return None


async def _relay_browser_to_vm(
    browser_ws: WebSocket,
    vm_ws: websockets.WebSocketClientProtocol,
):
    """Relay keystrokes from browser (xterm.js) to VM shell."""
    try:
        while True:
            msg = await browser_ws.receive()
            if msg.get("text"):
                await vm_ws.send(msg["text"])
            elif msg.get("bytes"):
                await vm_ws.send(msg["bytes"])
    except (WebSocketDisconnect, Exception):
        pass


async def _relay_vm_to_browser(
    vm_ws: websockets.WebSocketClientProtocol,
    browser_ws: WebSocket,
):
    """Relay PTY output from VM shell to browser (binary or text frames)."""
    try:
        async for message in vm_ws:
            if isinstance(message, bytes):
                await browser_ws.send_bytes(message)
            else:
                await browser_ws.send_text(message)
    except Exception:
        pass


@router.websocket("/ws/terminal")
async def ws_terminal_relay(websocket: WebSocket, session_id: str = ""):
    """
    WebSocket endpoint: relays an interactive terminal session between
    the browser and a MicroVM's platform shell.
    
    Query params:
      session_id — the notebook session (maps to a specific VM)
      token      — fallback for the x-proxy-auth.<token> subprotocol
    The Origin must be on the UI allowlist and the token must match, both
    checked before accept() (see the Origin + shared-secret gate below).
    """
    if not session_id:
        await websocket.close(code=4001, reason="session_id query param required")
        return

    # --- Origin + shared-secret gate (BEFORE accept() and BEFORE any VM
    # resume). The relay is reachable by any local browser tab, so an attacker
    # page could otherwise open ws://localhost/ws/terminal and both resume a
    # suspended VM (cost + state impact) and attach to the shell. We reject
    # before websocket.accept() so a rejected caller never gets an open socket,
    # and before _ensure_vm_awake so a rejected caller can never wake a VM.
    #
    # (a) Origin must be present AND on the loopback UI allowlist. Browsers
    #     always send Origin on a WS handshake; a missing Origin is a non-browser
    #     client and is rejected too.
    origin = websocket.headers.get("origin")
    if not origin or origin not in allowed_origins():
        await websocket.close(code=4403, reason="origin not allowed")
        return

    # (b) Token via a WS subprotocol `x-proxy-auth.<token>` (mirroring the
    #     outbound `lambda-microvms.authentication.<token>` pattern used to reach
    #     the VM shell), with a ?token= query-param fallback for clients that
    #     cannot set a subprotocol.
    accept_subprotocol = None
    supplied_token = ""
    for proto in websocket.headers.get("sec-websocket-protocol", "").split(","):
        proto = proto.strip()
        if proto.startswith(WS_SUBPROTOCOL_PREFIX):
            supplied_token = proto[len(WS_SUBPROTOCOL_PREFIX):]
            accept_subprotocol = proto
            break
    if not supplied_token:
        supplied_token = websocket.query_params.get("token", "")

    if not is_token_valid(supplied_token):
        await websocket.close(code=4403, reason="invalid or missing token")
        return

    # Resolve session → VM
    vm_manager = websocket.app.state.vm_manager
    session_vm = await vm_manager.get_session_vm_async(session_id)

    if not session_vm:
        await websocket.close(code=4004, reason="Session not found")
        return

    endpoint = session_vm["endpoint"]
    microvm_id = session_vm["vm_id"]

    # Accept the browser connection FIRST so we can stream a "waking…" status line
    # to the terminal while a suspended VM resumes (below), instead of a silent hang.
    # Echo back the auth subprotocol the client offered (if any) — a browser that
    # sends a Sec-WebSocket-Protocol expects the server to confirm one of them.
    if accept_subprotocol is not None:
        await websocket.accept(subprotocol=accept_subprotocol)
    else:
        await websocket.accept()
    logger.info(f"Terminal WebSocket: browser connected for session {session_id}")

    # PROACTIVELY RESUME a suspended VM and wait for RUNNING before attaching the
    # shell. Without this the terminal relied on the shell connection racing AWS
    # auto-resume, which intermittently attached to a still-suspended guest (the
    # reported "opened terminal but VM didn't wake" bug). Mirrors /resume + the
    # execute path's wake-on-traffic behavior.
    awake = await _ensure_vm_awake(vm_manager, microvm_id, websocket)
    if not awake:
        try:
            await websocket.send_text('\r\n\x1b[31m Sandbox is no longer available (it may have been terminated). '
                                      'Re-run a cell to launch a fresh one.\x1b[0m\r\n')
        except Exception:
            pass
        await websocket.close(code=4004, reason="VM not alive")
        return

    # Get shell-specific auth token (requires SHELL_INGRESS connector on the VM)
    try:
        token = await vm_manager.get_shell_auth_token(microvm_id)
    except Exception as e:
        await websocket.close(code=4003, reason=f"Shell token error: {e}")
        return

    # Build WebSocket URL and subprotocol auth
    ws_url = f"wss://{endpoint}/ws/shell"
    subprotocols = [
        "lambda-microvms",
        f"lambda-microvms.authentication.{token}",
    ]

    # Register this relay so the app's graceful-shutdown handler can close it
    # cleanly on an ECS deploy/drain (otherwise the terminal is hard-cut).
    active_terminals = getattr(websocket.app.state, "active_terminals", None)
    if active_terminals is not None:
        active_terminals.add(websocket)

    try:
        ssl_context = _create_ssl_context()

        # Connect to VM shell (with retry for suspended VMs)
        vm_ws = await _connect_to_vm_shell(ws_url, subprotocols, ssl_context, websocket)
        if not vm_ws:
            return

        logger.info(f"Terminal WebSocket: connected to VM {microvm_id} shell")

        # Bidirectional relay — cancel the other direction when one ends
        tasks = [
            asyncio.create_task(_relay_browser_to_vm(websocket, vm_ws)),
            asyncio.create_task(_relay_vm_to_browser(vm_ws, websocket)),
        ]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

        await vm_ws.close()

    except Exception as e:
        logger.warning(f"Terminal WebSocket error: {e}")
    finally:
        if active_terminals is not None:
            active_terminals.discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"Terminal WebSocket: closed for session {session_id}")
