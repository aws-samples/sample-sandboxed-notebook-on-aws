"""
Generation-state tracking + S3 persistence for Workbook Intelligence.

Part of: proxy.notebook.ai.intel

Owns:
  - the in-memory "is a generation actively running" registry (so callers can
    report status="generating" and de-duplicate concurrent requests), and
  - reading/writing the intel report JSON to S3
    (sessions/{session_id}/workbook-intel.json).
"""

import os
import json
import logging
import threading
from collections import deque


from proxy.platform import aws_clients

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-session intel-generation SCHEDULER (FIFO queue + self-draining worker).
#
# Every generation (full / delta file_upload / deletion) reads the current
# intel + data catalog and rewrites sessions/{id}/workbook-intel.json. So they
# are competing WRITERS to one document — running two at once (or dropping one
# because another is mid-flight) corrupts or loses work. Concretely we saw:
#   • a file uploaded while a full generation runs would be DROPPED (the boolean
#     guard skipped it) → the report silently omits the new file, and
#   • the old guard released "busy" between Phase 1 and Phase 2, so a delta could
#     start during Phase 2 and both put_object the same key (last-writer-wins).
#
# Fix: per session, a FIFO queue drained by a SINGLE worker thread. Generations
# for a session run strictly one-at-a-time, in submission order; because each
# job re-reads current state when it runs, later jobs naturally see earlier
# jobs' effects, so ordering alone yields a correct final document (no need for
# type-aware coalescing). Overlapping triggers are QUEUED, never dropped.
#
# EVENT-DRIVEN, no scheduler/timer (we deliberately removed background timers):
#   • enqueue into an IDLE session → the enqueue starts one worker thread.
#   • the worker, on finishing a job, pops the next queued job and runs it inline;
#     when the queue empties it clears "running" and the thread EXITS.
# So a thread exists only while there's work, and dies when the queue drains.
#
# Scope: in proxy PROCESS memory (correct for the single-instance deployment).
# If the proxy is ever horizontally scaled, the serialization point must move to
# a shared lock (e.g. pg_try_advisory_lock, like VM recovery) — this module is
# the single place to make that swap.
#
# Status API (is_generating / get_generating_trigger) is preserved for the route
# + frontend: is_generating = running OR queued; get_generating_trigger = the
# RUNNING job's trigger (so the 'updating vs removing' message matches reality).
# ---------------------------------------------------------------------------
_generating_lock = threading.Lock()

# session_id -> deque of pending jobs. A job is a dict:
#   {"run": callable() -> None, "trigger": str, "dedup_key": hashable}
_queues: dict[str, deque] = {}
# session_id -> the trigger of the job CURRENTLY running (for the UI status line).
# Absent from this dict == no job running for that session.
_running_trigger: dict[str, str] = {}

# Cap the per-session queue so a wedged/looping client can't grow it unbounded.
_MAX_QUEUE = 8


def is_generating(session_id: str) -> bool:
    """True if a generation is running OR queued for this session (the UI should
    keep showing 'updating' while anything is pending)."""
    with _generating_lock:
        return session_id in _running_trigger or bool(_queues.get(session_id))


def get_generating_trigger(session_id: str) -> str | None:
    """The trigger of the job CURRENTLY running ('file_upload'|'file_delete'|
    'manual'|...), or None. Reflects the running job (not merely queued ones) so
    the 'updating vs removing' message matches what's actually executing."""
    with _generating_lock:
        return _running_trigger.get(session_id)


def submit_generation(session_id: str, run, trigger: str = "manual",
                      dedup_key=None) -> bool:
    """Enqueue a generation job for a session and ensure a worker is draining it.

    `run` is a zero-arg callable that performs ONE generation (full/delta/deletion)
    and returns when done. Jobs for a session run strictly serially in FIFO order.

    Returns True if the job was accepted (running now or queued), False if it was
    dropped as an exact-duplicate of the job already at the tail of the queue
    (e.g. a double-clicked refresh, or repeated identical file events) or the
    queue is at its cap. Never blocks; starts at most one worker thread per idle
    session.
    """
    job = {"run": run, "trigger": trigger, "dedup_key": dedup_key}
    start_worker = False
    with _generating_lock:
        q = _queues.setdefault(session_id, deque())
        # Drop an exact-duplicate CONSECUTIVE enqueue (same dedup_key as the tail).
        if dedup_key is not None and q and q[-1].get("dedup_key") == dedup_key:
            logger.info(f"[intel] Dropping duplicate {trigger} enqueue for session {session_id[:8]}... (same as queued tail)")
            return False
        if len(q) >= _MAX_QUEUE:
            logger.warning(f"[intel] Queue full ({_MAX_QUEUE}) for session {session_id[:8]}... — dropping {trigger}")
            return False
        q.append(job)
        # Start a worker only if none is draining this session right now.
        if session_id not in _running_trigger:
            _running_trigger[session_id] = trigger  # provisional; set per job in _drain
            start_worker = True
    if start_worker:
        threading.Thread(
            target=_drain, args=(session_id,), daemon=True,
            name=f"intel-{session_id[:8]}",
        ).start()
    return True


def _drain(session_id: str) -> None:
    """Worker loop: run queued jobs one-at-a-time until the queue empties, then go
    idle and EXIT. No timer — the next enqueue into an idle session starts a fresh
    worker. Each job's exceptions are contained so one failure can't wedge the
    session (the worker clears its slot and moves on)."""
    while True:
        with _generating_lock:
            q = _queues.get(session_id)
            if not q:
                # Nothing left — clear running state and stop. A later enqueue
                # will start a new worker.
                _running_trigger.pop(session_id, None)
                _queues.pop(session_id, None)
                return
            job = q.popleft()
            _running_trigger[session_id] = job.get("trigger", "manual")
        try:
            job["run"]()
        except Exception as e:
            import traceback
            logger.error(f"[intel] Generation job failed for session {session_id[:8]}...: {e}\n{traceback.format_exc()}")
        # loop back to check for the next queued job


def save_intel_to_s3(session_id: str, intel_data: dict, bucket: str) -> str:
    """Save the intel data (JSON with structured cards + full report) to S3."""
    s3 = aws_clients.get_client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    s3_key = f"sessions/{session_id}/workbook-intel.json"
    s3.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=json.dumps(intel_data, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(f"[intel]    → Saved to s3://{bucket}/{s3_key}")
    return s3_key


def load_intel_from_s3(session_id: str, bucket: str) -> dict | None:
    """Load the intel data from S3. Returns None if not found."""
    s3 = aws_clients.get_client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    s3_key = f"sessions/{session_id}/workbook-intel.json"
    try:
        resp = s3.get_object(Bucket=bucket, Key=s3_key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        logger.warning(f"[intel] Failed to load from S3: {e}")
        return None
