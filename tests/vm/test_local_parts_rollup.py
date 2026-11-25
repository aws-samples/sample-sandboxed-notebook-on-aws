#!/usr/bin/env python3
# test-kind: offline
"""Offline regression test for the LOCAL partitioned-folder rollup.

A /tmp SUBDIR of part files (part-0000.parquet …) must surface as ONE dataset
row — the local analogue of the S3 parquet_prefix rollup — not one row per part,
and must NOT falsely merge a dir of two unrelated files.

Verifies, WITHOUT any network / live VM:
  1. rollup_local_files(): ONE partition folder for a parts dir (summed size,
     is_prefix flag); TWO standalone entries for two unrelated csvs (no merge).
  2. DataCatalog discovery against a synthesized root → one prefix 'local' entry
     (metadata.is_prefix True, summed size) + per-standalone-file entries.
  3. GET /files enumeration (_enumerate_tmp_files) → 3 entries not 6; the folder
     row's name ends '/' and is flagged is_prefix.
  4. LocalFileSchemaProvider snippets: prefix form for a '/'-suffixed source;
     byte-for-byte UNCHANGED single-file form otherwise.
  5. read_local_prefix / to_local_prefix helpers actually exist (import).

Run:  python tests/vm/test_local_parts_rollup.py   (exit 0 = pass, 1 = fail)
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def _build_tree(root: str):
    """Create under `root`:
      web_sessions_parts/part-0000.parquet … part-0003.parquet  (one dataset)
      sales.csv, targets.csv                                     (two unrelated)
    Returns the parts dir path."""
    import pandas as pd

    parts_dir = os.path.join(root, "web_sessions_parts")
    os.makedirs(parts_dir, exist_ok=True)
    df = pd.DataFrame({"session_id": [1, 2, 3], "events": [10, 20, 30]})
    for i in range(4):
        df.to_parquet(os.path.join(parts_dir, f"part-{i:04d}.parquet"))
    pd.DataFrame({"a": [1]}).to_csv(os.path.join(root, "sales.csv"), index=False)
    pd.DataFrame({"b": [2]}).to_csv(os.path.join(root, "targets.csv"), index=False)
    return parts_dir


def main() -> int:
    print("== 0. helpers exist ==")
    try:
        from app.notebook.helpers.readers import read_local_prefix  # noqa: F401
        from app.notebook.helpers.export import to_local_prefix  # noqa: F401
        check("read_local_prefix & to_local_prefix importable", True)
    except Exception as e:
        check("read_local_prefix & to_local_prefix importable", False, str(e))

    from app.notebook.data_catalog import rollup_local_files, DataCatalog

    with tempfile.TemporaryDirectory() as root:
        _build_tree(root)
        all_files = []
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                all_files.append(os.path.join(dirpath, n))

        print("== 1. rollup_local_files ==")
        standalone, folders = rollup_local_files(all_files, root=root)
        check("exactly ONE partition folder", len(folders) == 1,
              f"got {len(folders)}")
        if folders:
            f = folders[0]
            check("folder source_id ends with '/'", f["source_id"].endswith("/"),
                  f["source_id"])
            check("folder display_name ends with '/'", f["display_name"].endswith("/"),
                  f["display_name"])
            check("folder rolled up 4 parts", len(f["parts"]) == 4,
                  str(len(f["parts"])))
            summed = sum(os.path.getsize(p) for p in f["parts"])
            check("folder size_bytes == summed parts", f["size_bytes"] == summed,
                  f"{f['size_bytes']} vs {summed}")
        check("two unrelated csvs stay standalone (no false merge)",
              len(standalone) == 2 and all(p.endswith(".csv") for p in standalone),
              f"standalone={standalone}")

        print("== 2. DataCatalog discovery ==")
        cat = DataCatalog()
        cat._discover_local_files(root=root)
        entries = {e["source_id"]: e for e in cat.get_all()["entries"]}
        local_entries = [e for e in entries.values() if e["source_type"] == "local"]
        prefix_entries = [e for e in local_entries
                          if (e.get("metadata") or {}).get("is_prefix")]
        standalone_entries = [e for e in local_entries
                              if not (e.get("metadata") or {}).get("is_prefix")]
        check("one 'local' prefix catalog entry", len(prefix_entries) == 1,
              f"got {len(prefix_entries)}")
        if prefix_entries:
            pe = prefix_entries[0]
            check("prefix entry source_id ends '/'", pe["source_id"].endswith("/"),
                  pe["source_id"])
            check("prefix entry metadata.kind == parquet_prefix",
                  (pe.get("metadata") or {}).get("kind") == "parquet_prefix")
            check("prefix entry has inferred columns", len(pe["columns"]) > 0,
                  str(pe["columns"]))
        check("two standalone 'local' csv entries", len(standalone_entries) == 2,
              f"got {len(standalone_entries)}")

        print("== 3. GET /files enumeration ==")
        from app.notebook.routes import _enumerate_tmp_files
        rows = _enumerate_tmp_files(root=root)
        check("files list has 3 rows (not 6)", len(rows) == 3, f"got {len(rows)}")
        folder_rows = [r for r in rows if r.get("is_prefix")]
        check("one folder row flagged is_prefix", len(folder_rows) == 1,
              f"got {len(folder_rows)}")
        if folder_rows:
            fr = folder_rows[0]
            check("folder row name ends '/'", fr["name"].endswith("/"), fr["name"])
            check("folder row kind == parquet_prefix",
                  fr.get("kind") == "parquet_prefix")

    print("== 4. LocalFileSchemaProvider snippets ==")
    from proxy.platform.datasources.local import LocalFileSchemaProvider
    p = LocalFileSchemaProvider()

    py_prefix = p.get_python_snippet("/tmp/web_sessions_parts/")
    check("prefix python snippet uses read_local_prefix",
          "read_local_prefix('/tmp/web_sessions_parts/')" in py_prefix, py_prefix)
    sql_prefix = p.get_sql_snippet("/tmp/web_sessions_parts/")
    check("prefix sql snippet uses read_parquet + *.parquet",
          "read_parquet(" in sql_prefix and "*.parquet" in sql_prefix, sql_prefix)

    # Single-file forms must be BYTE-FOR-BYTE unchanged.
    py_single = p.get_python_snippet("/tmp/sales.csv")
    expected_py = "# Read local file\nsales = read_local('/tmp/sales.csv')\nsales.head()"
    check("single-file python snippet unchanged", py_single == expected_py, py_single)
    sql_single = p.get_sql_snippet("/tmp/sales.csv")
    check("single-file sql snippet unchanged",
          sql_single == "SELECT * FROM '/tmp/sales.csv' LIMIT 100", sql_single)

    print("== 5. checkpoint glob set unchanged ==")
    from app.notebook.data_catalog import local_tmp_file_globs
    globs = local_tmp_file_globs()
    check("local_tmp_file_globs still returns /tmp/** per-file patterns",
          all(g.startswith("/tmp/") for g in globs) and any("**" in g for g in globs),
          str(globs))

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {_failures}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
