"""
Save deadline — cooperative cancellation for a time-bounded checkpoint save.

Part of: app.platform.checkpoint (infrastructure layer)

The /suspend hook gives CheckpointManager.save() a SaveDeadline. The save
thread calls check(deadline, phase) between variables, between spool-write
slices, before every object or multipart part upload and before each manifest
write. Once the budget is spent (or the hook called cancel()), check raises
SaveCancelled; the save then aborts its multipart uploads, deletes its spool
files and never writes the manifest it was building, so the previous
checkpoint stays the committed one. With deadline=None (the /terminate and
/checkpoint-save paths) check is a no-op.
"""

import threading
import time


class SaveCancelled(Exception):
    """The save stopped at a cancellation point. `phase` names the point
    ("serialize", "upload", "manifest", "files", "tail", "metadata", "lock")."""

    def __init__(self, reason: str, phase: str):
        super().__init__(f"{reason} (during {phase})")
        self.reason = reason
        self.phase = phase


class SaveDeadline:
    """A time budget that starts when the object is created."""

    def __init__(self, budget_s: float, now=time.monotonic):
        self._now = now
        self.budget_s = float(budget_s)
        self._end = now() + self.budget_s
        self._cancelled = threading.Event()
        self._cancel_reason = ""

    def remaining(self) -> float:
        return self._end - self._now()

    def expired(self) -> bool:
        return self._cancelled.is_set() or self.remaining() <= 0

    def cancel(self, reason: str = "hook timed out") -> None:
        self._cancel_reason = reason
        self._cancelled.set()

    def check(self, phase: str) -> None:
        if self._cancelled.is_set():
            raise SaveCancelled(self._cancel_reason or "cancelled", phase)
        if self.remaining() <= 0:
            raise SaveCancelled(f"suspend save budget of {self.budget_s:g} s exceeded", phase)


def check(deadline, phase: str) -> None:
    """deadline.check(phase); a no-op for deadline=None."""
    if deadline is not None:
        deadline.check(phase)
