"""Run guards: timeout and interrupt for ONE cell, Python or SQL.

_RunGuard owns the mechanism shared by both cell kinds: the lock, the timeout
timer, the fire-once rule, `fired` / `reason` / error_message(), native-cancel
registration (add_cancel / remove_cancel), the idempotent finish() with its drain,
and the thread-local lookup (current_guard()). _PythonRunGuard and _SqlRunGuard
differ only in how they stop the work (_trigger).

Lives outside executor and sql_engine so helper modules (Athena runner, DynamoDB
reader) can register a native cancel or check `fired` without importing either.
sql_engine re-exports _RUN, _SqlRunGuard and _current_guard under the old names.
"""
import ctypes
import logging
import threading

from .run_limits import TIMEOUT_ERROR, INTERRUPT_ERROR, execution_timeout_seconds

logger = logging.getLogger(__name__)

# The guard of the cell running on THIS worker thread (None outside a cell).
_RUN = threading.local()


def _current_guard():
    return getattr(_RUN, "guard", None)


current_guard = _current_guard


def _set_async_exc(tid: int, exc) -> int:
    """PyThreadState_SetAsyncExc; exc=None clears a pending exception."""
    return ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.c_void_p(0) if exc is None else ctypes.py_object(exc))


class _RunGuard:
    """Timeout and interrupt for ONE cell, run on the calling worker thread.

    Same budget (EXECUTION_TIMEOUT_SECONDS) and the same error texts for every
    cell kind. While the cell runs, `executor.<_slot>` points here, so
    executor.interrupt() (POST /interrupt) reaches it.

    A guard fires at most once (timer or interrupt, whichever comes first), and
    never after finish(). finish() marks the run done under the same lock as the
    firing, then drains any still-pending async exception, so nothing can reach
    the code that runs next on this (pooled) thread.

    Subclasses implement _trigger(): how the work is stopped."""
    _slot = None  # executor attribute that points at the running guard

    def __init__(self, executor):
        self._executor = executor
        self._tid = threading.get_ident()
        self._lock = threading.Lock()
        self._cancels: list = []
        self._done = False
        self.reason = None  # "timeout" | "interrupt" once fired
        self.timeout_s = execution_timeout_seconds()
        self._timer = threading.Timer(self.timeout_s, self._fire, args=("timeout",))
        self._timer.daemon = True

    @property
    def fired(self) -> bool:
        return self.reason is not None

    def error_message(self) -> str:
        if self.reason == "timeout":
            return TIMEOUT_ERROR.format(seconds=self.timeout_s)
        return INTERRUPT_ERROR

    def start(self):
        _RUN.guard = self
        try:
            setattr(self._executor, self._slot, self)
        except AttributeError:
            pass
        self._timer.start()

    def add_cancel(self, fn):
        """Register a native cancel for the current engine phase. Ignored once the
        guard fired (the stop already issued ends the phase)."""
        with self._lock:
            if not self._done and self.reason is None:
                self._cancels.append(fn)

    def remove_cancel(self, fn):
        with self._lock:
            if fn in self._cancels:
                self._cancels.remove(fn)

    def interrupt(self) -> bool:
        """POST /interrupt. True when this call stopped the running cell."""
        return self._fire("interrupt")

    def _trigger(self, cancels: list) -> bool:
        """Stop the work. Called once, under the lock, with the registered cancels
        (they are called by _fire after the lock is released). Returns True when
        the guard now counts as fired."""
        raise NotImplementedError

    def _inject(self) -> bool:
        """Queue ONE KeyboardInterrupt on the worker thread. True when it queued."""
        res = _set_async_exc(self._tid, KeyboardInterrupt)
        if res > 1:  # more than one thread state matched: undo
            _set_async_exc(self._tid, None)
            return False
        return res == 1

    def _fire(self, reason: str) -> bool:
        with self._lock:
            if self._done or self.reason is not None:
                return False
            cancels = list(self._cancels)
            if not self._trigger(cancels):
                return False
            self.reason = reason
        for fn in cancels:
            try:
                fn()
            except Exception as e:
                logger.warning(f"Run guard cancel failed: {e}")
        return True

    def finish(self):
        """Idempotent; must run on the worker thread that called start(). After it
        returns no KeyboardInterrupt is pending or can be injected for this run."""
        with self._lock:
            self._done = True
            self._cancels.clear()
        self._timer.cancel()
        if getattr(_RUN, "guard", None) is self:
            _RUN.guard = None
        if getattr(self._executor, self._slot, None) is self:
            try:
                setattr(self._executor, self._slot, None)
            except AttributeError:
                pass
        # Cancel a pending async KeyboardInterrupt (NULL clears it).
        _set_async_exc(self._tid, None)


class _PythonRunGuard(_RunGuard):
    """Guard of one Python cell. Stops the cell by injecting KeyboardInterrupt into
    the worker thread (it lands when the thread next runs bytecode) and ALSO calls
    any native cancel a helper registered (read_athena -> stop_query_execution), so
    the remote work stops too. The cell's reason is only recorded when the
    injection actually happened."""
    _slot = "_py_run"

    def _trigger(self, cancels):
        return self._inject()


class _SqlRunGuard(_RunGuard):
    """Guard of one SQL cell. Two ways to stop:
      - an engine phase registered a native cancel (DuckDB con.interrupt(), Athena
        stop_query_execution): the cancels are called and the engine raises on its
        own. No async exception, so the engine's cleanup code runs normally.
      - otherwise (Aurora Data API, which has no cancel call, DynamoDB, result
        shaping): KeyboardInterrupt is injected, like a Python cell. It lands when
        the thread next runs bytecode, so a blocking C call finishes first."""
    _slot = "_sql_run"

    def _trigger(self, cancels):
        if not cancels:
            self._inject()
        return True
