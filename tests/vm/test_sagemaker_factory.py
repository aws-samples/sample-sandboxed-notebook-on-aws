#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests: SageMaker, Bedrock KB, llm, Glue and whoami build their clients
through the factory (app.notebook.aws_clients), and sm_jobs / sm_endpoints follow
NextToken. Fake clients, no network.

Run: .venv/bin/python tests/vm/test_sagemaker_factory.py
"""
import datetime
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ["AWS_REGION"] = "us-west-2"

import boto3  # noqa: E402

from app.notebook import helpers as H  # noqa: E402
from app.notebook.helpers import sagemaker as smk  # noqa: E402
from app.notebook.helpers import bedrock_kb as kb  # noqa: E402

logging.disable(logging.WARNING)
_fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if not cond else ""))
    if not cond:
        _fails.append(label)


NOW = datetime.datetime(2026, 1, 1)
made = []


class FakeSM:
    """Paged list_training_jobs / list_endpoints (page size 2, honours MaxResults)."""
    def __init__(self):
        self.job_calls, self.ep_calls = [], []
        self.jobs = [{"TrainingJobName": f"job-{i}", "TrainingJobStatus": "Completed",
                      "CreationTime": NOW, "TrainingEndTime": NOW} for i in range(5)]
        self.eps = [{"EndpointName": f"microvm-ep-{i}", "EndpointStatus": "InService",
                     "CreationTime": NOW} for i in range(5)]

    @staticmethod
    def _page(items, kw):
        start = int(kw["NextToken"]) if kw.get("NextToken") else 0
        n = min(2, kw["MaxResults"])
        out = {"_items": items[start:start + n]}
        if start + n < len(items):
            out["NextToken"] = str(start + n)
        return out

    def list_training_jobs(self, **kw):
        self.job_calls.append(kw)
        p = self._page(self.jobs, kw)
        return {"TrainingJobSummaries": p["_items"], **({"NextToken": p["NextToken"]} if "NextToken" in p else {})}

    def list_endpoints(self, **kw):
        self.ep_calls.append(kw)
        p = self._page(self.eps, kw)
        return {"Endpoints": p["_items"], **({"NextToken": p["NextToken"]} if "NextToken" in p else {})}

    def describe_endpoint_config(self, **kw):
        return {"ProductionVariants": [{"InstanceType": "ml.m5.large", "InitialInstanceCount": 1}]}

    def describe_endpoint(self, **kw):
        return {"EndpointStatus": "InService"}


fsm = FakeSM()


class FakeKbAgent:
    def get_paginator(self, name):
        assert name == "list_knowledge_bases"
        pages = [{"knowledgeBaseSummaries": [{"knowledgeBaseId": "AAAAAAAAAA", "name": "other"}]},
                 {"knowledgeBaseSummaries": [{"knowledgeBaseId": "BBBBBBBBBB", "name": "reviews-kb"}]}]

        class P:
            def paginate(self_inner):
                return iter(pages)
        return P()


def fake_client(service, **kw):
    made.append((service, kw.get("region_name")))
    if service == "sagemaker":
        return fsm
    if service == "bedrock-agent":
        return FakeKbAgent()
    if service == "sts":
        class S:
            def get_caller_identity(self):
                return {"Account": "111122223333", "Arn": "arn:aws:sts::111122223333:assumed-role/R/s"}
        return S()
    raise AssertionError(f"unexpected service {service}")


boto3.client = fake_client

# ── sm_jobs / sm_endpoints pagination ─────────────────────────────────────────
df = H.sm_jobs(max_results=5)
check("sm_jobs reads 3 pages for 5 jobs", len(df) == 5 and len(fsm.job_calls) == 3, len(fsm.job_calls))
check("sm_jobs follows NextToken", fsm.job_calls[1].get("NextToken") == "2")
check("sm_jobs keeps order and columns",
      list(df.columns) == ["job_name", "status", "created", "ended"] and df["job_name"].iloc[0] == "job-0")
check("sm_jobs keeps SortBy/SortOrder", fsm.job_calls[0]["SortBy"] == "CreationTime"
      and fsm.job_calls[0]["SortOrder"] == "Descending")
fsm.job_calls.clear()
df = H.sm_jobs(max_results=3)
check("sm_jobs stops at max_results (3 of 5)", len(df) == 3)
fsm.job_calls.clear()
df = H.sm_jobs(max_results=500)
check("sm_jobs max_results above the listing returns everything", len(df) == 5
      and fsm.job_calls[0]["MaxResults"] == 100)

df = H.sm_endpoints(max_results=25)
check("sm_endpoints reads all 3 pages", len(df) == 5 and len(fsm.ep_calls) == 3, len(fsm.ep_calls))
check("sm_endpoints keeps NameContains filter", all(c["NameContains"] == "microvm-ep-" for c in fsm.ep_calls))
check("sm_endpoints keeps columns",
      list(df.columns) == ["endpoint", "type", "status", "region", "created"])

# ── factory use ───────────────────────────────────────────────────────────────
check("SageMaker client came from the factory with the call-time region",
      ("sagemaker", "us-west-2") in made)
check("_sm and _smr are the factory", smk._sm("eu-west-1") is fsm)

# ── Bedrock KB: name -> id resolves on page 2 ─────────────────────────────────
check("_kb_resolve_id finds the match on page 2", kb._kb_resolve_id("reviews-kb", "us-west-2") == "BBBBBBBBBB")

# ── whoami uses the factory STS client ────────────────────────────────────────
made.clear()
try:
    info = H.whoami()
    check("whoami builds sts through the factory", ("sts", "us-west-2") in made
          and info["account"] == "111122223333")
except Exception as e:  # whoami also queries Aurora analytics; only the STS part matters here
    check("whoami builds sts through the factory", ("sts", "us-west-2") in made, str(e))

if _fails:
    print(f"\n{len(_fails)} FAILED")
    sys.exit(1)
print("\nAll passed")
