"""Attention, notifications and review actions (spec 0160).

Normalizes actionable conditions into attention items with lifecycle
(open/acknowledged/resolved/superseded) and applies policy-based dedupe/
coalescing, quiet hours, severity thresholds and routing. Actions execute in
their owner systems; the attention center records observable results only.
No secrets/log dumps in notification payloads.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_SEVERITY_RANK = {"debug": 0, "info": 1, "notice": 2, "warning": 3, "error": 4}
_LIFECYCLE = {"open", "acknowledged", "resolved", "superseded"}


@dataclass
class AttentionItem:
    id: str
    kind: str
    severity: str
    title: str
    summary: str
    lifecycle: str = "open"
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    dedupe_key: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    action_capability: str | None = None
    action_authorized: bool = False
    action_result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class NotificationPolicy:
    minimum_severity: str = "notice"
    dedupe_window_seconds: int = 300
    quiet_hours: dict[str, int] | None = None   # e.g. {"start": 22, "end": 7}
    enabled: bool = True


class AttentionCenter:
    """Coalescing attention registry with policy-based notifications."""

    def __init__(self, policy: NotificationPolicy | None = None,
                 notifier: Callable[[AttentionItem], None] | None = None) -> None:
        self.policy = policy or NotificationPolicy()
        self.notifier = notifier
        self._items: dict[str, AttentionItem] = {}
        self._lock = threading.RLock()
        self._notified: dict[str, float] = {}

    def raise_item(self, *, kind: str, severity: str, title: str, summary: str,
                   dedupe_key: str | None = None, evidence: dict | None = None,
                   links: list[str] | None = None,
                   action_capability: str | None = None) -> AttentionItem:
        """Raise (or coalesce into) an attention item."""
        with self._lock:
            if dedupe_key:
                for item in self._items.values():
                    if (item.dedupe_key == dedupe_key
                            and item.lifecycle in ("open", "acknowledged")):
                        item.links = list(dict.fromkeys(item.links + (links or [])))
                        return item
            item = AttentionItem(
                id=f"att_{uuid.uuid4().hex[:16]}", kind=kind, severity=severity,
                title=title, summary=summary, dedupe_key=dedupe_key,
                evidence=evidence or {}, links=links or [],
                action_capability=action_capability)
            self._items[item.id] = item
        self._maybe_notify(item)
        return item

    def _maybe_notify(self, item: AttentionItem) -> None:
        if not self.policy.enabled:
            return
        if _SEVERITY_RANK[item.severity] < _SEVERITY_RANK[self.policy.minimum_severity]:
            return
        if self._in_quiet_hours(time.time()):
            return
        if item.dedupe_key:
            last = self._notified.get(item.dedupe_key, 0)
            if time.time() - last < self.policy.dedupe_window_seconds:
                return  # coalesced: no repeated notification
            self._notified[item.dedupe_key] = time.time()
        if self.notifier:
            self.notifier(item)

    def _in_quiet_hours(self, ts: float) -> bool:
        qh = self.policy.quiet_hours
        if not qh:
            return False
        hour = time.localtime(ts).tm_hour
        start, end = qh.get("start", 0), qh.get("end", 0)
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def acknowledge(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            item.lifecycle = "acknowledged"
            return True

    def resolve(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            item.lifecycle = "resolved"
            item.resolved_at = time.time()
            return True

    def supersede(self, item_id: str) -> bool:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            item.lifecycle = "superseded"
            return True

    def execute_action(self, item_id: str, executor: Callable[[AttentionItem], str]) -> bool:
        """Execute an action in the owning system; record the observable result."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return False
            if not item.action_authorized:
                return False
        try:
            result = executor(item)
        except Exception as exc:
            result = f"failed: {exc}"
        with self._lock:
            item.action_result = result
            if result and not result.startswith("failed"):
                item.lifecycle = "resolved"
                item.resolved_at = time.time()
        return True

    def open_items(self, min_severity: str | None = None) -> list[AttentionItem]:
        with self._lock:
            items = [i for i in self._items.values()
                     if i.lifecycle in ("open", "acknowledged")]
        if min_severity:
            items = [i for i in items
                     if _SEVERITY_RANK[i.severity] >= _SEVERITY_RANK[min_severity]]
        items.sort(key=lambda i: (_SEVERITY_RANK[i.severity], -i.created_at), reverse=True)
        return items
