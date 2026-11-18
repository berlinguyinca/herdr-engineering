"""Single admission and placement contract for Herdr-controlled agents."""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .host_policy import HostSnapshot, evaluate_eligibility


@dataclass(frozen=True)
class PlacementRequest:
    request_id: str
    owner_id: str
    agent_family: str
    placement_mode: str
    resource_profile: dict
    project_id: str | None = None
    workspace_id: str | None = None
    parent_agent_id: str | None = None
    explicit_host_uid: str | None = None
    preferred_host_uid: str | None = None
    required_tags: frozenset[str] = frozenset()
    priority: str = "normal"
    idempotency_key: str | None = None
    mission_id: str | None = None
    spawn_depth: int = 0

    @property
    def cpu(self) -> float:
        return max(0.0, float(self.resource_profile.get("cpu_cores", 0)))

    @property
    def memory(self) -> float:
        return max(0.0, float(self.resource_profile.get("memory_gib", 0)))

    @property
    def disk(self) -> float:
        return max(0.0, float(self.resource_profile.get("disk_gib", 0)))


@dataclass
class HostCapacity:
    total_cpu: float
    used_cpu: float = 0
    reserved_cpu: float = 0
    total_memory_gib: float = 0
    used_memory_gib: float = 0
    reserved_memory_gib: float = 0
    total_disk_gib: float = 0
    used_disk_gib: float = 0
    reserved_disk_gib: float = 0
    reason_codes: tuple[str, ...] = ()

    def fits(self, request: PlacementRequest) -> bool:
        return (
            self.used_cpu + self.reserved_cpu + request.cpu <= self.total_cpu
            and self.used_memory_gib + self.reserved_memory_gib + request.memory <= self.total_memory_gib
            and self.used_disk_gib + self.reserved_disk_gib + request.disk <= self.total_disk_gib
        )


@dataclass(frozen=True)
class PlacementDecision:
    request_id: str
    state: str
    selected_host_uid: str | None
    explanations: dict[str, dict]
    policy_generation: int
    created_at: datetime


@dataclass
class Reservation:
    reservation_id: str
    request_id: str
    host_uid: str | None
    requested_cpu: float
    requested_memory: float
    requested_disk: float
    policy_revision: int
    expires_at: datetime
    state: str
    decision: PlacementDecision
    lease_id: str | None = None
    lease_token: str | None = None
    request_snapshot: dict | None = None
    queued_at: str | None = None

    def to_dict(self) -> dict:
        data = {
            "reservation_id": self.reservation_id,
            "request_id": self.request_id,
            "host_uid": self.host_uid,
            "requested_cpu": self.requested_cpu,
            "requested_memory": self.requested_memory,
            "requested_disk": self.requested_disk,
            "policy_revision": self.policy_revision,
            "expires_at": self.expires_at.isoformat(),
            "state": self.state,
            "lease_id": self.lease_id,
            "lease_token": self.lease_token,
            "request_snapshot": self.request_snapshot,
            "queued_at": self.queued_at,
            "decision": {
                "request_id": self.decision.request_id,
                "state": self.decision.state,
                "selected_host_uid": self.decision.selected_host_uid,
                "explanations": self.decision.explanations,
                "policy_generation": self.decision.policy_generation,
                "created_at": self.decision.created_at.isoformat(),
            },
        }
        return data


class Scheduler:
    def __init__(self, hosts: Callable[[], Iterable[tuple[HostSnapshot, HostCapacity]]], *, reservation_ttl=90.0,
                 owner_concurrency_limit=20, project_concurrency_limit=50,
                 mission_concurrency_limit=20, owner_cpu_limit=32, owner_memory_limit=64,
                 maximum_spawn_depth=3, parent_concurrency_limit=8, parent_burst_limit=20):
        self._hosts = hosts
        self._ttl = max(1.0, float(reservation_ttl))
        self._lock = threading.RLock()
        self._reservations: dict[str, Reservation] = {}
        self._by_request: dict[str, str] = {}
        self._rotation = 0
        self.owner_concurrency_limit = max(1, int(owner_concurrency_limit))
        self.project_concurrency_limit = max(1, int(project_concurrency_limit))
        self.mission_concurrency_limit = max(1, int(mission_concurrency_limit))
        self.owner_cpu_limit = max(0, float(owner_cpu_limit))
        self.owner_memory_limit = max(0, float(owner_memory_limit))
        self.maximum_spawn_depth = max(0, int(maximum_spawn_depth))
        self.parent_concurrency_limit = max(1, int(parent_concurrency_limit))
        self.parent_burst_limit = max(1, int(parent_burst_limit))

    def contextualize(self, request):
        """Derive ancestry from admitted requests, never a caller's depth hint."""
        parent = request.parent_agent_id
        depth, visited = 0, {request.request_id}
        inherited = {}
        while parent is not None:
            if parent in visited:
                raise ValueError("cyclic scheduler ancestry")
            visited.add(parent)
            reservation_id = self._by_request.get(parent)
            ancestor = self._reservations.get(reservation_id)
            if ancestor is None or ancestor.request_snapshot is None:
                raise ValueError("authoritative parent context is unavailable")
            context = ancestor.request_snapshot
            if context.get("owner_id") != request.owner_id:
                raise PermissionError("parent owner does not authorize this child")
            if depth == 0:
                inherited = context
            parent = context.get("parent_agent_id")
            depth += 1
        for field in ("mission_id", "project_id"):
            if inherited.get(field) is not None and getattr(request, field) not in (None, inherited[field]):
                raise PermissionError(f"child cannot change inherited {field}")
        return replace(request, spawn_depth=depth,
                       mission_id=inherited.get("mission_id") or request.mission_id,
                       project_id=inherited.get("project_id") or request.project_id)

    def _snapshot(self):
        return list(self._hosts())

    def _reservation_demand(self, host_uid: str) -> tuple[float, float, float]:
        active = [r for r in self._reservations.values()
                  if r.host_uid == host_uid and r.state in {"reserved", "committed"}]
        return (sum(r.requested_cpu for r in active),
                sum(r.requested_memory for r in active),
                sum(r.requested_disk for r in active))

    def _capacity(self, snapshot: HostSnapshot, capacity: HostCapacity) -> HostCapacity:
        cpu, memory, disk = self._reservation_demand(snapshot.host_uid)
        return HostCapacity(
            total_cpu=capacity.total_cpu, used_cpu=capacity.used_cpu,
            reserved_cpu=capacity.reserved_cpu + cpu,
            total_memory_gib=capacity.total_memory_gib, used_memory_gib=capacity.used_memory_gib,
            reserved_memory_gib=capacity.reserved_memory_gib + memory,
            total_disk_gib=capacity.total_disk_gib, used_disk_gib=capacity.used_disk_gib,
            reserved_disk_gib=capacity.reserved_disk_gib + disk,
            reason_codes=capacity.reason_codes,
        )

    def _score(self, request, snapshot, capacity):
        cpu_headroom = max(0.0, (capacity.total_cpu - capacity.used_cpu - capacity.reserved_cpu - request.cpu) / max(capacity.total_cpu, 1))
        mem_headroom = max(0.0, (capacity.total_memory_gib - capacity.used_memory_gib - capacity.reserved_memory_gib - request.memory) / max(capacity.total_memory_gib, 1))
        score = 0.55 * cpu_headroom + 0.35 * mem_headroom
        if request.preferred_host_uid == snapshot.host_uid:
            score += 0.08
        if request.priority == "owner-interactive":
            score += 0.02
        # Stable rotation only breaks exact ties, making explanations reproducible.
        score += ((int(hashlib.sha256(f"{request.request_id}:{snapshot.host_uid}".encode()).hexdigest()[:8], 16) % 1000) / 1_000_000)
        return round(score, 6)

    def plan(self, request: PlacementRequest, *, explicit_host_uid: str | None = None) -> PlacementDecision:
        request = self.contextualize(request)
        now = datetime.now(UTC)
        explanations: dict[str, dict] = {}
        candidates: list[tuple[float, HostSnapshot, HostCapacity]] = []
        explicit = explicit_host_uid or request.explicit_host_uid
        snapshots = [(snapshot, self._capacity(snapshot, capacity))
                     for snapshot, capacity in self._snapshot()]
        workspace_busy = request.workspace_id is not None and any(
            reservation.request_id != request.request_id
            and reservation.state in {"reserved", "committed"}
            and (reservation.request_snapshot or {}).get("workspace_id") == request.workspace_id
            for reservation in self._reservations.values())
        active = [reservation for reservation in self._reservations.values()
                  if reservation.request_id != request.request_id and reservation.state in {"reserved", "committed"}]
        owner_full = sum((reservation.request_snapshot or {}).get("owner_id") == request.owner_id
                         for reservation in active) >= self.owner_concurrency_limit
        project_full = request.project_id is not None and sum(
            (reservation.request_snapshot or {}).get("project_id") == request.project_id
            for reservation in active) >= self.project_concurrency_limit
        mission_full = request.mission_id is not None and sum(
            (reservation.request_snapshot or {}).get("mission_id") == request.mission_id
            for reservation in active) >= self.mission_concurrency_limit
        owned = [reservation for reservation in active
                 if (reservation.request_snapshot or {}).get("owner_id") == request.owner_id]
        owner_resources_full = (sum(reservation.requested_cpu for reservation in owned) + request.cpu > self.owner_cpu_limit
                                or sum(reservation.requested_memory for reservation in owned) + request.memory > self.owner_memory_limit)
        siblings = [reservation for reservation in self._reservations.values()
                    if request.parent_agent_id is not None and reservation.request_id != request.request_id
                    and (reservation.request_snapshot or {}).get("parent_agent_id") == request.parent_agent_id]
        parent_full = sum(reservation.state in {"reserved", "committed"} for reservation in siblings) >= self.parent_concurrency_limit
        recent_children = 0
        for sibling in siblings:
            try:
                created = datetime.fromisoformat(sibling.queued_at)
                recent_children += 0 <= (now - created).total_seconds() < 600
            except (ValueError, TypeError):
                recent_children += 1
        for snapshot, capacity in snapshots:
            eligibility = evaluate_eligibility(
                snapshot, placement_mode=request.placement_mode,
                explicit_host_uid=explicit, parent_host_uid=None,
                required_tags=request.required_tags, now=now,
            )
            reasons = list(eligibility.reason_codes)
            if owner_full:
                reasons.append("USER_QUOTA")
            if owner_resources_full:
                reasons.append("USER_RESOURCE_QUOTA")
            if project_full:
                reasons.append("PROJECT_QUOTA")
            if mission_full:
                reasons.append("MISSION_QUOTA")
            if request.spawn_depth > self.maximum_spawn_depth:
                reasons.append("SPAWN_DEPTH_LIMIT")
            if parent_full:
                reasons.append("SPAWN_FANOUT_LIMIT")
            if recent_children >= self.parent_burst_limit:
                reasons.append("SPAWN_RATE_LIMIT")
            if workspace_busy:
                reasons.append("WORKSPACE_LOCK")
            reasons.extend(capacity.reason_codes)
            if eligibility.eligible and not capacity.fits(request):
                reasons.append("RESERVATION_CAPACITY")
            score = self._score(request, snapshot, capacity) if not reasons else 0.0
            explanations[snapshot.host_uid] = {
                "host_name": snapshot.host_name,
                "eligible": not reasons,
                "reason_codes": list(dict.fromkeys(reasons)),
                "score": score,
                "policy_revision": snapshot.policy.revision,
                "measured": {
                    "total_cpu": capacity.total_cpu,
                    "available_cpu": max(0.0, capacity.total_cpu - capacity.used_cpu - capacity.reserved_cpu),
                    "available_memory_gib": max(0.0, capacity.total_memory_gib - capacity.used_memory_gib - capacity.reserved_memory_gib),
                    "total_memory_gib": capacity.total_memory_gib,
                },
            }
            if not reasons:
                candidates.append((score, snapshot, capacity))
        candidates.sort(key=lambda item: (-item[0], item[1].host_uid))
        selected = candidates[0][1].host_uid if candidates else None
        return PlacementDecision(
            request_id=request.request_id,
            state="planned" if selected else "queued",
            selected_host_uid=selected,
            explanations=explanations,
            policy_generation=max((s.policy.revision for s, _ in snapshots), default=0),
            created_at=now,
        )

    def reserve(self, request: PlacementRequest) -> Reservation:
        with self._lock:
            request = self.contextualize(request)
            existing_id = self._by_request.get(request.request_id)
            if existing_id:
                return self._reservations[existing_id]
            decision = self.plan(request)
            now = datetime.now(UTC)
            reservation = Reservation(
                reservation_id=f"reservation_{uuid4().hex}", request_id=request.request_id,
                host_uid=decision.selected_host_uid, requested_cpu=request.cpu,
                requested_memory=request.memory, requested_disk=request.disk,
                policy_revision=decision.explanations.get(decision.selected_host_uid or "", {}).get("policy_revision", 0),
                expires_at=now + timedelta(seconds=self._ttl), state="reserved" if decision.selected_host_uid else "queued",
                decision=decision,
                request_snapshot={**asdict(request), "required_tags": sorted(request.required_tags)},
                queued_at=now.isoformat(),
            )
            self._reservations[reservation.reservation_id] = reservation
            self._by_request[request.request_id] = reservation.reservation_id
            return reservation

    def retry_queued(self, reservation_id: str) -> Reservation:
        with self._lock:
            reservation = self._reservations[reservation_id]
            if reservation.state != "queued":
                raise RuntimeError("only queued reservations can be retried")
            if reservation.request_snapshot is None:
                raise RuntimeError("queued request context is unavailable")
            request = PlacementRequest(**reservation.request_snapshot)
            decision = self.plan(request)
            reservation.decision = decision
            reservation.host_uid = decision.selected_host_uid
            reservation.policy_revision = decision.explanations.get(
                decision.selected_host_uid or "", {}).get("policy_revision", 0)
            reservation.state = "reserved" if reservation.host_uid else "queued"
            reservation.expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl)
            return reservation

    def commit(self, reservation_id: str) -> Reservation:
        with self._lock:
            reservation = self._reservations[reservation_id]
            if reservation.state == "committed":
                return reservation
            if reservation.state != "reserved":
                raise RuntimeError(f"reservation is {reservation.state}")
            if reservation.expires_at <= datetime.now(UTC):
                raise RuntimeError("reservation expired before commit")
            current = {s.host_uid: (s, c) for s, c in self._snapshot()}.get(reservation.host_uid)
            if current is None or current[0].policy.revision != reservation.policy_revision:
                reservation.state = "queued"
                raise RuntimeError("policy changed before commit")
            request = reservation.request_snapshot or {}
            eligibility = evaluate_eligibility(
                current[0], placement_mode=request.get("placement_mode", "automatic"),
                explicit_host_uid=request.get("explicit_host_uid"),
                required_tags=request.get("required_tags", ()), now=datetime.now(UTC))
            if not eligibility.eligible:
                reservation.state = "queued"
                raise RuntimeError("policy changed before commit")
            self._validate_capacity(*current)
            reservation.state = "committed"
            reservation.lease_id = f"lease_{uuid4().hex}"
            reservation.lease_token = uuid4().hex
            return reservation

    def validate_execution_capacity(self, host_uid):
        """Recheck all held reservations against current host facts before launch."""
        with self._lock:
            current = next(((s, c) for s, c in self._snapshot() if s.host_uid == host_uid), None)
            if current is None:
                raise RuntimeError("host capacity unavailable before execution")
            self._validate_capacity(*current)

    def _validate_capacity(self, snapshot, base_capacity):
        capacity = self._capacity(snapshot, base_capacity)
        if capacity.reason_codes:
            raise RuntimeError("host capacity changed before execution: " + ",".join(capacity.reason_codes))
        if (capacity.used_cpu + capacity.reserved_cpu > capacity.total_cpu
                or capacity.used_memory_gib + capacity.reserved_memory_gib > capacity.total_memory_gib
                or capacity.used_disk_gib + capacity.reserved_disk_gib > capacity.total_disk_gib):
            raise RuntimeError("host capacity changed before execution")

    def release(self, reservation_id: str, *, state="released") -> Reservation:
        with self._lock:
            reservation = self._reservations[reservation_id]
            if reservation.state in {"released", "cancelled"}:
                return reservation
            reservation.state = state
            return reservation

    def cancel(self, reservation_id: str) -> Reservation:
        return self.release(reservation_id, state="cancelled")

    def reconcile(self, now: datetime | None = None) -> list[Reservation]:
        now = now or datetime.now(UTC)
        expired = []
        with self._lock:
            for reservation in self._reservations.values():
                if reservation.state == "reserved" and reservation.expires_at <= now:
                    expired.append(self.release(reservation.reservation_id, state="expired"))
        return expired

    def export_state(self) -> list[dict]:
        with self._lock:
            return [reservation.to_dict() for reservation in self._reservations.values()]

    def restore(self, rows: Iterable[dict]) -> None:
        """Restore reservations without mutating host telemetry capacity objects."""
        with self._lock:
            for row in rows:
                if not isinstance(row, dict) or not row.get("reservation_id"):
                    continue
                decision = row.get("decision") or {}
                try:
                    restored = Reservation(
                        reservation_id=str(row["reservation_id"]), request_id=str(row["request_id"]),
                        host_uid=row.get("host_uid"), requested_cpu=float(row.get("requested_cpu", 0)),
                        requested_memory=float(row.get("requested_memory", 0)), requested_disk=float(row.get("requested_disk", 0)),
                        policy_revision=int(row.get("policy_revision", 0)),
                        expires_at=datetime.fromisoformat(row["expires_at"]), state=str(row.get("state", "queued")),
                        lease_id=row.get("lease_id"),
                        lease_token=row.get("lease_token"),
                        request_snapshot=row.get("request_snapshot"),
                        queued_at=row.get("queued_at"),
                        decision=PlacementDecision(
                            request_id=str(decision.get("request_id", row["request_id"])),
                            state=str(decision.get("state", "queued")),
                            selected_host_uid=decision.get("selected_host_uid"),
                            explanations=decision.get("explanations") or {},
                            policy_generation=int(decision.get("policy_generation", 0)),
                            created_at=datetime.fromisoformat(decision["created_at"]),
                        ),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                self._reservations[restored.reservation_id] = restored
                self._by_request[restored.request_id] = restored.reservation_id

