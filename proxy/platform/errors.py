"""
Centralized error handling + observability for the proxy's FastAPI app.

Entry points:

  install_error_handling(app, namespace)  — app-level catch-all. Registers an
      exception handler so ANY exception that escapes ANY route handler is caught
      once, mapped to an HTTP status, logged with a full traceback, and recorded
      as a structured metric line. This is how we get "all routes wrapped" without
      touching every handler — and it also covers routes added later.

  json_error(status, msg)  — the one builder for an explicit error response a
      route returns itself. Always use it instead of formatting JSON by hand:
      the message is serialized with json.dumps, so quotes and newlines in an
      exception text cannot produce an invalid body.

Observability today is a STRUCTURED LOG LINE (JSON) that a CloudWatch metric filter
turns into a metric — no extra dependencies, no image rebuild. The emitter is
isolated in emit_error_metric() so it can be swapped for aws_embedded_metrics (EMF)
later without touching call sites.

Error response shape: {"error": "<message>"} (json_error and the catch-all).
Explicit Response/JSONResponse returns from handlers are values, not exceptions, so
they pass through untouched. FastAPI's own HTTPException / RequestValidationError are
handled by FastAPI's built-in handlers and are intentionally NOT swallowed here; they
render as {"detail": ...} (the auth dependency uses HTTPException), and the web
error reader accepts both shapes.
"""

import json
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Map known exception types to HTTP status codes. Anything not listed → 500.
# Seeded with stdlib types the code actually raises; grow this as domain
# exceptions are introduced (e.g. a future NotFoundError → 404).
KNOWN_EXCEPTIONS = {
    ValueError: 400,
    KeyError: 400,
    LookupError: 400,
    PermissionError: 403,
    FileNotFoundError: 404,
    NotImplementedError: 501,
    TimeoutError: 504,
}

# A single, easily-greppable prefix so a CloudWatch metric filter can match the
# line. Filter pattern example: { $.metric = "UnhandledErrors" }
_METRIC_MARKER = "APP_METRIC"


def json_error(status: int, msg: str, **extra) -> JSONResponse:
    """Error response with the shared {"error": msg} body and the given status.
    `extra` adds fields next to "error" (for example the empty "keys" list a
    reader keeps rendering, or the VM "state")."""
    return JSONResponse(status_code=status, content={**extra, "error": str(msg)})


def status_for(exc: Exception) -> int:
    """HTTP status for an exception: exact-type match first, then MRO (so a
    subclass of a known type inherits its status), else 500."""
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
    """Emit one structured metric line for an error condition.

    `metric` defaults to "UnhandledErrors" — the metric the app-level catch-all
    records for genuinely UNHANDLED exceptions (the one a
    CloudWatch alarm watches). A HANDLED/degraded condition that is still worth
    tracking (e.g. a best-effort resync push that timed out but was recovered)
    should pass its OWN metric name so it is dashboardable WITHOUT inflating the
    alarmed UnhandledErrors count. Each distinct metric needs its own CloudWatch
    metric filter (see local_install.sh) to become a real metric.

    This is the ONLY place metrics are produced, so upgrading to EMF/CloudWatch
    metrics later is a change here alone. Best-effort: never raise from telemetry.
    """
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
    """Register an app-level catch-all so every route is covered in one place."""

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        # Starlette/FastAPI HTTPExceptions carry an intended status + detail;
        # re-raise so FastAPI's own handler formats them (don't turn a 404 into a
        # 500). Note: in practice FastAPI routes HTTPException to its own handler
        # before reaching here, but this guard keeps behavior correct if it doesn't.
        if isinstance(exc, StarletteHTTPException):
            raise exc
        status = status_for(exc)
        handler = getattr(request.scope.get("endpoint", None), "__name__", "unknown")
        method = request.method
        path = request.url.path
        # Full traceback for debugging (replaces silent swallowing).
        logger.exception("Unhandled %s in %s (%s %s)",
                         type(exc).__name__, handler, method, path)
        emit_error_metric(namespace, handler, type(exc).__name__, status, method, path)
        # 5xx are hidden behind a generic message (don't leak internals); mapped
        # 4xx may surface the exception text since it's a client-caused condition.
        detail = "Internal server error." if status >= 500 else str(exc)
        return JSONResponse(status_code=status, content={"error": detail})
