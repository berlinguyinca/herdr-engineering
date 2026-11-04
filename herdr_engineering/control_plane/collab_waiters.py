"""A per-key counter of blocking long-poll waits.

Every waiting call holds a server thread, so one key (an agent id, an actor) may hold at most ``MAX_WAITERS`` of them at
once. ``collab_mcp`` (inbox and helper waits) and ``collab_artifacts`` (commit results) share this counter; it imports
neither of them.
"""
from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator

MAX_WAITERS = 2        # per key: each waiting call holds a server thread

_waiters: dict[str, int] = {}
_lock = threading.Lock()


@contextlib.contextmanager
def waiter_slot(key: str) -> Iterator[bool]:
    """Yield True holding one of ``key``'s wait slots (released on exit, whatever happens), or False when the key
    already holds ``MAX_WAITERS`` of them (nothing is held then)."""
    with _lock:
        held = _waiters.get(key, 0)
        acquired = held < MAX_WAITERS
        if acquired:
            _waiters[key] = held + 1
    try:
        yield acquired
    finally:
        if acquired:
            with _lock:
                left = _waiters.get(key, 1) - 1
                if left > 0:
                    _waiters[key] = left
                else:
                    _waiters.pop(key, None)


__all__ = ["MAX_WAITERS", "waiter_slot"]
