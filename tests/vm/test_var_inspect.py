#!/usr/bin/env python3
# test-kind: offline
"""
Unit tests: the read-only variable inspector behind GET /variables/schema,
POST /variables/inspect and GET /variables/redacted (app/notebook/var_inspect.py).
The AI chat agent uses these to see a variable's LIVE columns (e.g. `product_id`
after a cell renamed `productId`) without an approval-gated execute_code.
Covers: allowed/denied types, name validation, caps, NO repr()/str()/len()/getattr
on a hostile user object, sensitive-column omission, secret redaction (inspect
samples + get_variables(redact_secrets=True), plain get_variables unchanged),
sample_rows clamp, the 4096-byte bound, the 200-entry schema cap, and the routes'
status codes / no-exception-text error bodies.
Standalone (no pytest, no AWS): run with
    .venv/bin/python tests/vm/test_var_inspect.py
Exits non-zero on any failure. Each check prints PASS/FAIL.
"""
import json
import os
import sys
import types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("AWS_REGION", "us-west-2")
# .venv lacks plotly and duckdb — stub them BEFORE importing app code.
_plotly = types.ModuleType("plotly")
_px = types.ModuleType("plotly.express")
_plotly.express = _px
sys.modules.setdefault("plotly", _plotly)
sys.modules.setdefault("plotly.express", _px)
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from app.notebook.executor import SandboxExecutor  # noqa: E402
from app.notebook import var_inspect  # noqa: E402

_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(label)


CALLS = {"n": 0}


class Hostile:
    """Every dunder a careless inspector might call raises AND counts."""
    def __repr__(self):
        CALLS["n"] += 1
        raise RuntimeError("repr called")

    def __str__(self):
        CALLS["n"] += 1
        raise RuntimeError("str called")

    def __len__(self):
        CALLS["n"] += 1
        raise RuntimeError("len called")

    def __getattr__(self, item):
        CALLS["n"] += 1
        raise RuntimeError("getattr called")

    def __hash__(self):
        return 1

    def __eq__(self, other):
        CALLS["n"] += 1
        return False


class MyFrame(pd.DataFrame):
    pass


def make_executor():
    ex = SandboxExecutor()
    raw = pd.DataFrame({"productId": ["p1", "p2"], "rating": [4.0, 5.0], "verified_purchase": [True, False]})
    ex._namespace["reviews"] = raw.rename(columns={"productId": "product_id"})
    ex._namespace["wide"] = pd.DataFrame({f"c{i}": [i] for i in range(70)})
    ex._namespace["ratings"] = pd.Series([1.5, 2.5, 3.5], name="rating")
    ex._namespace["arr"] = np.zeros((3, 4), dtype="float32")
    ex._namespace["hostile"] = Hostile()
    ex._namespace["sub"] = MyFrame({"a": [1]})
    ex._namespace["cellhost"] = pd.DataFrame({"x": [Hostile(), Hostile()], "y": [1, 2]})
    ex._namespace["labelhost"] = pd.DataFrame([[1, 2]], columns=pd.Index(["ok", Hostile()], dtype=object))
    ex._namespace["users"] = pd.DataFrame({
        "name": ["a"], "password": ["pw1"], "api_key": ["k1"], "email": ["e@x"], "user_token": ["t1"]})
    ex._namespace["items"] = [1, 2, 3]
    ex._namespace["_private"] = 1
    ex._var_provenance["reviews"] = {"defined_by": "cell-6", "defined_at": 6, "modified_by": [],
                                     "last_cell": "cell-6", "last_exec": 6}
    ex._var_provenance["ratings"] = {"defined_by": "cell-7", "defined_at": 7, "modified_by": [],
                                     "last_cell": "cell-7", "last_exec": 7}
    return ex


def by_name(schema):
    return {e["name"]: e for e in schema["variables"]}


def main():
    ex = make_executor()
    CALLS["n"] = 0
    schema = ex.get_variable_schemas()
    s = by_name(schema)

    # (a) renamed column is live
    check("a: reviews kind dataframe", s["reviews"]["kind"] == "dataframe")
    check("a: product_id:object in schema", ["product_id", "object"] in s["reviews"]["columns"]
          or ["product_id", "str"] in s["reviews"]["columns"], str(s["reviews"]["columns"]))
    check("a: productId not in schema", all(c[0] != "productId" for c in s["reviews"]["columns"]))
    check("a: shape [2,3]", s["reviews"]["shape"] == [2, 3])
    check("a: defined_by from provenance", s["reviews"]["defined_by"] == "cell-6")
    check("a: recency order (ratings before reviews)",
          [e["name"] for e in schema["variables"]].index("ratings")
          < [e["name"] for e in schema["variables"]].index("reviews"))
    check("a: _private skipped", "_private" not in s)

    # (b) column cap
    check("b: 60 columns", len(s["wide"]["columns"]) == 60)
    check("b: more == 10", s["wide"]["more"] == 10)

    # (c) Series + ndarray
    check("c: series kind/shape/dtype", s["ratings"]["kind"] == "series" and s["ratings"]["shape"] == [3]
          and s["ratings"]["dtype"] == "float64" and s["ratings"]["columns"] == [["rating", "float64"]])
    check("c: ndarray kind/shape/dtype", s["arr"]["kind"] == "ndarray" and s["arr"]["shape"] == [3, 4]
          and s["arr"]["dtype"] == "float32" and s["arr"]["columns"] == [])
    check("c: builtin list gets len", s["items"]["len"] == 3 and s["items"]["kind"] == "other")

    # (d) hostile object: type only, no dunder called
    check("d: hostile kind other / type Hostile", s["hostile"]["kind"] == "other" and s["hostile"]["type"] == "Hostile")
    check("d: hostile len not computed", s["hostile"]["len"] is None)
    st, body = ex.inspect_variable("hostile")
    check("d: inspect hostile 200, no sample", st == 200 and body["sample"] == [])

    # (e) subclass is not trusted as a DataFrame
    check("e: DataFrame subclass kind other", s["sub"]["kind"] == "other" and s["sub"]["type"] == "MyFrame")

    # (f) hostile cell value
    st, body = ex.inspect_variable("cellhost")
    check("f: hostile cell rendered as <Hostile>", st == 200 and body["sample"][0]["x"] == "<Hostile>"
          and body["sample"][0]["y"] == "1", str(body.get("sample")))

    # (g) hostile column label
    check("g: hostile label as <Hostile>", [c[0] for c in s["labelhost"]["columns"]] == ["ok", "<Hostile>"],
          str(s["labelhost"]["columns"]))
    check("d/f/g: no hostile dunder was called", CALLS["n"] == 0, f"calls={CALLS['n']}")

    # (h) name validation
    for bad in ["_x", "1a", "a b", "__builtins__", 123, None, "a" * 200, ""]:
        st, body = ex.inspect_variable(bad)
        check(f"h: invalid name {bad!r:.20} → 400", st == 400 and body == {"error": "invalid variable name"})
    st, body = ex.inspect_variable("missing_var")
    check("h: missing → 404", st == 404 and body == {"error": "variable not found"})

    # (i) sensitive columns
    st, body = ex.inspect_variable("users")
    row = body["sample"][0]
    check("i: sensitive values omitted", all(row[c] == "<omitted>" for c in ("password", "api_key", "email", "user_token")),
          str(row))
    check("i: name kept", row["name"] == "a")
    check("i: omitted_columns listed", sorted(body["omitted_columns"]) == ["api_key", "email", "password", "user_token"])

    # (i2) MultiIndex (tuple) column labels: sensitivity from the raw tuple members
    ex._namespace["contacts"] = pd.DataFrame({("contact", "email"): ["a@example.com"],
                                              ("contact", "phone"): ["555-0100"],
                                              ("stats", "n"): [3]})
    CALLS["n"] = 0
    st, body = ex.inspect_variable("contacts")
    row = body["sample"][0]
    blob = json.dumps(body)
    check("i2: multiindex email/phone values not sent", "a@example.com" not in blob and "555-0100" not in blob, blob)
    check("i2: multiindex labels rendered from members",
          [c[0] for c in body["columns"]] == ["('contact', 'email')", "('contact', 'phone')", "('stats', 'n')"],
          str(body["columns"]))
    check("i2: multiindex omitted_columns listed",
          body["omitted_columns"] == ["('contact', 'email')", "('contact', 'phone')"], str(body["omitted_columns"]))
    check("i2: multiindex non-sensitive kept", row.get("('stats', 'n')") == "3", str(row))
    check("i2: three distinct row keys", len(row) == 3, str(row))
    ex._namespace["tuphost"] = pd.DataFrame([[1, 2]], columns=pd.Index([("ok", Hostile()), ("x", "secret")],
                                                                       dtype=object, tupleize_cols=False))
    CALLS["n"] = 0  # pandas itself may probe labels while BUILDING the frame
    st, body = ex.inspect_variable("tuphost")
    check("i2: hostile tuple member rendered safely", body["columns"][0][0] == "('ok', <Hostile>)", str(body["columns"]))
    check("i2: tuple member 'secret' omitted", body["sample"][0][body["columns"][1][0]] == "<omitted>", str(body["sample"]))
    check("i2: no hostile dunder called on tuple labels", CALLS["n"] == 0, f"calls={CALLS['n']}")
    ex._namespace.pop("tuphost", None)

    # (i3) duplicate labels: no value collapses, omission by position
    ex._namespace["dups"] = pd.DataFrame([["v1", "s1", "v2"]], columns=["a", "email", "a"])
    st, body = ex.inspect_variable("dups")
    row = body["sample"][0]
    check("i3: duplicate label keyed by position", row == {"a": "v1", "email": "<omitted>", "a#2": "v2"}, str(row))
    check("i3: duplicate omitted by position", body["omitted_columns"] == ["email"], str(body["omitted_columns"]))
    ex._namespace["dupsens"] = pd.DataFrame([["x", "y"]], columns=["phone", "phone"])
    st, body = ex.inspect_variable("dupsens")
    check("i3: both duplicate sensitive columns omitted",
          body["sample"][0] == {"phone": "<omitted>", "phone#1": "<omitted>"}
          and body["omitted_columns"] == ["phone", "phone#1"], str(body))
    for k in ("contacts", "dups", "dupsens"):
        ex._namespace.pop(k, None)

    # (j) secret redaction
    saved = {k: os.environ.get(k) for k in ("MICROVM_USER_ENV_KEYS", "DB_PW", "PLAIN", "JSON_SECRET")}
    try:
        dummy = "hunter2-very-secret"  # test-only dummy value
        os.environ["MICROVM_USER_ENV_KEYS"] = json.dumps({"DB_PW": "secret", "PLAIN": "env", "JSON_SECRET": "secret"})
        os.environ["DB_PW"] = dummy
        os.environ["PLAIN"] = "plain-env-value"
        os.environ["JSON_SECRET"] = json.dumps({"user": "svc", "password": "json-leaf-pass-xyz"})
        ex._namespace["leak"] = pd.DataFrame({"note": [dummy, f"conn=db://u:{dummy}@h", "json-leaf-pass-xyz", "plain-env-value"]})
        st, body = ex.inspect_variable("leak")
        vals = [r["note"] for r in body["sample"]]
        check("j: exact match redacted", vals[0] == "<redacted>", vals[0])
        check("j: substring redacted", dummy not in vals[1] and "<redacted>" in vals[1], vals[1])
        check("j: JSON leaf redacted", vals[2] == "<redacted>", vals[2])
        check("j: plain env value not treated as secret", "plain-env-value" not in var_inspect.secret_values())
        check("j: secret not anywhere in body", dummy not in json.dumps(body))
        ex._namespace["pw"] = dummy
        ex._namespace["long_pw"] = "x" * 90 + dummy  # repr cut at 100 → only a prefix left
        red = ex.get_variables(redact_secrets=True)
        check("j: get_variables redacted value", dummy not in red["pw"]["value"] and dummy not in red["pw"]["preview"],
              red["pw"]["value"])
        check("j: truncated prefix redacted", dummy[:8] not in red["long_pw"]["value"], red["long_pw"]["value"])
        plain = ex.get_variables()
        check("j: plain get_variables unchanged", dummy in plain["pw"]["value"])
        check("j: redact keys the same as plain", set(red) == set(plain))
        check("j: secret_values non-empty", len(var_inspect.secret_values()) >= 2)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ex._namespace.pop("leak", None)
        ex._namespace.pop("pw", None)
        ex._namespace.pop("long_pw", None)
    check("j: no secrets once the map is gone", var_inspect.secret_values() == set()
          or "MICROVM_USER_ENV_KEYS" in os.environ)

    # (k) sample_rows clamp
    ex._namespace["many"] = pd.DataFrame({"v": list(range(10))})
    st, body = ex.inspect_variable("many", 5)
    check("k: sample_rows 5 → 3 rows", len(body["sample"]) == 3)
    st, body = ex.inspect_variable("many", 0)
    check("k: sample_rows 0 → no sample", body["sample"] == [])
    st, body = ex.inspect_variable("many", "junk")
    check("k: bad sample_rows → default 3", len(body["sample"]) == 3)
    st, body = ex.inspect_variable("ratings")
    check("k: series sample", body["sample"] == ["1.5", "2.5", "3.5"], str(body["sample"]))

    # (l) 4096-byte bound
    ex._namespace["big"] = pd.DataFrame({f"long_column_name_number_{i:02d}": ["y" * 200] * 3 for i in range(60)})
    st, body = ex.inspect_variable("big")
    check("l: body <= 4096 bytes", len(json.dumps(body)) <= 4096, str(len(json.dumps(body))))
    check("l: truncated flag", body["truncated"] is True)
    check("l: values cut to 60", all(len(v) <= 60 for r in body["sample"] for v in r.values()))

    # (m) 200-entry cap
    items = [(f"v{i}", i) for i in range(250)]
    sch = var_inspect.build_schema(items, {})
    check("m: 200-entry cap", sch["count"] == 200 and len(sch["variables"]) == 200 and sch["truncated"] is True)
    check("m: under 48 KB", len(json.dumps(sch)) <= 48 * 1024 + 200)

    # redact_text unit
    sec = {"abcdef123"}
    check("redact: exact", var_inspect.redact_text("abcdef123", sec) == "<redacted>")
    check("redact: none", var_inspect.redact_text("hello", sec) == "hello")
    check("redact: no secrets passthrough", var_inspect.redact_text("abcdef123", set()) == "abcdef123")

    # (n) routes
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:
        print("SKIP  n: fastapi TestClient not importable")
        return
    from app.notebook import routes
    app = FastAPI()
    app.include_router(routes.router)
    app.state.executor = ex
    app.state.session_state = {"request_count": 0}
    c = TestClient(app)
    r = c.get("/variables/schema")
    check("n: schema 200", r.status_code == 200 and "reviews" in {e["name"] for e in r.json()["variables"]})
    r = c.post("/variables/inspect", json={"name": "reviews", "sample_rows": 3})
    check("n: inspect 200", r.status_code == 200 and r.json()["name"] == "reviews")
    r = c.post("/variables/inspect", json={"name": "_x"})
    check("n: inspect 400", r.status_code == 400)
    r = c.post("/variables/inspect", content=b"not json", headers={"content-type": "application/json"})
    check("n: bad json 400", r.status_code == 400)
    r = c.post("/variables/inspect", json=["reviews"])
    check("n: non-dict 400", r.status_code == 400)
    r = c.post("/variables/inspect", json={"name": "nope"})
    check("n: inspect 404", r.status_code == 404)
    r = c.get("/variables/redacted")
    check("n: redacted 200", r.status_code == 200 and "reviews" in r.json()["variables"])
    r = c.get("/variables")
    check("n: /variables still 200", r.status_code == 200)
    check("n: request_count incremented", app.state.session_state["request_count"] >= 8)
    orig_schema, orig_inspect, orig_vars = ex.get_variable_schemas, ex.inspect_variable, ex.get_variables

    def boom(*a, **k):
        raise ValueError("SECRETVALUE")
    ex.get_variable_schemas = boom
    ex.inspect_variable = boom
    ex.get_variables = boom
    try:
        r = c.get("/variables/schema")
        check("n: schema 500 no exception text", r.status_code == 500 and "SECRETVALUE" not in r.text
              and r.json() == {"error": "inspection failed"})
        r = c.post("/variables/inspect", json={"name": "reviews"})
        check("n: inspect 500 no exception text", r.status_code == 500 and "SECRETVALUE" not in r.text)
        r = c.get("/variables/redacted")
        check("n: redacted 500 no exception text", r.status_code == 500 and "SECRETVALUE" not in r.text)
    finally:
        ex.get_variable_schemas, ex.inspect_variable, ex.get_variables = orig_schema, orig_inspect, orig_vars

    # 504 timeout path (shrink the limit, block the worker briefly)
    import time as _time
    orig_to = routes._INSPECT_TIMEOUT_S
    routes._INSPECT_TIMEOUT_S = 0.05

    def slow(*a, **k):
        _time.sleep(0.3)
        return {"variables": [], "count": 0, "truncated": False}
    ex.get_variable_schemas = slow
    try:
        r = c.get("/variables/schema")
        check("n: schema 504 on timeout", r.status_code == 504 and r.json() == {"error": "inspection timed out"})
    finally:
        ex.get_variable_schemas = orig_schema
        routes._INSPECT_TIMEOUT_S = orig_to


if __name__ == "__main__":
    main()
    print(f"\n{'ALL PASS' if not _fails else f'{len(_fails)} FAILED: {_fails}'}")
    sys.exit(1 if _fails else 0)
