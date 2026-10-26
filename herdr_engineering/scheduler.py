"""Single admission and placement contract for Herdr-controlled agents."""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
    def __init__(self, hosts: Callable[[], Iterable[tuple[HostSnapshot, HostCapacity]]], *, reservation_ttl=90.0):
        self._hosts = hosts
        self._ttl = max(1.0, float(reservation_ttl))
        self._lock = threading.RLock()
        self._reservations: dict[str, Reservation] = {}
        self._by_request: dict[str, str] = {}
        self._rotation = 0

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
        now = datetime.now(UTC)
        explanations: dict[str, dict] = {}
        candidates: list[tuple[float, HostSnapshot, HostCapacity]] = []
        explicit = explicit_host_uid or request.explicit_host_uid
        snapshots = [(snapshot, self._capacity(snapshot, capacity))
                     for snapshot, capacity in self._snapshot()]
        for snapshot, capacity in snapshots:
            eligibility = evaluate_eligibility(
                snapshot, placement_mode=request.placement_mode,
                explicit_host_uid=explicit, parent_host_uid=None,
                required_tags=request.required_tags, now=now,
            )
            reasons = list(eligibility.reason_codes)
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
            )
            self._reservations[reservation.reservation_id] = reservation
            self._by_request[request.request_id] = reservation.reservation_id
            return reservation

    def commit(self, reservation_id: str) -> Reservation:
        with self._lock:
            reservation = self._reservations[reservation_id]
            if reservation.state == "committed":
                return reservation
            if reservation.state != "reserved":
                raise RuntimeError(f"reservation is {reservation.state}")
            current = {s.host_uid: (s, c) for s, c in self._snapshot()}.get(reservation.host_uid)
            if current is None or current[0].policy.revision != reservation.policy_revision:
                reservation.state = "queued"
                raise RuntimeError("policy changed before commit")
            eligibility = evaluate_eligibility(current[0], placement_mode="automatic", now=datetime.now(UTC))
            if not eligibility.eligible:
                reservation.state = "queued"
                raise RuntimeError("policy changed before commit")
            reservation.state = "committed"
            reservation.lease_id = f"lease_{uuid4().hex}"
            reservation.lease_token = uuid4().hex
            return reservation

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

