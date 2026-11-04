"""A small time-boxed cache where concurrent callers for one key wait for ONE computation.

The CI history report costs several seconds to build and every open CI page asks for it; built per request, the requests queue
behind each other, clients give up (broken pipes) and the page shows "Failed to fetch". Here the first caller builds it, the
others wait for that result, and later callers within ``ttl`` seconds get it without any work. A failed build is never cached.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class SingleFlightCache:
    def __init__(self, ttl: float, *, clock: Callable[[], float] = time.monotonic, max_entries: int = 32) -> None:
        self.ttl, self.clock, self.max_entries = ttl, clock, max_entries
        self._lock = threading.Lock()
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._building: dict[Any, threading.Lock] = {}

    def get(self, key: Any, build: Callable[[], Any]) -> Any:
        if self.ttl <= 0:
            return build()
        value = self._fresh(key)
        if value is not _MISS:
            return value
        with self._lock:
            gate = self._building.setdefault(key, threading.Lock())
        with gate:                                   # the first caller builds; the rest wait here, then read its result
            value = self._fresh(key)
            if value is not _MISS:
                return value
            try:
                value = build()
            finally:
                with self._lock:
                    if self._building.get(key) is gate:
                        del self._building[key]
            with self._lock:
                if len(self._entries) >= self.max_entries:
                    self._entries.pop(min(self._entries, key=lambda k: self._entries[k][0]))
                self._entries[key] = (self.clock() + self.ttl, value)
            return value

    def _fresh(self, key: Any) -> Any:
        with self._lock:
            entry = self._entries.get(key)
        return entry[1] if entry is not None and entry[0] > self.clock() else _MISS


_MISS = object()
