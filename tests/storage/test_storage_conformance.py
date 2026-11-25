#!/usr/bin/env python3
# test-kind: offline
"""
Storage backend CONFORMANCE suite.

One shared test body is run against every backend so SQLite (the default,
single-instance) and Postgres (multi-instance) are proven to behave identically
through the StorageBackend seam. This is what lets us swap SQLite -> Postgres for
the RDS move without touching proxy code.

  * SQLite  — always runs (temp DB file, no AWS, no server).
  * Postgres — runs ONLY if TEST_PG_DSN is set AND psycopg2 is importable;
               otherwise it is skipped (not failed) so the suite stays green in
               a laptop/CI environment with no database server.

    TEST_PG_DSN="postgresql://user:pass@localhost:5432/microvm_test" \
        python3 tests/storage/test_storage_conformance.py

Coverage: notebooks CRUD, vm_sessions + state log, metrics, agent_sessions,
workbook_intel, global + local entities (incl. partial-update semantics),
session_registry (B1), and the distributed session lock (B2:
acquire=True, re-acquire=False, release, re-acquire=True), and the notebook /
session delete cascade (F4: terminate-then-delete, shared session, and
session_intel_delete).

Run (SQLite only):
    python3 tests/storage/test_storage_conformance.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


# ── tiny test harness (same style as tests/checkpoint) ───────────────────────
_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


# ── the shared conformance body: runs against ANY StorageBackend ─────────────
def run_conformance(storage, label: str):
    print(f"\n=== Conformance: {label} ===")

    # ---- NOTEBOOKS --------------------------------------------------------
    # create-returns-id contract: the DB assigns the integer `id` (AUTOINCREMENT /
    # IDENTITY); the caller does NOT supply it. We capture the returned id and use
    # it for all subsequent lookups. Cells round-trip by IDENTITY (id), not by
    # exact-dict equality — _row_to_cell rehydrates the full cell shape (type/code/
    # output/... default to None/''), so comparing whole dicts would spuriously fail.
    nb = storage.notebook_create("My NB", description="d", tag="Drafts",
                                 cells=[{"id": "c1"}])
    nb_id = nb["id"]
    check(f"[{label}] notebook_create returns a DB-generated id", nb_id is not None)
    got = storage.notebook_get(nb_id)
    check(f"[{label}] notebook_get roundtrip", got and got["name"] == "My NB")
    check(f"[{label}] notebook_get cells preserved (by id)",
          [c["id"] for c in got["cells"]] == ["c1"])
    check(f"[{label}] notebook_get exposes updated_at watermark",
          got.get("updated_at") is not None)
    ok = storage.notebook_update(nb_id, name="Renamed", cells=[{"id": "c2"}])
    check(f"[{label}] notebook_update returns True", ok is True)
    got = storage.notebook_get(nb_id)
    check(f"[{label}] notebook_update applied",
          got["name"] == "Renamed" and [c["id"] for c in got["cells"]] == ["c2"])
    check(f"[{label}] notebook_update no-op returns False",
          storage.notebook_update(nb_id) is False)
    # Chart location (imageS3Key): own key round-trips; a foreign key is dropped.
    _sha = "a" * 64
    _own = f"notebook-outputs/{int(nb_id)}/c2/{_sha}.png"
    storage.notebook_update(nb_id, cells=[
        {"id": "c2", "imageS3Key": _own},
        {"id": "c3", "imageS3Key": f"notebook-outputs/{int(nb_id) + 1}/c3/{_sha}.png"}])
    got = {c["id"]: c for c in storage.notebook_get(nb_id)["cells"]}
    check(f"[{label}] imageS3Key round-trips (own key) / foreign key → None",
          got["c2"]["imageS3Key"] == _own and got["c3"]["imageS3Key"] is None
          and got["c2"]["image"] is None)
    check(f"[{label}] notebook_cell_image_keys", storage.notebook_cell_image_keys(nb_id) == {"c2": _own})
    # notebook_cell_image_keys_all: last-image column + every key in the html envelope.
    _k = lambda cell, ch, nb=None: f"notebook-outputs/{int(nb_id if nb is None else nb)}/{cell}/{ch * 64}.png"  # noqa: E731
    import json as _json
    _env = _json.dumps({"__outputs__": 1, "items": [
        {"t": "text", "text": "hi"},
        {"t": "image", "key": _k("e1", "1")}, {"t": "image", "key": _k("e1", "2")},
        {"t": "image", "key": _k("e1", "3")}, {"t": "image", "key": _k("e1", "3")},
        {"t": "image", "key": _k("e1", "4", nb=int(nb_id) + 1)},   # foreign notebook
        {"t": "image", "key": _k("other", "5")},                    # foreign cell
        {"t": "image", "key": "../../etc/passwd"}, {"t": "image"},
        {"t": "table", "payload": "{}", "key": _k("e1", "6")}]}, separators=(",", ":"))
    storage.notebook_update(nb_id, cells=[
        {"id": "e1", "html": _env, "imageS3Key": _k("e1", "3")},
        {"id": "legacy", "imageS3Key": _k("legacy", "7")},
        {"id": "plain", "html": '{"__df_table__":true}'},
        {"id": "fake", "html": '{"__outputs__":1,"items":'},
        {"id": "bare", "html": "<div>x</div>"}])
    _all = storage.notebook_cell_image_keys_all(nb_id)
    check(f"[{label}] notebook_cell_image_keys_all: envelope keys, last image first, deduped, foreign/garbage dropped",
          _all.get("e1") == [_k("e1", "3"), _k("e1", "1"), _k("e1", "2")])
    check(f"[{label}] notebook_cell_image_keys_all: legacy-only cell",
          _all.get("legacy") == [_k("legacy", "7")])
    check(f"[{label}] notebook_cell_image_keys_all: non-envelope / broken-envelope html ignored",
          set(_all) == {"e1", "legacy"})
    check(f"[{label}] notebook_cell_image_keys unchanged (last image only)",
          storage.notebook_cell_image_keys(nb_id) == {"e1": _k("e1", "3"), "legacy": _k("legacy", "7")})
    check(f"[{label}] notebook_cell_image_keys_all for an unknown notebook → {{}}",
          storage.notebook_cell_image_keys_all(999999) == {})
    storage.notebook_update(nb_id, cells=[{"id": "c2"}])
    check(f"[{label}] notebook_cell_image_keys_all on a notebook without images → {{}}",
          storage.notebook_cell_image_keys_all(nb_id) == {})
    storage.notebook_update(nb_id, cells=[{"id": "c2"}])
    lst = storage.notebook_list()
    check(f"[{label}] notebook_list includes the notebook",
          any(n["id"] == nb_id for n in lst))
    check(f"[{label}] notebook_delete returns True",
          storage.notebook_delete(nb_id) is True)
    check(f"[{label}] notebook_get after delete is None",
          storage.notebook_get(nb_id) is None)

    # ---- NOTEBOOK DAG (notebook_dag) -------------------------------------
    # Interface contract only; the full suite (locking, rollback, views) lives in
    # tests/storage/test_notebook_dag.py.
    def _doc(tag):
        return {"schema": 1, "version": 0, "built_at": None, "tag": tag,
                "nodes": [], "edges": [], "issues": []}
    nbg = storage.notebook_create("DagNB", cells=[{"id": "d1"}], session_id="dag-sess",
                                  dag_build=lambda prev: _doc("create"))
    nbg_id = nbg["id"]
    check(f"[{label}] notebook_create(dag_build=) returns 'dag' version 1",
          nbg.get("dag", {}).get("version") == 1 and nbg["dag"]["tag"] == "create")
    got_dag = storage.notebook_dag_get(nbg_id)
    check(f"[{label}] notebook_dag_get returns the stored doc (row-stamped)",
          got_dag is not None and got_dag["version"] == 1
          and got_dag["notebook_id"] == nbg_id and got_dag["built_at"])
    upd = storage.notebook_dag_update(nbg_id, lambda prev: dict(prev, tag="rmw"))
    check(f"[{label}] notebook_dag_update bumps version to 2",
          upd is not None and upd["version"] == 2 and upd["tag"] == "rmw")
    same = storage.notebook_dag_update(nbg_id, lambda prev: None)
    check(f"[{label}] notebook_dag_update build->None keeps version",
          same is not None and same["version"] == 2)
    seen_cells = []
    storage.notebook_dag_update(
        nbg_id, lambda prev, cells: seen_cells.extend(c["id"] for c in cells),
        with_cells=True)
    check(f"[{label}] notebook_dag_update(with_cells=True) passes stored cells",
          seen_cells == ["d1"])
    storage.notebook_update(nbg_id, cells=[{"id": "d2"}],
                            dag_build=lambda prev: dict(prev, tag="save"))
    check(f"[{label}] notebook_update(cells, dag_build=) stores the doc",
          storage.notebook_dag_get(nbg_id)["version"] == 3)
    # Change check (delta-plan §0.2): no-op → nothing written; runtime-only →
    # runtime_version bumps, version kept.
    noop = storage.notebook_dag_write(nbg_id, lambda prev: dict(prev, trigger="noop"))
    check(f"[{label}] notebook_dag_write no-op: wrote=False, version 3 kept",
          noop is not None and noop.wrote is False and noop.doc["version"] == 3
          and storage.notebook_dag_get(nbg_id)["version"] == 3)
    rv0 = storage.notebook_dag_get(nbg_id)["runtime_version"]
    rto = storage.notebook_dag_write(nbg_id, lambda prev: dict(prev, runtime={"clock": 1, "vars": {}, "runs": {}}))
    got_rt = storage.notebook_dag_get(nbg_id)
    check(f"[{label}] notebook_dag_write runtime-only: version kept, runtime_version +1, persisted",
          rto.wrote and not rto.graph_changed and got_rt["version"] == 3
          and got_rt["runtime_version"] == rv0 + 1 and got_rt["runtime"]["clock"] == 1)
    check(f"[{label}] notebook_dag_update on a missing notebook -> None",
          storage.notebook_dag_update(987654321, lambda prev: _doc("x")) is None
          and storage.notebook_dag_write(987654321, lambda prev: _doc("x")) is None)
    check(f"[{label}] notebook_id_for_session resolves the linked notebook",
          storage.notebook_id_for_session("dag-sess") == nbg_id)
    check(f"[{label}] notebook_id_for_session unknown/falsy -> None",
          storage.notebook_id_for_session("nope-sess") is None
          and storage.notebook_id_for_session("") is None)
    storage.notebook_delete(nbg_id)
    check(f"[{label}] notebook_delete removes the notebook_dag row",
          storage.notebook_dag_get(nbg_id) is None)

    # ---- NOTEBOOK DELETE: orphan cleanup vs history preservation ----------
    # A notebook has ONE stable session_id. Deleting it must clean session-scoped
    # orphans (agent_sessions, workbook_intel, local_file_entities) but PRESERVE cost/
    # run history (notebook_runs, vm_sessions, vm_state_log). session_registry is
    # removed only when the session has no still-alive VM.
    nbd = storage.notebook_create("ToDelete", cells=[{"id": "c1"}])
    nbd_id = nbd["id"]
    storage.notebook_update(nbd_id, session_id="sd")
    # agent_session keyed by the notebook's session_id ("sd") so the notebook-delete
    # cleanup (DELETE FROM agent_sessions WHERE notebook_id) removes it, cascading
    # its agent_messages.
    storage.agent_session_create("sd", {"session_id": "sd", "session_type": "AGENT"}, notebook_id=nbd_id)
    storage.agent_message_create("sd", "AI Assistant", 0,
                                 {"message": {"role": "user", "content": [{"text": "hi"}]}, "message_id": 0},
                                 notebook_id=nbd_id)
    storage.workbook_intel_save("sd", "s3://intel/sd")
    storage.local_entity_upsert("sd", "/tmp/d.csv", doc_s3_key="s3://d", status="ready")
    storage.session_registry_upsert("sd", "vmd", "http://d")
    storage.notebook_run_create("rund", notebook_id=nbd_id, session_id="sd",
                                microvm_id="vmd", trigger="run_all",
                                cells_executed=3, total_exec_ms=100.0)
    storage.vm_session_create("vmd", notebook_id=nbd_id, session_id="sd", memory_mib=2048)
    storage.vm_session_update_state("vmd", "TERMINATED")  # no live VM → registry cleanable

    check(f"[{label}] delete-orphans: notebook_delete True",
          storage.notebook_delete(nbd_id) is True)
    # Orphans cleaned:
    check(f"[{label}] delete-orphans: agent_sessions cleared",
          storage.agent_session_read("sd") is None)
    check(f"[{label}] delete-orphans: agent_messages cascade-cleared",
          storage.agent_message_list("sd", "AI Assistant") == [])
    check(f"[{label}] delete-orphans: workbook_intel cleared",
          storage.workbook_intel_get("sd") is None)
    check(f"[{label}] delete-orphans: local_file_entities cleared",
          storage.local_entity_get("sd", "/tmp/d.csv") is None)
    check(f"[{label}] delete-orphans: session_registry cleared (no live VM)",
          storage.session_registry_get("sd") is None)
    # History preserved:
    runs = storage.notebook_run_list(nbd_id, limit=10)
    check(f"[{label}] delete-orphans: notebook_runs PRESERVED",
          any(r["run_id"] == "rund" for r in runs))
    check(f"[{label}] delete-orphans: vm_sessions PRESERVED",
          storage.vm_session_get("vmd") is not None)
    check(f"[{label}] delete-orphans: vm_state_log PRESERVED",
          len(storage.vm_state_log_get("vmd")) >= 1)

    # session_registry is NOT removed while a live (non-TERMINATED) VM exists.
    nbl = storage.notebook_create("LiveVM", cells=[{"id": "c1"}])
    nbl_id = nbl["id"]
    storage.notebook_update(nbl_id, session_id="sl")
    storage.session_registry_upsert("sl", "vml", "http://l")
    storage.vm_session_create("vml", notebook_id=nbl_id, session_id="sl", memory_mib=2048)  # RUNNING
    storage.notebook_delete(nbl_id)
    check(f"[{label}] delete-orphans: session_registry KEPT while VM alive",
          storage.session_registry_get("sl") == {"vm_id": "vml", "endpoint": "http://l"})

    # ---- DELETE CASCADE after /terminate (live-check-2 F4) ----------------
    # (a) The live order: /terminate clears notebooks.session_id (notebook_clear_
    # session) BEFORE DELETE /notebooks/{id}. The session's rows must still go,
    # found through the kept vm_sessions / notebook_runs history.
    nbt = storage.notebook_create("TermThenDelete", cells=[{"id": "c1"}])
    nbt_id = nbt["id"]
    storage.notebook_update(nbt_id, session_id="st")
    storage.agent_session_create("st", {"session_id": "st", "session_type": "AGENT"}, notebook_id=nbt_id)
    storage.agent_message_create("st", "AI Assistant", 0,
                                 {"message": {"role": "user", "content": [{"text": "hi"}]}, "message_id": 0},
                                 notebook_id=nbt_id)
    storage.workbook_intel_save("st", "s3://intel/st")
    storage.local_entity_upsert("st", "/tmp/t1.csv", doc_s3_key="s3://t1", status="ready")
    storage.local_entity_upsert("st", "/tmp/t2.csv", doc_s3_key="s3://t2", status="ready")
    storage.session_registry_upsert("st", "vmt", "http://t")
    storage.vm_session_create("vmt", notebook_id=nbt_id, session_id="st", memory_mib=2048)
    storage.vm_session_update_state("vmt", "TERMINATED")
    storage.notebook_run_create("runt", notebook_id=nbt_id, session_id="st",
                                microvm_id="vmt", trigger="run_all",
                                cells_executed=1, total_exec_ms=10.0)
    storage.notebook_env_set(nbt_id, {"K": "v"}, [])
    storage.entity_upsert("s3://b/shared.csv", "s3", doc_s3_key="s3://doc/shared", status="ready")
    storage.notebook_clear_session("st")
    check(f"[{label}] cascade(terminate): notebooks.session_id cleared first",
          storage.notebook_get(nbt_id)["session_id"] is None)
    if label == "SQLite":
        storage._get_conn().execute("DELETE FROM _sync_deletes")  # isolate the tombstone check
    check(f"[{label}] cascade(terminate): notebook_delete True",
          storage.notebook_delete(nbt_id) is True)
    check(f"[{label}] cascade(terminate): workbook_intel gone",
          storage.workbook_intel_get("st") is None)
    check(f"[{label}] cascade(terminate): local_file_entities gone",
          storage.local_entity_list("st") == [])
    check(f"[{label}] cascade(terminate): agent_sessions + agent_messages gone",
          storage.agent_session_read("st") is None
          and storage.agent_message_list("st", "AI Assistant") == [])
    check(f"[{label}] cascade(terminate): session_registry gone (VM terminated)",
          storage.session_registry_get("st") is None)
    check(f"[{label}] cascade(terminate): notebook_env gone",
          not any(e["notebook_id"] == nbt_id for e in storage.notebook_env_list()))
    check(f"[{label}] cascade(terminate): shared data_source_entities row KEPT",
          storage.entity_get("s3://b/shared.csv") is not None)
    check(f"[{label}] cascade(terminate): run + vm_session + state log KEPT",
          any(r["run_id"] == "runt" for r in storage.notebook_run_list(nbt_id, limit=10))
          and storage.vm_session_get("vmt") is not None
          and len(storage.vm_state_log_get("vmt")) >= 1)
    if label == "SQLite":
        tomb_tables = {r["table_name"] for r in storage._get_conn().execute(
            "SELECT table_name FROM _sync_deletes").fetchall()}
        check(f"[{label}] cascade(terminate): workbook_intel + local_file_entities tombstoned",
              {"workbook_intel", "local_file_entities"} <= tomb_tables)

    # (b) A session shared by two notebooks: deleting one keeps the session's rows.
    nbs1 = storage.notebook_create("Shared1", cells=[{"id": "c1"}])
    nbs2 = storage.notebook_create("Shared2", cells=[{"id": "c1"}])
    storage.vm_session_create("vms", notebook_id=nbs1["id"], session_id="ss", memory_mib=2048)
    storage.vm_session_update_state("vms", "TERMINATED")
    storage.notebook_update(nbs2["id"], session_id="ss")
    storage.workbook_intel_save("ss", "s3://intel/ss")
    storage.local_entity_upsert("ss", "/tmp/s.csv", doc_s3_key="s3://s", status="ready")
    storage.notebook_delete(nbs1["id"])
    check(f"[{label}] cascade(shared): rows KEPT while another notebook uses the session",
          storage.workbook_intel_get("ss") is not None
          and len(storage.local_entity_list("ss")) == 1)
    storage.notebook_delete(nbs2["id"])
    check(f"[{label}] cascade(shared): rows gone once the last notebook is deleted",
          storage.workbook_intel_get("ss") is None and storage.local_entity_list("ss") == [])

    # (c) session_intel_delete (DELETE /sessions/{id}): intel + local entities only.
    storage.workbook_intel_save("sx", "s3://intel/sx")
    storage.local_entity_upsert("sx", "/tmp/x1.csv", doc_s3_key="s3://x1", status="ready")
    storage.local_entity_upsert("sx", "/tmp/x2.csv", doc_s3_key="s3://x2", status="ready")
    storage.workbook_intel_save("sy", "s3://intel/sy")
    storage.local_entity_upsert("sy", "/tmp/y.csv", doc_s3_key="s3://y", status="ready")
    storage.agent_session_create("sx", {"session_id": "sx", "session_type": "AGENT"})
    check(f"[{label}] session_intel_delete returns rows deleted (3)",
          storage.session_intel_delete("sx") == 3)
    check(f"[{label}] session_intel_delete: sx intel + entities gone",
          storage.workbook_intel_get("sx") is None and storage.local_entity_list("sx") == [])
    check(f"[{label}] session_intel_delete: other session (sy) untouched",
          storage.workbook_intel_get("sy") is not None and len(storage.local_entity_list("sy")) == 1)
    check(f"[{label}] session_intel_delete: agent_sessions row untouched",
          storage.agent_session_read("sx") is not None)
    check(f"[{label}] session_intel_delete falsy id -> 0",
          storage.session_intel_delete("") == 0)
    storage.session_intel_delete("sy")
    storage.agent_session_delete("sx")

    # ---- NOTEBOOK ENV (seal/unseal + MERGE semantics) --------------------
    # env_crypto.seal/unseal are monkeypatched to identity in main() (no AWS master
    # key needed), so a "sealed" value round-trips to the same string here. We assert
    # the launch-path contract (notebook_env_get returns real decrypted values) and
    # the new MERGE/remove behaviour that H1's write-only UI relies on.
    nbe = storage.notebook_create("EnvNB", cells=[{"id": "c1"}])
    nbe_id = nbe["id"]
    # (a) whole-object set, then get returns exactly those decrypted values.
    storage.notebook_env_set(nbe_id, {"A": "1", "B": "2"}, [])
    env = storage.notebook_env_get(nbe_id)["env_vars"]
    check(f"[{label}] notebook_env get returns decrypted values (launch contract)",
          env == {"A": "1", "B": "2"})
    # (b) merge_env=True updates one key, the other SURVIVES.
    storage.notebook_env_set(nbe_id, {"A": "9"}, [], merge_env=True)
    env = storage.notebook_env_get(nbe_id)["env_vars"]
    check(f"[{label}] notebook_env merge updates A, keeps B",
          env == {"A": "9", "B": "2"})
    # (c) merge_env=False (default) is still a whole-object REPLACE.
    storage.notebook_env_set(nbe_id, {"A": "9"}, [])
    env = storage.notebook_env_get(nbe_id)["env_vars"]
    check(f"[{label}] notebook_env non-merge replaces whole set",
          env == {"A": "9"})
    # (d) remove_env_keys under merge drops a key.
    storage.notebook_env_set(nbe_id, {"A": "9", "B": "2"}, [])  # reset to A,B
    storage.notebook_env_set(nbe_id, {}, [], merge_env=True, remove_env_keys=["B"])
    env = storage.notebook_env_get(nbe_id)["env_vars"]
    check(f"[{label}] notebook_env merge+remove drops B, keeps A",
          env == {"A": "9"})
    storage.notebook_delete(nbe_id)

    # ---- VM SESSIONS + STATE LOG -----------------------------------------
    storage.vm_session_create("vm1", notebook_id=None, session_id="s1",
                              memory_mib=4096, endpoint="http://e", checkpoint_enabled=True)
    sess = storage.vm_session_get("vm1")
    check(f"[{label}] vm_session_create -> RUNNING",
          sess and sess["state"] == "RUNNING")
    storage.vm_session_update_state("vm1", "SUSPENDED")
    storage.vm_session_update_state("vm1", "SUSPENDED")  # dedup: no double-log
    storage.vm_session_update_state("vm1", "TERMINATED")
    sess = storage.vm_session_get("vm1")
    check(f"[{label}] vm_session_update_state -> TERMINATED",
          sess["state"] == "TERMINATED")
    check(f"[{label}] terminated_at set on TERMINATED",
          sess.get("terminated_at") is not None)
    log = storage.vm_state_log_get("vm1")
    states = [r["new_state"] for r in log]
    check(f"[{label}] state log ordered RUNNING,SUSPENDED,TERMINATED (deduped)",
          states == ["RUNNING", "SUSPENDED", "TERMINATED"])
    # burst_mb_seconds is the ONLY cost quantity persisted on vm_sessions now
    # (running/suspended reconstructed from the log via v_vm_sessions; dollars in
    # Python). vm1 is TERMINATED here — update still writes the column.
    storage.vm_session_update_burst("vm1", 3.0)
    sess = storage.vm_session_get("vm1")
    check(f"[{label}] vm_session_update_burst persisted",
          abs(sess["burst_mb_seconds"] - 3.0) < 1e-9)
    # active list excludes terminated
    storage.vm_session_create("vm2", session_id="s2", memory_mib=2048)
    active = storage.vm_session_list_active()
    ids = {a["microvm_id"] for a in active}
    check(f"[{label}] list_active excludes TERMINATED, includes RUNNING",
          "vm2" in ids and "vm1" not in ids)

    # ---- METRICS ----------------------------------------------------------
    storage.metrics_record("vm2", 12.5, 40.0, 800.0, 10.0, 2000.0, 100, 200, 7, 123.0)
    latest = storage.metrics_get_latest("vm2")
    check(f"[{label}] metrics_get_latest roundtrip",
          latest and abs(latest["cpu_pct"] - 12.5) < 1e-9)
    hist = storage.metrics_get_history("vm2", minutes=5)
    check(f"[{label}] metrics_get_history returns recent row", len(hist) >= 1)
    storage.metrics_cleanup(hours=24)  # nothing recent should be deleted
    check(f"[{label}] metrics_cleanup keeps recent",
          storage.metrics_get_latest("vm2") is not None)

    # ---- METRICS HISTORY (summary + bucketed downsampling + runs per VM) ---
    # Same window/bucket semantics in every backend: [floor(start), floor(end)+1),
    # buckets of bucket_sec counted from floor(start), empty buckets omitted.
    import time as _time
    base = int(_time.time()) - 3600 + 0.25
    ks = list(range(0, 60)) + list(range(660, 690))
    for k in ks:
        storage.metrics_record(
            "vmh", 92.0 if k == 30 else 10.0, 20.0, 3900.0 if k == 40 else 800.0,
            31.0 if k >= 660 else 30.0, 2000.0 + (k >= 660) * 10, 0, 0, 5, float(k),
            recorded_at=base + k)
    for k in range(5):
        storage.metrics_record("vmh2", 99.0, 99.0, 9999.0, 99.0, 9999.0, 0, 0, 1, 1.0,
                               recorded_at=base + 5 + k)
    s = storage.metrics_get_summary("vmh", base - 10, base + 700)
    check(f"[{label}] metrics_get_summary counts only this VM's window samples",
          s is not None and s["samples"] == 90)
    check(f"[{label}] metrics_get_summary cpu peak + earliest peak time",
          s and s["cpu_max"] == 92.0 and abs(s["cpu_max_at"] - (base + 30)) < 0.01)
    check(f"[{label}] metrics_get_summary memory peak + time",
          s and s["mem_used_max"] == 3900.0 and abs(s["mem_used_max_at"] - (base + 40)) < 0.01)
    check(f"[{label}] metrics_get_summary disk peak at first 31% sample",
          s and s["disk_pct_max"] == 31.0 and abs(s["disk_pct_max_at"] - (base + 660)) < 0.01
          and s["disk_used_max"] == 2010.0)
    check(f"[{label}] metrics_get_summary first/last sample times",
          s and abs(s["first_at"] - base) < 0.01 and abs(s["last_at"] - (base + 689)) < 0.01)
    check(f"[{label}] metrics_get_summary cpu average",
          s and abs(s["cpu_avg"] - (89 * 10.0 + 92.0) / 90) < 1e-6)
    check(f"[{label}] metrics_get_summary empty window -> None",
          storage.metrics_get_summary("vmh", base + 700, base + 800) is None)
    # base = B + 0.25 (B whole second): [B+0.75, B+0.85] -> [B, B+1) holds k=0 only;
    # [B+1.05, B+2.05] -> [B+1, B+3) holds k=1, k=2.
    one = storage.metrics_get_summary("vmh", base + 0.5, base + 0.6)
    two = storage.metrics_get_summary("vmh", base + 0.8, base + 1.8)
    check(f"[{label}] metrics window is [floor(start), floor(end)+1)",
          one is not None and one["samples"] == 1
          and two is not None and two["samples"] == 2
          and abs(two["first_at"] - (base + 1)) < 0.01)
    b = storage.metrics_get_bucketed("vmh", base, base + 700, 10)
    check(f"[{label}] metrics_get_bucketed non-empty buckets only, ascending",
          [r["bucket"] for r in b] == [0, 1, 2, 3, 4, 5, 66, 67, 68])
    check(f"[{label}] metrics_get_bucketed samples per bucket",
          all(r["samples"] == 10 for r in b))
    by = {r["bucket"]: r for r in b}
    check(f"[{label}] metrics_get_bucketed per-bucket MAX keeps the cpu peak",
          by.get(3, {}).get("cpu_max") == 92.0 and by.get(2, {}).get("cpu_max") == 10.0)
    check(f"[{label}] metrics_get_bucketed per-bucket AVG",
          by.get(3) and abs(by[3]["cpu_avg"] - (9 * 10.0 + 92.0) / 10) < 1e-6)
    check(f"[{label}] metrics_get_bucketed memory peak bucket",
          by.get(4, {}).get("mem_used_max") == 3900.0 and by.get(4, {}).get("mem_used_avg")
          and abs(by[4]["mem_used_avg"] - (9 * 800.0 + 3900.0) / 10) < 1e-6)
    check(f"[{label}] metrics_get_bucketed bucket time span",
          by.get(66) and abs(by[66]["t_first"] - (base + 660)) < 0.01
          and abs(by[66]["t_last"] - (base + 669)) < 0.01 and by[66]["disk_pct_max"] == 31.0)
    wide = storage.metrics_get_bucketed("vmh", base, base + 700, 1000)
    check(f"[{label}] metrics_get_bucketed one wide bucket holds all samples",
          len(wide) == 1 and wide[0]["samples"] == 90 and wide[0]["bucket"] == 0)
    check(f"[{label}] metrics_get_bucketed other VM isolated",
          len(storage.metrics_get_bucketed("vmh2", base, base + 700, 10)) == 1
          and storage.metrics_get_bucketed("vmh2", base, base + 700, 10)[0]["samples"] == 5)
    check(f"[{label}] metrics_get_bucketed unknown VM -> []",
          storage.metrics_get_bucketed("vm-none", base, base + 700, 10) == [])
    # metrics_get_burst: the cost tracker's rule over stored samples (burst-cost-time).
    for k, u in enumerate([500.0, 3000.0, 3000.0, 3500.0, 500.0]):
        storage.metrics_record("vmb", 10.0, 20.0, u, 30.0, 1000.0, 0, 0, 1, 1.0,
                               recorded_at=base + k)
    storage.metrics_record("vmb", 10.0, 20.0, 0.0, 30.0, 1000.0, 0, 0, 1, 1.0,
                           recorded_at=base + 5)  # ignored: mem_used_mb == 0
    storage.metrics_record("vmb2", 10.0, 20.0, 9000.0, 30.0, 1000.0, 0, 0, 1, 1.0,
                           recorded_at=base)
    storage.metrics_record("vmb2", 10.0, 20.0, 9000.0, 30.0, 1000.0, 0, 0, 1, 1.0,
                           recorded_at=base + 1)
    bu = storage.metrics_get_burst("vmb", 2048.0)
    check(f"[{label}] metrics_get_burst one episode: seconds, MB-seconds, peak, episodes",
          bu is not None and bu["samples"] == 5 and bu["burst_samples"] == 3
          and abs(bu["burst_seconds"] - 3.0) < 1e-6
          and abs(bu["burst_mb_seconds"] - (952 + 952 + 1452)) < 1e-6
          and bu["burst_episodes"] == 1 and bu["peak_mem_mb"] == 3500.0)
    fb = storage.metrics_get_burst("vmb2", 2048.0)
    check(f"[{label}] metrics_get_burst first sample bills nothing but is an episode",
          fb is not None and abs(fb["burst_seconds"] - 1.0) < 1e-6 and fb["burst_episodes"] == 1)
    check(f"[{label}] metrics_get_burst is per VM and burst-free below the tier",
          storage.metrics_get_burst("vmb", 4096.0)["burst_samples"] == 0)
    check(f"[{label}] metrics_get_burst unknown VM -> None",
          storage.metrics_get_burst("vm-none", 2048.0) is None)

    def _iso(sec):
        from datetime import datetime as _dt, timezone as _tz
        return _dt.fromtimestamp(base + sec, _tz.utc).isoformat()
    storage.notebook_run_create("runh1", session_id="sh", microvm_id="vmh", trigger="run_all",
                                started_at=_iso(0), finished_at=_iso(50),
                                cells_executed=12, cells_errored=1, total_exec_ms=50000.0,
                                run_cells=[{"cell_id": "c1"}])
    storage.notebook_run_create("runh2", session_id="sh", microvm_id="vmh", trigger="single",
                                started_at=_iso(660), finished_at=_iso(670), cells_executed=1)
    storage.notebook_run_create("runh3", session_id="sh", microvm_id="vmh2", trigger="run_from",
                                started_at=_iso(300), finished_at=_iso(310), cells_executed=3)
    storage.notebook_run_create("runh4", session_id="sx", microvm_id="vm-other",
                                started_at=_iso(400), finished_at=_iso(410))
    rl = storage.notebook_run_list_for_vms(["vmh", "vmh2"])
    check(f"[{label}] notebook_run_list_for_vms only these VMs, newest first",
          [r["run_id"] for r in rl] == ["runh2", "runh3", "runh1"])
    check(f"[{label}] notebook_run_list_for_vms light rows (no run_cells)",
          rl and all("run_cells" not in r and "run_cells_json" not in r for r in rl))
    check(f"[{label}] notebook_run_list_for_vms fields",
          rl and rl[-1]["trigger"] == "run_all" and rl[-1]["cells_executed"] == 12
          and rl[-1]["cells_errored"] == 1 and rl[-1]["total_exec_ms"] == 50000.0
          and rl[-1]["started_at"] == _iso(0))
    check(f"[{label}] notebook_run_list_for_vms limit",
          [r["run_id"] for r in storage.notebook_run_list_for_vms(["vmh", "vmh2"], limit=1)]
          == ["runh2"])
    check(f"[{label}] notebook_run_list_for_vms empty list -> []",
          storage.notebook_run_list_for_vms([]) == [])

    # ---- AGENT SESSIONS (Strands SessionManager backing store) -----------
    # The chat agent persists its conversation here (agent_sessions + agent_messages,
    # accessed only via the interface). Mirrors what RepositorySessionManager does:
    # create session, create agent, append messages with increasing message_id, list
    # with offset (for windowed rehydration), read one, and cascade-delete. Plus the
    # agent->UI-bubble transcript translation (merge assistant text across a tool
    # round-trip into one bubble; drop toolUse/toolResult; drop tool-result-only user).
    AID = "AI Assistant"
    check(f"[{label}] agent_session_read empty -> None",
          storage.agent_session_read("as1") is None)
    check(f"[{label}] agent_message_list empty -> []",
          storage.agent_message_list("as1", AID) == [])

    storage.agent_session_create("as1", {"session_id": "as1", "session_type": "AGENT"}, notebook_id=None)
    check(f"[{label}] agent_session_create + read", storage.agent_session_read("as1") is not None)
    storage.agent_create("as1", AID, {"agent_id": AID, "state": {}, "conversation_manager_state": {}})
    check(f"[{label}] agent_create + read", storage.agent_read("as1", AID) is not None)

    # A realistic turn with a tool round-trip: user -> assistant(text+toolUse) ->
    # user(toolResult only) -> assistant(text).
    turn = [
        {"role": "user", "content": [{"text": "What is the session id?"}]},
        {"role": "assistant", "content": [{"text": "Let me check."},
                                          {"toolUse": {"name": "execute_code", "toolUseId": "t1", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "as1"}]}}]},
        {"role": "assistant", "content": [{"text": "The session id is as1."}]},
    ]
    for i, m in enumerate(turn):
        storage.agent_message_create("as1", AID, i, {"message": m, "message_id": i}, notebook_id=None)

    allmsgs = storage.agent_message_list("as1", AID)
    check(f"[{label}] agent_message_list returns all (4)", len(allmsgs) == 4)
    check(f"[{label}] agent_message_list offset=2 -> 2", len(storage.agent_message_list("as1", AID, offset=2)) == 2)
    check(f"[{label}] agent_message_list limit=1 -> 1", len(storage.agent_message_list("as1", AID, limit=1)) == 1)
    one = storage.agent_message_read("as1", AID, 3)
    check(f"[{label}] agent_message_read by id", one is not None and one["message_id"] == 3)

    # message ordering is by message_id ascending
    ids = [m.get("message_id") for m in allmsgs]
    check(f"[{label}] agent_message_list ordered by message_id", ids == [0, 1, 2, 3])

    # transcript translation -> exactly 2 UI bubbles: user + one merged assistant.
    bubbles = storage.agent_transcript_get("as1")
    ok_bubbles = (
        len(bubbles) == 2
        and bubbles[0] == {"role": "user", "content": "What is the session id?"}
        and bubbles[1]["role"] == "assistant"
        and "Let me check." in bubbles[1]["content"]
        and "The session id is as1." in bubbles[1]["content"]
        and "toolUse" not in str(bubbles) and "toolResult" not in str(bubbles)
    )
    check(f"[{label}] agent_transcript_get merges turn -> 2 bubbles, tools dropped", ok_bubbles)

    # cascade delete removes messages too
    storage.agent_session_delete("as1")
    check(f"[{label}] agent_session_delete clears session", storage.agent_session_read("as1") is None)
    check(f"[{label}] agent_session_delete cascades messages",
          storage.agent_message_list("as1", AID) == [])

    # ---- WORKBOOK INTEL ---------------------------------------------------
    check(f"[{label}] workbook_intel_get empty -> None",
          storage.workbook_intel_get("wsess") is None)
    storage.workbook_intel_save("wsess", "s3://k/v1")
    storage.workbook_intel_save("wsess", "s3://k/v2")
    wi = storage.workbook_intel_get("wsess")
    check(f"[{label}] workbook_intel version bump on re-save",
          wi and wi["s3_key"] == "s3://k/v2" and wi["version"] == 2)

    # ---- GLOBAL ENTITIES (partial update) --------------------------------
    check(f"[{label}] entity_get unknown -> None",
          storage.entity_get("src1") is None)
    storage.entity_upsert("src1", "s3", doc_s3_key="s3://doc", status="ready",
                          change_signal={"etag": "abc"})
    e = storage.entity_get("src1")
    check(f"[{label}] entity_upsert insert",
          e and e["doc_s3_key"] == "s3://doc" and e["change_signal"] == {"etag": "abc"})
    # partial update: only status; doc_s3_key must be preserved
    storage.entity_upsert("src1", "s3", status="discovering")
    e = storage.entity_get("src1")
    check(f"[{label}] entity_upsert partial keeps doc_s3_key",
          e["status"] == "discovering" and e["doc_s3_key"] == "s3://doc")
    storage.entity_upsert("src2", "athena", status="ready")
    lst = storage.entity_list("athena")
    check(f"[{label}] entity_list filters by type",
          len(lst) == 1 and lst[0]["source_id"] == "src2")

    # ---- LOCAL FILE ENTITIES (session-scoped, partial update) ------------
    check(f"[{label}] local_entity_get unknown -> None",
          storage.local_entity_get("s1", "/tmp/a.csv") is None)
    storage.local_entity_upsert("s1", "/tmp/a.csv", doc_s3_key="s3://la",
                                change_signal={"size": 10}, status="ready")
    le = storage.local_entity_get("s1", "/tmp/a.csv")
    check(f"[{label}] local_entity_upsert insert",
          le and le["doc_s3_key"] == "s3://la" and le["change_signal"] == {"size": 10})
    storage.local_entity_upsert("s1", "/tmp/a.csv", status="stale")
    le = storage.local_entity_get("s1", "/tmp/a.csv")
    check(f"[{label}] local_entity_upsert partial keeps doc_s3_key",
          le["status"] == "stale" and le["doc_s3_key"] == "s3://la")
    # scoping: same filepath, different session is a different row
    storage.local_entity_upsert("s2", "/tmp/a.csv", status="ready")
    check(f"[{label}] local_entity_list scoped by session",
          len(storage.local_entity_list("s1")) == 1 and len(storage.local_entity_list("s2")) == 1)

    # ---- SESSION REGISTRY (B1) -------------------------------------------
    check(f"[{label}] session_registry_get unknown -> None",
          storage.session_registry_get("reg1") is None)
    storage.session_registry_upsert("reg1", "vmA", "http://a")
    r = storage.session_registry_get("reg1")
    check(f"[{label}] session_registry_upsert insert",
          r == {"vm_id": "vmA", "endpoint": "http://a"})
    storage.session_registry_upsert("reg1", "vmB", "http://b")
    r = storage.session_registry_get("reg1")
    check(f"[{label}] session_registry_upsert overwrite (VM swap)",
          r == {"vm_id": "vmB", "endpoint": "http://b"})
    # session_registry_all: enumerate every mapping (used by startup reconcile).
    storage.session_registry_upsert("reg2", "vmC", "http://c")
    all_rows = storage.session_registry_all()
    by_sid = {row["session_id"]: row for row in all_rows}
    check(f"[{label}] session_registry_all returns both rows",
          by_sid.get("reg1") == {"session_id": "reg1", "vm_id": "vmB", "endpoint": "http://b"}
          and by_sid.get("reg2") == {"session_id": "reg2", "vm_id": "vmC", "endpoint": "http://c"})
    storage.session_registry_delete("reg2")
    storage.session_registry_delete("reg1")
    check(f"[{label}] session_registry_delete removes",
          storage.session_registry_get("reg1") is None)
    remaining_sids = {row["session_id"] for row in storage.session_registry_all()}
    check(f"[{label}] session_registry_all drops deleted rows",
          "reg1" not in remaining_sids and "reg2" not in remaining_sids)

    # ---- DISTRIBUTED SESSION LOCK (B2) -----------------------------------
    check(f"[{label}] try_session_lock first acquire -> True",
          storage.try_session_lock("locksess") is True)
    check(f"[{label}] try_session_lock re-acquire while held -> False",
          storage.try_session_lock("locksess") is False)
    # a DIFFERENT session is independently lockable
    check(f"[{label}] try_session_lock other session -> True",
          storage.try_session_lock("locksess2") is True)
    storage.release_session_lock("locksess")
    check(f"[{label}] try_session_lock after release -> True",
          storage.try_session_lock("locksess") is True)
    # release is idempotent / safe when not held
    storage.release_session_lock("never_held")  # must not raise
    check(f"[{label}] release_session_lock unheld is safe no-op", True)
    # cleanup locks
    storage.release_session_lock("locksess")
    storage.release_session_lock("locksess2")

    # ---- SYNC TOMBSTONES (SQLite-only) -----------------------------------
    # The SQLite backend records an AFTER DELETE tombstone into _sync_deletes for
    # each deleted operational row (single + composite PKs as JSON), which the
    # generic SQLite→Aurora sync replays as deletes. Postgres is the sync TARGET
    # and carries NO sync tables/triggers, so this section is SQLite-only.
    if label == "SQLite":
        conn = storage._get_conn()
        # Delete a notebook with a cell. The notebook (a ROOT table) IS tombstoned;
        # its cell (a CASCADE-CHILD via ON DELETE CASCADE) is deliberately NOT — the
        # sync worker relies on Aurora's own cascade for child deletion and syncs
        # cells parent-scoped, so per-child tombstones would be redundant churn.
        tnb = storage.notebook_create("Tomb", cells=[{"id": "tc1"}])
        conn.execute("DELETE FROM _sync_deletes")  # isolate this check
        storage.notebook_delete(tnb["id"])
        toms = conn.execute(
            "SELECT table_name, pk_json FROM _sync_deletes ORDER BY seq"
        ).fetchall()
        tables = [t["table_name"] for t in toms]
        check(f"[{label}] tombstone: root table (notebooks) IS recorded on delete",
              "notebooks" in tables)
        check(f"[{label}] tombstone: cascade-child (notebook_cells) is NOT tombstoned",
              "notebook_cells" not in tables)
        # The notebooks tombstone carries the single PK as JSON.
        import json as _json
        nb_tomb = next((t for t in toms if t["table_name"] == "notebooks"), None)
        pk = _json.loads(nb_tomb["pk_json"]) if nb_tomb else {}
        check(f"[{label}] tombstone: notebooks pk_json is {{'id': <id>}}",
              set(pk.keys()) == {"id"} and pk["id"] == tnb["id"])

        # In-place child churn is gone: rewriting a notebook's cells (delete+reinsert
        # in _write_cells) must NOT emit notebook_cells tombstones anymore.
        tnb2 = storage.notebook_create("Churn", cells=[{"id": "a"}, {"id": "b"}])
        conn.execute("DELETE FROM _sync_deletes")
        storage.notebook_update(tnb2["id"], cells=[{"id": "a"}])  # rewrites cell rows
        churn = conn.execute(
            "SELECT COUNT(*) c FROM _sync_deletes WHERE table_name='notebook_cells'"
        ).fetchone()["c"]
        check(f"[{label}] no cell tombstone churn on save (delete+reinsert)",
              churn == 0)

    # ---- notebook_owned_session_ids (vm-card-regroup Fix B) ---------------
    # Read-only: the sessions notebook_delete treats as OWNED, so the caller can
    # remove their S3 checkpoint prefixes after the DB delete. Same rule.
    nbo = storage.notebook_create("Owned1", cells=[{"id": "c1"}])
    storage.notebook_update(nbo["id"], session_id="so1")
    storage.workbook_intel_save("so1", "s3://intel/so1")
    check(f"[{label}] owned_sessions: a notebook alone with its session → ['so1']",
          storage.notebook_owned_session_ids(nbo["id"]) == ["so1"])
    check(f"[{label}] owned_sessions: read-only (notebook + intel rows still there)",
          storage.notebook_get(nbo["id"]) is not None
          and storage.workbook_intel_get("so1") is not None)
    nbo2a = storage.notebook_create("OwnedShared1", cells=[{"id": "c1"}])
    nbo2b = storage.notebook_create("OwnedShared2", cells=[{"id": "c1"}])
    storage.notebook_update(nbo2a["id"], session_id="ss2")
    storage.vm_session_create("vmss2", notebook_id=nbo2b["id"], session_id="ss2", memory_mib=2048)
    check(f"[{label}] owned_sessions: shared session → [] for the first notebook",
          storage.notebook_owned_session_ids(nbo2a["id"]) == [])
    storage.notebook_delete(nbo2a["id"])
    check(f"[{label}] owned_sessions: shared session → ['ss2'] once the other is deleted",
          storage.notebook_owned_session_ids(nbo2b["id"]) == ["ss2"])
    nbo3 = storage.notebook_create("OwnedAfterTerminate", cells=[{"id": "c1"}])
    storage.notebook_update(nbo3["id"], session_id="so3")
    storage.vm_session_create("vmso3", notebook_id=nbo3["id"], session_id="so3", memory_mib=2048)
    storage.vm_session_update_state("vmso3", "TERMINATED")
    storage.notebook_run_create("runso3b", notebook_id=nbo3["id"], session_id="so3b",
                                microvm_id="vmso3", trigger="run_all",
                                cells_executed=1, total_exec_ms=1.0)
    storage.notebook_clear_session("so3")
    check(f"[{label}] owned_sessions: found via vm_sessions/notebook_runs after /terminate",
          storage.notebook_owned_session_ids(nbo3["id"]) == ["so3", "so3b"])
    check(f"[{label}] owned_sessions: unknown notebook id → []",
          storage.notebook_owned_session_ids("999999999") == [])
    for _nb in (nbo, nbo2b, nbo3):
        storage.notebook_delete(_nb["id"])

    # ---- notebook_latest_session_id (restore-arch-bug) ---------------------
    # Re-links a notebook whose session_id /terminate cleared: the session of its
    # LATEST vm_sessions row, never another notebook's, never an older one.
    nbl = storage.notebook_create("Latest", cells=[{"id": "c1"}])
    check(f"[{label}] latest_session: no VM rows → None",
          storage.notebook_latest_session_id(nbl["id"]) is None)
    storage.vm_session_create("vml1", notebook_id=nbl["id"], session_id="sl-old", memory_mib=1024,
                              architecture="x86_64", launched_at="2026-01-01T00:00:00+00:00")
    storage.vm_session_create("vml2", notebook_id=nbl["id"], session_id="sl-new", memory_mib=1024,
                              architecture="x86_64", launched_at="2026-01-02T00:00:00+00:00")
    storage.vm_session_create("vml3", notebook_id=nbl["id"], session_id="sl-old", memory_mib=1024,
                              architecture="x86_64", launched_at="2026-01-01T12:00:00+00:00")
    storage.notebook_update(nbl["id"], session_id="sl-new")
    storage.notebook_clear_session("sl-new")
    check(f"[{label}] latest_session: after /terminate cleared the link → the LATEST session",
          storage.notebook_latest_session_id(nbl["id"]) == "sl-new"
          and (storage.notebook_get(nbl["id"]) or {}).get("session_id") is None)
    nbl_other = storage.notebook_create("LatestOther", cells=[{"id": "c1"}])
    storage.notebook_update(nbl_other["id"], session_id="sl-new")
    check(f"[{label}] latest_session: latest session now another notebook's → None (no older fallback)",
          storage.notebook_latest_session_id(nbl["id"]) is None)
    check(f"[{label}] latest_session: unknown notebook id → None",
          storage.notebook_latest_session_id("999999999") is None)
    # Pool-granted VM: launched_at = its MINT time, OLDER than the previous cold
    # launch, but it ran (and was terminated) later. It must win.
    nbp = storage.notebook_create("LatestPool", cells=[{"id": "c1"}])
    storage.vm_session_create("vmp-cold", notebook_id=nbp["id"], session_id="sp-old", memory_mib=1024,
                              architecture="x86_64", launched_at="2026-03-02T00:00:00+00:00")
    storage.vm_session_update_state("vmp-cold", "TERMINATED")
    time.sleep(0.01)
    storage.vm_session_create("vmp-pool", notebook_id=nbp["id"], session_id="sp-new", memory_mib=1024,
                              architecture="arm64", launched_at="2026-03-01T00:00:00+00:00")
    storage.vm_session_update_state("vmp-pool", "TERMINATED")
    check(f"[{label}] latest_session: a later-terminated pooled VM (older mint time) wins",
          storage.notebook_latest_session_id(nbp["id"]) == "sp-new")
    for _nb in (nbl, nbl_other, nbp):
        storage.notebook_delete(_nb["id"])
    _parity_checks(storage, label)
    _concurrency_checks(storage, label)
    _conflict_checks(storage, label)


def _is_iso_utc(v):
    """True for an ISO-8601 string with a +00:00 UTC offset (the shape every
    backend must return)."""
    from datetime import datetime as _dt
    if not isinstance(v, str) or not v.endswith("+00:00"):
        return False
    try:
        _dt.fromisoformat(v)
        return True
    except ValueError:
        return False


def _parity_checks(storage, label: str):
    """Behavior both backends must share: version history, pool ledger, cost
    quantities, paging bounds, retention, NULL ordering, timestamp shape,
    curated views."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from proxy.storage.versioning import fingerprint_hash

    def _ago(**kw):
        return (_dt.now(_tz.utc) - _td(**kw)).isoformat()

    # ---- notebook_create return keys + timestamp shape ----------------------
    nbp = storage.notebook_create("ParityNB", cells=[{"id": "p1"}])
    check(f"[{label}] notebook_create returns id + created_at",
          nbp.get("id") is not None and _is_iso_utc(nbp.get("created_at")))
    nbp_id = nbp["id"]
    got = storage.notebook_get(nbp_id)
    check(f"[{label}] notebook created_at/updated_at are ISO UTC strings",
          _is_iso_utc(got["created_at"]) and _is_iso_utc(got["updated_at"]))
    check(f"[{label}] notebook_list timestamps are ISO UTC strings",
          all(_is_iso_utc(n["created_at"]) and _is_iso_utc(n["updated_at"])
              for n in storage.notebook_list()))

    # ---- NOTEBOOK VERSIONS (all 7 methods) ----------------------------------
    cells_a = [{"id": "a", "type": "code", "code": "x = 1"},
               {"id": "b", "type": "code", "code": "y = 2"}]
    cells_b = [{"id": "a", "type": "code", "code": "x = 10"},
               {"id": "b", "type": "code", "code": "y = 2"}]
    v1 = storage.notebook_version_create(nbp_id, cells_a, label="first", trigger="manual")
    v2 = storage.notebook_version_create(nbp_id, cells_a, trigger="auto")
    v3 = storage.notebook_version_create(nbp_id, cells_b, trigger="auto")
    check(f"[{label}] notebook_version_create header keys",
          {"version_group_id", "notebook_id", "label", "trigger", "created_at",
           "cell_count"} <= set(v1) and v1["cell_count"] == 2 and v1["label"] == "first"
          and _is_iso_utc(v1["created_at"]))
    vl = storage.notebook_version_list(nbp_id)
    check(f"[{label}] notebook_version_list newest first, headers only",
          [v["version_group_id"] for v in vl]
          == [v3["version_group_id"], v2["version_group_id"], v1["version_group_id"]]
          and all("cells" not in v and _is_iso_utc(v["created_at"]) for v in vl))
    check(f"[{label}] notebook_version_list limit",
          len(storage.notebook_version_list(nbp_id, limit=2)) == 2)
    hit = storage.notebook_version_find_by_fingerprint(nbp_id, fingerprint_hash(cells_a))
    check(f"[{label}] find_by_fingerprint returns the most recent matching header",
          hit is not None and hit["version_group_id"] == v2["version_group_id"])
    check(f"[{label}] find_by_fingerprint miss -> None",
          storage.notebook_version_find_by_fingerprint(nbp_id, "0" * 64) is None)
    full = storage.notebook_version_get(v3["version_group_id"])
    check(f"[{label}] notebook_version_get returns ordered cells",
          full is not None and [c["id"] for c in full["cells"]] == ["a", "b"]
          and full["cells"][0]["code"] == "x = 10")
    check(f"[{label}] notebook_version_get unknown -> None",
          storage.notebook_version_get("no-such-version") is None)
    cv = storage.notebook_cell_versions(nbp_id, "a")
    check(f"[{label}] notebook_cell_versions: one row per version, newest first",
          [r["version_group_id"] for r in cv]
          == [v3["version_group_id"], v2["version_group_id"], v1["version_group_id"]]
          and cv[0]["code"] == "x = 10" and _is_iso_utc(cv[0]["created_at"]))
    check(f"[{label}] notebook_cell_versions unknown cell -> []",
          storage.notebook_cell_versions(nbp_id, "zzz") == [])
    check(f"[{label}] negative limit -> [] (versions, cell versions, runs)",
          storage.notebook_version_list(nbp_id, limit=-1) == []
          and storage.notebook_cell_versions(nbp_id, "a", limit=-1) == []
          and storage.notebook_run_list(nbp_id, limit=-1) == [])
    check(f"[{label}] notebook_version_delete is scoped by notebook_id",
          storage.notebook_version_delete("999999999", v2["version_group_id"]) is False
          and storage.notebook_version_get(v2["version_group_id"]) is not None)
    check(f"[{label}] notebook_version_delete removes header + cell rows",
          storage.notebook_version_delete(nbp_id, v2["version_group_id"]) is True
          and storage.notebook_version_get(v2["version_group_id"]) is None
          and len(storage.notebook_cell_versions(nbp_id, "a")) == 2)
    check(f"[{label}] notebook_version_delete unknown -> False",
          storage.notebook_version_delete(nbp_id, v2["version_group_id"]) is False)
    for i in range(3):
        storage.notebook_version_create(
            nbp_id, [{"id": "a", "type": "code", "code": f"z = {i}"}], trigger="auto")
    check(f"[{label}] notebook_version_prune removes old AUTO versions only",
          storage.notebook_version_prune(nbp_id, keep=1) == 3
          and {v["trigger"] for v in storage.notebook_version_list(nbp_id)}
          == {"manual", "auto"}
          and storage.notebook_version_get(v1["version_group_id"]) is not None
          and len(storage.notebook_version_list(nbp_id)) == 2)

    # ---- NULL ordering + malformed timestamps (runs) ------------------------
    storage.notebook_run_create("pr_done", notebook_id=nbp_id, trigger="run_all",
                                started_at=_ago(minutes=5), finished_at=_ago(minutes=4))
    storage.notebook_run_create("pr_open", notebook_id=nbp_id, trigger="run_all",
                                started_at=_ago(minutes=1), finished_at=None)
    storage.notebook_run_create("pr_old_done", notebook_id=nbp_id, trigger="run_all",
                                started_at=_ago(minutes=9), finished_at=_ago(minutes=8))
    rl = [r["run_id"] for r in storage.notebook_run_list(nbp_id)]
    check(f"[{label}] notebook_run_list: NULL finished_at sorts last",
          rl[-1] == "pr_open" and set(rl) == {"pr_done", "pr_open", "pr_old_done"})
    check(f"[{label}] notebook_run_list limit=0 -> []",
          storage.notebook_run_list(nbp_id, limit=0) == [])
    storage.notebook_run_create("pv_open", notebook_id=nbp_id, microvm_id="vmpv",
                                started_at=None, finished_at=None)
    storage.notebook_run_create("pv_done", notebook_id=nbp_id, microvm_id="vmpv",
                                started_at=_ago(minutes=3), finished_at=_ago(minutes=2))
    check(f"[{label}] notebook_run_list_for_vms: NULL sort key last",
          [r["run_id"] for r in storage.notebook_run_list_for_vms(["vmpv"])]
          == ["pv_done", "pv_open"]
          and storage.notebook_run_list_for_vms(["vmpv"], limit=-1) == [])
    bad = storage.notebook_run_create("pr_bad", notebook_id=nbp_id, trigger="single",
                                      started_at="garbage", finished_at="2024-13-99")
    check(f"[{label}] notebook_run_create: malformed timestamps -> NULL (returned)",
          bad.get("started_at") is None and bad.get("finished_at") is None)
    stored = {r["run_id"]: r for r in storage.notebook_run_list(nbp_id, limit=50)}
    check(f"[{label}] notebook_run_create: malformed timestamps stored as NULL",
          stored["pr_bad"]["started_at"] is None and stored["pr_bad"]["finished_at"] is None)
    off = storage.notebook_run_create("pr_off", notebook_id=nbp_id, trigger="single",
                                      started_at="2024-05-01T10:00:00+05:00")
    check(f"[{label}] notebook_run_create: offset input normalized to UTC",
          off["started_at"] == "2024-05-01T05:00:00+00:00")
    check(f"[{label}] run list timestamps (created_at/updated_at/started_at) are ISO UTC",
          all(_is_iso_utc(r["created_at"]) and _is_iso_utc(r["updated_at"])
              and (r["started_at"] is None or _is_iso_utc(r["started_at"]))
              for r in stored.values()))

    # ---- COST QUANTITIES (vm1 is TERMINATED with burst 3.0, see VM SESSIONS) -
    cq = storage.vm_cost_quantities("vm1")
    check(f"[{label}] vm_cost_quantities: keys and numeric types",
          cq is not None
          and {"state", "memory_mib", "running_secs", "suspended_secs",
               "burst_mb_seconds"} <= set(cq)
          and all(isinstance(cq[k], float)
                  for k in ("running_secs", "suspended_secs", "burst_mb_seconds"))
          and cq["state"] == "TERMINATED" and abs(cq["burst_mb_seconds"] - 3.0) < 1e-9)
    cqs = storage.vm_cost_quantities_for_session("s1")
    check(f"[{label}] vm_cost_quantities_for_session: rows typed, timestamps ISO UTC",
          len(cqs) >= 1 and all(isinstance(r["running_secs"], float)
                                and _is_iso_utc(r["launched_at"]) for r in cqs))
    check(f"[{label}] vm_cost_quantities_many: present ids only",
          set(storage.vm_cost_quantities_many(["vm1", "vm-none"])) == {"vm1"})

    # ---- VM session / metrics timestamp shape -------------------------------
    sess = storage.vm_session_get("vm1")
    check(f"[{label}] vm_session timestamps are ISO UTC strings",
          _is_iso_utc(sess["launched_at"]) and _is_iso_utc(sess["terminated_at"])
          and _is_iso_utc(sess["updated_at"]))
    check(f"[{label}] metrics_get_latest timestamp is an ISO UTC string",
          _is_iso_utc(storage.metrics_get_latest("vm2")["timestamp"]))

    # ---- metrics_cleanup deletes old rows, keeps recent ---------------------
    now_ts = _dt.now(_tz.utc).timestamp()
    storage.metrics_record("vmclean_old", 1.0, 1.0, 100.0, 1.0, 100.0, 0, 0, 1, 1.0,
                           recorded_at=now_ts - 3 * 3600)
    storage.metrics_record("vmclean_new", 1.0, 1.0, 100.0, 1.0, 100.0, 0, 0, 1, 1.0,
                           recorded_at=now_ts - 60)
    storage.metrics_cleanup(hours=2)
    check(f"[{label}] metrics_cleanup deletes rows older than the window",
          storage.metrics_get_latest("vmclean_old") is None)
    check(f"[{label}] metrics_cleanup keeps rows inside the window",
          storage.metrics_get_latest("vmclean_new") is not None)

    # ---- burst parity (behavior, replaces the old source-text check) --------
    storage.metrics_record("vmbp", 1.0, 1.0, 3000.0, 1.0, 100.0, 0, 0, 1, 1.0,
                           recorded_at=now_ts - 20)
    storage.metrics_record("vmbp", 1.0, 1.0, 3000.0, 1.0, 100.0, 0, 0, 1, 1.0,
                           recorded_at=now_ts - 19)
    bp = storage.metrics_get_burst("vmbp", 2048.0)
    check(f"[{label}] metrics_get_burst: 2 samples above the tier -> 1 episode, 1s",
          bp is not None and bp["burst_samples"] == 2 and bp["burst_episodes"] == 1
          and abs(bp["burst_seconds"] - 1.0) < 1e-6
          and abs(bp["burst_mb_seconds"] - 952.0) < 1e-6)

    # ---- POOL VMS -----------------------------------------------------------
    arch, tier = "arm64", 2048
    pa = storage.pool_vm_create("pv-a", arch, tier, "http://a", "fake-a")
    check(f"[{label}] pool_vm_create returns the row and starts 'launching'",
          pa["vm_id"] == "pv-a" and pa["state"] == "launching"
          and pa["real_session_id"] is None)
    storage.pool_vm_create("pv-b", arch, tier, "http://b", "fake-b", state="available")
    storage.pool_vm_create("pv-c", arch, tier, "http://c", "fake-c", state="available")
    storage.pool_vm_create("pv-d", "x86_64", 4096, "http://d", "fake-d", state="available")
    check(f"[{label}] pool_vm_set_state: known -> True, unknown -> False",
          storage.pool_vm_set_state("pv-a", "available") is True
          and storage.pool_vm_set_state("pv-none", "available") is False)
    storage.pool_vm_backdate("pv-a", launched_at=_ago(seconds=300))
    storage.pool_vm_backdate("pv-b", launched_at=_ago(seconds=100))
    storage.pool_vm_backdate("pv-c", launched_at=_ago(seconds=10))
    check(f"[{label}] pool_vm_backdate: known -> True, unknown -> False, no-op -> False",
          storage.pool_vm_backdate("pv-none", launched_at=_ago(seconds=1)) is False
          and storage.pool_vm_backdate("pv-a") is False)
    check(f"[{label}] pool_vm_list filters (state/arch/size)",
          {r["vm_id"] for r in storage.pool_vm_list()} >= {"pv-a", "pv-b", "pv-c", "pv-d"}
          and {r["vm_id"] for r in storage.pool_vm_list(architecture=arch, size_mib=tier)}
          == {"pv-a", "pv-b", "pv-c"}
          and [r["vm_id"] for r in storage.pool_vm_list(architecture="x86_64")] == ["pv-d"]
          and storage.pool_vm_list(state="no-such-state") == [])
    check(f"[{label}] pool_vm_list timestamps are ISO UTC strings",
          all(_is_iso_utc(r["launched_at"]) and _is_iso_utc(r["updated_at"])
              for r in storage.pool_vm_list()))
    check(f"[{label}] pool_vm_count_by_size counts the tier's available VMs",
          storage.pool_vm_count_by_size(arch, tier, states=("available",)) == 3
          and storage.pool_vm_count_by_size(arch, tier, states=("launching",)) == 0
          and storage.pool_vm_count_by_size("x86_64", 4096, states=("available",)) == 1)
    old = storage.pool_vm_list_older_than(60, state="available")
    check(f"[{label}] pool_vm_list_older_than by launched_at (backdate seam)",
          {r["vm_id"] for r in old if r["vm_id"].startswith("pv-")} == {"pv-a", "pv-b"}
          and all(_is_iso_utc(r["launched_at"]) for r in old))
    storage.pool_vm_backdate("pv-c", updated_at=_ago(seconds=500))
    check(f"[{label}] pool_vm_list_older_than by updated_at",
          "pv-c" in {r["vm_id"] for r in storage.pool_vm_list_older_than(
              60, state="available", since_column="updated_at")}
          and "pv-b" not in {r["vm_id"] for r in storage.pool_vm_list_older_than(
              60, state="available", since_column="updated_at")})
    c1 = storage.pool_vm_claim_youngest(arch, tier, "real-1")
    check(f"[{label}] pool_vm_claim_youngest takes the youngest, -> 'granting'",
          c1 is not None and c1["vm_id"] == "pv-c" and c1["state"] == "granting"
          and c1["real_session_id"] == "real-1" and _is_iso_utc(c1["launched_at"]))
    c2 = storage.pool_vm_claim_youngest(arch, tier, "real-2")
    check(f"[{label}] pool_vm_claim_youngest: next youngest second",
          c2 is not None and c2["vm_id"] == "pv-b")
    check(f"[{label}] pool_vm_claim_youngest ignores other tiers",
          storage.pool_vm_claim_youngest("arm64", 8192, "real-x") is None)
    # Atomic single claim: one 'available' spare, 8 contenders -> exactly one wins.
    results, errors = _race(8, lambda i: storage.pool_vm_claim_youngest(arch, tier, f"race-{i}"))
    winners = [r for r in results if r is not None]
    check(f"[{label}] pool_vm_claim_youngest: concurrent claims -> exactly one winner",
          not errors and len(winners) == 1 and winners[0]["vm_id"] == "pv-a")
    check(f"[{label}] pool_vm_claim_youngest: tier exhausted -> None",
          storage.pool_vm_claim_youngest(arch, tier, "real-3") is None)
    storage.pool_vm_set_state("pv-a", "available", real_session_id=None)
    check(f"[{label}] pool_vm_set_state(real_session_id=None) clears it",
          [r for r in storage.pool_vm_list() if r["vm_id"] == "pv-a"][0]["real_session_id"] is None)
    for vid in ("pv-a", "pv-b", "pv-c", "pv-d"):
        storage.pool_vm_delete(vid)
    storage.pool_vm_delete("pv-none")  # no-op
    check(f"[{label}] pool_vm_delete removes rows (unknown id is a no-op)",
          not any(r["vm_id"].startswith("pv-") for r in storage.pool_vm_list()))

    # ---- NOTEBOOK OWNED SESSIONS (behavior, replaces the source-text check) -
    nbx = storage.notebook_create("OwnX", cells=[{"id": "c1"}])
    nby = storage.notebook_create("OwnY", cells=[{"id": "c1"}])
    storage.notebook_update(nbx["id"], session_id="own-x")
    storage.notebook_update(nby["id"], session_id="own-y")
    storage.vm_session_create("vm-own-y", notebook_id=nbx["id"], session_id="own-y",
                              memory_mib=2048)
    check(f"[{label}] owned_session_ids: only sessions no other notebook uses",
          storage.notebook_owned_session_ids(nbx["id"]) == ["own-x"])
    for _n in (nbx, nby):
        storage.notebook_delete(_n["id"])

    # ---- CURATED VIEWS (v_nb_*): same names + readable on this backend ------
    from proxy.storage import postgres_db as _pg
    pg_views = sorted(v for v in _pg.view_names() if v.startswith("v_nb_"))
    if label == "SQLite":
        conn = storage._get_conn()
        sq_views = sorted(r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name LIKE 'v_nb_%'"))
        rows = {v: conn.execute(f"SELECT * FROM {v} LIMIT 1") for v in sq_views}
        cols = {v: [d[0] for d in c.description] for v, c in rows.items()}
    else:
        sq_views = pg_views
        cols = {}
        with storage._db() as (conn, cur):
            for v in pg_views:
                cur.execute(f"SELECT * FROM {v} LIMIT 1")
                cols[v] = [d[0] for d in cur.description]
    check(f"[{label}] v_nb_* view set equals the Postgres DDL view set",
          sq_views == pg_views and len(pg_views) >= 8)
    check(f"[{label}] every v_nb_* view is queryable and joins on notebook_id",
          all(c for c in cols.values())
          and all("notebook_id" in cols[v] for v in cols
                  if v in ("v_nb_notebook", "v_nb_cells", "v_nb_runs", "v_nb_versions")))
    runs_view = storage._get_conn().execute(
        "SELECT run_id, started_at, finished_at, duration_sec FROM v_nb_runs WHERE notebook_id = ?",
        (nbp_id,)).fetchall() if label == "SQLite" else None
    if runs_view is not None:
        by = {r["run_id"]: r for r in runs_view}
        check(f"[{label}] v_nb_runs: malformed run -> NULL timestamps and duration",
              by["pr_bad"]["started_at"] is None and by["pr_bad"]["duration_sec"] is None
              and by["pr_done"]["duration_sec"] is not None)

    storage.notebook_delete(nbp_id)


def _race(n, fn):
    """Run fn(i) on n threads released together; return (results, errors)."""
    import threading
    barrier = threading.Barrier(n)
    results, errors = [None] * n, []

    def _worker(i):
        try:
            barrier.wait()
            results[i] = fn(i)
        except Exception as e:  # noqa: BLE001 - collected and asserted on
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def _concurrency_checks(storage, label: str):
    """Atomic upserts and state transitions under concurrent threads
    (code-review D-B-06 / D-B-10)."""
    # ---- entity_upsert: 8 first-time upserts of one key, different partial fields.
    def _entity(i):
        kw = {}
        if i == 0:
            kw["doc_s3_key"] = "s3://doc/race"
        elif i == 1:
            kw["change_signal"] = {"etag": "e-race"}
        elif i == 2:
            kw["status"] = "ready"
        storage.entity_upsert("s3://b/race.csv", "s3", **kw)
    _, errors = _race(8, _entity)
    check(f"[{label}] concurrent entity_upsert: no error (no unique-key race)",
          not errors)
    row = storage.entity_get("s3://b/race.csv") or {}
    check(f"[{label}] concurrent entity_upsert: union of fields, none overwritten by None",
          row.get("doc_s3_key") == "s3://doc/race"
          and row.get("change_signal") == {"etag": "e-race"}
          and row.get("status") == "ready" and row.get("source_type") == "s3")

    # ---- local_entity_upsert: same race on the (session_id, filepath) key.
    def _local(i):
        kw = {}
        if i == 0:
            kw["doc_s3_key"] = "s3://doc/lrace"
        elif i == 1:
            kw["change_signal"] = {"mtime": 7}
        elif i == 2:
            kw["status"] = "ready"
        storage.local_entity_upsert("srace", "/tmp/race.csv", **kw)
    _, errors = _race(8, _local)
    check(f"[{label}] concurrent local_entity_upsert: no error", not errors)
    row = storage.local_entity_get("srace", "/tmp/race.csv") or {}
    check(f"[{label}] concurrent local_entity_upsert: union of fields kept",
          row.get("doc_s3_key") == "s3://doc/lrace"
          and row.get("change_signal") == {"mtime": 7} and row.get("status") == "ready")

    # ---- state transitions: one vm_state_log row per real transition.
    storage.vm_session_create("vmrace", notebook_id=None, session_id="srace",
                              memory_mib=2048, endpoint="http://race")
    _, errors = _race(8, lambda i: storage.vm_session_update_state("vmrace", "SUSPENDED"))
    check(f"[{label}] concurrent update_state: no error", not errors)
    states = [r["new_state"] for r in storage.vm_state_log_get("vmrace")]
    check(f"[{label}] concurrent update_state: exactly one SUSPENDED log row",
          states == ["RUNNING", "SUSPENDED"])

    def _terminate(i):
        if i % 2:
            return storage.vm_session_mark_terminated("vmrace")
        storage.vm_session_update_state("vmrace", "TERMINATED")
        return None
    results, errors = _race(8, _terminate)
    check(f"[{label}] concurrent mark_terminated/update_state: no error", not errors)
    states = [r["new_state"] for r in storage.vm_state_log_get("vmrace")]
    check(f"[{label}] concurrent terminate race: exactly one TERMINATED log row",
          states == ["RUNNING", "SUSPENDED", "TERMINATED"])
    check(f"[{label}] concurrent terminate race: mark_terminated returned True at most once",
          sum(1 for r in results if r is True) <= 1)
    sess = storage.vm_session_get("vmrace") or {}
    check(f"[{label}] concurrent terminate race: row TERMINATED with terminated_at",
          sess.get("state") == "TERMINATED" and sess.get("terminated_at") is not None)


def _raw(storage, label: str, sql: str, params=()):
    """Run one statement on the backend's own connection ('?' placeholders) and
    return the first row as a tuple (None for no row / no result set). For
    columns the interface has no setter or getter for (image_arn, created_at)."""
    if label == "SQLite":
        conn = storage._get_conn()
        row = conn.execute(sql, params).fetchone()
        conn.commit()
        return tuple(row) if row is not None else None
    with storage._db() as (conn, cur):
        cur.execute(sql.replace("?", "%s"), params)
        return cur.fetchone() if cur.description else None


def _conflict_checks(storage, label: str):
    """Re-creating an existing key (code-review item #6). Same rule on every
    backend: vm_session_create keeps burst_mb_seconds and image_arn, clears
    terminated_at, takes the rest from the new call and appends a state-log row;
    notebook_run_create keeps created_at and takes the rest from the new call."""
    # ---- vm_session_create on an existing microvm_id
    storage.vm_session_create("vmdup", session_id="sdup1", memory_mib=2048,
                              endpoint="http://old", idle_timeout_sec=60)
    storage.vm_session_update_burst("vmdup", 7.5)
    _raw(storage, label, "UPDATE vm_sessions SET image_arn = ? WHERE microvm_id = ?",
         ("arn:img-1", "vmdup"))
    storage.vm_session_update_state("vmdup", "TERMINATED")
    _raw(storage, label, "UPDATE vm_sessions SET updated_at = ? WHERE microvm_id = ?",
         ("2000-01-01T00:00:00Z", "vmdup"))
    check(f"[{label}] re-create setup: TERMINATED with terminated_at",
          (storage.vm_session_get("vmdup") or {}).get("terminated_at") is not None)
    if label == "SQLite":
        storage._get_conn().execute("DELETE FROM _sync_deletes")
        storage._get_conn().commit()
    log_before = len(storage.vm_state_log_get("vmdup"))
    storage.vm_session_create("vmdup", session_id="sdup2", memory_mib=4096,
                              endpoint="http://new", idle_timeout_sec=120)
    sess = storage.vm_session_get("vmdup") or {}
    check(f"[{label}] re-create vm_session: burst_mb_seconds kept",
          abs((sess.get("burst_mb_seconds") or 0) - 7.5) < 1e-9)
    arn = _raw(storage, label, "SELECT image_arn FROM vm_sessions WHERE microvm_id = ?",
               ("vmdup",))
    check(f"[{label}] re-create vm_session: image_arn kept", arn and arn[0] == "arn:img-1")
    check(f"[{label}] re-create vm_session: terminated_at cleared",
          sess.get("terminated_at") is None)
    check(f"[{label}] re-create vm_session: state RUNNING and new columns applied",
          sess.get("state") == "RUNNING" and sess.get("session_id") == "sdup2"
          and sess.get("memory_mib") == 4096 and sess.get("endpoint") == "http://new"
          and sess.get("idle_timeout_sec") == 120)
    check(f"[{label}] re-create vm_session: one state-log row appended",
          len(storage.vm_state_log_get("vmdup")) == log_before + 1)
    wm = _raw(storage, label, "SELECT updated_at FROM vm_sessions WHERE microvm_id = ?",
              ("vmdup",))
    check(f"[{label}] re-create vm_session: updated_at watermark advanced",
          wm and not str(wm[0]).startswith("2000-01-01"))
    if label == "SQLite":
        # Updated in place, so the sync worker gets an upsert and no tombstone.
        tomb = _raw(storage, label, "SELECT COUNT(*) FROM _sync_deletes")
        check("[SQLite] re-create vm_session: no _sync_deletes tombstone", tomb[0] == 0)

    # ---- notebook_run_create on an existing run_id
    storage.notebook_run_create("rundup", notebook_id=None, session_id="sdup1",
                                trigger="single", cells_executed=1, total_exec_ms=10.0)
    _raw(storage, label,
         "UPDATE notebook_runs SET created_at = ?, updated_at = ? WHERE run_id = ?",
         ("2000-01-01 00:00:00", "2000-01-01T00:00:00Z", "rundup"))
    storage.notebook_run_create("rundup", notebook_id=None, session_id="sdup2",
                                trigger="run_all", cells_executed=3, total_exec_ms=30.0)
    row = _raw(storage, label,
               "SELECT created_at, updated_at, session_id, trigger, cells_executed, total_exec_ms "
               "FROM notebook_runs WHERE run_id = ?", ("rundup",))
    check(f"[{label}] re-create notebook_run: created_at kept",
          row and str(row[0]).startswith("2000-01-01"))
    check(f"[{label}] re-create notebook_run: updated_at watermark advanced",
          row and not str(row[1]).startswith("2000-01-01"))
    check(f"[{label}] re-create notebook_run: new columns applied",
          row and row[2] == "sdup2" and row[3] == "run_all" and row[4] == 3
          and abs(row[5] - 30.0) < 1e-9)
    if label == "SQLite":
        tomb = _raw(storage, label, "SELECT COUNT(*) FROM _sync_deletes")
        check("[SQLite] re-create notebook_run: no _sync_deletes tombstone", tomb[0] == 0)


# ── backend factories ────────────────────────────────────────────────────────
def _run_sqlite():
    """Fresh SQLite backend on a temp file so we never touch the real dev DB."""
    import proxy.storage.sqlite_db as sq
    tmp = tempfile.mkdtemp(prefix="conf_sqlite_")
    sq.DB_DIR = tmp
    sq.DB_PATH = os.path.join(tmp, "conf.db")
    # reset thread-local so we don't reuse a connection to the old path
    sq._local = __import__("threading").local()
    # reset in-process lock state between runs
    sq._HELD_SESSION_LOCKS.clear()
    storage = sq.SqliteStorage()
    storage.initialize()
    run_conformance(storage, "SQLite")


def _run_postgres(dsn):
    """Run the conformance body against Postgres in an ISOLATED schema.

    A throwaway schema parity_<rand> is created and selected through the DSN
    'options' (-c search_path=...); only that schema is dropped afterwards. If
    the DSN already pins a non-public search_path it is used as-is (and left in
    place). The body issues DELETEs on the main tables, so it refuses to run
    against the public schema unless TEST_PG_ALLOW_PUBLIC=1."""
    import secrets
    import psycopg2
    from psycopg2.extensions import make_dsn, parse_dsn
    from proxy.storage.postgres_db import PostgresStorage

    created = None
    admin_dsn = dsn
    opts = parse_dsn(dsn).get("options", "") or ""
    if "search_path" not in opts and os.environ.get("TEST_PG_ALLOW_PUBLIC") != "1":
        created = "parity_" + secrets.token_hex(4)
        admin = psycopg2.connect(dsn)
        try:
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(f"CREATE SCHEMA {created}")
        finally:
            admin.close()
        dsn = make_dsn(dsn, options=(opts + f" -c search_path={created}").strip())
    try:
        storage = PostgresStorage(connection_string=dsn)
        with storage._db() as (conn, cur):
            cur.execute("SELECT current_schema()")
            schema = cur.fetchone()[0]
        if schema == "public" and os.environ.get("TEST_PG_ALLOW_PUBLIC") != "1":
            raise RuntimeError("refusing to run conformance on the public schema "
                               "(set a non-public search_path or TEST_PG_ALLOW_PUBLIC=1)")
        print(f"(Postgres conformance schema: {schema})")
        run_conformance(storage, "Postgres")
    finally:
        if created:
            admin = psycopg2.connect(admin_dsn)
            try:
                admin.autocommit = True
                with admin.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {created} CASCADE")
            finally:
                admin.close()


def _patch_env_crypto_identity():
    """Make seal/unseal identity so the notebook_env checks need no AWS master key.

    The storage backends import seal_env_vars/unseal_env_vars lazily from
    proxy.platform.env_crypto inside notebook_env_set/get, so patching the module
    attributes here is picked up. reset_cache_for_tests clears any cached key."""
    import proxy.platform.env_crypto as ec
    ec.seal_env_vars = lambda notebook_id, env_vars: dict(env_vars or {})
    ec.unseal_env_vars = lambda notebook_id, env_vars: dict(env_vars or {})
    ec.reset_cache_for_tests()


def main():
    _patch_env_crypto_identity()
    _run_sqlite()
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        print("\n(skipping Postgres: set TEST_PG_DSN to run the Postgres conformance)")
    else:
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            print("\n(skipping Postgres: psycopg2 not installed — "
                  "pip install psycopg2-binary)")
        else:
            try:
                _run_postgres(dsn)
            except Exception as e:
                # A connection/setup failure should FAIL loudly (the user asked
                # for Postgres), not silently skip.
                check(f"[Postgres] backend ran without error: {e}", False)

    # ── summary ──────────────────────────────────────────────────────────
    passed = sum(1 for _, ok in _checks if ok)
    total = len(_checks)
    print(f"\n{'='*60}\nRESULT: {passed}/{total} checks passed")
    if passed != total:
        print("FAILED checks:")
        for name, ok in _checks:
            if not ok:
                print(f"  ❌ {name}")
        sys.exit(1)
    print("ALL CONFORMANCE CHECKS PASSED ✓")


# ── pytest entry point ───────────────────────────────────────────────────────
# The body above is a script-style harness (check()/main() print + sys.exit),
# which `pytest` does NOT collect — so `pytest tests/storage/...` reported "no
# tests ran" and this conformance coverage was easy to miss in CI. This thin
# test_* wrapper makes it discoverable: it runs the SQLite conformance and
# asserts every check passed, surfacing the failed names in the assertion.
# (Postgres stays opt-in via TEST_PG_DSN + the __main__ runner, so pytest on a
# laptop/CI with no DB server stays green.)
def test_sqlite_conformance():
    _checks.clear()
    _patch_env_crypto_identity()
    _run_sqlite()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "SQLite conformance failures: " + "; ".join(failed)


def test_postgres_conformance():
    """Same body against Postgres; skipped without TEST_PG_DSN or psycopg2."""
    import pytest
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        pytest.skip("psycopg2 not installed")
    _checks.clear()
    _patch_env_crypto_identity()
    _run_postgres(dsn)
    failed = [name for name, ok in _checks if not ok]
    assert not failed, "Postgres conformance failures: " + "; ".join(failed)




if __name__ == "__main__":
    main()
