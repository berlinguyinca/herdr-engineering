"""Session journal and timeline projection (spec 0090).

Ingests ActivityEvent records, maintains a newest-first timeline per session,
and derives a Started / Completed / Current / Next summary projection.
Idempotent (stable event id), out-of-order-safe, persists across restarts.
No hidden chain-of-thought is ever persisted.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ActivityEvent

_SESSION_FIELD = "herdr_session_id"


@dataclass
class SessionSummary:
    started: str | None = None
    completed: list[str] = field(default_factory=list)
    current: str | None = None
    current_status: str | None = None
    next: str | None = None

    def to_dict(self) -> dict:
        return {"started": self.started, "completed": self.completed,
                "current": self.current, "current_status": self.current_status,
                "next": self.next}


class SessionJournal:
    """Persistent, idempotent, out-of-order-safe activity journal."""

    def __init__(self, store_path: Path | None = None) -> None:
        self._path = store_path or Path(
            os.environ.get("HERDR_ENGINEERING_STATE",
                           str(Path.home() / ".local/share/herdr-engineering/state"))) / "journal.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._events: dict[str, ActivityEvent] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                ev = ActivityEvent(**json.loads(line))
                self._events[ev.id] = ev
            except Exception:
                continue  # skip corrupt tail entries without failing the journal

    def _append(self, ev: ActivityEvent) -> None:
        with self._lock:
            try:
                import dataclasses
                with open(self._path, "a") as f:
                    # persist raw dataclass fields (asdict) so the event can
                    # be reconstructed on load; to_dict() adds derived views
                    f.write(json.dumps(dataclasses.asdict(ev), sort_keys=True) + "\n")
            except OSError:
                pass  # persistence is best-effort; in-memory still correct

    def ingest(self, ev: ActivityEvent) -> bool:
        """Idempotent ingest. Returns True if newly recorded (or updated)."""
        with self._lock:
            if ev.id in self._events:
                return False
            self._events[ev.id] = ev
        self._append(ev)
        return True

    def timeline(self, session_id: str) -> list[ActivityEvent]:
        """Newest-first timeline for a session."""
        with self._lock:
            events = [e for e in self._events.values()
                      if getattr(e, _SESSION_FIELD) == session_id]
        events.sort(key=lambda e: e.occurred_at, reverse=True)
        return events

    def all_recent(self, limit: int = 200) -> list[ActivityEvent]:
        with self._lock:
            events = list(self._events.values())
        events.sort(key=lambda e: e.occurred_at, reverse=True)
        return events[:limit]

    def search(self, *, repository: str | None = None, worktree_id: str | None = None,
               branch: str | None = None, machine_id: str | None = None,
               session_id: str | None = None, text: str | None = None,
               limit: int = 100) -> list[ActivityEvent]:
        with self._lock:
            events = list(self._events.values())
        def match(e: ActivityEvent) -> bool:
            if repository and e.repository != repository:
                return False
            if worktree_id and e.worktree_id != worktree_id:
                return False
            if branch and e.branch != branch:
                return False
            if machine_id and e.machine_id != machine_id:
                return False
            if session_id and getattr(e, _SESSION_FIELD) != session_id:
                return False
            if text and text.lower() not in e.summary.lower():
                return False
            return True
        matched = [e for e in events if match(e)]
        matched.sort(key=lambda e: e.occurred_at, reverse=True)
        return matched[:limit]

    def summary(self, session_id: str) -> SessionSummary:
        """Derive Started/Completed/Current/Next from explicit structured facts."""
        timeline = self.timeline(session_id)
        started = None
        completed: list[str] = []
        current = None
        current_status = None
        next_step = None
        for ev in timeline:
            if started is None and ev.type == "session.started":
                started = ev.summary
            if ev.type == "task.completed":
                completed.append(ev.summary)
            if ev.type == "task.started":
                current = ev.summary
                current_status = ev.status
            if ev.type == "note" and ev.metadata.get("kind") == "next":
                next_step = ev.summary
        completed = completed[:3]  # bounded
        return SessionSummary(started=started, completed=completed,
                              current=current, current_status=current_status,
                              next=next_step)

    def session_ids(self) -> list[str]:
        with self._lock:
            return sorted({getattr(e, _SESSION_FIELD) for e in self._events.values()
                           if getattr(e, _SESSION_FIELD)})

    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def drop(self) -> None:
        with self._lock:
            self._events.clear()
        if self._path.exists():
            self._path.unlink()
