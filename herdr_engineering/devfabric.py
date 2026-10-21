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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .contracts import DevServiceLease
from .errors import ConflictError

_LO = 18000
_HI = 28999

# Expired/failed leases older than this are dropped from the store on load and
# during reconciliation. Without a retention window the store grows without
# bound (observed: 6,551 leases, 6,408 of them expired -> a 3.8 MB JSON file),
# and every heartbeat rewrites the whole blob, burning a core. Pruning keeps
# the store proportional to live services.
_RETENTION_SECONDS = 24 * 60 * 60  # keep expired leases for 24h


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _parse_time(iso: str | None):
    if not iso:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


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
        self._dirty = False
        self._load()

    def _prune_expired(self, now=None) -> int:
        """Drop expired/failed leases older than the retention window.

        Returns the number pruned. Cheap when the store is already small (the
        common case after the first prune); this is what stops the store from
        growing without bound over weeks of agent churn.
        """
        cutoff = _parse_time(now or _now_iso()) - timedelta(seconds=_RETENTION_SECONDS)
        with self._lock:
            doomed = [lid for lid, lease in self._leases.items()
                      if lease.state in ("expired", "failed")
                      and _parse_time(lease.expires_at or lease.created_at) < cutoff]
            for lid in doomed:
                del self._leases[lid]
        if doomed:
            self._dirty = True
        return len(doomed)

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
        self._prune_expired()

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
        self._prune_expired()
        return True

    def gc(self) -> dict:
        """Drop expired/failed leases past the retention window and persist.

        One-shot maintenance path (CLI ``devfabric gc``) to shrink a store that
        accumulated garbage before pruning existed. Returns stats.
        """
        before = len(self._leases)
        pruned = self._prune_expired()
        self.persist()
        return {"pruned": pruned, "before": before, "after": len(self._leases)}

    def persist(self) -> None:
        """Write the store only if something changed since the last write.

        Batching callers (the gateway's probe loop) mark many leases dirty and
        call this once, instead of writing the whole store per lease (which
        burned a core when the store grew to thousands of leases).
        """
        with self._lock:
            if not self._dirty:
                return
            self._write_store()
            self._dirty = False

    def _write_store(self) -> None:
        try:
            payload = {"leases": [lease.to_dict() for lease in self._leases.values()]}
            tmp = self._path.with_suffix(".tmp")
            # Compact JSON (no indent) keeps the store small and the serialization
            # cheap; it is parsed back with json.loads, so the format is internal.
            tmp.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
            os.replace(tmp, self._path)
            try:
                self._loaded_mtime = self._path.stat().st_mtime
            except OSError:
                pass
        except OSError:
            pass

    def _persist(self) -> None:
        """Legacy immediate-write helper; equivalent to persist() with force."""
        with self._lock:
            self._dirty = True
        self.persist()



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

    def renew_and_mark(self, lease_id: str, ok: bool, error: str | None = None) -> bool:
        """In-memory probe outcome + TTL renewal for one lease (no persist).

        Returns True if anything changed. The gateway's probe loop calls this
        for every active lease and then ``persist()`` once, so N lease heartbeats
        cost one store write instead of N full-store rewrites.
        """
        now = _now_iso()
        changed = False
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or lease.state != "active":
                return False
            if ok:
                if lease.health_last_ok_at is None:
                    changed = True
                lease.health_last_ok_at = now
                if lease.health_last_error is not None:
                    changed = True
                lease.health_last_error = None
            else:
                if lease.health_last_error != error:
                    changed = True
                lease.health_last_error = error
            # Renew the TTL ONLY on a healthy probe. An unhealthy target must not
            # be renewed, otherwise it never expires and the route never unbinds.
            if ok:
                new_expires = _add_seconds(now, self._ttl)
                if lease.expires_at != new_expires:
                    changed = True
                    lease.renewed_at = now
                    lease.expires_at = new_expires
        if changed:
            with self._lock:
                self._dirty = True
        return changed

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
        # Drop expired leases past the retention window so the store stays
        # proportional to live services instead of accumulating garbage.
        self._prune_expired(now)
        if expired:
            self._persist()
        elif self._dirty:
            # prune_expired may have dropped leases; persist the smaller store.
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

    Issue #76: every per-connection step is bounded (connect, peek, pipe) and
    worker failures are non-fatal — a dead or stuck worker can never hold the
    accept/route path or starve subsequent requests.
    """

    def __init__(self, lease: DevServiceLease, bind_host: str = "127.0.0.1",
                 private_only: bool = True,
                 inactivity_timeout: float = 30.0) -> None:
        self.lease = lease
        self.bind_host = bind_host
        self.private_only = private_only
        self.inactivity_timeout = inactivity_timeout
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
        """Route ONE connection to the lease target (bounded; issue #76).

        Failures here are per-connection and non-fatal: log, close the client
        socket and return. The accept loop is a separate thread, so no worker
        can ever wedge the forwarder.
        """
        target = None
        try:
            # Bounded dial (was already 5s, kept explicit).
            target = socket.create_connection(
                (self.lease.target_host, self.lease.target_port), timeout=5)
            proxy_pair(
                client, target,
                inactivity_timeout=self.inactivity_timeout,
            )
        except OSError as exc:
            # Target unreachable: fail the client cleanly (502 when it is an
            # HTTP client) instead of silently resetting; lease stays
            # registered. (Issue #76: a down backend must not hang a request.)
            _log_devfabric("devfabric", "WARNING",
                           "forwarder backend unreachable for %s:%s: %s",
                           self.lease.target_host, self.lease.target_port, exc)
            _send_http_502(client)
        except Exception as exc:  # never let a worker die unlogged (issue #76b)
            _log_devfabric("devfabric", "ERROR",
                           "forwarder connection worker failed for %s:%s: %r",
                           self.lease.target_host, self.lease.target_port, exc)
        finally:
            for s in (client, target):
                if s is not None:
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
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


def proxy_pair(client: socket.socket, target: socket.socket,
               inactivity_timeout: float = 30.0) -> None:
    """Bidirectionally pump bytes between two sockets until either closes.

    Protocol-transparent: HTTP/HTTPS/WebSocket/SSE/HMR/generic TCP all flow
    through the same byte pipe. Used by both DevServiceForwarder (per-host
    mode) and the fabric gateway router.

    Issue #76: the pipe is BOUNDED. Each direction arms an ``inactivity_timeout``
    on ``recv`` (re-armed after every op); a peer that accepts the TCP
    handshake but never reads or responds ends the pipe in both directions
    after the timeout instead of blocking the worker forever. Both directions
    are joined with a bounded ``join`` so a leaked pump cannot hang the caller,
    and both sockets are shut down and closed when the pipe ends.
    """
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            src.settimeout(inactivity_timeout)
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except (OSError, TimeoutError, ValueError):
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
    # Bounded joins (issue #76c): once the activity deadline passes, give up
    # waiting on any leaked pump — daemon threads cannot hold the accept/route
    # path.
    deadline = inactivity_timeout + 10.0
    t1.join(timeout=deadline)
    t2.join(timeout=deadline)
    for s in (client, target):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass


def _log_devfabric(component: str, level: str, msg: str, *args) -> None:
    """Best-effort structured log line for devfabric data-path events."""
    import logging
    try:
        logging.getLogger(component).log(
            getattr(logging, level, logging.INFO), msg, *args)
    except Exception:
        pass


def _send_http_502(client: socket.socket, reason: str = "backend unavailable") -> None:
    """Send a short HTTP 502 so HTTP clients fail cleanly instead of seeing a
    raw TCP reset. Best effort: non-HTTP clients just get a close."""
    body = f"{reason}\n".encode()
    head = (
        b"HTTP/1.1 502 Bad Gateway\r\n"
        b"Content-Type: text/plain\r\n"
        b"Connection: close\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
    )
    try:
        client.settimeout(2.0)
        client.sendall(head + body)
    except OSError:
        pass
    finally:
        try:
            client.settimeout(None)
        except OSError:
            pass


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
                  client_prefix: bytes = b"", inactivity_timeout: float = 30.0,
                  connect_timeout: float = 5.0) -> None:
    """Like :func:`proxy_pair`, but first flushes ``client_prefix`` (the bytes
    already read while peeking) into the client->target direction.

    Issue #76: the pipe MUST be bounded. A connection whose peer accepts the
    TCP handshake but never reads or responds (e.g. a wedged or crashed
    backend) used to block the worker thread forever inside ``recv``/``sendall``
    — and because the forwarder's accept/route path is single-threaded, that
    one stuck worker wedged the whole forwarder. Now:

    * each pump direction has an ``inactivity_timeout`` on ``recv``; a peer that
      sends nothing (or whose ``sendall`` cannot drain) for that long ends the
      pipe in both directions. ``timeout`` is re-armed after every operation.
    * ``connect_timeout`` bounds the initial ``target.connect_ex``.
    * both directions are joined with a bounded ``join`` so a leaked pump can
      never hang the caller (pumps are daemon threads).
    """
    def _pump(src: socket.socket, dst: socket.socket, prefix: bytes = b"") -> None:
        try:
            src.settimeout(inactivity_timeout)
            if prefix:
                dst.sendall(prefix)
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except (OSError, TimeoutError, ValueError):
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
    # Bounded joins: once one direction is done, give the other a grace period,
    # then move on. Pumps are daemon threads, so an unjoinable pump cannot
    # hold the accept/route path (issue #76c).
    t1.join(timeout=inactivity_timeout + connect_timeout + 10.0)
    t2.join(timeout=inactivity_timeout + connect_timeout + 10.0)
    for s in (client, target):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass


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
                   fallback_port: int, inactivity_timeout: float = 30.0,
                   peek_timeout: float = 5.0) -> None:
    """Peek the first request line, pick the target (longest-prefix route match,
    else fallback), and byte-pipe. The peeked bytes are replayed first, so the
    target sees the original request intact.

    Issue #76: both the peek and the full pipe are bounded, and per-connection
    failures are non-fatal (log + close the client; never hang). A client that
    connects but never sends a full request line gives up its worker after
    ``peek_timeout`` (falling back to the catch-all target) instead of holding
    it forever; a backend that accepts but never reads ends the pipe after
    ``inactivity_timeout``.
    """
    try:
        path, buffered = _peek_request_path(client, timeout=peek_timeout)
        host, port = match_target(path, routes, fallback_host, fallback_port)
        try:
            target = socket.create_connection((host, port), timeout=5)
        except OSError as exc:
            _log_devfabric("devfabric", "WARNING",
                           "route_and_pipe: target %s:%s unreachable: %s",
                           host, port, exc)
            _send_http_502(client)
            return
        prefixed_pipe(client, target, client_prefix=buffered,
                      inactivity_timeout=inactivity_timeout)
    except Exception as exc:  # worker failures must be non-fatal (issue #76b)
        _log_devfabric("devfabric", "ERROR",
                       "route_and_pipe worker failed: %r", exc)
    finally:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.close()
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
