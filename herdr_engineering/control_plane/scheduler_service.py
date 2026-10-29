"""Control-plane adapter for the single scheduler domain service."""
from __future__ import annotations

import math
import secrets
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..host_policy import default_policy, evaluate_eligibility
from ..scheduler import HostCapacity, PlacementRequest, Scheduler


class SchedulerService:
    def __init__(self, repo, *, reservation_ttl=90.0, owner_queue_limit=100,
                 project_queue_limit=100, global_queue_limit=1000,
                 owner_concurrency_limit=20, project_concurrency_limit=50,
                 mission_concurrency_limit=20, owner_cpu_limit=32, owner_memory_limit=64,
                 maximum_spawn_depth=3, parent_concurrency_limit=8, parent_burst_limit=20):
        self.repo = repo
        self._scheduler_options = {"reservation_ttl": reservation_ttl,
                                   "owner_concurrency_limit": owner_concurrency_limit,
                                   "project_concurrency_limit": project_concurrency_limit,
                                   "mission_concurrency_limit": mission_concurrency_limit,
                                   "owner_cpu_limit": owner_cpu_limit, "owner_memory_limit": owner_memory_limit,
                                   "maximum_spawn_depth": maximum_spawn_depth,
                                   "parent_concurrency_limit": parent_concurrency_limit,
                                   "parent_burst_limit": parent_burst_limit}
        self.scheduler = self._fresh_scheduler()
        self.scheduler.restore(repo.list_scheduler_reservations())
        self._owner_queue_limit = owner_queue_limit
        self._project_queue_limit = project_queue_limit
        self._global_queue_limit = int(global_queue_limit)
        self.native_registry = None

    def _fresh_scheduler(self):
        return Scheduler(self._hosts, **self._scheduler_options)

    def configure_native(self, adapter):
        from ..adapter_registry import AdapterRegistry, register_native_families
        from ..workspace import WorkspaceManifestStore
        def resolve_host(uid):
            host = next((host for host in self.repo.list_hosts() if host.get("host_uid") == uid), None)
            if host is None:
                return None
            for alias in host.get("aliases") or [host["host_id"]]:
                machine = adapter.resolve_machine(alias)
                if machine is not None:
                    return {"host_uid": uid, "machine": machine.label}
            return None
        self.native_registry = register_native_families(
            AdapterRegistry(self), adapter, resolve_host=resolve_host,
            resolve_workspace=WorkspaceManifestStore(self.repo).resolve)

    def launch_native(self, body, principal):
        if self.native_registry is None:
            raise RuntimeError("native scheduler launcher is disabled")
        return asdict(self.native_registry.launch(body, principal))

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
                reason_codes=tuple(capacity.get("reason_codes", ())),
            )))
        return rows

    def observe_host(self, authenticated_host, body):
        """Accept resource facts only for the host resolved from its fleet token."""
        with self.repo.scheduler_admission():
            return self._observe_host(authenticated_host, body)

    def _observe_host(self, authenticated_host, body):
        import re
        uid = body.get("host_uid")
        if not isinstance(uid, str) or not re.fullmatch(r"host-[a-f0-9]{32}", uid):
            raise ValueError("invalid stable host identity")
        stamp = datetime.fromisoformat(body.get("captured_at", ""))
        now = datetime.now(UTC)
        if stamp.tzinfo is None or not -5 <= (now - stamp).total_seconds() <= 60:
            raise ValueError("stale or future resource sample")
        previous = self.repo.get_host(authenticated_host) or {}
        if previous.get("host_uid") not in (None, uid):
            raise ValueError("host identity conflict")
        if any(host.get("host_uid") == uid and host.get("host_id") != authenticated_host
               for host in self.repo.list_hosts()):
            raise ValueError("duplicate host identity requires operator reconciliation")
        observed = self.repo.scheduler_observation(authenticated_host)
        sequence = body.get("sequence")
        if type(sequence) is not int or sequence < 0 or (observed and sequence <= observed["sample"]["sequence"]):
            raise ValueError("out-of-order resource sample")
        def metric(key):
            value = body.get(key)
            return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        cores, utilization = metric("cpu_count"), metric("cpu_utilization")
        memory, available = metric("memory_total_gib"), metric("memory_available_gib")
        disk, free = metric("disk_total_gib"), metric("disk_free_gib")
        pressure = body.get("pressure") or {}
        overloaded = any(type(pressure.get(kind)) in (int, float) and pressure[kind] >= threshold
                         for kind, threshold in (("cpu", .4), ("memory", .1), ("io", .4)))
        known = (all(value is not None for value in (cores, utilization, memory, available, disk, free))
                 and utilization <= 1 and available <= memory and free <= disk)
        reasons = []
        if not known:
            reasons.append("CAPABILITY_UNKNOWN")
        for kind, threshold, reason in (("cpu", .4, "CPU_PRESSURE"), ("memory", .1, "MEMORY_PRESSURE"), ("io", .4, "IO_PRESSURE")):
            if type(pressure.get(kind)) in (int, float) and pressure[kind] >= threshold:
                reasons.append(reason)
        if memory and available is not None and available / memory < .15:
            reasons.append("MEMORY_PRESSURE")
        if disk and free is not None and free < max(20, disk * .1):
            reasons.append("DISK_LOW")
        policy = previous.get("scheduler_policy") or {"mode": default_policy(
            platform=str(body.get("platform", "unknown")), aliases=[authenticated_host]).mode.value, "revision": 0}
        capacity = {"total_cpu": cores * .8 if known and not overloaded else 0,
                    "used_cpu": cores * utilization if known else 0,
                    "total_memory_gib": memory * .85 if known else 0,
                    "used_memory_gib": memory - available if known else 0,
                    "total_disk_gib": max(0, free - max(20, disk * .1)) if known else 0,
                    "reason_codes": sorted(set(reasons))}
        host = {"host_id": authenticated_host, "host_name": authenticated_host,
                "host_uid": uid, "aliases": [authenticated_host],
                "platform": str(body.get("platform", "unknown")),
                "scheduler_owned": True, "scheduler_trusted": True,
                "scheduler_policy": policy, "scheduler_capacity": capacity,
                "status": "healthy", "last_seen": stamp.isoformat()}
        sample = {"at": stamp.isoformat(), "sequence": sequence,
            "freshness_seconds": max(0, (now - stamp).total_seconds()), "resource_sample": body,
            "confidence": "known" if known else "partial"}
        self.repo.save_scheduler_observation(authenticated_host, {"host": host, "sample": sample})
        return {"accepted": True, "host_uid": uid, "capacity_known": known}

    @staticmethod
    def request(body: dict[str, Any], principal: str) -> PlacementRequest:
        profile = body.get("resource_profile")
        if not isinstance(profile, dict) or not isinstance(profile.get("class"), str):
            raise ValueError("resource_profile.class is required")
        if profile["class"] not in {"interactive", "coding", "planning", "review", "build-test", "benchmark", "observer", "maintenance"}:
            raise ValueError("unsupported resource_profile.class")
        if body.get("agent_family", "agent") not in {"pi", "claude-code", "codex", "agent"}:
            raise ValueError("unsupported agent_family")
        if body.get("placement_mode", "automatic") not in {"automatic", "manual"}:
            raise ValueError("unsupported placement_mode")
        if body.get("priority", "normal") not in {"owner-interactive", "normal", "background"}:
            raise ValueError("unsupported priority")
        if body.get("placement_mode") == "manual" and not body.get("explicit_host_uid"):
            raise ValueError("manual placement requires explicit_host_uid")
        if type(body.get("spawn_depth", 0)) is not int or body.get("spawn_depth", 0) < 0:
            raise ValueError("spawn_depth must be a nonnegative integer")
        defaults = {"interactive": (1, 2, 1), "coding": (1, 2, 1), "planning": (.5, 1, .5),
                    "review": (1, 2, 1), "build-test": (2, 4, 2), "benchmark": (2, 4, 2),
                    "observer": (.25, .5, .5), "maintenance": (.5, 1, 1)}
        cpu, memory, disk = defaults[profile["class"]]
        profile = {"cpu_cores": cpu, "memory_gib": memory, "disk_gib": disk, **profile}
        for field in ("cpu_cores", "memory_gib", "disk_gib"):
            value = profile.get(field, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"resource_profile.{field} must be a finite nonnegative number")
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
            mission_id=body.get("mission_id"), spawn_depth=body.get("spawn_depth", 0),
        )

    def host_cards(self):
        with self.repo.scheduler_admission():
            return self._host_cards()

    def _host_cards(self):
        cards = []
        for record in self.repo.list_hosts():
            snapshot = self.repo.host_snapshot(record.get("host_id"))
            if snapshot is None:
                continue
            age = snapshot.telemetry_freshness_seconds
            if snapshot.telemetry_at is not None:
                elapsed = (datetime.now(UTC) - snapshot.telemetry_at).total_seconds()
                age = max(age if age is not None else 0, elapsed)
            cards.append({
                "host_uid": snapshot.host_uid, "host_name": snapshot.host_name,
                "aliases": list(snapshot.aliases), "platform": snapshot.platform,
                "owned": snapshot.owned, "trusted": snapshot.trusted,
                "reachable": snapshot.reachable,
                "policy": {"mode": snapshot.policy.mode.value, "revision": snapshot.policy.revision},
                "telemetry": {"at": snapshot.telemetry_at.isoformat() if snapshot.telemetry_at else None,
                              "freshness_seconds": age},
                "capacity": record.get("scheduler_capacity") or {},
            })
        return cards

    def overview(self):
        with self.repo.scheduler_admission() as reservations:
            return self._overview(reservations)

    def _overview(self, reservations):
        # Use an allowlist: lease tokens and raw checkpoint/session data never
        # belong in dashboard history, even for an authenticated viewer.
        fields = ("reservation_id", "request_id", "host_uid", "state", "expires_at",
                  "execution_claimed", "execution_claimed_at", "requested_cpu", "requested_memory")
        history = [{key: row.get(key) for key in fields} for row in reservations]
        return {"hosts": self._host_cards(), "history": history[-200:],
                "queued_agents": sum(row.get("state") == "queued" for row in reservations),
                "claimed_executions": sum(row.get("state") == "committed" and bool(row.get("execution_claimed"))
                                          for row in reservations),
                "reserved_cpu": sum(float(row.get("requested_cpu", 0)) for row in reservations
                                    if row.get("state") in {"reserved", "committed"}),
                "queue": [row for row in history if row.get("state") == "queued"]}

    def simulate(self, body):
        from ..replay import placement_preview
        excluded = body.get("excluded_hosts", [])
        if not isinstance(excluded, list) or len(excluded) > 200 or not all(isinstance(uid, str) for uid in excluded):
            raise ValueError("excluded_hosts must be a bounded list")
        days = body.get("days", 1)
        if days not in (1, 7, 30):
            raise ValueError("simulator window must be 1, 7, or 30 days")
        cutoff = datetime.now(UTC) - timedelta(days=days)
        rows = []
        for row in self.repo.list_scheduler_reservations():
            try:
                captured = datetime.fromisoformat((row.get("decision") or {})["created_at"])
            except (KeyError, ValueError, TypeError):
                continue
            if captured.tzinfo is not None and captured >= cutoff:
                rows.append(row)
        return placement_preview(rows, cpu_weight=body.get("cpu_weight", .6),
                                  memory_weight=body.get("memory_weight", .4), excluded_hosts=excluded)

    def plan(self, body, principal):
        request = self.request(body, principal)
        with self.repo.scheduler_admission() as rows:
            scheduler = self._fresh_scheduler()
            scheduler.restore(rows)
            decision = scheduler.plan(request, explicit_host_uid=body.get("explicit_host_uid"))
        return {"request_id": decision.request_id, "state": decision.state,
                "selected_host_uid": decision.selected_host_uid,
                "explanations": decision.explanations,
                "policy_generation": decision.policy_generation,
                "created_at": decision.created_at.isoformat()}

    def reserve(self, body, principal):
        request = self.request(body, principal)
        with self.repo.scheduler_admission() as rows:
            self.scheduler = self._fresh_scheduler()
            self.scheduler.restore(rows)
            request = self.scheduler.contextualize(request)
            existing = next((row for row in rows if row.get("request_id") == body.get("request_id")), None)
            if existing is not None:
                snapshot = existing.get("request_snapshot") or {}
                if snapshot.get("owner_id") != principal:
                    raise PermissionError("reservation owner mismatch")
                normalized = {**asdict(request), "required_tags": sorted(request.required_tags)}
                if snapshot != normalized:
                    raise ValueError("request_id conflict: placement constraints changed")
                return existing
            return self._reserve(body, principal, rows=rows, request=request)

    def _reserve(self, body, principal, *, rows, request=None):
        request = request or self.request(body, principal)
        reservation = self.scheduler.reserve(request)
        if reservation.state == "queued":
            try:
                self._check_queue_limits(rows, request)
            except RuntimeError:
                self.scheduler.cancel(reservation.reservation_id)
                raise
        self.repo.save_scheduler_reservation(reservation.to_dict())
        return self._reservation(reservation)

    def _check_queue_limits(self, rows, request):
        # Count authoritative rows while holding scheduler_admission, not local history.
        # Caps apply only to new entries: a lowered cap must not discard or block
        # retries/draining of requests that were already admitted to the queue.
        queued = [row.get("request_snapshot") or {} for row in rows if row.get("state") == "queued"]
        if len(queued) >= self._global_queue_limit:
            raise RuntimeError("global queue cap reached")
        if sum(row.get("owner_id") == request.owner_id for row in queued) >= self._owner_queue_limit:
            raise RuntimeError("owner queue cap reached")
        if (request.project_id and sum(row.get("project_id") == request.project_id for row in queued)
                >= self._project_queue_limit):
            raise RuntimeError("project queue cap reached")

    def retry_queued(self, reservation_id):
        with self.repo.scheduler_admission() as rows:
            self.scheduler = self._fresh_scheduler()
            self.scheduler.restore(rows)
            reservation = self.scheduler.retry_queued(reservation_id)
            self.repo.save_scheduler_reservation(reservation.to_dict())
            return self._reservation(reservation)

    def drain_queue(self, *, limit=20, now=None):
        """Admit a bounded fair batch using persisted queue age and project demand."""
        now = now or datetime.now(UTC)
        with self.repo.scheduler_admission() as rows:
            self.scheduler = self._fresh_scheduler()
            self.scheduler.restore(rows)
            active = {}
            for row in rows:
                if row.get("state") in {"reserved", "committed"}:
                    project = (row.get("request_snapshot") or {}).get("project_id")
                    active[project] = active.get(project, 0) + 1
            def order(row):
                request = row.get("request_snapshot") or {}
                try:
                    age = max(0, (now - datetime.fromisoformat(row["queued_at"])).total_seconds())
                except (KeyError, TypeError, ValueError):
                    age = 0
                rank = {"owner-interactive": 0, "normal": 1, "background": 2}.get(request.get("priority"), 1)
                return (max(0, rank - int(age // 300)), active.get(request.get("project_id"), 0),
                        row.get("queued_at") or "", row["reservation_id"])
            queued = [row for row in rows if row.get("state") == "queued" and row.get("request_snapshot")]
            admitted = []
            for _ in range(max(0, min(int(limit), 100))):
                if not queued:
                    break
                queued.sort(key=order)
                row = queued.pop(0)
                reservation = self.scheduler.retry_queued(row["reservation_id"])
                if reservation.state == "reserved":
                    self.repo.save_scheduler_reservation(reservation.to_dict())
                    admitted.append(reservation.to_dict())
                    project = (row.get("request_snapshot") or {}).get("project_id")
                    active[project] = active.get(project, 0) + 1
            return admitted

    def commit(self, reservation_id):
        if not self.repo.scheduler_execution_ready():
            raise RuntimeError("durable scheduler persistence is required before execution")
        with self.repo.scheduler_admission() as rows:
            row = next((row for row in rows if row.get("reservation_id") == reservation_id), None)
            if row is None:
                raise KeyError(reservation_id)
            if row.get("state") == "committed":
                lease = next((lease for lease in self.repo.list_scheduler_leases()
                              if lease.get("lease_id") == row.get("lease_id")), None)
                if lease is not None and lease.get("state") != "running":
                    raise RuntimeError("execution requires reconciliation before commit retry")
                return row
            self.scheduler.restore(rows)
            return self._commit(reservation_id)

    def _commit(self, reservation_id):
        reservation = self.scheduler.commit(reservation_id)
        existing = next((row for row in self.repo.list_scheduler_leases()
                         if row.get("lease_id") == reservation.lease_id), None)
        if existing is not None:
            if existing.get("state") != "running":
                raise RuntimeError("execution requires reconciliation before commit retry")
            return self._reservation(reservation)
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
        with self.repo.scheduler_admission() as rows:
            row = next((row for row in rows if row.get("reservation_id") == reservation_id), None)
            if row is None:
                raise KeyError(reservation_id)
            if row.get("state") == "committed" and row.get("execution_claimed"):
                raise PermissionError("verified execution stop is required before capacity release")
            self.scheduler = self._fresh_scheduler()
            self.scheduler.restore(rows)
            return self._release(reservation_id, state)

    def _release(self, reservation_id, state="released"):
        reservation = self.scheduler.release(reservation_id, state=state)
        self.repo.save_scheduler_reservation(reservation.to_dict())
        if reservation.lease_id:
            lease = next((row for row in self.repo.list_scheduler_leases()
                          if row.get("lease_id") == reservation.lease_id), None)
            if lease:
                lease["state"] = state
                self.repo.save_scheduler_lease(lease)
        return self._reservation(reservation)

    def stop_supervised(self, reservation_id, *, supervisor, receipt):
        """Host-side stop proof; HTTP callers cannot supply a stop boolean."""
        from ..recovery import ExecutionLease
        with self.repo.scheduler_admission() as rows:
            row = next((row for row in rows if row.get("reservation_id") == reservation_id), None)
            if row is None:
                raise KeyError(reservation_id)
            if receipt.agent_id != row["request_id"] or receipt.host_uid != row.get("host_uid"):
                raise PermissionError("scope receipt does not match reservation")
            lease = next((lease for lease in self.repo.list_scheduler_leases()
                          if lease.get("lease_id") == row.get("lease_id")), None)
            if lease is None or lease.get("generation") != receipt.generation:
                raise PermissionError("execution lease generation mismatch")
            execution = ExecutionLease(row["request_id"], row["host_uid"], lease["generation"],
                                       lease.get("token", ""), False)
            stopped = supervisor.stop(execution, receipt)
            if not isinstance(stopped, dict) or stopped.get("stop_verified") is not True:
                raise RuntimeError("execution stop remains unverified")
            self.scheduler = self._fresh_scheduler()
            self.scheduler.restore(rows)
            return self._release(reservation_id)

    def record_scope_accounting(self, reservation_id, *, supervisor, receipt):
        with self.repo.scheduler_admission() as rows:
            row = next((row for row in rows if row.get("reservation_id") == reservation_id), None)
            if row is None:
                raise KeyError(reservation_id)
            if receipt.agent_id != row["request_id"] or receipt.host_uid != row.get("host_uid"):
                raise PermissionError("scope accounting identity mismatch")
            metrics = supervisor.accounting(receipt)
            if (metrics.get("agent_id") != row["request_id"]
                    or metrics.get("host_uid") != row["host_uid"]
                    or metrics.get("generation") != receipt.generation):
                raise PermissionError("scope accounting observation mismatch")
            self.repo.record_event("scheduler_execution", reservation_id, "ExecutionResourceObserved",
                                   metrics, require_durable=True, publish=False)
            return metrics

    def claim_execution(self, reservation_id):
        """Claim native submission once; uncertain submissions remain claimed."""
        with self.repo.scheduler_admission() as rows:
            row = next((row for row in rows if row.get("reservation_id") == reservation_id), None)
            if row is None:
                raise KeyError(reservation_id)
            if row.get("state") != "committed" or row.get("execution_claimed"):
                raise RuntimeError("execution requires reconciliation before launch")
            host = next((host for host, _ in self._hosts() if host.host_uid == row.get("host_uid")), None)
            request = row.get("request_snapshot") or {}
            if host is None or host.policy.revision != row.get("policy_revision"):
                raise RuntimeError("policy changed before execution claim")
            eligible = evaluate_eligibility(
                host, placement_mode=request.get("placement_mode", "automatic"),
                explicit_host_uid=request.get("explicit_host_uid"),
                required_tags=request.get("required_tags", ()))
            if not eligible.eligible:
                raise RuntimeError("policy changed before execution claim")
            self.scheduler = self._fresh_scheduler()
            self.scheduler.restore(rows)
            self.scheduler.validate_execution_capacity(row["host_uid"])
            row = dict(row, execution_claimed=True,
                       execution_claimed_at=datetime.now(UTC).isoformat())
            self.repo.save_scheduler_reservation(row)
            return row

    def reconcile(self, now=None):
        """Persist terminal expiry transitions after restart or periodic sweep."""
        with self.repo.scheduler_admission() as rows:
            self.scheduler = self._fresh_scheduler()
            self.scheduler.restore(rows)
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
        now = datetime.now(UTC)
        try:
            expiry = datetime.fromisoformat(lease["expires_at"])
            live = expiry.tzinfo is not None and expiry > now
        except (KeyError, TypeError, ValueError):
            live = False
        if not live:
            raise PermissionError("lease expired; execution requires reconciliation")
        lease["heartbeat_at"] = now.isoformat()
        lease["expires_at"] = (now + timedelta(seconds=max(1, float(ttl)))).isoformat()
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

