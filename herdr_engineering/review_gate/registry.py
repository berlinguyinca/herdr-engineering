"""Which agent wrote a PR: maps a PR head branch to a live control-plane session.

Rules (sessions record no repo, so these are heuristics):
- candidates are sessions whose ``branch`` equals the PR head ref; when a
  session has a ``cwd``, its basename must equal the repo name of the PrKey;
- several candidates -> the most recently active one (``updated_at``);
- kind: ``mission_source == "pi"`` -> ``"pi"``; otherwise a session bound to a
  herdr pane (``pane_id`` set, the ``pane:<host>:<pane>`` link collab agents
  use) -> ``"collab"``; no matching session -> ``"unknown"``. A matching
  session with neither marker is also ``"unknown"`` but keeps its identifiers;
- ``pin`` records the first resolution on stream ``pr_gate_author`` so it
  survives the session's death; a pinned author wins on later lookups.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict

from herdr_engineering.control_plane.repo import ControlPlaneRepo

from .models import Author, PrKey

ENTITY = "pr_gate_author"


def _payload(event: dict) -> dict:
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def _basename(path: str) -> str:
    return path.rstrip("/").rpartition("/")[2]


class AuthorRegistry:
    def __init__(self, repo: ControlPlaneRepo):
        self._repo = repo
        self._lock = threading.RLock()

    def _pinned(self, key: PrKey) -> Author | None:
        pinned = None
        for event in self._repo.stream(ENTITY, str(key)):
            if event["event_type"] == "author_pinned":
                pinned = Author(**_payload(event))
        return pinned

    def pin(self, key: PrKey, author: Author) -> None:
        with self._lock:
            if self._pinned(key) == author:
                return
            self._repo.record_event(ENTITY, str(key), "author_pinned", asdict(author))

    def author_of(self, key: PrKey, head_ref: str) -> Author:
        with self._lock:
            pinned = self._pinned(key)
            if pinned is not None:
                return pinned
            name = key.repo.rpartition("/")[2]
            matches = [
                s
                for s in self._repo.list_sessions()
                if s.get("branch") == head_ref and (not s.get("cwd") or _basename(s["cwd"]) == name)
            ]
            if not matches:
                return Author(kind="unknown")
            s = max(matches, key=lambda x: x.get("updated_at") or "")
            if s.get("mission_source") == "pi":
                kind = "pi"
            elif s.get("pane_id"):
                kind = "collab"
            else:
                kind = "unknown"
            return Author(
                kind=kind,
                model=s.get("model") or None,
                host_id=s.get("host_id"),
                pane_id=s.get("pane_id"),
                session_id=s.get("session_id"),
                mission_id=s.get("mission_id"),
            )
