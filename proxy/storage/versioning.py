"""
Shared helpers for notebook DOCUMENT versioning — the single source of truth for
(a) which cell fields make up a "document", (b) de-duplicating cells by id, and
(c) hashing a document so identical snapshots can be detected cheaply.

Used by BOTH storage backends (to store a fingerprint on each version header and
to de-dupe cells before insert) AND the version-create route (to decide whether a
non-manual snapshot would be a no-op). Keeping this in one place guarantees the
route's dedup decision and the stored rows can never drift apart.

This module has no DB or web dependencies, so both layers can import it safely.
"""

import hashlib
import json


def versioned_tuple(cell: dict) -> tuple:
    """The DOCUMENT-only fields of a cell, in a stable order. Mirrors the columns
    stored in notebook_cell_versions; runtime output/exec/dataflow are excluded."""
    return (
        str(cell.get("id")),
        cell.get("type") or "code",
        cell.get("code") or "",
        cell.get("outputVariable"),
        cell.get("aiExplanation"),
        cell.get("aiExplanationSource"),
        cell.get("generated"),
    )


def dedupe_cells_by_id(cells: list) -> list:
    """Return cells with a non-None id, FIRST-WINS de-duplicated by id, order
    preserved. The version tables key on (version_group_id, cell_id), so a document
    that (through a bug/import) carries two cells with the same id would otherwise
    violate the primary key and abort the ENTIRE version write. First-wins keeps the
    earliest occurrence, matching the order the fingerprint is computed in."""
    seen = set()
    out = []
    for c in cells or []:
        if c.get("id") is None:
            continue
        cid = str(c.get("id"))
        if cid in seen:
            continue
        seen.add(cid)
        out.append(c)
    return out


def document_fingerprint(cells: list) -> list:
    """Ordered list of versioned-field tuples for the (de-duped) document. Two
    documents are identical iff their fingerprints are equal (order-sensitive)."""
    return [versioned_tuple(c) for c in dedupe_cells_by_id(cells)]


def fingerprint_hash(cells: list) -> str:
    """Stable hex hash of a document's fingerprint — stored on the version header
    so dedup is a single indexed lookup instead of re-reading every version's cells.
    Uses SHA-256 over a canonical JSON encoding of the fingerprint tuples."""
    fp = document_fingerprint(cells)
    canonical = json.dumps(fp, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
