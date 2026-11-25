"""
Session checkpoint and restore — S3-based persistence.

Part of: app.platform (infrastructure layer)

Saves executor state (namespace + local files) to S3 on termination, and
restores them when launching a new MicroVM from a session checkpoint. Includes
per-step timing for performance analysis.

This package is the refactored form of the former single-file
`app/platform/checkpoint.py`. It is split by responsibility:

  archive.py           — tar/path safety helpers (_safe_join, _safe_extractall)
  namespace_filter.py  — save/restore namespace filtering (runtime-object +
                         display-object exclusion, restore sanitization)
  delta.py             — pure delta core (per-variable + per-file change detection)
  nspickle.py          — checkpoint pickler/unpickler: the notebook namespace dict
                         is written as the "notebook-namespace" marker and bound
                         to the live namespace at restore; top-level notebook
                         classes, functions, shared data values and shared
                         closure cells are referenced by name from other
                         variables ("mvm-ns-class" / "mvm-ns-function" /
                         "mvm-ns-value" / "mvm-ns-cell")
  deadline.py          — SaveDeadline / SaveCancelled (time-bounded suspend save)
  manager.py           — CheckpointManager (the S3 save/restore orchestration)

This __init__ re-exports the full public surface so existing import sites keep
working unchanged:

  from app.platform.checkpoint import CheckpointManager       # app/server.py
  from app.platform.checkpoint import build_var_blobs, ...     # tests
  from app.platform import checkpoint as ckpt; ckpt.CheckpointManager

`glob` is re-exported here on purpose: tests monkeypatch `ckpt.glob.glob`, which
rebinds the attribute on the shared `glob` module object that manager.py reads
through `glob.glob(...)` — so the patch still takes effect after the split.

S3 structure (DELTA layout — only changed objects re-uploaded per save):
  sessions/{session_id}/manifest.json        — var manifest {name: {hash,size,object
                                               [,refs][,by_value]}}
  sessions/{session_id}/vars/{name}.pkl      — one object per large variable
  sessions/{session_id}/vars/__small__.pkl   — batched small variables
  sessions/{session_id}/files_manifest.json  — file manifest {name: {size,mtime_ns,object}}
  sessions/{session_id}/files/{name}.gz      — one gzip object per large /tmp file
  sessions/{session_id}/files/__small__.tar.gz — batched small /tmp files
  sessions/{session_id}/requirements.txt     — runtime-installed packages
  sessions/{session_id}/metadata.json        — session info
  sessions/{session_id}/dag.json             — notebook DAG doc + runtime section
                                               {clock, vars, runs} + checkpoint id
  sessions/{session_id}/imports.json         — import ledger (replayed on restore)
dag.json replaces provenance.json; an old provenance.json is only read, as a
restore fallback when dag.json is absent (never written).
"""

# Re-exported so tests can monkeypatch `ckpt.glob.glob` and have manager.py see it
# (both reference the same `glob` module object).
import glob  # noqa: F401

from .archive import _safe_join, _safe_extractall  # noqa: F401
from .namespace_filter import (  # noqa: F401
    _EXCLUDED_TYPE_NAMES,
    _EXCLUDED_MODULE_PREFIXES,
    _is_live_runtime_object,
    _has_runtime_object,
    _sanitize_restored_namespace,
    _filter_namespace_for_save,
    _is_excluded_from_checkpoint,
)
from .delta import (  # noqa: F401
    DELTA_SMALL_VAR_BYTES,
    DELTA_SMALL_FILE_BYTES,
    _pickle_one,
    _hash_bytes,
    build_var_blobs,
    compute_delta,
    file_signature,
    compute_file_delta,
)
from .nspickle import NAMESPACE_MARKER, NamespacePickler, NamespaceUnpickler  # noqa: F401
from .deadline import SaveCancelled, SaveDeadline  # noqa: F401
from .manager import CheckpointManager  # noqa: F401

__all__ = [
    "CheckpointManager",
    "NAMESPACE_MARKER",
    "NamespacePickler",
    "NamespaceUnpickler",
    "SaveCancelled",
    "SaveDeadline",
    # delta core (unit-tested directly)
    "build_var_blobs",
    "compute_delta",
    "file_signature",
    "compute_file_delta",
    "DELTA_SMALL_VAR_BYTES",
    "DELTA_SMALL_FILE_BYTES",
    "_pickle_one",
    "_hash_bytes",
    # namespace filtering
    "_is_live_runtime_object",
    "_has_runtime_object",
    "_sanitize_restored_namespace",
    "_filter_namespace_for_save",
    "_is_excluded_from_checkpoint",
    "_EXCLUDED_TYPE_NAMES",
    "_EXCLUDED_MODULE_PREFIXES",
    # archive/path safety
    "_safe_join",
    "_safe_extractall",
    # re-exported module (tests patch ckpt.glob.glob)
    "glob",
]
