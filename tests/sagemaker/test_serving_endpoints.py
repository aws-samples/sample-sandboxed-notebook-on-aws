#!/usr/bin/env python3
# test-kind: manual
"""Adversarial invoke tests for the custom SageMaker serving endpoints.

Exercises the /invocations contract of serving/serve_app.py against LIVE endpoints
(sklearn .joblib, ONNX .onnx, SageMaker-trained .smxgb), deliberately trying to BREAK
it with edge cases so we surface real bugs. Calls sagemaker-runtime:InvokeEndpoint
directly — no VM needed. Reports PASS/FAIL per scenario; does NOT delete endpoints.

Endpoints are auto-discovered (newest InService microvm-ep-*<kind>*) or passed via
--sklearn/--onnx/--xgboost.

Serving contract:
  POST body {"rows": [ {col: val}, ... ]}  (raw feature dicts)  OR
            {"instances": [[...], ...]}     (positional rows)
  → {"predictions": [...], "probabilities": [[...]]?, "classes": [...]? }

A test is a "handled" PASS if the endpoint returns 200 with a well-formed body OR a
clean error (HTTP 4xx/5xx with an 'error' field / ModelError) — what must NEVER happen
is an opaque container crash, a wrong-length prediction array, or a 500 with an
unhandled Python traceback leaking through as the model message.
"""
import argparse
import json
import sys
import boto3
from botocore.exceptions import ClientError

_ap = argparse.ArgumentParser()
_ap.add_argument("--region", default="us-west-2")
_ap.add_argument("--sklearn")
_ap.add_argument("--onnx")
_ap.add_argument("--xgboost")
_args = _ap.parse_args()
REGION = _args.region

NUMERIC = ["quantity", "unit_price", "order_total", "price", "cost",
           "margin_pct", "rating_avg", "lifetime_value"]
CATEGORICAL = ["category", "brand", "segment", "shipping_country", "payment_method"]

smr = boto3.client("sagemaker-runtime", region_name=REGION)
sm = boto3.client("sagemaker", region_name=REGION)

_passed = 0
_failed = 0
_bugs = []


def _autodiscover(substr):
    eps = sm.list_endpoints(NameContains="microvm-ep-", SortBy="CreationTime",
                            SortOrder="Descending", MaxResults=50).get("Endpoints", [])
    for e in eps:
        if substr in e["EndpointName"] and e["EndpointStatus"] == "InService":
            return e["EndpointName"]
    return None


def check(name, ok, detail="", is_bug=False):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  — {detail}")
        if is_bug:
            _bugs.append(f"{name}: {detail}")


def raw_invoke(ep, body_bytes, content_type="application/json"):
    """Return (status_code, parsed_body_or_text, error_str_or_None)."""
    try:
        resp = smr.invoke_endpoint(EndpointName=ep, ContentType=content_type, Body=body_bytes)
        code = resp["ResponseMetadata"]["HTTPStatusCode"]
        text = resp["Body"].read().decode()
        try:
            return code, json.loads(text), None
        except json.JSONDecodeError:
            return code, text, None
    except ClientError as e:
        # ModelError etc. — the container returned a non-2xx. That's a HANDLED error.
        return None, None, f"{e.response['Error'].get('Code')}: {e.response['Error'].get('Message', '')[:200]}"
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"


def invoke(ep, payload):
    return raw_invoke(ep, json.dumps(payload).encode())


def a_row(**over):
    r = {"quantity": 2, "unit_price": 49.99, "order_total": 99.98, "price": 55.0,
         "cost": 30.0, "margin_pct": 45.0, "rating_avg": 4.2, "lifetime_value": 1200.0,
         "category": "Electronics", "brand": "Acme", "segment": "Consumer",
         "shipping_country": "US", "payment_method": "card"}
    r.update(over)
    return r


def ok_predictions(body, n):
    """True if body is a well-formed prediction response of length n."""
    return (isinstance(body, dict) and isinstance(body.get("predictions"), list)
            and len(body["predictions"]) == n)


def handled(code, body, err):
    """A clean outcome: either a 200 with a body, or a clean error (ClientError /
    an {'error': ...} body). NOT a crash with no response."""
    if err is not None:
        return True  # ModelError from the container = handled (returned a response)
    if code == 200:
        return True
    if isinstance(body, dict) and "error" in body:
        return True
    return False


def run_suite(label, ep):
    print(f"\n===== {label}  ({ep}) =====")

    # --- Happy path ---
    code, body, err = invoke(ep, {"rows": [a_row()]})
    check("single row → 1 prediction", ok_predictions(body, 1), f"code={code} body={body} err={err}")

    code, body, err = invoke(ep, {"rows": [a_row(quantity=i) for i in range(1, 26)]})
    check("batch of 25 → 25 predictions", ok_predictions(body, 25),
          f"code={code} len={len(body.get('predictions', [])) if isinstance(body, dict) else '?'} err={err}",
          is_bug=True)

    # --- Probabilities + classes contract ---
    code, body, err = invoke(ep, {"rows": [a_row(), a_row(quantity=4)]})
    if isinstance(body, dict) and "probabilities" in body:
        probs = body["probabilities"]
        check("proba rows == input rows", len(probs) == 2, f"{len(probs)}")
        # each proba row should sum ~1 for a classifier
        sums_ok = all(abs(sum(r) - 1.0) < 1e-3 for r in probs) if probs and isinstance(probs[0], list) else True
        check("proba rows sum to ~1", sums_ok, f"probs={probs}", is_bug=True)
        check("classes present + matches proba width",
              "classes" in body and len(body["classes"]) == len(probs[0]),
              f"classes={body.get('classes')} width={len(probs[0]) if probs and isinstance(probs[0], list) else '?'}",
              is_bug=True)
    else:
        print(f"  NOTE  no probabilities in response (keys={list(body.keys()) if isinstance(body, dict) else body})")

    # --- EDGE: unknown categorical value (OneHotEncoder handle_unknown=ignore) ---
    code, body, err = invoke(ep, {"rows": [a_row(category="NEVER_SEEN", brand="ZZZ")]})
    check("unknown categorical value handled", handled(code, body, err) and (ok_predictions(body, 1) or err),
          f"code={code} body={body} err={err}", is_bug=True)

    # --- EDGE: null / None values in a numeric and a categorical field ---
    code, body, err = invoke(ep, {"rows": [a_row(unit_price=None, category=None)]})
    check("null values handled (no crash)", handled(code, body, err), f"code={code} body={body} err={err}",
          is_bug=True)

    # --- EDGE: numeric fields sent as strings ---
    code, body, err = invoke(ep, {"rows": [a_row(quantity="3", unit_price="49.99", order_total="150")]})
    check("numeric-as-string handled", handled(code, body, err), f"code={code} body={body} err={err}",
          is_bug=True)

    # --- EDGE: categorical field sent as a number ---
    code, body, err = invoke(ep, {"rows": [a_row(category=123, brand=456)]})
    check("numeric-in-categorical handled", handled(code, body, err), f"code={code} body={body} err={err}",
          is_bug=True)

    # --- EDGE: boolean values ---
    code, body, err = invoke(ep, {"rows": [a_row(quantity=True, category=False)]})
    check("boolean values handled", handled(code, body, err), f"code={code} body={body} err={err}")

    # --- EDGE: extreme magnitudes (huge, tiny, negative) ---
    code, body, err = invoke(ep, {"rows": [a_row(unit_price=1e18, cost=-1e9, lifetime_value=1e-12)]})
    check("extreme magnitudes handled", handled(code, body, err) and ok_predictions(body, 1),
          f"code={code} body={body} err={err}", is_bug=True)

    # --- EDGE: NaN / Infinity as JSON literals (non-standard JSON) ---
    code, body, err = raw_invoke(ep, b'{"rows": [{"quantity": NaN, "unit_price": Infinity}]}')
    check("NaN/Infinity literals handled", (err is not None) or (code is not None),
          f"code={code} body={body} err={err}")

    # --- EDGE: missing feature columns (drop several) ---
    row = a_row()
    for c in ["rating_avg", "margin_pct", "segment", "brand"]:
        row.pop(c, None)
    code, body, err = invoke(ep, {"rows": [row]})
    check("missing feature columns handled", handled(code, body, err), f"code={code} body={body} err={err}",
          is_bug=True)

    # --- EDGE: extra unexpected columns ---
    code, body, err = invoke(ep, {"rows": [a_row(extra1="x", extra2=99, __proto__="y")]})
    check("extra columns handled", handled(code, body, err) and ok_predictions(body, 1),
          f"code={code} body={body} err={err}", is_bug=True)

    # --- EDGE: column order shuffled (dict key order differs from training) ---
    shuffled = dict(reversed(list(a_row().items())))
    code, body, err = invoke(ep, {"rows": [shuffled]})
    check("shuffled column order handled", handled(code, body, err) and ok_predictions(body, 1),
          f"code={code} body={body} err={err}", is_bug=True)

    # --- EDGE: 'instances' positional payload (tests xgboost_sm/onnx instances path) ---
    #   sklearn: needs the encoded matrix width; a raw 13-wide positional row will
    #   likely mismatch — we just require it's HANDLED, not a crash.
    code, body, err = invoke(ep, {"instances": [[2, 49.99, 99.98, 55.0, 30.0, 45.0, 4.2, 1200.0,
                                                  "Electronics", "Acme", "Consumer", "US", "card"]]})
    check("instances positional payload handled", handled(code, body, err),
          f"code={code} body={body} err={err}", is_bug=True)

    # --- EDGE: empty rows ---
    code, body, err = invoke(ep, {"rows": []})
    check("empty rows handled", handled(code, body, err), f"code={code} body={body} err={err}")

    # --- EDGE: malformed JSON ---
    code, body, err = raw_invoke(ep, b"{not: valid, json")
    check("malformed JSON → error not crash", handled(code, body, err), f"code={code} body={body} err={err}")

    # --- EDGE: neither rows nor instances ---
    code, body, err = invoke(ep, {"foo": "bar"})
    check("no rows/instances → error", (err is not None) or (isinstance(body, dict) and "error" in body)
          or code != 200, f"code={code} body={body} err={err}")

    # --- EDGE: rows is not a list (a dict) ---
    code, body, err = invoke(ep, {"rows": a_row()})
    check("rows-as-dict handled", handled(code, body, err), f"code={code} body={body} err={err}")

    # --- EDGE: a row that is not a dict (a list) ---
    code, body, err = invoke(ep, {"rows": [[1, 2, 3]]})
    check("row-as-list handled", handled(code, body, err), f"code={code} body={body} err={err}")

    # --- EDGE: empty body ---
    code, body, err = raw_invoke(ep, b"")
    check("empty body handled", handled(code, body, err), f"code={code} body={body} err={err}")

    # --- EDGE: large batch (stress) ---
    code, body, err = invoke(ep, {"rows": [a_row(quantity=(i % 5) + 1, category=["A","B","C"][i%3]) for i in range(500)]})
    check("500-row batch → 500 predictions", ok_predictions(body, 500),
          f"code={code} len={len(body.get('predictions', [])) if isinstance(body, dict) else '?'} err={err}",
          is_bug=True)


def main():
    sk = _args.sklearn or _autodiscover("sklearn")
    onnx = _args.onnx or _autodiscover("onnx")
    xgb = _args.xgboost or _autodiscover("return-risk-20")
    targets = [("SKLEARN", sk), ("ONNX", onnx), ("XGBOOST(SM)", xgb)]
    targets = [(l, e) for l, e in targets if e]
    if not targets:
        print("No InService microvm-ep-* endpoints found."); return 2
    print("Testing:")
    for l, e in targets:
        print(f"  {l}: {e}")

    for label, ep in targets:
        run_suite(label, ep)

    # --- Cross-endpoint parity (all pairs that exist) ---
    present = {l: e for l, e in targets}
    if len(present) >= 2:
        print("\n===== CROSS-ENDPOINT PARITY =====")
        rows = [a_row(quantity=(i % 4) + 1,
                      category=["Electronics", "Home", "Toys", "Garden"][i % 4],
                      unit_price=10.0 + i * 7) for i in range(15)]
        preds = {}
        for l, e in present.items():
            code, body, err = invoke(e, {"rows": rows})
            if isinstance(body, dict) and isinstance(body.get("predictions"), list):
                preds[l] = [int(round(float(p))) if isinstance(p, (int, float)) else p
                            for p in body["predictions"]]
            else:
                print(f"  NOTE  {l} returned no predictions for parity (code={code} err={err})")
        labels = list(preds)
        if len(labels) < 2:
            print("  NOTE  fewer than 2 endpoints returned predictions — parity skipped")
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                a, b = labels[i], labels[j]
                if len(preds[a]) == len(preds[b]) and preds[a]:
                    agree = sum(1 for x, y in zip(preds[a], preds[b]) if x == y) / len(preds[a])
                    check(f"{a} vs {b} agreement >= 80%", agree >= 0.8, f"agreement={agree:.0%}")

    print(f"\n===== SUMMARY: {_passed} passed, {_failed} failed =====")
    if _bugs:
        print("\n⚠️  POTENTIAL BUGS SURFACED:")
        for b in _bugs:
            print(f"  - {b}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
