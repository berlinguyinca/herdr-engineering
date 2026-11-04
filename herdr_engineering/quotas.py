"""Fair admission queues and hard resource envelopes."""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class ResourceEnvelope:
    requested_cpu: float
    minimum_cpu: float
    maximum_cpu: float
    requested_memory_gib: float
    maximum_memory_gib: float
    expected_seconds: float
    workload_class: str


@dataclass
class QueueEntry:
    request_id: str
    owner_id: str
    project_id: str | None
    priority: str
    created_at: float
    envelope: ResourceEnvelope


class FairQueue:
    def __init__(self, *, owner_limits=None, project_limits=None, global_limit=1000):
        self.owner_limits = dict(owner_limits or {})
        self.project_limits = dict(project_limits or {})
        self.global_limit = int(global_limit)
        self.entries: list[QueueEntry] = []

    def add(self, entry: QueueEntry) -> None:
        if len(self.entries) >= self.global_limit:
            raise RuntimeError("global queue cap reached")
        owner_limit = self.owner_limits.get(entry.owner_id, self.owner_limits.get("*", self.global_limit))
        if sum(e.owner_id == entry.owner_id for e in self.entries) >= owner_limit:
            raise RuntimeError("owner queue cap reached")
        project_limit = self.project_limits.get(entry.project_id, self.project_limits.get("*", self.global_limit))
        if entry.project_id and sum(e.project_id == entry.project_id for e in self.entries) >= project_limit:
            raise RuntimeError("project queue cap reached")
        self.entries.append(entry)

    def ordered(self, now=None) -> list[QueueEntry]:
        now = monotonic() if now is None else now
        rank = {"owner-interactive": 0, "normal": 1, "background": 2}
        return sorted(self.entries, key=lambda e: (rank.get(e.priority, 1), -(now - e.created_at), e.request_id))

