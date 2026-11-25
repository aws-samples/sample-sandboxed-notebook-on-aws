#!/usr/bin/env python3
# test-kind: offline
"""
Offline test: names the executor injects into the namespace (the per-run
display() shim and the helpers) are never checkpointed, never override the
live executor's own objects on restore, and display() keeps working after a
restore. Drives the REAL SandboxExecutor + CheckpointManager.save()/restore()
against the in-memory FakeS3 of test_dag_checkpoint.py (no AWS, no MicroVM).

Regression for ac2b712: the display() shim is a closure over the executor, so
dill saved vars/display.pkl = a full copy of the executor and its namespace.

Proves:
  • save: no `display`, no alias of the shim, no injected helper in the
    manifest; the manifest is exactly the user vars; the saved size is the user
    vars' size (not ~2x); a user `display = 5` IS saved.
  • restore into a fresh executor: no stale `display` is adopted; display(df)
    in the next cell yields its tables in the result outputs.
  • an OLD-format checkpoint (made with the buggy filter: vars/display.pkl +
    the helpers) restores without replacing the live display shim or helpers,
    deletes nothing from S3, and display() works afterwards; the next save
    drops the stale entries (normal delete) and the checkpoint still restores.
  • the restore-override inference: a stale shim left in the namespace is
    rebound by the next execute(), so display() output is NOT lost (refuted).
Run:
    env -i PATH="$PATH" HOME="$HOME" AWS_REGION=us-west-2 AWS_ACCESS_KEY_ID=testing \
      AWS_SECRET_ACCESS_KEY=testing AWS_SHARED_CREDENTIALS_FILE=/dev/null \
      AWS_CONFIG_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. \
      .venv/bin/python tests/checkpoint/test_injected_shims_not_saved.py
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.notebook.executor import SandboxExecutor  # noqa: E402
from app.platform.checkpoint.delta import build_var_blobs  # noqa: E402
from app.platform.checkpoint.namespace_filter import (  # noqa: E402
    _drop_injected_from_restored,
    _filter_namespace_for_save,
    _is_display_shim,
)
from test_dag_checkpoint import FakeS3, _Patched, _manager  # noqa: E402

_checks = []


def check(name, ok):
    _checks.append((name, bool(ok)))
    print(f"  {'✓' if ok else '❌'} {name}")


SESSION = "sess-shims"
PREFIX = f"sessions/{SESSION}"
USER_CELL = (
    "import pandas as pd\n"
    "big = list(range(40000))\n"
    "df = pd.DataFrame({'a': [1, 2, 3]})\n"
    "df2 = pd.DataFrame({'b': [4, 5]})\n"
    "small = {'k': 1}\n"
)
USER_VARS = {"big", "df", "df2", "small"}


def _manifest(s3):
    return json.loads(s3.store[f"{PREFIX}/manifest.json"].decode("utf-8"))["vars"]


def _tables(result):
    return [o for o in (result.outputs or []) if o.get("t") == "table"]


def _seeded_executor():
    ex = SandboxExecutor()
    r = ex.execute(USER_CELL, "c1")
    assert r.success, r.error
    # A run that uses display() (binds the shim) + an alias of the shim.
    r = ex.execute("display(df)\nshow = display", "c2")
    assert r.success, r.error
    return ex


def _restore(s3, ex):
    mgr = _manager(ex)
    with _Patched(s3):
        mgr.restore(SESSION)
    return mgr


def _check_pure_filter(ex):
    print("\nPure filter")
    shim = ex._display_fn
    check("the live display() shim is recognised", _is_display_shim(shim))

    def display(x):  # a user's own display function
        return x
    check("a user-defined display function is not a shim", not _is_display_shim(display))
    saved, skipped = _filter_namespace_for_save({"display": shim, "x": 1, "alias": shim})
    check("filter (no registry) skips the shim under any name",
          set(saved) == {"x"} and set(skipped["injected"]) == {"display", "alias"})
    saved, _ = _filter_namespace_for_save({"display": display, "x": 1})
    check("filter keeps a user-defined display function", set(saved) == {"display", "x"})
    helpers = ex._injected_helpers()
    name = "read_s3_csv"
    saved, _ = _filter_namespace_for_save({name: helpers[name], "x": 1}, injected=helpers)
    check("filter skips a helper still bound to the injected object", set(saved) == {"x"})
    saved, _ = _filter_namespace_for_save({name: 5}, injected=helpers)
    check("filter keeps a user value under a helper name", saved == {name: 5})
    ns = {"display": 5, name: helpers[name], "x": 1}
    dropped = _drop_injected_from_restored(ns, helpers)
    check("restore drop keeps a user display = 5, drops the helper copy",
          ns == {"display": 5, "x": 1} and dropped == [name])


def _check_save(ex):
    print("\nSave (fixed image)")
    s3 = FakeS3()
    mgr = _manager(ex)
    with _Patched(s3):
        mgr.save(SESSION)
    man = _manifest(s3)
    helpers = set(ex._injected_helpers())
    check("no `display` in the manifest", "display" not in man)
    check("no alias of the shim (`show`) in the manifest", "show" not in man)
    check("no injected helper in the manifest", not (set(man) & helpers))
    check("manifest is exactly the user vars", set(man) == USER_VARS)
    check("no vars/display.pkl object written", f"{PREFIX}/vars/display.pkl" not in s3.store)
    user_blobs, _ = build_var_blobs({n: ex._namespace[n] for n in USER_VARS})
    user_bytes = sum(m["size"] for m in user_blobs.values())
    serialized = mgr.last_save_sizes.get("serialize", 0)
    check(f"saved size ≈ user vars ({serialized} B vs {user_bytes} B, not ~2x)",
          user_bytes > 0 and serialized <= user_bytes * 1.05)
    check("save summary vars= counts the user vars only",
          mgr.last_save_summary.get("vars") == len(USER_VARS))
    return s3


def _check_user_display_saved():
    print("\nUser-defined display")
    ex = SandboxExecutor()
    r = ex.execute("display = 5\nx = 1", "c1")
    check("cell `display = 5` ran", r.success)
    s3 = FakeS3()
    with _Patched(s3):
        _manager(ex).save(SESSION)
    check("a user `display = 5` IS saved", "display" in _manifest(s3))
    ex2 = SandboxExecutor()
    _restore(s3, ex2)
    check("and restored as 5", ex2._namespace.get("display") == 5)


def _check_restore_fresh(s3):
    print("\nRestore (fixed checkpoint) into a fresh executor")
    keys_before = dict(s3.store)
    ex2 = SandboxExecutor()
    _restore(s3, ex2)
    check("user vars restored", all(n in ex2._namespace for n in USER_VARS))
    check("no stale `display` adopted", "display" not in ex2._namespace)
    check("restore deleted/rewrote nothing in S3", s3.store == keys_before)
    r = ex2.execute("display(df)\ndisplay(df2)\nprint('done')", "c3")
    tabs = _tables(r)
    check("display() after restore: both tables in the outputs",
          r.success and len(tabs) == 2 and "done" in r.output)


def _old_format_checkpoint(ex):
    """The checkpoint the buggy image wrote: every non-module name, i.e. the
    user vars + the display shim (+ alias) + the helpers. Built by calling the
    real delta save with that namespace, with the old image's pickler (no
    notebook-namespace marker: the shim's closure carries the executor and a
    full copy of its namespace)."""
    import app.platform.checkpoint.manager as manager_mod
    s3 = FakeS3()
    ns = {k: v for k, v in ex._namespace.items()
          if not (k.startswith("__") and k.endswith("__"))
          and type(v).__name__ != "module"}
    mgr = _manager(ex)
    real = manager_mod.build_var_blobs
    manager_mod.build_var_blobs = lambda *a, **kw: real(*a, **{**kw, "namespace": None})
    try:
        mgr._save_namespace_delta(s3, PREFIX, ns, {}, {})
    finally:
        manager_mod.build_var_blobs = real
    return s3


def _check_old_format(ex):
    print("\nRestore an OLD-format checkpoint (vars/display.pkl + helpers)")
    s3 = _old_format_checkpoint(ex)
    man = _manifest(s3)
    check("precondition: old manifest lists display, show and helpers",
          {"display", "show", "read_s3_csv"} <= set(man))
    check("precondition: display is its own large object (it copies `big`)",
          man["display"]["object"] == "vars/display.pkl" and f"{PREFIX}/vars/display.pkl" in s3.store)

    # The inference: a stale shim in the namespace → does display() lose items?
    ex_inf = SandboxExecutor()
    import dill
    stale = dill.loads(s3.store[f"{PREFIX}/vars/display.pkl"])
    closure = [c.cell_contents for c in (stale.__closure__ or ())]
    check("inference setup: the old display.pkl is a closure over a COPY of the executor",
          any(isinstance(c, SandboxExecutor) and c is not ex_inf for c in closure))
    ex_inf._namespace.update({"display": stale, "df": ex._namespace["df"]})
    r = ex_inf.execute("display(df)", "c-inf")
    check("inference refuted: execute() rebinds display, the table is in the outputs",
          r.success and len(_tables(r)) == 1 and ex_inf._namespace["display"] is ex_inf._display_fn)

    keys_before = set(s3.store)
    ex3 = SandboxExecutor()
    ex3.execute("pre = 1", "c0")      # a live shim exists before the restore
    live_shim = ex3._display_fn
    live_helper = ex3._namespace["read_s3_csv"]
    mgr3 = _restore(s3, ex3)
    check("live display shim kept (not replaced by the checkpointed copy)",
          ex3._namespace.get("display") is live_shim)
    check("live helper kept", ex3._namespace.get("read_s3_csv") is live_helper)
    check("the shim alias `show` is not adopted", "show" not in ex3._namespace)
    check("user vars restored", all(n in ex3._namespace for n in USER_VARS))
    check("RESTORE vars= counts the user vars only",
          mgr3._last_restore_var_count == len(USER_VARS))
    check("restore deleted nothing from S3 (display.pkl still readable)",
          set(s3.store) == keys_before)
    r = ex3.execute("display(df)\ndisplay(df2)", "c4")
    check("display() after the old-format restore: both tables in the outputs",
          r.success and len(_tables(r)) == 2)

    print("\nNext save after the old-format restore")
    with _Patched(s3):
        mgr3.save(SESSION)
    man2 = _manifest(s3)
    check("new manifest drops display / show / helpers",
          not ({"display", "show"} | set(ex3._injected_helpers())) & set(man2))
    check("stale vars/display.pkl removed by the normal delete path",
          f"{PREFIX}/vars/display.pkl" not in s3.store)
    ex4 = SandboxExecutor()
    _restore(s3, ex4)
    check("the re-saved checkpoint restores (user vars + pre)",
          all(n in ex4._namespace for n in USER_VARS | {"pre"}))


def _run():
    ex = _seeded_executor()
    _check_pure_filter(ex)
    s3 = _check_save(ex)
    _check_restore_fresh(s3)
    _check_old_format(ex)
    _check_user_display_saved()


def test_injected_shims_not_saved():
    """pytest entry point."""
    _checks.clear()
    _run()
    failed = [name for name, ok in _checks if not ok]
    assert not failed, f"failed checks: {failed}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    _run()
    passed = sum(1 for _, ok in _checks if ok)
    print(f"\n{passed}/{len(_checks)} checks passed")
    sys.exit(0 if passed == len(_checks) else 1)
