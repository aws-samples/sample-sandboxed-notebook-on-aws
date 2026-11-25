"""
VM Pool Manager — standalone service that keeps a warm pool of pre-booted,
suspended MicroVM spares, built entirely against the VmBackend interface.
Sibling package of proxy/ and app/. The proxy imports only the stdlib-only
pool_manager.config (shared grant budget), so the public names below are
resolved lazily: importing pool_manager or pool_manager.config never pulls in
the server, the manager or a backend.
"""
import importlib

_EXPORTS = {
    "PoolConfig": "pool_manager.config",
    "load_config": "pool_manager.config",
    "FakeVmBackend": "pool_manager.fake_backend",
    "GrantError": "pool_manager.manager",
    "PoolEmpty": "pool_manager.manager",
    "PoolManager": "pool_manager.manager",
    "create_app": "pool_manager.server",
    "VmBackend": "pool_manager.vm_backend",
    "VmHandle": "pool_manager.vm_backend",
}

__all__ = [
    "VmBackend",
    "VmHandle",
    "FakeVmBackend",
    "PoolManager",
    "PoolEmpty",
    "GrantError",
    "create_app",
    "load_config",
    "PoolConfig",
]


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'pool_manager' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
