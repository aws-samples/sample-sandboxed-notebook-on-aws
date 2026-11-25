#!/usr/bin/env python3
# test-kind: offline
"""Offline test: Athena tables are listed as database.table in the AI inventory.

Bug: the per-turn inventory listed Athena tables by bare name, so the model used the
default database for an Iceberg table. Covers (agent + proxy fix-error context):
  * generic sources, two databases + an Iceberg entry -> db.table and Iceberg tag;
  * columns attach by source_id (same table name in two databases stay apart);
  * legacy athena[] fallback renders db.table;
  * no blank line inside the block (the transcript splits on blank lines);
  * fix-error context renders db.table;
  * system prompts no longer say 'database ... only'.
Run:  python tests/ai/test_athena_qualified_inventory.py   (exit 0 = pass, 1 = fail)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "agent"))

from agent.ai_assistant import notebook_agent as agent_na  # noqa: E402
from proxy.notebook.ai import notebook_agent as proxy_na  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("FAIL:", msg)


def _src(db, name, **extra):
    d = {"source_type": "athena", "source_id": f"{db}.{name}", "display_name": name, "database": db}
    d.update(extra)
    return d


SOURCES = {
    "sources": [
        _src("microvm_demo_db", "orders"),
        _src("microvm_demo_db", "shared"),
        _src("microvm_demo_iceberg", "product_price_history", table_format="iceberg", catalog="s3tablescatalog"),
        _src("microvm_demo_iceberg", "shared", table_format="iceberg"),
    ],
    "_catalog": {"entries": [
        {"source_id": "microvm_demo_iceberg.product_price_history", "display_name": "product_price_history",
         "status": "discovered", "columns": [{"name": "product_id"}, {"name": "price"}]},
        {"source_id": "microvm_demo_db.shared", "display_name": "shared", "status": "discovered",
         "columns": [{"name": "a_col"}]},
        {"source_id": "microvm_demo_iceberg.shared", "display_name": "shared", "status": "discovered",
         "columns": [{"name": "b_col"}]},
    ]},
}

# --- agent inventory, generic sources ---
block = agent_na._build_available_data_block({"data_sources": SOURCES})
check("  - microvm_demo_db.orders" in block, "default-db table rendered db.table")
ice = [l for l in block.splitlines() if "microvm_demo_iceberg.product_price_history" in l]
check(len(ice) == 1 and "Iceberg" in ice[0], "iceberg entry rendered db.table with Iceberg tag")
check("columns: product_id, price" in (ice[0] if ice else ""), "columns attached by source_id")
db_shared = [l for l in block.splitlines() if "microvm_demo_db.shared" in l]
ic_shared = [l for l in block.splitlines() if "microvm_demo_iceberg.shared" in l]
check(db_shared and "a_col" in db_shared[0] and "b_col" not in db_shared[0], "same-name table in db 1 keeps its own columns")
check(ic_shared and "b_col" in ic_shared[0] and "a_col" not in ic_shared[0], "same-name table in db 2 keeps its own columns")
check("\n\n" not in block, "no blank line inside the block")
check("  - product_price_history" not in block, "no bare athena name line")

# --- columns fall back to display_name when only a bare-name entry exists ---
fb = agent_na._build_available_data_block({"data_sources": {
    "sources": [_src("microvm_demo_db", "customers")],
    "_catalog": {"entries": [{"display_name": "customers", "status": "discovered", "columns": ["id", "name"]}]},
}})
check("microvm_demo_db.customers" in fb and "columns: id, name" in fb, "bare display_name column fallback")

# --- legacy athena[] fallback ---
legacy = agent_na._build_available_data_block({"data_sources": {"athena": [
    {"name": "orders", "database": "microvm_demo_db", "columns": [{"name": "order_id"}]},
    {"name": "old", "columns": [{"name": "x"}]},
]}})
check("  - microvm_demo_db.orders — columns: order_id" in legacy, "legacy athena[] renders db.table with columns")
check("  - old — columns: x" in legacy, "legacy athena[] without database stays bare")
check("\n\n" not in legacy, "legacy block has no blank line")

# --- proxy fix-error context ---
ctx = proxy_na._build_fix_context_section({"data_sources": SOURCES})
check("athena: microvm_demo_iceberg.product_price_history · Iceberg" in ctx, "fix context renders iceberg db.table")
check("athena: microvm_demo_db.orders" in ctx, "fix context renders default db.table")
check("columns: product_id, price" in ctx, "fix context attaches columns by source_id")

# --- prompts ---
for path in ("agent/ai_assistant/prompts.py", "proxy/notebook/ai/prompts.py"):
    text = open(os.path.join(_ROOT, path)).read()
    check('database "{athena_db}" only' not in text, f"{path}: 'database only' wording removed")
    check("exactly as listed" in text, f"{path}: always-qualify rule present")

if _fails:
    print(f"{len(_fails)} failed")
    sys.exit(1)
print("OK")
