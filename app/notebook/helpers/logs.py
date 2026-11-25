"""DATA LOADING — Amazon CloudWatch Logs (Logs Insights + raw filter), HTTPS/443."""

import re
import time

import boto3
import pandas as pd

from ._common import _log_read, logger
from ._common import _REGION
from ..aws_clients import aws_region as _aws_region
from .. import aws_clients as _aws


# =============================================================================
# DATA LOADING — Amazon CloudWatch Logs
# =============================================================================
# (Categorized under DATA LOADING so the log readers sit with the other readers in
# the Snippets panel / autocomplete.) CloudWatch Logs is a pure HTTPS API
# (logs.<region>.amazonaws.com, 443), so it works within the MicroVM's managed
# egress (80/443/53) just like Athena/Aurora — no socket. The VM exec role is
# granted the query actions (logs:StartQuery/GetQueryResults/StopQuery/
# DescribeLogGroups/FilterLogEvents) in iam/exec-role-permissions.json.
#
# TWO query models CloudWatch offers, both surfaced here:
#   • Logs Insights (read_logs / ask_logs) — the pipe-based query language
#     (fields | filter | stats ... by | sort | limit). Submit → poll → fetch, just
#     like read_athena. This is the analytical one (aggregations, parse, bin()).
#   • FilterLogEvents (grep_logs / tail_logs) — simple substring/pattern grep over
#     raw events, no aggregation. The "just show me the matching lines" path.

# Insights is NOT SQL — do not try to pass SQL here (use ask_logs to turn plain
# English into a valid Insights query, or read_logs with native Insights syntax).
#
# NOTEBOOK DEFAULT: all four readers take log_group as OPTIONAL. Omit it and they
# default to THIS notebook's own CloudWatch log group(s) — resolved across every VM
# the session has run on (see _resolve_notebook_log_groups). notebook_log_groups()
# shows what that resolves to. Pass an explicit log_group (or use log_groups() to
# discover others) to query a different source.


def _logs_client():
    return _aws.aws_client("logs", _aws_region())


# ─── Optional log_stream filter (generic, explicit, contains-semantics) ──────
# All four readers take an OPTIONAL log_stream filter. It is a CONTAINS match on the
# CloudWatch log STREAM name, so it narrows a (shared) log group to a subset of its
# streams — e.g. one VM's stream within a per-tier group (VM stream names end in the
# microvm id: "2026/10/05[1.0]microvm-<id>"), or a day's streams by date prefix.
# Semantics are identical across both query models even though the two CloudWatch
# APIs differ:
#   • Insights (read_logs/ask_logs): no stream param on the API → we inject a
#     `filter @logStream like /<escaped>/` pipe stage (a regex contains match).
#   • FilterLogEvents (grep_logs/tail_logs): no contains filter on the API (only a
#     logStreamNamePrefix) → we describe the group's streams, keep the ones whose
#     name CONTAINS the filter, and pass those as logStreamNames. One extra
#     describe call buys the same contains-semantics the Insights path has.
# Purely explicit: when log_stream is None nothing is filtered (whole group).


def _escape_insights_regex(s: str) -> str:
    """Escape a literal string for use inside an Insights `like /…/` regex. Stream
    names contain regex metacharacters ([ ] . / etc.), so a raw name would be a
    broken/overbroad pattern; this makes `log_stream` match literally."""
    # Insights regex is Java-flavored; escaping the standard metacharacters (incl.
    # '/', which also delimits the literal) makes any stream name match verbatim.
    return re.sub(r'([\\.^$|?*+()\[\]{}/])', r'\\\1', s)


def _resolve_stream_names(client, log_group: str, log_stream: str,
                          cap: int = 100) -> list[str]:
    """Return the stream names in `log_group` whose name CONTAINS `log_stream`
    (case-sensitive substring) — the FilterLogEvents equivalent of the Insights
    `@logStream like` contains match. Pages describe_log_streams and filters client
    side (the API only offers a PREFIX filter, which can't match the microvm id that
    sits at the END of a VM stream name). Capped at `cap` names (FilterLogEvents
    accepts at most 100 logStreamNames). Returns [] if nothing matches."""
    matches: list[str] = []
    paginator = client.get_paginator("describe_log_streams")
    try:
        for page in paginator.paginate(logGroupName=log_group):
            for st in page.get("logStreams", []):
                name = st.get("logStreamName", "")
                if log_stream in name:
                    matches.append(name)
                    if len(matches) >= cap:
                        return matches
    except client.exceptions.ResourceNotFoundException:
        raise
    return matches


# ─── "This notebook's" log group resolution ──────────────────────────────────
# When read_logs/ask_logs/grep_logs/tail_logs are called WITHOUT a log_group, they
# default to the CURRENT notebook's own CloudWatch log group(s). A notebook is one
# session_id that, over its lifetime, may be served by SEVERAL VMs (rotations /
# reactive-recovery swaps), each on a possibly-different size tier → a different log
# group (/aws/lambda-microvms/{IMAGE_BASE_NAME}-{arch}-{mem}). "This notebook's
# logs" therefore means ALL those groups, merged.
#
#   • Preferred (Option B): query the v_nb_session_cost analytics view (via Aurora)
#     for every (arch, memory_mib) tier this session has used → all its log groups,
#     covering the full history across VM swaps. Same view cost.py already reads.
#   • Fallback (Option A): if Aurora analytics isn't configured (the proxy was
#     launched WITHOUT --AuroraSetup) or the view has no row yet, fall back to just
#     THIS VM's own log group, built from the identity env the /run hook publishes
#     (MICROVM_ARCH / MICROVM_MEMORY_MIB / IMAGE_BASE_NAME). This keeps a no-arg
#     read_logs() working — it just won't see logs from earlier VMs of the session.


def _own_log_group() -> str | None:
    """This VM's OWN CloudWatch log group from the launch identity env, or None if
    the env isn't populated. /aws/lambda-microvms/{IMAGE_BASE_NAME}-{arch}-{mem}."""
    import os
    base = os.environ.get("IMAGE_BASE_NAME") or "notebook-microvm"
    arch = os.environ.get("MICROVM_ARCH") or os.environ.get("DEFAULT_IMAGE_ARCH")
    mem = os.environ.get("MICROVM_MEMORY_MIB")
    if not arch or not mem:
        return None
    return f"/aws/lambda-microvms/{base}-{arch}-{mem}"


def _resolve_notebook_log_groups() -> list[str]:
    """Resolve the current notebook's CloudWatch log group(s).

    Returns a de-duplicated list of log-group names (oldest tier first where known).
    Tries the full across-VM-swaps set from the v_nb_session_cost view (Aurora);
    gracefully falls back to just this VM's own group when Aurora isn't configured
    or has no row for the session yet. Returns [] only when NOTHING is resolvable
    (no Aurora AND no identity env) — the callers turn that into a clear error.
    """
    import os

    base = os.environ.get("IMAGE_BASE_NAME") or "notebook-microvm"
    session_id = os.environ.get("MICROVM_SESSION_ID") or ""
    groups: list[str] = []
    seen: set[str] = set()

    def _add(g: str | None):
        if g and g not in seen:
            seen.add(g)
            groups.append(g)

    # --- Option B: all tiers this session used, from the analytics view ----------
    cluster = os.environ.get("AURORA_DATA_API_CLUSTER_ARN", "")
    secret = os.environ.get("AURORA_DATA_API_SECRET_ARN", "")
    if cluster and secret and session_id:
        try:
            from .aurora import read_aurora
            # One session can have multiple rows (one per VM/tier) in the per-VM
            # analytics; select the distinct (arch, mem) tiers it has run on.
            q = read_aurora(
                "SELECT DISTINCT architecture, memory_mib FROM v_nb_session_cost "
                "WHERE session_id = :sid",
                parameters=[{"name": "sid", "value": {"stringValue": session_id}}],
            )
            if q is not None and len(q):
                for _, row in q.iterrows():
                    arch = row.get("architecture") or os.environ.get("DEFAULT_IMAGE_ARCH")
                    mem = row.get("memory_mib")
                    if arch and mem is not None and str(mem).strip() not in ("", "None"):
                        _add(f"/aws/lambda-microvms/{base}-{arch}-{int(mem)}")
        except Exception as e:
            # Aurora may be resuming, the view may lag, or the role may lack access —
            # never fail the log read for this; just fall through to the own-group.
            logger.warning(
                f"notebook log-group resolve via Aurora failed "
                f"({type(e).__name__}); falling back to this VM's own log group."
            )

    # --- Option A fallback: this VM's own group (always try to include it) --------
    _add(_own_log_group())
    return groups


def _default_log_group(single: bool = False):
    """Resolve the current notebook's log group(s) for a helper called without an
    explicit `log_group`. Raises a clear error if nothing is resolvable.

    single=True returns ONE group name (the most relevant = this VM's own group,
    else the first resolved) for the single-group readers (grep_logs/tail_logs);
    single=False returns the full list for the Insights readers (read_logs/ask_logs),
    which can query many groups at once."""
    groups = _resolve_notebook_log_groups()
    if not groups:
        raise RuntimeError(
            "No log_group given and this notebook's log group couldn't be resolved "
            "(no session identity in the runtime env). Pass log_group=... explicitly "
            "— run log_groups('/aws/lambda-microvms/') to list what's available."
        )
    if single:
        # Prefer this VM's own group for a single-group grep/tail so the newest
        # lines come from the VM actually running the cell.
        own = _own_log_group()
        return own if own in groups else groups[0]
    return groups


def _time_window(hours: float, start, end):
    """Resolve a (start_ms, end_ms) epoch-millisecond window. Explicit start/end
    (epoch seconds, or anything pandas.to_datetime parses) win; otherwise it's the
    last `hours` hours ending now. CloudWatch wants epoch SECONDS for Insights and
    MILLISECONDS for FilterLogEvents — we return ms and the callers scale."""
    now = time.time()
    if end is not None:
        end_s = pd.to_datetime(end).timestamp() if not isinstance(end, (int, float)) else float(end)
    else:
        end_s = now
    if start is not None:
        start_s = pd.to_datetime(start).timestamp() if not isinstance(start, (int, float)) else float(start)
    else:
        start_s = end_s - float(hours) * 3600.0
    return int(start_s * 1000), int(end_s * 1000)


@_log_read("cloudwatch")
def read_logs(query: str, log_group=None, hours: float = 1, start=None, end=None,
              limit: int = 1000, poll_timeout: float = 60.0,
              log_stream: str = None) -> pd.DataFrame:
    """
    Run a CloudWatch Logs **Insights** query and return the results as a DataFrame.

    Insights is a pipe-based language (NOT SQL): `fields ... | filter ... |
    stats ... by ... | sort ... | limit ...`. The time range is a parameter here,
    NOT part of the query. Each result row becomes a DataFrame row; the columns are
    whatever fields the query emits (@timestamp, @message, your `stats`/`parse`
    aliases). `@timestamp` is parsed to datetime and numeric columns are coerced.

    Args:
      query:       the Insights query string.
      log_group:   one log-group name, or a list of names (Insights supports many).
                   OMIT it to default to THIS notebook's own log group(s) — every
                   VM the session has run on (merged). Run log_groups() to list
                   other groups you can pass explicitly.
      hours:       relative window — the last N hours ending now (default 1). Use
                   start/end for an explicit range instead.
      start, end:  explicit window bounds (epoch seconds or any datetime pandas can
                   parse, e.g. "2024-06-01" / a pd.Timestamp). Override `hours`.
      limit:       max rows the query returns (default 1000; also enforce in-query
                   with `| limit` for aggregations).
      poll_timeout: seconds to wait for the async query to finish (default 60).
      log_stream:  OPTIONAL stream-name CONTAINS filter. When set, a
                   `filter @logStream like /<escaped>/` stage is prepended so the
                   query only sees events from streams whose name contains this
                   string — e.g. one VM's stream (its name ends in the microvm id)
                   or a day's streams by date. None (default) = the whole group.

    Example:
        # Error count per hour from this notebook platform's own VM logs
        read_logs('''
            fields @timestamp, @message
            | filter @message like /ERROR/
            | stats count() as errors by bin(1h)
        ''', log_group="/aws/lambda-microvms/notebook-microvm-arm64-2048", hours=24)

    Tip: `log_groups()` lists what you can query; `ask_logs(question, log_group)`
    writes the Insights query for you from plain English.

    ⚠️ COST: Insights bills per GB of log data SCANNED, so narrow the time window
    and add a `filter` early in the pipe for large/busy log groups.
    """
    client = _logs_client()
    if log_group is None:
        groups = _default_log_group(single=False)
    else:
        groups = [log_group] if isinstance(log_group, str) else list(log_group)
    start_ms, end_ms = _time_window(hours, start, end)
    # Optional stream filter → prepend a `filter @logStream like /…/` stage. A
    # leading filter stage is valid Insights regardless of what the user's query
    # starts with (fields/stats/filter/…), so this composes with any query.
    effective_query = query
    if log_stream:
        effective_query = (
            f"filter @logStream like /{_escape_insights_regex(log_stream)}/\n| "
            + query.lstrip().lstrip("|").lstrip()
        )
    # Insights takes epoch SECONDS.
    kwargs = {
        "startTime": start_ms // 1000,
        "endTime": end_ms // 1000,
        "queryString": effective_query,
        "limit": int(limit),
    }
    # One name → logGroupName; many → logGroupNames (both accepted by the API).
    if len(groups) == 1:
        kwargs["logGroupName"] = groups[0]
    else:
        kwargs["logGroupNames"] = groups

    try:
        query_id = client.start_query(**kwargs)["queryId"]
    except client.exceptions.ResourceNotFoundException as e:
        raise FileNotFoundError(
            f"read_logs: log group not found: {groups}. Run log_groups() to see what "
            f"exists (names are case-sensitive and start with '/')."
        ) from e

    # Poll to completion (Complete/Failed/Cancelled/Timeout), bounded.
    deadline = time.time() + poll_timeout
    resp = None
    while True:
        resp = client.get_query_results(queryId=query_id)
        status = resp.get("status")
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        if time.time() > deadline:
            try:
                client.stop_query(queryId=query_id)
            except Exception:
                pass
            raise TimeoutError(
                f"read_logs: query did not finish within {poll_timeout:.0f}s "
                f"(status={status}). Narrow the time window / add a filter, or raise "
                f"poll_timeout."
            )
        time.sleep(0.5)

    if status != "Complete":
        raise RuntimeError(f"read_logs: Insights query ended with status={status}.")

    # results is a list of rows; each row is a list of {field, value} dicts.
    rows = []
    for row in resp.get("results", []):
        rows.append({c["field"]: c["value"] for c in row})
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # CloudWatch adds a hidden @ptr pointer column — drop it (not user data).
    if "@ptr" in df.columns:
        df = df.drop(columns=["@ptr"])
    # Coerce @timestamp to datetime; convert other columns to numeric so stats aggs
    # (count/sum/avg/bin values) come back as numbers, not strings. Best-effort.
    if "@timestamp" in df.columns:
        df["@timestamp"] = pd.to_datetime(df["@timestamp"], errors="coerce")
    for col in df.columns:
        if col in ("@timestamp", "@message", "@logStream", "@log"):
            continue
        num = pd.to_numeric(df[col], errors="coerce")
        orig_notna = df[col].notna()
        # Only convert when EVERY non-null value parses as numeric. A partial match
        # means the column is really a string that merely CONTAINS numeric-looking
        # values (e.g. a `stats ... by route` key mixing "200" and "/api") —
        # coercing would NaN-out the non-numeric rows and change dtype.
        if not (orig_notna.any() and (num.notna() == orig_notna).all()):
            continue
        # Even when every value parses, a leading zero (e.g. a zero-padded code
        # "007", an account/order id "00123") signals the column is a STRING
        # identifier, not a quantity — numeric coercion would silently drop the
        # zeros and corrupt the id. Leave such columns as strings. (Plain "0", a
        # decimal like "0.5", or a sign/decimal form are NOT leading-zero ids.)
        if _has_leading_zero_id(df[col][orig_notna]):
            continue
        df[col] = num
    return df


def _has_leading_zero_id(values) -> bool:
    """True if any value is a leading-zero integer string like "007"/"00123" — a
    sign the column is a string identifier, not a numeric quantity (coercing it
    would drop the zeros). "0" alone and decimals like "0.5"/"0.0" are NOT flagged."""
    for v in values:
        s = str(v).strip()
        if len(s) >= 2 and s[0] == "0" and s[1].isdigit():
            # starts with 0 followed by another digit → "00", "01", "007", "0123".
            return True
    return False


@_log_read("cloudwatch")
def grep_logs(pattern: str, log_group=None, hours: float = 1, start=None, end=None,
              limit: int = 1000, log_stream: str = None) -> pd.DataFrame:
    """
    Grep raw CloudWatch log events for a substring/pattern — the "just show me the
    matching lines" path (no Insights syntax, no aggregation). Returns a DataFrame
    with columns: timestamp, log_stream, message.

    Args:
      pattern:    a CloudWatch filter pattern. A plain word matches that substring;
                  see the CloudWatch filter-pattern syntax for quoted phrases, OR,
                  and JSON/space-delimited matching.
      log_group:  the log-group name to search. OMIT it to default to THIS
                  notebook's own log group (the VM running this cell). To grep
                  across every VM the session used, prefer read_logs (Insights
                  accepts multiple groups).
      hours:      relative window (last N hours, default 1); or pass start/end.
      limit:      max events to return (default 1000).
      log_stream: OPTIONAL stream-name CONTAINS filter — restrict the grep to
                  streams whose name contains this string (e.g. a microvm id, or a
                  date). The group's streams are listed and the matching ones
                  queried. None (default) = all streams in the group.

    Example:
        grep_logs("Timeout", hours=6)   # this notebook's own logs
        grep_logs("Timeout", "/aws/lambda-microvms/notebook-microvm-arm64-2048", hours=6)
        grep_logs("ERROR", log_stream="microvm-b10ca2de")   # one VM's stream only

    For aggregation/stats use read_logs (Insights); for the newest lines use tail_logs.
    """
    client = _logs_client()
    if log_group is None:
        log_group = _default_log_group(single=True)
    start_ms, end_ms = _time_window(hours, start, end)
    limit = int(limit)
    # A non-positive limit means "no events wanted" — return the empty frame rather
    # than calling the API (FilterLogEvents requires 1 ≤ limit ≤ 10000, so limit<=0
    # would raise a ValidationException).
    if limit <= 0:
        return pd.DataFrame(columns=["timestamp", "log_stream", "message"])
    # Optional stream filter → resolve the matching stream names (contains match)
    # and scope FilterLogEvents to them. If the filter matches NOTHING, there are no
    # events to return — short-circuit to an empty frame (rather than falling back
    # to the whole group, which would silently ignore the filter).
    stream_names = None
    if log_stream:
        try:
            stream_names = _resolve_stream_names(client, log_group, log_stream)
        except client.exceptions.ResourceNotFoundException as e:
            raise FileNotFoundError(
                f"grep_logs: log group not found: {log_group!r}. Run log_groups() to "
                f"see what exists."
            ) from e
        if not stream_names:
            return pd.DataFrame(columns=["timestamp", "log_stream", "message"])
    events = []
    token = None
    try:
        while len(events) < limit:
            # Clamp the per-request limit into the API's valid 1..10000 range. The
            # loop condition guarantees limit - len(events) >= 1 here, but max(1, …)
            # is a belt-and-braces guard against ever sending 0.
            batch = max(1, min(10000, limit - len(events)))
            kwargs = {
                "logGroupName": log_group,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": batch,
            }
            if stream_names:
                kwargs["logStreamNames"] = stream_names
            if pattern:
                kwargs["filterPattern"] = pattern
            if token:
                kwargs["nextToken"] = token
            resp = client.filter_log_events(**kwargs)
            events.extend(resp.get("events", []))
            token = resp.get("nextToken")
            if not token:
                break
    except client.exceptions.ResourceNotFoundException as e:
        raise FileNotFoundError(
            f"grep_logs: log group not found: {log_group!r}. Run log_groups() to see "
            f"what exists."
        ) from e

    rows = [{
        "timestamp": pd.to_datetime(e.get("timestamp"), unit="ms"),
        "log_stream": e.get("logStreamName", ""),
        "message": (e.get("message") or "").rstrip("\n"),
    } for e in events[:limit]]
    return pd.DataFrame(rows, columns=["timestamp", "log_stream", "message"])


@_log_read("cloudwatch")
def tail_logs(log_group=None, lines: int = 50, hours: float = 1,
              log_stream: str = None) -> pd.DataFrame:
    """
    The most-recent N events from a log group (newest first) — the quick "what just
    happened?" peek, analogous to head_s3 for a file. Returns timestamp, log_stream,
    message. Equivalent to grep_logs with no pattern, sorted newest-first and capped.

    log_group:  OMIT it to tail THIS notebook's own logs (the VM running this cell).
    log_stream: OPTIONAL stream-name CONTAINS filter — tail only streams whose name
                contains this string (e.g. a microvm id). None = all streams.

    Example:
        tail_logs(lines=100)   # this notebook's own logs
        tail_logs("/aws/lambda-microvms/notebook-microvm-arm64-2048", lines=100)
        tail_logs(log_stream="microvm-b10ca2de", lines=100)   # one VM's stream only
    """
    if log_group is None:
        log_group = _default_log_group(single=True)
    df = grep_logs("", log_group, hours=hours, limit=max(1, int(lines)) * 4,
                   log_stream=log_stream)
    if df.empty:
        return df
    return (df.sort_values("timestamp", ascending=False)
              .head(int(lines)).reset_index(drop=True))


def notebook_log_groups() -> list[str]:
    """The CloudWatch log group(s) that belong to THIS notebook — every VM the
    current session has run on (across rotations/recovery swaps), merged.

    This is what read_logs()/ask_logs()/grep_logs()/tail_logs() default to when you
    omit `log_group`. Call it to see exactly which group(s) that resolves to, or to
    pass the list somewhere explicitly.

    Resolution: the full across-swaps set from the analytics view when Aurora is
    configured, else just this VM's own group (so it still works without
    --AuroraSetup — it just won't include earlier VMs of the session).

    Example:
        notebook_log_groups()                  # e.g. ['/aws/lambda-microvms/notebook-microvm-arm64-2048']
        read_logs('fields @timestamp,@message | limit 20')   # uses these automatically
    """
    return _resolve_notebook_log_groups()


def log_groups(prefix: str = None, limit: int = 100) -> pd.DataFrame:
    """
    List the CloudWatch log groups you can query, as a DataFrame (columns:
    log_group, stored_mb, retention_days, created). The discovery helper — run it to
    find a `log_group` name to pass to read_logs / grep_logs / ask_logs.

    prefix: only groups whose name starts with this (e.g. "/aws/lambda-microvms/"
      for this notebook platform's own VM logs). Default None → all accessible groups
      (up to `limit`).

    Example:
        log_groups("/aws/lambda-microvms/")   # this platform's VM log groups
    """
    client = _logs_client()
    groups = []
    paginator = client.get_paginator("describe_log_groups")
    pg_kwargs = {}
    if prefix:
        pg_kwargs["logGroupNamePrefix"] = prefix
    for page in paginator.paginate(**pg_kwargs):
        for g in page.get("logGroups", []):
            groups.append({
                "log_group": g.get("logGroupName", ""),
                "stored_mb": round(g.get("storedBytes", 0) / (1024 * 1024), 2),
                "retention_days": g.get("retentionInDays"),  # None = never expire
                "created": pd.to_datetime(g.get("creationTime"), unit="ms") if g.get("creationTime") else None,
            })
            if len(groups) >= limit:
                break
        if len(groups) >= limit:
            break
    df = pd.DataFrame(groups, columns=["log_group", "stored_mb", "retention_days", "created"])
    if len(df):
        print(f"  {len(df)} log group(s)" + (f" under {prefix!r}" if prefix else ""))
    return df


def _log_group_fields(log_group: str, hours: float = 24) -> list:
    """Best-effort: the field names CloudWatch has seen in a log group recently
    (via get_log_group_fields). Used as context for ask_logs so the LLM references
    REAL fields. Returns [] on any failure (the model can still use @message)."""
    try:
        client = _logs_client()
        resp = client.get_log_group_fields(logGroupName=log_group)
        return [f["name"] for f in resp.get("logGroupFields", []) if f.get("name")]
    except Exception:
        return []


@_log_read("cloudwatch")
def ask_logs(question: str, log_group=None, hours: float = 1, start=None, end=None,
             limit: int = 1000, show_query: bool = True, model: str = None,
             log_stream: str = None) -> pd.DataFrame:
    """
    Ask a question of your logs in **plain English** — an LLM writes the CloudWatch
    Logs Insights query, then it's run with read_logs and the DataFrame is returned.
    This is the "I don't want to learn Insights syntax" front door (CloudWatch is
    NOT SQL, so there's no SQL to translate — we go English → Insights directly).

    Args:
      question:   what you want, e.g. "error count per hour over the last day" or
                  "the 20 slowest requests with their latency".
      log_group:  the log-group name (or list) to query — same as read_logs. OMIT
                  it to ask of THIS notebook's own log group(s) (across every VM
                  the session used).
      hours / start / end / limit: the time window + row cap, passed to read_logs.
      show_query: print the generated Insights query before running (default True)
                  so it's transparent and you can copy/tweak it into read_logs.
      model:      override the Bedrock model (default BEDROCK_MODEL_ID).
      log_stream: OPTIONAL stream-name CONTAINS filter, passed through to read_logs
                  (restricts to streams whose name contains this string, e.g. a
                  microvm id). None (default) = the whole group.

    Example:
        ask_logs("how many ERROR lines per hour in the last 24h",
                 log_group="/aws/lambda-microvms/notebook-microvm-arm64-2048", hours=24)

    Returns the result DataFrame (same shape read_logs produces). The generated
    query is also attached as df.attrs['insights_query'].
    """
    from .llm import invoke_llm  # lazy — avoids import cost when logs aren't used

    # Resolve "this notebook's" groups up front so the field hint + the actual read
    # use the SAME groups (and the LLM sees a real log group's fields).
    if log_group is None:
        log_group = _default_log_group(single=False)
    primary_group = log_group if isinstance(log_group, str) else (log_group[0] if log_group else "")
    fields = _log_group_fields(primary_group, hours=hours) if primary_group else []
    fields_hint = (
        f"\nFields seen in this log group (use these where relevant): "
        f"{', '.join(fields[:40])}" if fields else ""
    )
    system = (
        "You translate a user's request into a single Amazon CloudWatch Logs "
        "Insights query. Output ONLY the query — no prose, no markdown fences, no "
        "explanation. Rules:\n"
        "- It is NOT SQL. Use the pipe syntax: fields | filter | stats ... by ... | "
        "sort ... | limit ...\n"
        "- NO SQL constructs: there is no CASE/WHEN/END, no GROUP BY, no HAVING. For "
        "conditionals use nested if(cond, a, b); for conditional counts use "
        "sum(if(cond, 1, 0)).\n"
        "- Do NOT include start/end times — the caller sets the time range "
        "separately. Never put an absolute date or a `filter @timestamp ...` bound "
        "in the query.\n"
        "- Time buckets: write `stats count(*) as n by bin(5m) as bucket` — ALWAYS "
        "alias the bin with `as <name>`. To order by time, `sort <name> asc` using "
        "that ALIAS. NEVER `sort bin(5m)` — sorting by a bin() expression is a "
        "syntax error. Only ever sort/reference an ALIASED field.\n"
        "- Every stats aggregate should be aliased with `as <name>` and only those "
        "aliases (or @timestamp/@message) may appear in sort.\n"
        "- Use `filter @message like /regex/` for text matching and `parse` to pull "
        "fields out of @message when needed. Prefer @timestamp and @message when "
        "unsure of field names."
    )
    prompt = f"Request: {question}{fields_hint}\n\nCloudWatch Logs Insights query:"
    query = invoke_llm(prompt, system=system, model=model, temperature=0.0, max_tokens=400)
    # Strip any stray code fences / backticks the model may add despite instructions.
    query = (query or "").strip()
    if query.startswith("```"):
        query = query.strip("`")
        # drop an optional leading language tag line
        query = "\n".join(line for line in query.splitlines() if line.strip().lower() not in ("sql", "insights"))
    query = query.strip()
    if not query:
        raise RuntimeError(
            "ask_logs: the model returned no query. Rephrase the question, or write "
            "the Insights query yourself and pass it to read_logs()."
        )
    if show_query:
        print("Insights query:\n" + query + "\n")
    df = read_logs(query, log_group, hours=hours, start=start, end=end, limit=limit,
                   log_stream=log_stream)
    try:
        df.attrs["insights_query"] = query
    except Exception:
        pass
    return df
