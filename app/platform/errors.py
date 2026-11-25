"""
Centralized error handling + observability for the in-VM sandbox FastAPI app.

VM-side twin of proxy/platform/errors.py — the proxy and the VM app are separate
deployables and can't share a module, so the logic is duplicated here.

Entry point:

  install_error_handling(app, namespace)  — app-level catch-all. Any exception
      that escapes ANY route handler is caught once, mapped to an HTTP status,
      logged with a full traceback, and recorded as a structured metric line.
      Covers every route (and future routes) without per-handler edits.

Observability is a STRUCTURED LOG LINE (JSON) that a CloudWatch metric filter turns
into a metric — no extra dependencies, no change to the baked image beyond this
code, and upgradeable to aws_embedded_metrics (EMF) later by changing
emit_error_metric() alone. VM logs already ship to CloudWatch (the Logs panel).

Response shape matches the rest of the app: JSONResponse with {"error": ...}.
Explicit Response/JSONResponse returns pass through untouched; FastAPI's own
HTTPException / validation errors keep their built-in handling.
"""

import json
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Known exception → HTTP status. Anything not listed → 500. Grow as needed.
KNOWN_EXCEPTIONS = {
    ValueError: 400,
    KeyError: 400,
    LookupError: 400,
    PermissionError: 403,
    FileNotFoundError: 404,
    NotImplementedError: 501,
    TimeoutError: 504,
}

# Greppable prefix for a CloudWatch metric filter, e.g. { $.metric = "UnhandledErrors" }.
_METRIC_MARKER = "APP_METRIC"


def status_for(exc: Exception) -> int:
    """HTTP status for an exception: exact type, then MRO, else 500."""
    t = type(exc)
    if t in KNOWN_EXCEPTIONS:
        return KNOWN_EXCEPTIONS[t]
    for base, code in KNOWN_EXCEPTIONS.items():
        if isinstance(exc, base):
            return code
    return 500


def emit_error_metric(namespace: str, handler: str, error_type: str,
                      status: int, method: str = "", path: str = "",
                      metric: str = "UnhandledErrors") -> None:
    """Emit one structured metric line. The ONLY metric-producing site, so the
    EMF upgrade is local to this function. Best-effort; never raises.

    `metric` defaults to "UnhandledErrors" (what the app-level catch-all records
    for genuinely UNHANDLED exceptions — the alarmed metric).
    A HANDLED/degraded condition worth tracking should pass its OWN metric name so
    it stays dashboardable without inflating UnhandledErrors; each distinct metric
    needs its own CloudWatch metric filter (see local_install.sh)."""
    try:
        logger.info("%s %s", _METRIC_MARKER, json.dumps({
            "metric": metric,
            "value": 1,
            "unit": "Count",
            "namespace": namespace,
            "handler": handler,
            "error_type": error_type,
            "status": status,
            "method": method,
            "path": path,
        }))
    except Exception:
        pass


def install_error_handling(app, namespace: str) -> None:
    """Register an app-level catch-all covering every route in one place."""

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        if isinstance(exc, StarletteHTTPException):
            raise exc
        status = status_for(exc)
        handler = getattr(request.scope.get("endpoint", None), "__name__", "unknown")
        method = request.method
        path = request.url.path
        logger.exception("Unhandled %s in %s (%s %s)",
                         type(exc).__name__, handler, method, path)
        emit_error_metric(namespace, handler, type(exc).__name__, status, method, path)
        detail = "Internal server error." if status >= 500 else str(exc)
        return JSONResponse(status_code=status, content={"error": detail})
