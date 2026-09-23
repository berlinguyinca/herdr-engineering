"""Transparent development service fabric (spec 0110).

A lease registry implementing DevServiceLease with atomic collision-free
external port allocation across hosts, TTL/heartbeat/expiry, stale lease
reconciliation, idempotent registration and private-only addressing.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .contracts import DevServiceLease
from .errors import ConflictError

_LO = 18000
_HI = 28999


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class DevServiceRegistry:
    """Thread-safe, persistent dev-service lease registry."""

    def __init__(self, store_path: Path | None = None,
                 port_range: tuple = (_LO, _HI),
                 lease_ttl_seconds: int = 90) -> None:
        self._path = store_path or Path(
            os.environ.get("HERDR_ENGINEERING_STATE",
                           str(Path.home() / ".local/share/herdr-engineering/state"))) / "leases.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lo, self._hi = port_range
        self._ttl = lease_ttl_seconds
        self._leases: dict[str, DevServiceLease] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for raw in data.get("leases", []):
                lease = DevServiceLease(**raw)
                self._leases[lease.id] = lease
        except Exception:
            pass

    def _persist(self) -> None:
        try:
            payload = {"leases": [lease.to_dict() for lease in self._leases.values()]}
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            os.replace(tmp, self._path)
        except OSError:
            pass

    def _is_port_taken(self, port: int, exclude_id: str | None = None) -> bool:
        for lid, lease in self._leases.items():
            if lid == exclude_id:
                continue
            if lease.external_port == port and lease.state in ("active", "draining", "pending"):
                return True
        return False

    def _allocate_port(self, exclude_id: str | None = None) -> int:
        for port in range(self._lo, self._hi + 1):
            if not self._is_port_taken(port, exclude_id):
                return port
        raise ConflictError("no free external dev port in configured range")

    def register(self, *, target_machine_id: str, target_host: str, target_port: int,
                 protocol: str = "http", owner_herdr_session_id: str | None = None,
                 owner_repository: str | None = None, owner_worktree_id: str | None = None,
                 owner_process_id: str | None = None, label: str | None = None,
                 lease_id: str | None = None) -> DevServiceLease:
        """Register (or idempotently renew) a lease. Allocates a collision-free port."""
        now = _now_iso()
        # Idempotent registration for a stable owner/process identity.
        if owner_herdr_session_id or owner_process_id:
            for lease in self._leases.values():
                if (lease.owner_herdr_session_id and lease.owner_herdr_session_id == owner_herdr_session_id
                        and lease.owner_process_id and lease.owner_process_id == owner_process_id
                        and lease.target_port == target_port):
                    lease.renewed_at = now
                    lease.state = "active"
                    self._persist()
                    return lease
        with self._lock:
            if lease_id and lease_id in self._leases:
                lease = self._leases[lease_id]
                lease.renewed_at = now
                lease.state = "active"
                self._persist()
                return lease
            port = self._allocate_port()
            lid = lease_id or f"lease_{uuid.uuid4().hex[:16]}"
            expires = _add_seconds(now, self._ttl)
            lease = DevServiceLease(
                id=lid, external_port=port, protocol=protocol,
                target_machine_id=target_machine_id, target_host=target_host,
                target_port=target_port,
                owner_herdr_session_id=owner_herdr_session_id,
                owner_repository=owner_repository,
                owner_worktree_id=owner_worktree_id,
                owner_process_id=owner_process_id,
                label=label, state="active", created_at=now,
                renewed_at=now, expires_at=expires)
            self._leases[lid] = lease
            self._persist()
            return lease

    def heartbeat(self, lease_id: str) -> DevServiceLease | None:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return None
            if lease.state in ("expired", "failed"):
                lease.state = "active"
            lease.renewed_at = _now_iso()
            lease.expires_at = _add_seconds(lease.renewed_at, self._ttl)
            self._persist()
            return lease

    def close(self, lease_id: str) -> bool:
        with self._lock:
            if lease_id in self._leases:
                self._leases[lease_id].state = "expired"
                self._persist()
                return True
            return False

    def mark_health(self, lease_id: str, ok: bool, error: str | None = None) -> None:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return
            if ok:
                lease.health_last_ok_at = _now_iso()
                lease.health_last_error = None
            else:
                lease.health_last_error = error

    def reconcile(self, now_iso: str | None = None) -> list[str]:
        """Expire stale leases (TTL exceeded). Returns ids expired."""
        now = now_iso or _now_iso()
        expired = []
        with self._lock:
            for lid, lease in self._leases.items():
                if lease.state in ("active", "pending", "draining") and lease.expires_at < now:
                    lease.state = "expired"
                    lease.health_last_error = "lease expired (heartbeat timeout)"
                    expired.append(lid)
        if expired:
            self._persist()
        return expired

    def active_leases(self) -> list[DevServiceLease]:
        self.reconcile()
        with self._lock:
            return [lease for lease in self._leases.values() if lease.state == "active"]

    def get(self, lease_id: str) -> DevServiceLease | None:
        return self._leases.get(lease_id)

    def resolve_port(self, external_port: int) -> DevServiceLease | None:
        self.reconcile()
        for lease in self._leases.values():
            if lease.external_port == external_port and lease.state == "active":
                return lease
        return None


def _add_seconds(iso: str, seconds: int) -> str:
    from datetime import timedelta
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
