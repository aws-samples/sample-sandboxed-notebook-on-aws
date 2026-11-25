"""Cell run limits shared by the executor, the run guards and the helpers.

Lives in its own module so run_guard.py and helpers/_athena.py can use the
timeout budget and the error texts without importing executor.py (which imports
nothing from them, but is large and imported first). executor.py re-exports these
names, so `from app.notebook.executor import TIMEOUT_ERROR` keeps working.
"""
import os

# Read at CALL time, never at import: the module is imported into the Firecracker
# snapshot at image-build time, before the /run hook injects the launch env vars.
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 60

# Error texts of a cell stopped by the timeout timer or by POST /interrupt
# (Python cells and SQL cells alike, see run_guard.py).
TIMEOUT_ERROR = "Execution timed out after {seconds}s — possible infinite loop or long-running computation"
INTERRUPT_ERROR = "Execution interrupted by user"


def execution_timeout_seconds() -> int:
    """Max seconds a single cell may run before it's forcefully interrupted."""
    try:
        return int(os.environ.get("EXECUTION_TIMEOUT_SECONDS", DEFAULT_EXECUTION_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_EXECUTION_TIMEOUT_SECONDS
