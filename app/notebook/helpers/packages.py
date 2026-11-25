"""UTILITIES — install/uninstall Python packages from a cell.

`install_package("scikit-learn")` does EXACTLY what the Packages side-panel does:
it pip-installs into the running sandbox AND records the package in the checkpoint
manager so it survives suspend/restore (a bare `pip install` from a cell would be
lost when the VM is rotated). Use it when a cell needs a library that isn't baked
into the image — e.g. `install_package("xgboost")` before loading an xgboost model,
or `install_package("skl2onnx")` before an ONNX export.

Runtime wiring: the server registers the process's executor + checkpoint manager
here at startup (see server.py -> _register_runtime), mirroring how data_catalog
exposes its active catalog. The helper then drives the SAME code path as POST
/install, so cell-installed packages behave identically to panel-installed ones.
"""

import logging

logger = logging.getLogger(__name__)

# Process singletons, injected by server.py at startup so this helper can reach the
# executor (runs pip + resolves the pinned spec) and the checkpoint manager (persists
# the package list to S3). Kept module-global — the VM runs exactly one of each —
# exactly like data_catalog._ACTIVE_CATALOG.
_EXECUTOR = None
_CHECKPOINT_MANAGER = None


def _register_runtime(executor, checkpoint_manager) -> None:
    """Called once by server.py after the executor + checkpoint manager exist, so
    the install_package helper can drive the same path as the /install route."""
    global _EXECUTOR, _CHECKPOINT_MANAGER
    _EXECUTOR = executor
    _CHECKPOINT_MANAGER = checkpoint_manager


# =============================================================================
# UTILITIES — package management (same path as the Packages side-panel)
# =============================================================================

def install_package(package: str) -> dict:
    """
    Install a Python package into this sandbox — the same action as the Packages
    side-panel, so the package is pip-installed AND recorded so it SURVIVES a VM
    suspend/restore (a plain `pip install` in a cell would be lost on rotation).

    Use it when a cell needs a library that isn't pre-baked into the image:
        install_package("xgboost")        # then load_model(...) an xgboost model
        install_package("skl2onnx")       # then export a model to ONNX
        install_package("statsmodels==0.14.2")   # a pinned version

    Accepts a PyPI name with an optional version specifier (e.g. "pandas>=2.0").
    Returns a dict: {success, output, error, installed_spec} where installed_spec is
    the resolved pinned "name==x.y.z" (what gets persisted for restore).
    """
    if _EXECUTOR is None:
        return {"success": False, "output": "",
                "error": "package installer not available in this runtime",
                "installed_spec": None}
    result = _EXECUTOR.install_package(package)
    # Mirror the /install route: on success, record the RESOLVED pin and flush the
    # package list to S3 immediately so it can't be lost by a later frozen checkpoint.
    if result.success and _CHECKPOINT_MANAGER is not None:
        spec = result.installed_spec or package
        try:
            _CHECKPOINT_MANAGER.record_package_install(spec)
            _CHECKPOINT_MANAGER.flush_packages()
        except Exception as e:  # persistence is best-effort — never break the install
            logger.warning(f"install_package: recorded install but flush failed: {e}")
    if result.success:
        print(f"✓ Installed {result.installed_spec or package}")
    else:
        print(f"✗ Install failed: {(result.error or '').strip()[:200]}")
    return {
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "installed_spec": result.installed_spec,
        # Same failure diagnostics as the /install route (empty on success).
        "error_kind": result.install_error_kind,
        "details": result.install_details,
        "arch": result.install_arch,
    }


def uninstall_package(package: str) -> dict:
    """
    Uninstall a Python package from this sandbox — the same action as the Packages
    side-panel's remove. Also drops it from the checkpoint list so it doesn't
    reappear after a VM restore.

        uninstall_package("xgboost")

    Returns a dict: {success, output, error}.
    """
    import re
    import subprocess
    import sys

    package = (package or "").strip()
    if not package:
        return {"success": False, "output": "", "error": "No package specified"}
    # Validate the name the SAME way executor.install_package does — a safe PyPI name
    # (optionally with extras / a version specifier), never a pip FLAG. The subprocess
    # already uses the list form (no shell), but matching install's guard keeps the two
    # symmetric and rejects a flag-looking arg (e.g. '--index-url=...') outright.
    _PKG_RE = re.compile(
        r'^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?'   # package name
        r'(\[([a-zA-Z0-9._-]+,?\s*)*\])?'              # optional extras [dev,test]
        r'([><=!~]=?[a-zA-Z0-9.*]+)?$'                 # optional version specifier
    )
    if package.startswith('-') or '--' in package or ' ' in package or not _PKG_RE.match(package):
        err = (f"Invalid package name: '{package}'. Use a valid PyPI package name "
               f"(e.g. 'xgboost'); names cannot contain flags or spaces.")
        print(f"✗ {err}")
        return {"success": False, "output": "", "error": err}
    # Uninstall drops the pinned version specifier — pip uninstall takes the bare name.
    bare = re.split(r'[<>=!~\[]', package, maxsplit=1)[0].strip()
    try:
        from app.platform.pip_helper import pip_lock
        # One pip at a time on this VM (PipBusy is reported like any other error).
        with pip_lock(wait_s=40):
            result = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", bare],
                capture_output=True, text=True, timeout=60,
            )
    except Exception as e:
        print(f"✗ Uninstall error: {e}")
        return {"success": False, "output": "", "error": str(e)}

    if result.returncode == 0:
        if _CHECKPOINT_MANAGER is not None:
            try:
                _CHECKPOINT_MANAGER.record_package_uninstall(bare)
                _CHECKPOINT_MANAGER.flush_packages()
            except Exception as e:
                logger.warning(f"uninstall_package: removed but flush failed: {e}")
        print(f"✓ Uninstalled {bare}")
        return {"success": True, "output": result.stdout.strip(), "error": None}
    err = (result.stderr or "").strip() or "Uninstall failed"
    print(f"✗ Uninstall failed: {err[:200]}")
    return {"success": False, "output": "", "error": err}
