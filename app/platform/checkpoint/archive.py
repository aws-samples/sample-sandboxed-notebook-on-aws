"""
Tar/path safety helpers for checkpoint restore.

Part of: app.platform.checkpoint (infrastructure layer)

Restore reads file names + a tar archive from an S3 session checkpoint. A
poisoned checkpoint could carry crafted names ('../../app/server.py',
'/etc/cron.d/x') that write outside /tmp; these helpers validate every path and
tar member before touching the filesystem (CWE-22 / Bandit B202).
"""

import os
import tarfile
import logging

logger = logging.getLogger(__name__)


def _safe_join(base_dir: str, name: str) -> str:
    """Resolve `name` under `base_dir`, rejecting path traversal and absolute paths.

    Restore reads file names + a tar archive from the S3 session checkpoint. Without
    validation a crafted name like '../../app/server.py' or '/etc/cron.d/x' would let
    a poisoned checkpoint write outside /tmp and overwrite app code or system files
    inside the VM (CWE-22 / Bandit B202). This normalizes and confirms the final path
    stays within base_dir, raising ValueError otherwise.
    """
    base = os.path.realpath(base_dir)
    # os.path.join treats an absolute `name` as replacing base — normpath then
    # catches that (and any ../) because the result won't be under base.
    target = os.path.realpath(os.path.join(base, name))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"unsafe path in checkpoint archive: {name!r}")
    return target


def _safe_extractall(tar: "tarfile.TarFile", base_dir: str) -> None:
    """Extract a tar archive into base_dir, one validated member at a time.

    Only regular files and directories are extracted; symlinks, hardlinks, and
    device/fifo entries are skipped (they can point outside the tree or be abused).
    Python 3.11 in the VM image predates tarfile's `filter='data'` (3.12+), so we
    validate members explicitly here.
    """
    for member in tar.getmembers():
        # Reject traversal/absolute names before touching the filesystem.
        _safe_join(base_dir, member.name)
        if member.issym() or member.islnk():
            logger.warning(f"   Skipping link entry in checkpoint archive: {member.name!r}")
            continue
        if not (member.isreg() or member.isdir()):
            logger.warning(f"   Skipping non-regular entry in checkpoint archive: {member.name!r}")
            continue
        tar.extract(member, path=base_dir)
