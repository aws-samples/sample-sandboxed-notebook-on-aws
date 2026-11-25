#!/usr/bin/env python3
# test-kind: offline
"""
Offline tests for the VM log stream lookup (proxy/notebook/routes/logs.py::
_find_log_stream, code-review C-B-05).

A log group is shared by every VM of one image tier, so the session VM's stream
can be far behind the newest ones. Covers:
  - a stream at position 90 of 120 (newest first) is found by paging;
  - the name is cached: a second lookup makes no API call;
  - _forget_log_stream drops the cached name;
  - an absent VM returns None after at most _STREAM_LOOKUP_MAX_PAGES pages.

Run:
    .venv/bin/python tests/proxy/test_log_stream_lookup.py
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("AWS_REGION", "us-west-2")

from proxy.notebook.routes import logs  # noqa: E402

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))


class FakeLogsClient:
    """describe_log_streams over a fixed newest-first list, paged by nextToken."""

    def __init__(self, names, endless=False):
        self.names = names
        self.endless = endless
        self.calls = 0

    def describe_log_streams(self, logGroupName, orderBy, descending, limit, nextToken=None):
        assert orderBy == "LastEventTime" and descending is True
        self.calls += 1
        start = int(nextToken or 0)
        page = self.names[start:start + limit]
        resp = {"logStreams": [{"logStreamName": n} for n in page]}
        if self.endless or start + limit < len(self.names):
            resp["nextToken"] = str(start + limit)
        return resp


def _names(n):
    return [f"2026/11/17/[$LATEST]microvm-vm{i:04d}" for i in range(n)]


def main():
    group = "/aws/lambda-microvms/notebook-microvm-arm64-2048"
    names = _names(120)
    target = "vm0090"

    print("\n-- paging past the newest page --")
    logs._stream_cache.clear()
    client = FakeLogsClient(names)
    found = logs._find_log_stream(client, group, target)
    check("stream at position 90 of 120 found", found == names[90], str(found))
    check("found within 2 pages of 50", client.calls == 2, str(client.calls))

    print("\n-- cache --")
    client.calls = 0
    again = logs._find_log_stream(client, group, target)
    check("second lookup served from cache", again == names[90] and client.calls == 0,
          f"{again} calls={client.calls}")
    logs._forget_log_stream(group, target)
    logs._find_log_stream(client, group, target)
    check("forget drops the cached name (API called again)", client.calls == 2, str(client.calls))

    print("\n-- absent VM --")
    logs._stream_cache.clear()
    client = FakeLogsClient(names)
    check("absent VM in a finite group returns None",
          logs._find_log_stream(client, group, "vm-missing") is None)
    check("finite group stops at the last page", client.calls == 3, str(client.calls))
    endless = FakeLogsClient(_names(5000), endless=True)
    check("absent VM in a huge group returns None",
          logs._find_log_stream(endless, group, "vm-missing") is None)
    check("lookup bounded to _STREAM_LOOKUP_MAX_PAGES",
          endless.calls == logs._STREAM_LOOKUP_MAX_PAGES, str(endless.calls))
    check("a miss is not cached", (group, "vm-missing") not in logs._stream_cache)

    passed = sum(1 for _, ok in _checks if ok)
    print(f"\nRESULT: {passed}/{len(_checks)} checks passed")
    if passed != len(_checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
