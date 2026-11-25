"""Extract the 'AWS Data Sources' notebook into a test fixture (READ-ONLY).

Opens proxy/data/microvm.db through a ``mode=ro`` URI, so the live database
is never written. Produces tests/fixtures/aws_data_sources_cells.json: a list
of ``{id, type, code, position, output_variable, vars_read, vars_written,
sources_read}`` ordered by position, used by tests/dag/test_dag_real_notebook.py.

Usage (from the repo root):
    python3 tests/fixtures/extract_aws_data_sources.py [notebook_id]

The default notebook id is 14. The stored vars_*/sources_read lists predate
the DAG (they come from the pre-DAG per-cell dataflow columns); once those
columns are removed this script can no longer be re-run, and the checked-in
fixture is the source of truth.
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "proxy" / "data" / "microvm.db"
OUT = Path(__file__).resolve().parent / "aws_data_sources_cells.json"


def _json_list(text):
    if not text:
        return []
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def main(notebook_id: int = 14) -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT cell_id, type, code, position, output_variable, "
            "vars_read_json, vars_written_json, sources_read_json "
            "FROM notebook_cells WHERE notebook_id = ? ORDER BY position",
            (notebook_id,),
        ).fetchall()
    finally:
        conn.close()
    cells = [
        {
            "id": r[0],
            "type": r[1] or "code",
            "code": r[2] or "",
            "position": r[3],
            "output_variable": r[4],
            "vars_read": _json_list(r[5]),
            "vars_written": _json_list(r[6]),
            "sources_read": _json_list(r[7]),
        }
        for r in rows
    ]
    if not cells:
        print(f"notebook {notebook_id} not found / has no cells", file=sys.stderr)
        return 1
    # The repo is public: replace any 12-digit AWS account id (bucket names like
    # microvm-sandbox-artifacts-<acct>-<region>, ARNs) with the documentation
    # placeholder so a regenerated fixture never leaks the real account.
    text = re.sub(r"(?<!\d)\d{12}(?!\d)", "123456789012",
                  json.dumps(cells, indent=1, ensure_ascii=False))
    OUT.write_text(text + "\n")
    print(f"wrote {len(cells)} cells to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 14))
