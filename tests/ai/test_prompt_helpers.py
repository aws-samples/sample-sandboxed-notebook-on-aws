#!/usr/bin/env python3
# test-kind: offline
"""Regression test for the hybrid helper-doc injection into the AI prompts.

Verifies, WITHOUT any network / live VM:
  1. scripts.gen_helpers_block generates a block that is up to date (--check parity).
  2. proxy.notebook.ai.helpers_block imports and HELPERS_BLOCK is non-trivial.
  3. proxy.notebook.ai.prompts imports and assembles cleanly.
  4. NO template placeholders are left unfilled in the assembled prompts.
  5. The auto-generated block actually made it into the base prompt (spot-check
     several helpers across categories, incl. the newest: train_model, helpers).
  6. The hand-written behavioral steering ("HOUSE IDIOMS", PREFER train_model,
     one-call-per-row, composition idioms) is still present.
  7. The notebook_agent module (the agent entrypoint) imports without error.

Run:  python scripts/test_prompt_helpers.py    (exit 0 = pass, 1 = fail)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def main() -> int:
    print("== 1. generator freshness (--check) ==")
    from scripts.gen_helpers_block import build_block, render_module
    gen_block = build_block()
    gen_module = render_module(gen_block)
    on_disk_path = os.path.join(_ROOT, "proxy", "notebook", "ai", "helpers_block.py")
    on_disk = ""
    if os.path.exists(on_disk_path):
        with open(on_disk_path, encoding="utf-8") as f:
            on_disk = f.read()
    check("helpers_block.py is up to date with the generator",
          on_disk.strip() == gen_module.strip(),
          "run: python scripts/gen_helpers_block.py")

    print("== 2. helpers_block module ==")
    from proxy.notebook.ai.helpers_block import HELPERS_BLOCK
    check("HELPERS_BLOCK is a substantial string", isinstance(HELPERS_BLOCK, str) and len(HELPERS_BLOCK) > 500,
          f"len={len(HELPERS_BLOCK) if isinstance(HELPERS_BLOCK, str) else 'n/a'}")
    check("HELPERS_BLOCK has category headers",
          all(h in HELPERS_BLOCK for h in ("Machine Learning / SageMaker:", "Generative AI:", "Data Loading:")))

    print("== 3. prompts assemble ==")
    from proxy.notebook.ai import prompts
    base = prompts.NOTEBOOK_AGENT_BASE_PROMPT
    chat = prompts.NOTEBOOK_AGENT_PROMPT
    check("NOTEBOOK_AGENT_BASE_PROMPT non-empty", isinstance(base, str) and len(base) > 1000)
    check("NOTEBOOK_AGENT_PROMPT non-empty", isinstance(chat, str) and len(chat) > 1000)

    print("== 4. no unfilled placeholders (incl. the fix prompts) ==")
    fix_err = prompts.FIX_ERROR_PROMPT
    fix_sql = prompts.FIX_SQL_ERROR_PROMPT
    # Any __UPPER_SNAKE__ token that survived substitution is a bug. (Excludes the
    # {var} .format() fields, which are filled per-request downstream, not here.)
    for label, text in (("base", base), ("chat", chat),
                        ("fix_error", fix_err), ("fix_sql", fix_sql)):
        leftover = sorted(set(re.findall(r"__[A-Z][A-Z0-9_]*__", text)))
        check(f"no leftover __PLACEHOLDER__ in {label} prompt", not leftover,
              f"found {leftover}")
    # The fix-error prompt must carry the auto-generated block AND still be a valid
    # .format() template (its {code}/{error} fields intact — .format shouldn't choke).
    check("FIX_ERROR_PROMPT contains the auto-generated helper block",
          "train_model(" in fix_err)
    try:
        _ = fix_err.format(code="x=1", error="boom")
        check("FIX_ERROR_PROMPT.format(code=,error=) still works", True)
    except Exception as e:  # noqa: BLE001
        check("FIX_ERROR_PROMPT.format(code=,error=) still works", False,
              f"{type(e).__name__}: {e}")

    print("== 5. auto-generated block is IN the prompt (spot-check helpers) ==")
    for fn in ["train_model", "helpers", "read_athena", "llm_classify",
               "semantic_search", "predict_batch", "save_model", "this_notebook"]:
        check(f"prompt mentions {fn}(", f"{fn}(" in base)

    print("== 6. hand-written steering preserved ==")
    for phrase in [
        "HOUSE IDIOMS",
        "PREFER train_model",
        "ONE Bedrock call PER ROW",
        "embed_texts(corpus) ONCE",
        "helpers()",
    ]:
        check(f"prompt keeps steering: {phrase!r}", phrase in base)

    print("== 7. agent entrypoint imports ==")
    try:
        import proxy.notebook.ai.notebook_agent  # noqa: F401
        check("proxy.notebook.ai.notebook_agent imports", True)
    except Exception as e:  # noqa: BLE001
        check("proxy.notebook.ai.notebook_agent imports", False, f"{type(e).__name__}: {e}")

    print("== 8. agentic fix wiring ==")
    from proxy.notebook.ai import notebook_agent as na
    check("fix_error_agentic exists", hasattr(na, "fix_error_agentic"))
    check("shared _build_fix_context_section exists", hasattr(na, "_build_fix_context_section"))
    from proxy.notebook.ai.constants import FIX_VERIFY_MAX_ATTEMPTS
    check("FIX_VERIFY_MAX_ATTEMPTS is a positive int",
          isinstance(FIX_VERIFY_MAX_ATTEMPTS, int) and FIX_VERIFY_MAX_ATTEMPTS >= 1,
          f"value={FIX_VERIFY_MAX_ATTEMPTS}")
    from proxy.notebook.ai.prompts import FIX_VERIFY_DIRECTIVE
    check("FIX_VERIFY_DIRECTIVE mentions execute_code verification",
          "execute_code" in FIX_VERIFY_DIRECTIVE and "verification_protocol" in FIX_VERIFY_DIRECTIVE)
    # fix_error_agentic must gracefully fall back (no crash) when there's no session
    # to execute against — it should call the one-shot fix. We can't run a real model
    # here, so just assert the no-session guard returns quickly via the fallback path
    # by checking it doesn't require a live agent for the SQL/markdown short-circuit.
    try:
        # markdown short-circuits to fix_error which returns the code unchanged — no
        # model call, no network. Safe to invoke.
        out = na.fix_error_agentic("# md", "err", {"cell_type": "markdown"})
        check("fix_error_agentic markdown short-circuit returns dict",
              isinstance(out, dict) and "fixed_code" in out)
    except Exception as e:  # noqa: BLE001
        check("fix_error_agentic markdown short-circuit returns dict", False, f"{type(e).__name__}: {e}")
    # ai route imports the agentic fn
    try:
        import proxy.notebook.routes.ai as ai_routes  # noqa: F401
        check("routes/ai.py imports (wires agent_fix_error_agentic)", True)
    except Exception as e:  # noqa: BLE001
        check("routes/ai.py imports", False, f"{type(e).__name__}: {e}")

    print("== 9. SageMaker helpers (Phase 1) ==")
    try:
        import app.notebook.helpers as _h
        sm_names = ["sm_train", "sm_training_job_status", "sm_load_model", "sm_predict_batch", "sm_jobs"]
        check("sm_* helpers exported from helpers package",
              all(hasattr(_h, n) for n in sm_names))
        from app.notebook.completion_catalog import build_helpers_meta
        _cat = {m["name"]: m["category"] for m in build_helpers_meta()}
        check("sm_* helpers categorized under Machine Learning",
              all((_cat.get(n) or "").startswith("Machine Learning") for n in sm_names),
              detail=str({n: _cat.get(n) for n in sm_names}))
        # sm_* appear in the auto-generated block that ships with the agent
        from proxy.notebook.ai.helpers_block import HELPERS_BLOCK as _hb
        check("sm_train + sm_load_model present in generated HELPERS_BLOCK",
              "sm_train(" in _hb and "sm_load_model(" in _hb)
        # hand-written SageMaker steering present in the base prompt
        from proxy.notebook.ai.prompts import NOTEBOOK_AGENT_BASE_PROMPT as _bp
        check("base prompt carries the SCALE-OUT ML (SageMaker) idiom",
              "SCALE-OUT ML (SageMaker)" in _bp and "NON-BLOCKING" in _bp)
        # estimator guard rejects an unsupported estimator BEFORE any AWS call
        import app.notebook.helpers.sagemaker as _sm
        import pandas as _pd
        try:
            _sm.sm_train(_pd.DataFrame({"a": [1, 2, 3, 4], "y": [0, 1, 0, 1]}),
                         "y", estimator="not_a_real_estimator")
            check("sm_train rejects unsupported estimator early", False,
                  "no ValueError raised")
        except ValueError:
            check("sm_train rejects unsupported estimator early", True)
    except Exception as e:  # noqa: BLE001
        check("SageMaker helpers section", False, f"{type(e).__name__}: {e}")

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("RESULT: all checks passed ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
