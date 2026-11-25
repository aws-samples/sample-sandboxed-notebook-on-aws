"""Wait-for-RUNNING schedule after an explicit ResumeMicrovm.

Shared by the proxy's resume wait loops (POST /resume, terminal _ensure_vm_awake,
_checkpoint_session_vm, set-env _resume_and_wait). The explicit resume and the
wait for RUNNING stay; only how soon RUNNING is noticed changes. A VM is
RUNNING 0.43-0.75 s after ResumeMicrovm, so the old fixed 2 s / 5 s sleeps
before the first check were pure slack (.agents/tasks/resume-wait-history/
report.md, schedule S).

Schedule: sleep BEFORE each state read: 0.3 s before the first read, 0.25 s
while under 3 s of elapsed wait, 1.0 s after that, until the caller's budget
runs out (the last read lands at the budget). After a failed read (an
exception, or get_vm_state's "UNKNOWN" for a throttled/transient error) the
delay doubles from the previous one, capped at 2.0 s (the shape of
cold_launch_poll_delay).

Wake decision (wake_and_wait, sites 14-16): resume when the first read says
SUSPENDED. SUSPENDING settles at SUSPENDED (0.44-1.3 s) and stays there, so a
wait that started on SUSPENDING, or whose resume raised, re-issues the resume
when a later read says SUSPENDED (resume-wait-history report section 4). A
resume of ours that succeeded is never re-issued: right after ResumeMicrovm,
GetMicrovm can still show a stale SUSPENDED.
"""
import asyncio
import logging
import time

_logger = logging.getLogger(__name__)

RESUME_FIRST_CHECK_SEC = 0.3
RESUME_FAST_STEP_SEC = 0.25
RESUME_FAST_WINDOW_SEC = 3.0
RESUME_SLOW_STEP_SEC = 1.0
RESUME_MAX_BACKOFF_SEC = 2.0

# In-loop re-resume: at most this many tries per wait, at least this far apart
# (counted from this call's previous try); never once one of ours succeeded.
RESUME_REISSUE_MAX_TRIES = 3
RESUME_REISSUE_MIN_GAP_SEC = 1.0

# Seams so tests can drive the schedule with a fake clock and fake sleep.
_clock = time.monotonic


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def resume_poll_delay(attempt: int, elapsed_s: float, prev_delay: float = 0.0,
                      failed: bool = False) -> float:
    """Seconds to sleep BEFORE resume-wait read number `attempt` (1-based),
    given `elapsed_s` seconds already spent waiting."""
    if attempt <= 1:
        base = RESUME_FIRST_CHECK_SEC
    elif elapsed_s < RESUME_FAST_WINDOW_SEC:
        base = RESUME_FAST_STEP_SEC
    else:
        base = RESUME_SLOW_STEP_SEC
    if failed:
        return min(RESUME_MAX_BACKOFF_SEC, max(base, prev_delay * 2))
    return base


async def _try_resume(resume, vm_id: str, who: str, log) -> bool:
    """Issue one resume; True when it returned without raising. Never raises."""
    log = log or _logger
    try:
        await resume()
    except Exception as e:
        log.warning(f"{who}: resume of {vm_id} failed: {e}")
        return False
    log.info(f"{who}: resuming suspended VM {vm_id}")
    return True


async def wake_and_wait(first_state, read_state, resume, budget_s: float, *,
                        dead_states=frozenset(), retry_errors: bool = False,
                        vm_id: str = "", who: str = "resume-wait", log=None):
    """Resume when `first_state` is SUSPENDED, then wait_until_running with the
    in-loop re-resume enabled. `resume` is an async no-arg callable issuing
    ResumeMicrovm. Returns what wait_until_running returns."""
    resumed, last_try = False, None
    if first_state == "SUSPENDED":
        last_try = _clock()
        resumed = await _try_resume(resume, vm_id, who, log)
    return await wait_until_running(
        read_state, budget_s, dead_states=dead_states, retry_errors=retry_errors,
        resume=resume, resumed=resumed, last_resume_try=last_try,
        vm_id=vm_id, who=who, log=log)


async def wait_until_running(read_state, budget_s: float, *, dead_states=frozenset(),
                             retry_errors: bool = False, resume=None, resumed: bool = True,
                             last_resume_try=None, vm_id: str = "",
                             who: str = "resume-wait", log=None):
    """Poll `await read_state()` on the schedule above until it returns RUNNING,
    a state in `dead_states`, or `budget_s` seconds have elapsed.

    Returns the last state read (None when no read succeeded). With
    retry_errors=False a read exception propagates to the caller (its existing
    error handling); with True the read is retried after a backoff.

    With `resume` given and `resumed` False (no resume of this call has
    succeeded yet), a read of SUSPENDED issues `await resume()`, at most
    RESUME_REISSUE_MAX_TRIES times and at least RESUME_REISSUE_MIN_GAP_SEC
    after the previous try (`last_resume_try`: clock value of the caller's own
    failed try, if any). A raising resume is logged and the wait goes on."""
    start = _clock()
    attempt, delay, failed, last = 0, 0.0, False, None
    tries = 0
    while True:
        attempt += 1
        delay = resume_poll_delay(attempt, _clock() - start, delay, failed)
        remaining = budget_s - (_clock() - start)
        if remaining <= 1e-9:  # float slack: never a near-zero extra read
            return last
        await _sleep(min(delay, remaining))
        try:
            st = await read_state()
        except Exception:
            if not retry_errors:
                raise
            failed = True
            continue
        last = st
        failed = st == "UNKNOWN"
        if st == "RUNNING" or st in dead_states:
            return st
        if (st == "SUSPENDED" and resume is not None and not resumed
                and tries < RESUME_REISSUE_MAX_TRIES
                and (last_resume_try is None
                     or _clock() - last_resume_try >= RESUME_REISSUE_MIN_GAP_SEC - 1e-9)):
            tries += 1
            last_resume_try = _clock()
            resumed = await _try_resume(resume, vm_id, who, log)
