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

    def port_range(self) -> tuple[int, int]:
        """The gateway's reserved front-door port range (lo, hi).

        The gateway binds this whole range on its bind hosts, not just the
        ports that currently hold leases. Consumers (e.g. the fabric agent)
        must exclude the entire range, not merely the leased subset.
        """
        return self._lo, self._hi

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

    def _allocate_port(self, preferred: int | None = None,
                       exclude_id: str | None = None) -> int:
        """Allocate a collision-free external port.

        ``preferred`` is honored when it is a bindable dev port (>=1024) that
        is free in both the lease registry and the local OS — this is what
        makes ``localhost:4040 -> dev.lan:4040`` port-matching work by default.
        Falls back to the configured range when the preferred port is taken
        (e.g. another host already owns it) or unbindable (privileged).
        """
        if preferred is not None:
            if (preferred >= 1024
                    and not self._is_port_taken(preferred, exclude_id)
                    and _os_port_free(preferred)):
                return preferred
        for port in range(self._lo, self._hi + 1):
            if not self._is_port_taken(port, exclude_id) and _os_port_free(port):
                return port
        raise ConflictError("no free external dev port in configured range")

    def register(self, *, target_machine_id: str, target_host: str, target_port: int,
                 protocol: str = "http", owner_herdr_session_id: str | None = None,
                 owner_repository: str | None = None, owner_worktree_id: str | None = None,
                 owner_process_id: str | None = None, label: str | None = None,
                 lease_id: str | None = None,
                 preferred_port: int | None = None,
                 routes: list | None = None) -> DevServiceLease:
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
            # Port-matching is on by default: prefer target_port unless the
            # caller explicitly requested a different preferred port.
            if preferred_port is None:
                preferred_port = target_port
            port = self._allocate_port(preferred=preferred_port)
            lid = lease_id or f"lease_{uuid.uuid4().hex[:16]}"
            expires = _add_seconds(now, self._ttl)
            lease = DevServiceLease(
                id=lid, external_port=port, protocol=protocol,
                target_machine_id=target_machine_id, target_host=target_host,
                target_port=target_port, routes=routes,
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


# -- Same-origin app edge (path-routing forwarder) ---------------------------
# A dev app is often two (or more) services — e.g. a UI on one port and an API
# on another — but the browser wants ONE origin. The fabric is deliberately an
# L4 byte pipe (one external port -> one target) and stays that way; the
# OriginEdge adds the missing L7 hop: peek the first HTTP request line, pick the
# target by longest-prefix path match, then byte-pipe (reusing the same
# transparent pump, so WS/SSE/HMR still flow). Non-HTTP connections fall back to
# the lease's catch-all target instead of hanging.

_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH", "TRACE", "CONNECT"})


def _peek_request_path(client: socket.socket, timeout: float = 5.0,
                       max_bytes: int = 8192) -> tuple[str | None, bytes]:
    """Read the first HTTP request line.

    Returns ``(path, buffered)``: ``buffered`` is every byte read so far (to be
    replayed to the target) and ``path`` is the request target — or ``None`` when
    the bytes do not look like an HTTP request (e.g. a raw TLS ClientHello).
    """
    client.settimeout(timeout)
    buf = b""
    try:
        while b"\r\n" not in buf and len(buf) < max_bytes:
            chunk = client.recv(4096)
            if not chunk:
                break
            buf += chunk
    except (TimeoutError, OSError):
        pass
    finally:
        try:
            client.settimeout(None)
        except OSError:
            pass
    if not buf:
        return None, buf
    line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split()
    if len(parts) >= 2 and parts[0].upper() in _HTTP_METHODS:
        return parts[1], buf
    return None, buf


def path_matches(path: str, prefix: str) -> bool:
    """True when ``path`` is under ``prefix`` (``/api`` matches ``/api`` and
    ``/api/...`` but not ``/apix``; ``/`` matches everything)."""
    if prefix == "/":
        return True
    if path == prefix:
        return True
    return path.startswith(prefix.rstrip("/") + "/")


def match_target(path: str | None, routes, fallback_host: str, fallback_port: int):
    """Longest-prefix route match for ``path``; else the fallback target.

    ``path`` may be ``None`` (non-HTTP) -> fallback. Returns ``(host, port)``.
    """
    if path is None or not routes:
        return fallback_host, fallback_port
    best = None
    best_len = -1
    for r in routes:
        if path_matches(path, r.prefix) and len(r.prefix) > best_len:
            best = (r.host, r.port)
            best_len = len(r.prefix)
    return best if best is not None else (fallback_host, fallback_port)


def prefixed_pipe(client: socket.socket, target: socket.socket,
                  client_prefix: bytes = b"") -> None:
    """Like :func:`proxy_pair`, but first flushes ``client_prefix`` (the bytes
    already read while peeking) into the client->target direction."""
    def _pump(src: socket.socket, dst: socket.socket, prefix: bytes = b"") -> None:
        try:
            if prefix:
                dst.sendall(prefix)
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

    t1 = threading.Thread(target=_pump, args=(client, target, client_prefix), daemon=True)
    t2 = threading.Thread(target=_pump, args=(target, client), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    """Read up to the end of an HTTP head; returns (head incl. blank line, remainder)."""
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 65536:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    idx = buf.find(b"\r\n\r\n")
    if idx < 0:
        return buf, b""
    return buf[: idx + 4], buf[idx + 4:]


def rewriting_pipe(client: socket.socket, target: socket.socket, port: int, external: str) -> None:
    """Byte-pipe that filters the headers of the origin's responses.

    Each exchange is forced to ``Connection: close`` so every response passes the
    head filter. Upgrade requests (websockets) are passed through untouched."""
    from .urlrewrite import force_close, is_upgrade, rewrite_response_head
    req_head, rest = _read_head(client)
    upgrade = is_upgrade(req_head)
    if not upgrade and req_head.endswith(b"\r\n\r\n"):
        req_head = force_close(req_head[:-4]) + b"\r\n\r\n"
    if upgrade:
        prefixed_pipe(client, target, client_prefix=req_head + rest)
        return
    try:
        target.sendall(req_head + rest)
    except OSError:
        return

    def up() -> None:
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                target.sendall(data)
        except OSError:
            pass
        finally:
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t = threading.Thread(target=up, daemon=True)
    t.start()
    try:
        resp_head, body = _read_head(target)
        if resp_head.endswith(b"\r\n\r\n"):
            resp_head = rewrite_response_head(resp_head[:-4], port, external) + b"\r\n\r\n"
        client.sendall(resp_head + body)
        while True:
            data = target.recv(65536)
            if not data:
                break
            client.sendall(data)
    except OSError:
        pass
    finally:
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    t.join(timeout=5)


def route_and_pipe(client: socket.socket, routes, fallback_host: str,
                   fallback_port: int) -> None:
    """Peek the first request line, pick the target (longest-prefix route match,
    else fallback), and byte-pipe. The peeked bytes are replayed first, so the
    target sees the original request intact."""
    path, buffered = _peek_request_path(client)
    host, port = match_target(path, routes, fallback_host, fallback_port)
    target = socket.create_connection((host, port), timeout=5)
    try:
        prefixed_pipe(client, target, client_prefix=buffered)
    finally:
        try:
            target.close()
        except OSError:
            pass


class OriginEdge:
    """Binds a same-origin lease's external port and routes each connection to
    the matching route target (by URL path) or the lease's catch-all target.

    Same lifecycle as :class:`DevServiceForwarder` (``start``/``stop``/``bound``,
    private-only bind), but multi-target: the lease carries ``routes`` (path ->
    backend) and its own ``target_host``/``target_port`` are the fallback.
    Protocol-transparent after the initial request-line peek.
    """

    def __init__(self, lease: DevServiceLease, bind_host: str = "127.0.0.1",
                 private_only: bool = True) -> None:
        self.lease = lease
        self.bind_host = bind_host
        self.private_only = private_only
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> OriginEdge:
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
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self

    def _accept_loop(self) -> None:
        routes = self.lease.routes
        fb_host, fb_port = self.lease.target_host, self.lease.target_port
        while not self._stopping.is_set():
            try:
                client, _addr = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=route_and_pipe,
                             args=(client, routes, fb_host, fb_port),
                             daemon=True).start()

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


def forwarder_for(lease: DevServiceLease, bind_host: str = "127.0.0.1",
                  private_only: bool = True):
    """The right forwarder for a lease: a multi-target :class:`OriginEdge` when
    the lease carries ``routes`` (same-origin app), else the plain 1:1
    :class:`DevServiceForwarder."""
    if getattr(lease, "routes", None):
        return OriginEdge(lease, bind_host=bind_host, private_only=private_only)
    return DevServiceForwarder(lease, bind_host=bind_host, private_only=private_only)


def _add_seconds(iso: str, seconds: int) -> str:
    from datetime import timedelta
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
