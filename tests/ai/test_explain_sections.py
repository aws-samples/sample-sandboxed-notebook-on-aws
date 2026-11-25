#!/usr/bin/env python3
# test-kind: offline
"""Offline tests for proxy.notebook.ai.explain_sections.

Run:  .venv/bin/python tests/ai/test_explain_sections.py   (exit 0 = pass)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def sec(a, b, title="T", expl="E"):
    return {"title": title, "start_line": a, "end_line": b, "explanation": expl}


def covers(sections, n):
    pos = 1
    for s in sections:
        if s["start_line"] != pos or s["end_line"] < s["start_line"]:
            return False
        pos = s["end_line"] + 1
    return pos == n + 1


def code_lines(n):
    return "\n".join(f"x{i} = {i}" for i in range(n))


def main() -> int:
    from proxy.notebook.ai import explain_sections as es

    print("parse_model_json")
    check("plain", es.parse_model_json('{"a": 1}') == {"a": 1})
    check("fenced", es.parse_model_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("trailing prose", es.parse_model_json('Here you go: {"a": 1} hope it helps') == {"a": 1})
    for bad in ("", "no json here", "[1,2]", '{"a": '):
        try:
            es.parse_model_json(bad)
            check(f"rejects {bad!r}", False)
        except ValueError:
            check(f"rejects {bad!r}", True)

    print("validate / repair")
    check("valid unchanged", es.validate_sections([sec(1, 5), sec(6, 10)], 10) == [])
    check("gap detected", es.validate_sections([sec(1, 4), sec(6, 10)], 10) != [])
    check("overlap detected", es.validate_sections([sec(1, 6), sec(5, 10)], 10) != [])
    check("out of range detected", es.validate_sections([sec(1, 12)], 10) != [])
    check("reversed detected", es.validate_sections([sec(5, 1)], 10) != [])
    check("non-integer detected", es.validate_sections([sec("a", 3)], 10) != [])
    check("empty list invalid", es.validate_sections([], 10) != [])
    check("short coverage detected", es.validate_sections([sec(1, 8)], 10) != [])
    r = es.repair_sections([sec(1, 4), sec(7, 10)], 10)
    check("gap repaired", covers(r, 10), str(r))
    r = es.repair_sections([sec(1, 6), sec(5, 10)], 10)
    check("overlap repaired", covers(r, 10), str(r))
    r = es.repair_sections([sec(8, 20), sec(-3, 7)], 10)
    check("unsorted + out-of-range repaired", covers(r, 10) and r[0]["start_line"] == 1, str(r))
    r = es.repair_sections([sec("2", 5.0), sec(6, 9)], 10)
    check("numeric strings/floats coerced, ends at n", covers(r, 10), str(r))
    check("nothing usable -> None", es.repair_sections([{"title": "x"}, "junk"], 10) is None)
    check("empty explanation dropped", es.repair_sections([sec(1, 5, expl=""), sec(6, 10)], 10)[0]["start_line"] == 1)

    print("fallback")
    code = "\n".join(["# load", "a = 1", "b = 2", "", "# clean", "c = a + b", "d = c", ""]
                     + [f"z{i} = {i}" for i in range(30)])
    fb = es.fallback_sections(code)
    n = len(es.split_lines(code))
    check("fallback covers all lines", covers(fb, n), str(fb))
    check("generic titles", all(s["title"].startswith("Lines ") for s in fb))
    check("splits at blank/comment", len(fb) >= 2)
    check("explanations empty", all(s["explanation"] == "" for s in fb))
    long_block = code_lines(130)
    fb2 = es.fallback_sections(long_block)
    check("long unbroken block chunked and covered", len(fb2) >= 3 and covers(fb2, 130), str(len(fb2)))
    check("short code -> one section", len(es.fallback_sections(code_lines(10))) == 1)

    print("normalize")
    c12 = code_lines(12)
    obj = {"overview": "Overview.", "sections": [sec(1, 4, "A", "ea"), sec(5, 8, "B", "eb"), sec(9, 12, "C", "ec")],
           "depends_on": [], "notes": ""}
    res, probs = es.normalize(obj, c12, set())
    check("12 lines + 3 sections collapses to 1", len(res["sections"]) == 1 and probs == [], str(res["sections"]))
    check("collapsed covers 1..12", res["sections"][0]["start_line"] == 1 and res["sections"][0]["end_line"] == 12)
    check("collapsed uses model explanation", res["sections"][0]["explanation"] == "ea")
    res, probs = es.normalize({"overview": "O", "sections": []}, c12, set())
    check("short code, no sections -> overview as explanation", res["sections"][0]["explanation"] == "O")
    c120 = code_lines(120)
    four = [sec(1, 30, "A"), sec(31, 60, "B"), sec(61, 90, "C"), sec(91, 120, "D")]
    res, probs = es.normalize({"overview": "O", "sections": four}, c120, set())
    check("120 lines + 4 sections stays 4, contiguous", len(res["sections"]) == 4
          and covers(res["sections"], 120) and not res["repaired"] and probs == [])
    res, probs = es.normalize({"overview": "O", "sections": [sec(1, 30), sec(35, 90), sec(95, 100)]}, c120, set())
    check("bad ranges repaired, no problems", res["repaired"] and covers(res["sections"], 120) and probs == [])
    res, probs = es.normalize({"overview": "O", "sections": "nope"}, c120, set())
    check("unusable sections -> fallback + problem", res["fallback"] and covers(res["sections"], 120) and probs != [])
    res, probs = es.normalize({"sections": four}, c120, set())
    check("missing overview is a problem", probs != [])
    obj = {"overview": "O", "sections": [sec(1, 12)], "depends_on": [
        {"cell": "PY-1", "provides": ["df", 3], "why": "w"}, {"cell": "PY-9", "provides": [], "why": "x"},
        "junk"]}
    res, _ = es.normalize(obj, c12, {"PY-1", "SQL-1"})
    check("unknown depends_on dropped, provides stringified", [d["cell"] for d in res["depends_on"]] == ["PY-1"]
          and res["depends_on"][0]["provides"] == ["df", "3"], str(res["depends_on"]))
    big = "word " * 5000 + "end."
    res, _ = es.normalize({"overview": big, "sections": [{**sec(1, 12), "explanation": big}], "notes": big},
                          c12, set())
    check("long text preserved", res["overview"] == big.strip() and res["sections"][0]["explanation"] == big.strip()
          and res["notes"] == big.strip())

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILED: {_failures}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
