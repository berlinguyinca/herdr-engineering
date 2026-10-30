"""The one choke point for blocked calendars (list, read, free/busy, create all go through it).

Blocking is by the calendar's real id; the alias ``primary`` is resolved to its real id first, so blocking either spelling
blocks both. Ids are trimmed and compared case-sensitively. A refusal carries one fixed message and never says whether the
calendar exists.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from .google_workspace import PRIMARY, GoogleError


class CalendarPolicy:
    def __init__(self, blocked: Iterable[str], resolve_primary: Callable[[], str]):
        self.blocked = {b.strip() for b in blocked if isinstance(b, str) and b.strip()}
        self._resolve, self._primary = resolve_primary, None
        self._lock = threading.Lock()

    def _real_primary(self) -> str:
        with self._lock:
            if self._primary is None:
                self._primary = str(self._resolve()).strip()     # a failure propagates: never fail open
            return self._primary

    def allows(self, calendar_id: str) -> bool:
        if not self.blocked:
            return True
        cid = str(calendar_id).strip()
        if cid in self.blocked:
            return False
        if cid == PRIMARY or PRIMARY in self.blocked:       # the alias and the real id are one calendar
            real = self._real_primary()
            return not (real in self.blocked or (cid == real and PRIMARY in self.blocked))
        return True

    def filter(self, calendars: list[dict]) -> list[dict]:
        out = []
        for cal in calendars:
            cid = cal.get("id") if isinstance(cal, dict) else None
            if isinstance(cid, str) and self.allows(cid) and not (cal.get("primary") is True and not self.allows(PRIMARY)):
                out.append(cal)
        return out

    def require(self, calendar_id: str) -> None:
        if not self.allows(calendar_id):
            raise GoogleError("calendar_blocked")
