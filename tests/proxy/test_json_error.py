#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the shared JSON error body (proxy/platform/errors.py::json_error,
code-review B-B-02 / C-B-04 / I-B-03).

Covers:
  - a message containing a double quote and a newline (the shape of a botocore
    error text) round-trips through json.loads with the given status;
  - extra fields sit next to "error";
  - no route module under proxy/platform or proxy/notebook still builds a JSON
    body by hand from an f-string (content=f'{{"error": ...}}') or a %-format.

Run:
    .venv/bin/python tests/proxy/test_json_error.py
"""

import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from proxy.platform.errors import json_error  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))


# Hand-built JSON bodies: f-strings with a doubled brace before a quoted key, or
# a %-formatted '{"error": ... "%s"}' literal.
_FSTRING_BODY = re.compile(r"""\bf(['"])\{\{\s*\\?["']""")
_PERCENT_BODY = re.compile(r"""['"]\{\s*\\?["']error\\?["'].*%s.*['"]\s*%""")


def _scan():
    hits = []
    for sub in ("proxy/platform", "proxy/notebook"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, sub)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if _FSTRING_BODY.search(line) or _PERCENT_BODY.search(line):
                            hits.append(f"{os.path.relpath(path, ROOT)}:{i}")
    return hits


def main():
    print("\n=== json_error ===")
    msg = 'An error occurred (ValidationException): Invalid "x"\nsecond line'
    resp = json_error(502, msg)
    check("status carried", resp.status_code == 502, resp.status_code)
    check("media type is JSON", resp.media_type == "application/json", resp.media_type)
    try:
        body = json.loads(resp.body)
    except ValueError as e:
        body = None
        check("body parses", False, str(e))
    check("quote + newline round-trip", body == {"error": msg}, body)

    resp = json_error(403, "outside prefix", keys=[])
    check("extra fields kept next to error",
          json.loads(resp.body) == {"keys": [], "error": "outside prefix"}, resp.body)

    resp = json_error(500, ValueError('bad "v"'))
    check("non-str message is stringified",
          json.loads(resp.body) == {"error": 'bad "v"'}, resp.body)

    hits = _scan()
    check("no hand-built JSON error bodies under proxy/platform, proxy/notebook",
          not hits, ", ".join(hits))

    passed = sum(1 for _, ok in _checks if ok)
    print(f"\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
