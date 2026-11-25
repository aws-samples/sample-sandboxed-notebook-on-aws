"""Pure (de)serialization helpers for per-cell runtime fields.

Shared by SqliteStorage and PostgresStorage so both backends map the
notebook_cells runtime columns (peak_mem_mb, peak_cpu_pct, image_s3_key)
identically. Stdlib only — no DB imports.

Dataflow facts (reads/writes/sources/staleness) are no longer stored per cell:
they live in the per-notebook DAG document (notebook_dag table, built by
proxy/notebook/dag.py, which also normalizes data-source identifiers).

Chart images: only the S3 LOCATION of a cell's chart PNG is stored
(notebook_cells.image_s3_key), never the bytes. The key layout is
``notebook-outputs/<notebook_id>/<cell_segment>/<sha256>.png`` and every
writer stores a key only when it belongs to the notebook/cell being written
(valid_image_key), so storage can never hold a foreign or traversal key.
"""

import hashlib
import json
import logging
import math
import re

logger = logging.getLogger(__name__)

IMAGE_KEY_PREFIX = "notebook-outputs/"

# Raised by both backends' initialize() when notebook_cells predates image_s3_key.
# Not a migration: schema changes are drop-and-recreate.
SCHEMA_RESET_MESSAGE = (
    "notebook_cells.image_s3_key missing — reinstall with local_install.sh "
    "--AuroraSetup --AuroraReset --WarmPool (wipes DBs)"
)

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_KEY_RE = re.compile(r"^notebook-outputs/(\d+)/([A-Za-z0-9_-]{1,130})/([0-9a-f]{64})\.png$")


def as_float(v):
    """Return v as a finite float, or None. bool is rejected (it is an int)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def cell_key_segment(cell_id) -> str:
    """The S3 path segment for a cell id: the id itself when it is a safe
    [A-Za-z0-9_-]{1,128} token, else "h-" + sha256(id) (never user-visible)."""
    cid = str(cell_id)
    if _SEGMENT_RE.match(cid):
        return cid
    return "h-" + hashlib.sha256(cid.encode("utf-8")).hexdigest()


def notebook_image_prefix(notebook_id) -> str:
    return f"{IMAGE_KEY_PREFIX}{int(notebook_id)}/"


def image_key(notebook_id, cell_id, sha: str) -> str:
    """notebook-outputs/<nid>/<cell_segment>/<sha256>.png"""
    return f"{notebook_image_prefix(notebook_id)}{cell_key_segment(cell_id)}/{sha}.png"


def valid_image_key(notebook_id, cell_id, key):
    """Return `key` only when it is a well-formed chart key that belongs to this
    notebook and this cell; otherwise None (foreign, traversal and garbage keys
    are dropped silently)."""
    if key is None or key == "":
        return None
    try:
        if not isinstance(key, str) or cell_id is None:
            raise ValueError("not a str key / no cell id")
        m = _KEY_RE.fullmatch(key)
        if not m:
            raise ValueError("malformed")
        if m.group(1) != str(int(notebook_id)):
            raise ValueError("other notebook")
        if m.group(2) != cell_key_segment(cell_id):
            raise ValueError("other cell")
    except (ValueError, TypeError) as e:
        logger.debug("image key dropped (nb=%s cell=%s): %s", notebook_id, cell_id, e)
        return None
    return key


def image_sha_from_key(key):
    """The sha256 hex of a well-formed key, else None."""
    m = _KEY_RE.fullmatch(key) if isinstance(key, str) else None
    return m.group(3) if m else None


# A cell with several rich outputs stores its ordered items as a JSON envelope inside
# the existing `html` string (written by the web app). The 14-char prefix is what the
# storage queries use to find such cells without parsing every row.
OUTPUTS_ENVELOPE_PREFIX = '{"__outputs__"'


def envelope_image_keys(notebook_id, cell_id, html) -> list:
    """The valid image keys listed in a cell's `__outputs__` envelope, in item order
    and without duplicates. [] for any non-envelope `html`, unparsable JSON, or
    keys that are malformed or belong to another notebook/cell (valid_image_key)."""
    if not isinstance(html, str) or not html.startswith(OUTPUTS_ENVELOPE_PREFIX):
        return []
    try:
        doc = json.loads(html)
    except ValueError:
        return []
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return []
    keys = []
    for it in items:
        if isinstance(it, dict) and it.get("t") == "image":
            k = valid_image_key(notebook_id, cell_id, it.get("key"))
            if k and k not in keys:
                keys.append(k)
    return keys


def cell_all_image_keys(notebook_id, cell_id, image_s3_key, html) -> list:
    """Every key a cell references: the legacy last-image column first, then the
    envelope keys, deduplicated, own notebook/cell only."""
    keys = []
    last = valid_image_key(notebook_id, cell_id, image_s3_key)
    if last:
        keys.append(last)
    for k in envelope_image_keys(notebook_id, cell_id, html):
        if k not in keys:
            keys.append(k)
    return keys


def echo_cells_with_valid_keys(notebook_id, cells) -> list:
    """notebook_create's echo of the client cells, with each imageS3Key replaced
    by what storage actually kept (a validated key for the new id, else None),
    so the response never echoes a raw client key."""
    out = []
    for c in cells or []:
        if isinstance(c, dict) and "imageS3Key" in c:
            c = {**c, "imageS3Key": valid_image_key(notebook_id, c.get("id"), c.get("imageS3Key"))}
        out.append(c)
    return out
