"""Parsing, validation, repair and fallback for "Explain this code" (pure).

The model returns strict JSON::

    {"overview": str,
     "sections": [{"title", "start_line", "end_line", "explanation"}],
     "depends_on": [{"cell", "provides": [str], "why": str}],
     "notes": str}

Section ranges are 1-based, inclusive, contiguous, non-overlapping and in
order, covering 1..N of the cell's code (see ``explain_context.split_lines``).
Explanation text is never truncated here.
"""
import json
import re

from proxy.notebook.ai.constants import EXPLAIN_MAX_SECTIONS, EXPLAIN_SHORT_LINES
from proxy.notebook.ai.explain_context import split_lines

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?|\n?```\s*$")
_LONG_RUN = 60     # fallback: a section longer than this is cut into chunks
_CHUNK = 40


def parse_model_json(text):
    """Parse the model reply into a dict. Tolerates code fences and prose
    before/after the object. Raises ValueError when no JSON object is found."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty model reply")
    s = text.strip()
    s = _FENCE.sub("", s).strip() if s.startswith("```") else s
    try:
        obj = json.loads(s)
    except ValueError:
        start = s.find("{")
        if start < 0:
            raise ValueError("no JSON object in model reply")
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[start:])
        except ValueError as e:
            raise ValueError(f"invalid JSON: {e}")
    if not isinstance(obj, dict):
        raise ValueError("model reply is not a JSON object")
    return obj


def _as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
        return int(v)
    return None


def validate_sections(sections, n_lines):
    """List of human-readable problems ([] when the ranges are valid)."""
    errs = []
    if not isinstance(sections, list) or not sections:
        return ["sections must be a non-empty list"]
    prev_end = 0
    for i, s in enumerate(sections, 1):
        if not isinstance(s, dict):
            errs.append(f"section {i} is not an object")
            continue
        a, b = _as_int(s.get("start_line")), _as_int(s.get("end_line"))
        if a is None or b is None:
            errs.append(f"section {i}: start_line/end_line must be integers")
            continue
        if a < 1 or b > n_lines:
            errs.append(f"section {i}: range {a}-{b} is outside 1-{n_lines}")
        if b < a:
            errs.append(f"section {i}: end_line {b} is before start_line {a}")
        if a != prev_end + 1:
            errs.append(f"section {i}: starts at {a} but must start at {prev_end + 1} "
                        "(contiguous, non-overlapping, in order)")
        prev_end = b
    if prev_end != n_lines and not errs:
        errs.append(f"sections end at line {prev_end} but the code has {n_lines} lines")
    return errs


def _coerce_sections(sections):
    out = []
    for s in sections if isinstance(sections, list) else []:
        if not isinstance(s, dict):
            continue
        a, b = _as_int(s.get("start_line")), _as_int(s.get("end_line"))
        expl = s.get("explanation")
        expl = expl.strip() if isinstance(expl, str) else ""
        if a is None or b is None or not expl:
            continue
        title = s.get("title")
        out.append({"title": title.strip() if isinstance(title, str) and title.strip() else "",
                    "start_line": a, "end_line": b, "explanation": expl})
    return out


def repair_sections(sections, n_lines):
    """Make the ranges cover 1..n contiguously. Returns a new list, or None when
    nothing usable remains. Clamps to 1..n, sorts, trims overlaps, closes gaps by
    extending the previous section, forces first start=1 and last end=n."""
    secs = _coerce_sections(sections)
    if not secs or n_lines < 1:
        return None
    for s in secs:
        s["start_line"] = min(max(s["start_line"], 1), n_lines)
        s["end_line"] = min(max(s["end_line"], 1), n_lines)
        if s["end_line"] < s["start_line"]:
            s["start_line"], s["end_line"] = s["end_line"], s["start_line"]
    secs.sort(key=lambda s: (s["start_line"], s["end_line"]))
    out = []
    for s in secs:
        if out and s["end_line"] <= out[-1]["end_line"]:
            continue  # fully inside the previous section
        if out and s["start_line"] <= out[-1]["end_line"]:
            s["start_line"] = out[-1]["end_line"] + 1  # trim overlap
        out.append(s)
    for i, s in enumerate(out):
        if i == 0:
            s["start_line"] = 1
        else:
            out[i - 1]["end_line"] = s["start_line"] - 1  # close gaps
    out[-1]["end_line"] = n_lines
    for i, s in enumerate(out, 1):
        if not s["title"]:
            s["title"] = f"Lines {s['start_line']}-{s['end_line']}"
    return out


def _is_comment(line):
    t = line.lstrip()
    return t.startswith("#") or t.startswith("--")


def fallback_sections(code):
    """Split at blank lines / comment lines into contiguous sections with short
    generic titles and an empty explanation. Covers 1..N exactly."""
    lines = split_lines(code)
    n = len(lines)
    if n == 0:
        return []
    if n <= EXPLAIN_SHORT_LINES:
        return [{"title": f"Lines 1-{n}", "start_line": 1, "end_line": n, "explanation": ""}]
    starts = [0]
    for i in range(1, n):
        prev_blank = not lines[i - 1].strip()
        if lines[i].strip() and (prev_blank or (_is_comment(lines[i]) and not _is_comment(lines[i - 1]))):
            starts.append(i)
    spans = []
    for k, a in enumerate(starts):
        b = (starts[k + 1] if k + 1 < len(starts) else n)
        if b - a > _LONG_RUN:
            for c in range(a, b, _CHUNK):
                spans.append([c, min(c + _CHUNK, b)])
        else:
            spans.append([a, b])
    while len(spans) > EXPLAIN_MAX_SECTIONS:
        j = min(range(len(spans) - 1), key=lambda i: spans[i + 1][1] - spans[i][0])
        spans[j][1] = spans[j + 1][1]
        del spans[j + 1]
    return [{"title": f"Lines {a + 1}-{b}", "start_line": a + 1, "end_line": b, "explanation": ""}
            for a, b in spans]


def _str_list(v):
    return [str(x) for x in v if isinstance(x, (str, int, float)) and str(x)] if isinstance(v, list) else []


def normalize(obj, code, known_labels):
    """Validate/repair a parsed model object against the cell's code.

    Returns ``(result, problems)``. ``result`` is always usable
    (``{overview, sections, depends_on, notes, repaired, fallback}``); ``problems``
    lists what is wrong when a retry is worthwhile (missing overview, or a long
    cell with no usable sections). ``known_labels`` is the set of cell labels
    that may appear in depends_on; unknown labels are dropped.
    """
    obj = obj if isinstance(obj, dict) else {}
    problems = []
    n = len(split_lines(code))
    overview = obj.get("overview")
    overview = overview.strip() if isinstance(overview, str) else ""
    if not overview:
        problems.append("overview must be a non-empty string")
    raw_sections = obj.get("sections")
    repaired = False
    fallback = False
    if n <= EXPLAIN_SHORT_LINES:
        coerced = _coerce_sections(raw_sections)
        expl = coerced[0]["explanation"] if coerced else overview
        title = coerced[0]["title"] if len(coerced) == 1 and coerced[0]["title"] else "Explanation"
        sections = [{"title": title, "start_line": 1, "end_line": max(n, 1), "explanation": expl}]
        repaired = isinstance(raw_sections, list) and len(raw_sections) > 1
    else:
        errs = validate_sections(raw_sections, n)
        coerced = _coerce_sections(raw_sections)
        if not errs and len(coerced) == len(raw_sections):
            sections = coerced
        else:
            sections = repair_sections(raw_sections, n)
            repaired = sections is not None
            if sections is None:
                problems.append("sections must be a list of objects with title, start_line, "
                                "end_line and a non-empty explanation covering lines 1-%d" % n)
                sections = fallback_sections(code)
                fallback = True
    deps = []
    for d in obj.get("depends_on") if isinstance(obj.get("depends_on"), list) else []:
        if isinstance(d, dict) and str(d.get("cell")) in known_labels:
            why = d.get("why")
            deps.append({"cell": str(d["cell"]), "provides": _str_list(d.get("provides")),
                         "why": why.strip() if isinstance(why, str) else ""})
    notes = obj.get("notes")
    notes = notes.strip() if isinstance(notes, str) else ""
    return ({"overview": overview, "sections": sections, "depends_on": deps, "notes": notes,
             "repaired": repaired, "fallback": fallback}, problems)
