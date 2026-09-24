"""Transparent development service fabric (spec 0110).

A lease registry implementing DevServiceLease with atomic collision-free
external port allocation across hosts, TTL/heartbeat/expiry, stale lease
reconciliation, idempotent registration and private-only addressing.
"""
from __future__ import annotations

import json
import os
import socket
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


def _os_port_free(port: int) -> bool:
    """Probe whether the OS has ``port`` bound on loopback.

    EADDRINUSE means taken; any other probe error is treated leniently as
    free (the forwarder's own bind will still fail loudly on a real
    collision).
    """
    import errno
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            return False
        return True
    finally:
        s.close()


def _parse_leases(data: dict) -> dict[str, DevServiceLease]:
    """Parse a store payload into leases.

    to_dict() nests target/owner/health and drops the flat fields, so the
    flat constructor arguments are reconstructed here.
    """
    leases: dict[str, DevServiceLease] = {}
    for raw in data.get("leases", []):
        d = dict(raw)
        target = d.pop("target", None) or {}
        owner = d.pop("owner", None) or {}
        d.pop("health", None)
        d.setdefault("target_machine_id", target.get("machine_id"))
        d.setdefault("target_host", target.get("host"))
        d.setdefault("target_port", target.get("port"))
        d.setdefault("owner_user_id", owner.get("user_id"))
        d.setdefault("owner_herdr_session_id", owner.get("herdr_session_id"))
        d.setdefault("owner_repository", owner.get("repository"))
        d.setdefault("owner_worktree_id", owner.get("worktree_id"))
        d.setdefault("owner_process_id", owner.get("process_id"))
        try:
            lease = DevServiceLease(**d)
        except TypeError:
            continue  # skip malformed entries rather than losing the whole store
        leases[lease.id] = lease
    return leases


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
        self._loaded_mtime: float | None = None
        self._load()

    @property
    def store_path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            self._loaded_mtime = None
            return
        try:
            self._loaded_mtime = self._path.stat().st_mtime
            data = json.loads(self._path.read_text())
        except Exception:
            return
        with self._lock:
            self._leases = _parse_leases(data)

    def reload_if_changed(self) -> bool:
        """Re-read the store if another process changed it on disk.

        The gateway is the registry's primary writer; this is a backstop so a
        gateway restart never loses leases an overlapping process persisted
        (e.g. a register served by an old process that was mid-shutdown).
        """
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._loaded_mtime:
            return False
        self._loaded_mtime = mtime
        try:
            data = json.loads(self._path.read_text())
        except Exception:
            return False
        with self._lock:
            self._leases = _parse_leases(data)
        return True

    def _persist(self) -> None:
        try:
            payload = {"leases": [lease.to_dict() for lease in self._leases.values()]}
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            os.replace(tmp, self._path)
            try:
                self._loaded_mtime = self._path.stat().st_mtime
            except OSError:
                pass
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
        """Allocate a port free of both other leases AND live OS bindings."""
        for port in range(self._lo, self._hi + 1):
            if not self._is_port_taken(port, exclude_id) and _os_port_free(port):
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


class DevServiceForwarder:
    """Binds an allocated external port and TCP-proxies to the lease target.

    This is the data path of the dev fabric: the lease registry decides which
    external port a service owns; the forwarder makes traffic on that port
    actually reach ``target_host:target_port``. HTTP, HTTPS, WebSocket, SSE,
    HMR and generic TCP all flow through the same byte pipe.

    Private-only: when ``private_only`` is set (the default), the bind address
    must be loopback or a private address (RFC1918, link-local, CGNAT, or the
    Tailscale range). Public bind addresses are refused.
    """

    def __init__(self, lease: DevServiceLease, bind_host: str = "127.0.0.1",
                 private_only: bool = True) -> None:
        self.lease = lease
        self.bind_host = bind_host
        self.private_only = private_only
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> DevServiceForwarder:
        from .observability import _is_public_ip
        if self.private_only and _is_public_ip(self.bind_host):
            raise ConflictError(
                f"refusing public bind address {self.bind_host} "
                "(dev fabric is private-only)")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_host, self.lease.external_port))
        sock.listen(64)
        sock.settimeout(0.5)
        self._listener = sock

        def _accept_loop() -> None:
            while not self._stopping.is_set():
                try:
                    client, _addr = self._listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._proxy, args=(client,), daemon=True).start()

        self._accept_thread = threading.Thread(target=_accept_loop, daemon=True)
        self._accept_thread.start()
        return self

    def _proxy(self, client: socket.socket) -> None:
        target = None
        try:
            target = socket.create_connection(
                (self.lease.target_host, self.lease.target_port), timeout=5)

            def _pump(src: socket.socket, dst: socket.socket) -> None:
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            t1 = threading.Thread(target=_pump, args=(client, target), daemon=True)
            t2 = threading.Thread(target=_pump, args=(target, client), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        except OSError:
            pass  # target unreachable: drop the connection, lease stays registered
        finally:
            for s in (client, target):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def stop(self) -> None:
        self._stopping.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None

    @property
    def bound(self) -> bool:
        return self._listener is not None


def proxy_pair(client: socket.socket, target: socket.socket) -> None:
    """Bidirectionally pump bytes between two sockets until either closes.

    Protocol-transparent: HTTP/HTTPS/WebSocket/SSE/HMR/generic TCP all flow
    through the same byte pipe. Used by both DevServiceForwarder (per-host
    mode) and the fabric gateway router.
    """
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t1 = threading.Thread(target=_pump, args=(client, target), daemon=True)
    t2 = threading.Thread(target=_pump, args=(target, client), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def _add_seconds(iso: str, seconds: int) -> str:
    from datetime import timedelta
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
