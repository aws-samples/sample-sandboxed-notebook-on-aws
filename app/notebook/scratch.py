"""
Agent exploration scratch scopes.

Part of: app.notebook (application layer)

An AI agent's execute_code probe (origin="agent") runs against an OVERLAY
`{**user_namespace, **scratch}`: it can read every user variable, but names it
creates or rebinds land in its scope's scratch dict, never in the user namespace.
A scope (e.g. "chat:<session>", "fix:<id>", "intel:<id>") lets probes of the same
turn chain (probe 1 computes `a`, probe 2 reads it); the proxy/agent clears the
scope at turn end. TTL + an LRU cap are the backstop for a crashed turn.

Pure stdlib. NOT thread-safe on its own: the executor owns one instance and
touches it only while holding its _exec_lock. Scratch is never checkpointed, never
part of the runtime provenance and never listed by /variables.
"""

import re
import time
from collections import OrderedDict

# Allowed scope keys: short, printable, no spaces.
SCOPE_RE = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")

MAX_SCOPES = 8
TTL_S = 1800  # 30 min idle


def valid_scope(scope) -> bool:
    """True when `scope` is a str matching SCOPE_RE."""
    return type(scope) is str and SCOPE_RE.match(scope) is not None


class ScratchScopes:
    """scope → scratch dict, at most `max_scopes` (LRU eviction) and dropped after
    `ttl_s` seconds without use. `now` is injectable for tests."""

    def __init__(self, max_scopes: int = MAX_SCOPES, ttl_s: float = TTL_S, now=time.monotonic):
        self._max = max(1, int(max_scopes))
        self._ttl = float(ttl_s)
        self._now = now
        # scope → (scratch dict, last-used timestamp); order = LRU (oldest first).
        self._scopes: "OrderedDict[str, list]" = OrderedDict()

    def sweep(self) -> None:
        """Drop scopes idle for longer than the TTL."""
        cutoff = self._now() - self._ttl
        for scope in [s for s, (_, ts) in self._scopes.items() if ts < cutoff]:
            del self._scopes[scope]

    def get(self, scope: str) -> dict:
        """The scope's scratch dict (created on demand); marks it most recently used."""
        self.sweep()
        entry = self._scopes.get(scope)
        if entry is None:
            entry = [{}, self._now()]
            self._scopes[scope] = entry
            while len(self._scopes) > self._max:
                self._scopes.popitem(last=False)
        else:
            entry[1] = self._now()
            self._scopes.move_to_end(scope)
        return entry[0]

    def peek(self, scope: str):
        """The scope's scratch dict, or None. Does not create or touch it."""
        self.sweep()
        entry = self._scopes.get(scope)
        return entry[0] if entry is not None else None

    def clear(self, scope: str) -> bool:
        """Drop one scope. True when it existed."""
        return self._scopes.pop(scope, None) is not None

    def clear_all(self) -> None:
        self._scopes.clear()

    def __len__(self) -> int:
        return len(self._scopes)
