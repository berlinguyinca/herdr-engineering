"""Transparent dev-fabric gateway (spec 0110, component #3).

The gateway is the fleet's single front door for the ``dev:<port>`` address
space. It owns:

- a **control API** (register / heartbeat / close / list / resolve) that host
  registrar commands use to publish leases into the shared registry;
- a **router** that dynamically binds each leased external port on the
  gateway's private interfaces and byte-proxies to the lease's
  ``target_host:target_port`` (protocol-transparent: HTTP/HTTPS/WebSocket/
  SSE/HMR/generic TCP);
- a **probe loop** that TCP-probes every active lease target; a healthy probe
  renews the lease, an unhealthy one does not, so TTL expiry + reconcile
  removes dead routes and frees the port.

Security: control API and router bind to loopback and/or the tailnet only
(private by default); the control API requires a bearer token (secret read
from the environment or a machine-local file, never from the repo).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .devfabric import DevServiceRegistry, proxy_pair
from .errors import ConflictError, UnreachableError

_FABRIC_TOKEN_FILE = os.path.join("~", ".config", "herdr-engineering", "fabric-token")


def read_fabric_token() -> str | None:
    tok = os.environ.get("HERDR_ENGINEERING_FABRIC_TOKEN")
    if tok:
        return tok
    try:
        with open(os.path.expanduser(_FABRIC_TOKEN_FILE)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False


def tailnet_ipv4() -> str | None:
    """Best-effort discovery of this host's Tailscale IPv4 (no dependency on
    the tailscale CLI being on PATH for the caller)."""
    import subprocess
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def magicdns_name() -> str | None:
    """Stable Tailscale MagicDNS name for this host (e.g. bender.<net>.ts.net)."""
    import subprocess
    try:
        out = subprocess.run(["tailscale", "status", "--json"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            data = json.loads(out.stdout)
            return data.get("Self", {}).get("DNSName", "").rstrip(".") or None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


class GatewayRouter:
    """Dynamically binds leased external ports and proxies to lease targets.

    A port is bound the moment a lease becomes active and unbound when the
    lease closes or expires, so the gateway never holds listeners for
    unleased ports.
    """

    def __init__(self, bind_hosts: list[str]) -> None:
        self._bind_hosts = [h for h in bind_hosts if h]
        self._listeners: dict[int, list[socket.socket]] = {}
        self._accept_threads: dict[int, list[threading.Thread]] = {}
        self._lock = threading.RLock()

    def bind(self, port: int, target_host: str, target_port: int) -> bool:
        with self._lock:
            if port in self._listeners:
                return True
            listeners: list[socket.socket] = []
            threads: list[threading.Thread] = []
            for host in self._bind_hosts:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind((host, port))
                    s.listen(64)
                    s.settimeout(0.5)
                except OSError:
                    continue  # interface not available here
                listeners.append(s)
                threads.append(threading.Thread(target=self._accept_loop,
                                                args=(s, port, target_host, target_port),
                                                daemon=True))
            if not listeners:
                for s in listeners:
                    s.close()
                return False
            self._listeners[port] = listeners
            self._accept_threads[port] = threads
            for t in threads:
                t.start()
            return True

    def _accept_loop(self, listener: socket.socket, port: int,
                     target_host: str, target_port: int) -> None:
        while True:
            try:
                client, _addr = listener.accept()
            except TimeoutError:
                with self._lock:
                    if port not in self._listeners:
                        break
                continue
            except OSError:
                break
            threading.Thread(target=self._proxy_one,
                             args=(client, target_host, target_port),
                             daemon=True).start()

    def _proxy_one(self, client: socket.socket, target_host: str,
                   target_port: int) -> None:
        target = None
        try:
            target = socket.create_connection((target_host, target_port), timeout=5)
            proxy_pair(client, target)
        except OSError:
            pass  # target down: drop connection; probe loop will expire the lease
        finally:
            for s in (client, target):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def unbind(self, port: int) -> bool:
        with self._lock:
            listeners = self._listeners.pop(port, None)
            threads = self._accept_threads.pop(port, [])
        if not listeners:
            return False
        for s in listeners:
            try:
                s.close()
            except OSError:
                pass
        # Join the accept loops: a close() while accept() is in flight can
        # still let ONE connection through, so wait for the threads to exit
        # before declaring the port released.
        for t in threads:
            t.join(timeout=2.0)
        return True

    def bound_ports(self) -> list[int]:
        with self._lock:
            return sorted(self._listeners)


class FabricGateway:
    """Control API + router + probe loop for the shared dev-fabric registry."""

    def __init__(self, registry: DevServiceRegistry,
                 router: GatewayRouter, control_host: str = "127.0.0.1",
                 control_port: int = 29999, url_base: str | None = None,
                 probe_interval: float = 15.0, token: str | None = None) -> None:
        self.registry = registry
        self.router = router
        self.control_host = control_host
        self.control_port = control_port
        # Stable base for the dev:<port> address (MagicDNS name preferred,
        # falling back to the tailnet IP or the control host).
        self.url_base = url_base or magicdns_name() or tailnet_ipv4() or control_host
        self.probe_interval = probe_interval
        self.token = token if token is not None else read_fabric_token()
        self._httpd: ThreadingHTTPServer | None = None
        self._probe_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sync_router()

    # -- router sync -------------------------------------------------------
    def _sync_router(self) -> None:
        """Bind all active leases and unbind ports with no active lease."""
        active = {lease.external_port: lease for lease in self.registry.active_leases()}
        for port, lease in active.items():
            if not self.router.bind(port, lease.target_host, lease.target_port):
                # stale binding to a different target: release so the next
                # cycle rebinds to the current one
                self.router.unbind(port)
        for port in self.router.bound_ports():
            if port not in active:
                self.router.unbind(port)

    # -- probe loop ---------------------------------------------------------
    def _probe(self) -> None:
        while not self._stop.is_set():
            try:
                # Pick up store changes made by an overlapping process
                # (e.g. a register served by a gateway that was mid-shutdown).
                self.registry.reload_if_changed()
                for lease in self.registry.active_leases():
                    ok, err = _tcp_probe(lease.target_host, lease.target_port)
                    self.registry.mark_health(lease.id, ok, err)
                    if ok:
                        self.registry.heartbeat(lease.id)  # healthy = renewed
                # Self-heal the router: bind active leases, unbind ports whose
                # lease expired/closed. (active_leases() reconciles internally,
                # so the expired ids are consumed there — syncing is the robust
                # way to release their ports.)
                self._sync_router()
            except Exception:
                pass  # probe cycle is best-effort; never kill the loop
            self._stop.wait(self.probe_interval)

    # -- lifecycle -----------------------------------------------------------
    def _lock_path(self) -> Path:
        return self.registry.store_path.parent / "gateway.lock"

    def _acquire_lock(self) -> None:
        """Refuse to run two gateways over one registry store.

        Lock content is ``pid:nonce`` so two gateway instances in the same
        process (tests) are also distinguished, while a same-pid restart
        after a crash can still take over a stale lock.
        """
        lock_path = self._lock_path()
        if lock_path.exists():
            try:
                old_pid, _, _nonce = (lock_path.read_text() or "").strip().partition(":")
                old_pid = int(old_pid or 0)
            except (ValueError, OSError):
                old_pid = 0
            if old_pid:
                if old_pid == os.getpid():
                    raise ConflictError(
                        "another fabric gateway instance is already running in "
                        "this process; stop it first")
                if _pid_alive(old_pid):
                    raise ConflictError(
                        f"another fabric gateway is already running (pid {old_pid}); "
                        f"stop it or wait for it to exit")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(f"{os.getpid()}:{uuid.uuid4().hex[:8]}")
        except OSError:
            pass  # lock is best-effort; the store remains the source of truth

    def _release_lock(self) -> None:
        lock_path = self._lock_path()
        try:
            content = (lock_path.read_text() or "").strip()
            if content.startswith(f"{os.getpid()}:"):
                lock_path.unlink()
        except (OSError, ValueError):
            pass

    def start(self) -> FabricGateway:
        self._acquire_lock()
        self._sync_router()
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.control_host, self.control_port), handler)
        # port 0 lets the OS pick a free control port (tests)
        self.control_port = self._httpd.server_address[1]
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self._probe_thread = threading.Thread(target=self._probe, daemon=True)
        self._probe_thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._release_lock()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        for port in self.router.bound_ports():
            self.router.unbind(port)

    # -- API ops (called by the HTTP handler) --------------------------------
    def op_register(self, body: dict[str, Any]) -> dict[str, Any]:
        lease = self.registry.register(
            target_machine_id=body["target_machine_id"],
            target_host=body["target_host"],
            target_port=int(body["target_port"]),
            protocol=body.get("protocol", "http"),
            owner_herdr_session_id=body.get("owner_herdr_session_id"),
            owner_repository=body.get("owner_repository"),
            owner_worktree_id=body.get("owner_worktree_id"),
            owner_process_id=body.get("owner_process_id"),
            label=body.get("label"),
            lease_id=body.get("lease_id"))
        self.router.bind(lease.external_port, lease.target_host, lease.target_port)
        return {"lease": lease.to_dict(), "url": self.stable_url(lease.external_port)}

    def op_heartbeat(self, lease_id: str) -> dict[str, Any]:
        lease = self.registry.heartbeat(lease_id)
        return {"ok": lease is not None,
                "lease": lease.to_dict() if lease else None}

    def op_close(self, lease_id: str) -> dict[str, Any]:
        lease = self.registry.get(lease_id)
        ok = self.registry.close(lease_id)
        if ok and lease is not None:
            self.router.unbind(lease.external_port)
        return {"ok": ok}

    def op_list(self) -> dict[str, Any]:
        leases = self.registry.active_leases()
        return {"leases": [dict(lease.to_dict(), url=self.stable_url(lease.external_port))
                           for lease in leases]}

    def op_resolve(self, port: int) -> dict[str, Any]:
        lease = self.registry.resolve_port(port)
        return {"port": port,
                "lease": lease.to_dict() if lease else None,
                "url": self.stable_url(port) if lease else None}

    def stable_url(self, port: int) -> str:
        return f"http://{self.url_base}:{port}"


def _tcp_probe(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, str(exc)


def _make_handler(gateway: FabricGateway):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence request logging
            pass

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not gateway.token:
                return True  # token-less mode (loopback-only deployments)
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {gateway.token}"

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            if self.path == "/list":
                self._send(200, gateway.op_list())
                return
            if self.path.startswith("/resolve/"):
                try:
                    port = int(self.path.rsplit("/", 1)[1])
                except ValueError:
                    self._send(400, {"error": "bad port"})
                    return
                self._send(200, gateway.op_resolve(port))
                return
            if self.path == "/healthz":
                self._send(200, {"ok": True, "bound_ports": gateway.router.bound_ports(),
                                 "url_base": gateway.url_base})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send(400, {"error": "invalid json"})
                return
            if self.path == "/register":
                try:
                    self._send(200, gateway.op_register(body))
                except (KeyError, ValueError) as exc:
                    self._send(400, {"error": f"bad register body: {exc}"})
                return
            if self.path == "/heartbeat":
                lease_id = body.get("lease_id", "")
                self._send(200, gateway.op_heartbeat(lease_id))
                return
            if self.path == "/close":
                lease_id = body.get("lease_id", "")
                self._send(200, gateway.op_close(lease_id))
                return
            self._send(404, {"error": "not found"})

    return _Handler


class FabricClient:
    """Client used by host registrar commands to publish leases to the gateway."""

    def __init__(self, gateway_url: str, token: str | None = None) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.token = token if token is not None else read_fabric_token()

    def _req(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = self.gateway_url + path
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token or ''}",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise UnreachableError(f"fabric gateway error {exc.code} for {url}",
                                   target=url) from exc
        except Exception as exc:
            raise UnreachableError(f"fabric gateway unreachable: {exc}", target=url) from exc

    def register(self, **kwargs: Any) -> dict[str, Any]:
        return self._req("POST", "/register", kwargs)

    def heartbeat(self, lease_id: str) -> dict[str, Any]:
        return self._req("POST", "/heartbeat", {"lease_id": lease_id})

    def close(self, lease_id: str) -> dict[str, Any]:
        return self._req("POST", "/close", {"lease_id": lease_id})

    def list(self) -> dict[str, Any]:
        return self._req("GET", "/list")

    def resolve(self, port: int) -> dict[str, Any]:
        return self._req("GET", f"/resolve/{port}")

    def healthz(self) -> dict[str, Any]:
        return self._req("GET", "/healthz")


def gateway_url_from_config(config: dict[str, Any]) -> str | None:
    """Control-plane URL for the fabric gateway from config.

    ``fabric.gateway_url`` may be the full control URL (http://host:port) or
    just the stable base (host or MagicDNS name); a bare base is given the
    default control port. Empty/absent means "no remote gateway" (local mode).
    """
    fabric = config.get("fabric") or {}
    # The environment variable wins: it lets operators point at a gateway
    # without editing config (secret-free; the URL is not a credential).
    base = os.environ.get("HERDR_ENGINEERING_FABRIC_URL") or fabric.get("gateway_url")
    if not base:
        return None
    base = base.rstrip("/")
    if base.startswith("http://") or base.startswith("https://"):
        return base
    return f"http://{base}:{fabric.get('control_port', 29999)}"
