"""Control-plane adapter for the single scheduler domain service."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from ..quotas import FairQueue, QueueEntry, ResourceEnvelope
from ..scheduler import HostCapacity, PlacementRequest, Scheduler


class SchedulerService:
    def __init__(self, repo, *, reservation_ttl=90.0, owner_queue_limit=100,
                 project_queue_limit=100, global_queue_limit=1000):
        self.repo = repo
        self.scheduler = Scheduler(self._hosts, reservation_ttl=reservation_ttl)
        self.scheduler.restore(repo.list_scheduler_reservations())
        self.queue = FairQueue(global_limit=global_queue_limit)
        self.queue.owner_limits = {"*": owner_queue_limit}
        self.queue.project_limits = {"*": project_queue_limit}
        self._queued_requests: set[str] = set()

    def _hosts(self):
        rows = []
        for record in self.repo.list_hosts():
            snapshot = self.repo.host_snapshot(record.get("host_id"))
            if snapshot is None:
                continue
            capacity = record.get("scheduler_capacity") or {}
            rows.append((snapshot, HostCapacity(
                total_cpu=float(capacity.get("total_cpu", 0)),
                used_cpu=float(capacity.get("used_cpu", 0)),
                reserved_cpu=float(capacity.get("reserved_cpu", 0)),
                total_memory_gib=float(capacity.get("total_memory_gib", 0)),
                used_memory_gib=float(capacity.get("used_memory_gib", 0)),
                reserved_memory_gib=float(capacity.get("reserved_memory_gib", 0)),
                total_disk_gib=float(capacity.get("total_disk_gib", 0)),
                used_disk_gib=float(capacity.get("used_disk_gib", 0)),
                reserved_disk_gib=float(capacity.get("reserved_disk_gib", 0)),
            )))
        return rows

    @staticmethod
    def request(body: dict[str, Any], principal: str) -> PlacementRequest:
        profile = body.get("resource_profile")
        if not isinstance(profile, dict) or not isinstance(profile.get("class"), str):
            raise ValueError("resource_profile.class is required")
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required")
        return PlacementRequest(
            request_id=request_id, owner_id=principal,
            agent_family=body.get("agent_family", "agent"),
            placement_mode=body.get("placement_mode", "automatic"),
            resource_profile=profile,
            project_id=body.get("project_id"), workspace_id=body.get("workspace_id"),
            parent_agent_id=body.get("parent_agent_id"),
            explicit_host_uid=body.get("explicit_host_uid"),
            preferred_host_uid=body.get("preferred_host_uid"),
            required_tags=frozenset(body.get("required_tags") or ()),
            priority=body.get("priority", "normal"),
            idempotency_key=body.get("idempotency_key"),
        )

    def host_cards(self):
        cards = []
        for record in self.repo.list_hosts():
            snapshot = self.repo.host_snapshot(record.get("host_id"))
            if snapshot is None:
                continue
            cards.append({
                "host_uid": snapshot.host_uid, "host_name": snapshot.host_name,
                "aliases": list(snapshot.aliases), "platform": snapshot.platform,
                "owned": snapshot.owned, "trusted": snapshot.trusted,
                "reachable": snapshot.reachable,
                "policy": {"mode": snapshot.policy.mode.value, "revision": snapshot.policy.revision},
                "telemetry": {"at": record.get("last_seen"), "freshness_seconds": snapshot.telemetry_freshness_seconds},
                "capacity": record.get("scheduler_capacity") or {},
            })
        return cards

    def plan(self, body, principal):
        request = self.request(body, principal)
        decision = self.scheduler.plan(request, explicit_host_uid=body.get("explicit_host_uid"))
        return {"request_id": decision.request_id, "state": decision.state,
                "selected_host_uid": decision.selected_host_uid,
                "explanations": decision.explanations,
                "policy_generation": decision.policy_generation,
                "created_at": decision.created_at.isoformat()}

    def reserve(self, body, principal):
        request = self.request(body, principal)
        reservation = self.scheduler.reserve(request)
        if reservation.state == "queued" and request.request_id not in self._queued_requests:
            profile = request.resource_profile
            try:
                self.queue.add(QueueEntry(
                    request_id=request.request_id, owner_id=principal,
                    project_id=request.project_id, priority=request.priority,
                    created_at=monotonic(), envelope=ResourceEnvelope(
                        requested_cpu=request.cpu, minimum_cpu=request.cpu,
                        maximum_cpu=request.cpu, requested_memory_gib=request.memory,
                        maximum_memory_gib=request.memory, expected_seconds=0,
                        workload_class=str(profile["class"]))))
            except RuntimeError:
                self.scheduler.cancel(reservation.reservation_id)
                raise
            self._queued_requests.add(request.request_id)
        self.repo.save_scheduler_reservation(reservation.to_dict())
        return self._reservation(reservation)

    def commit(self, reservation_id):
        reservation = self.scheduler.commit(reservation_id)
        self.repo.save_scheduler_reservation(reservation.to_dict())
        self.repo.save_scheduler_lease({
            "lease_id": reservation.lease_id,
            "request_id": reservation.request_id,
            "reservation_id": reservation.reservation_id,
            "host_uid": reservation.host_uid,
            "generation": 1,
            "token": reservation.lease_token,
            "state": "running",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=90)).isoformat(),
        })
        return self._reservation(reservation)

    def release(self, reservation_id, state="released"):
        reservation = self.scheduler.release(reservation_id, state=state)
        self.repo.save_scheduler_reservation(reservation.to_dict())
        if reservation.lease_id:
            lease = next((row for row in self.repo.list_scheduler_leases()
                          if row.get("lease_id") == reservation.lease_id), None)
            if lease:
                lease["state"] = state
                self.repo.save_scheduler_lease(lease)
        return self._reservation(reservation)

    def reconcile(self, now=None):
        """Persist terminal expiry transitions after restart or periodic sweep."""
        expired = self.scheduler.reconcile(now)
        for reservation in expired:
            self.repo.save_scheduler_reservation(reservation.to_dict())
        return [self._reservation(reservation) for reservation in expired]

    def heartbeat(self, lease_id, *, generation, token, ttl=90):
        lease = next((row for row in self.repo.list_scheduler_leases()
                      if row.get("lease_id") == lease_id), None)
        if lease is None:
            raise KeyError(lease_id)
        if lease.get("state") != "running":
            raise PermissionError("lease is not running")
        if int(generation) != int(lease.get("generation", 0)):
            raise PermissionError("lease generation is stale")
        if not isinstance(token, str) or not secrets.compare_digest(token, str(lease.get("token", ""))):
            raise PermissionError("lease token is invalid")
        lease["heartbeat_at"] = datetime.now(UTC).isoformat()
        lease["expires_at"] = (datetime.now(UTC) + timedelta(seconds=max(1, float(ttl)))).isoformat()
        return self.repo.save_scheduler_lease(lease)

    def reconcile_leases(self, now=None):
        now = now or datetime.now(UTC)
        fenced = []
        for lease in self.repo.list_scheduler_leases():
            if lease.get("state") != "running" or not lease.get("expires_at"):
                continue
            try:
                expired = datetime.fromisoformat(lease["expires_at"]) <= now
            except (TypeError, ValueError):
                expired = True
            if expired:
                lease["state"] = "fenced"
                lease["fenced_at"] = now.isoformat()
                fenced.append(self.repo.save_scheduler_lease(lease))
        return fenced

    @staticmethod
    def _reservation(reservation):
        return reservation.to_dict()

