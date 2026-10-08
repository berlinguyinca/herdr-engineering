"""Per-host auto-registration agent for the dev fabric (spec 0110).

Watches for web services that start on THIS host and registers them with the
fabric gateway automatically, so any web service on any fleet host appears at
a stable ``dev:<port>`` URL with zero manual steps, and shows up in the web
UI on the gateway host.

Model
-----
- Every ``interval`` seconds the agent scans the host's listening TCP ports.
- A listener is a *candidate web service* when a TCP connect to
  ``127.0.0.1:<port>`` succeeds AND an HTTP ``GET /`` returns status < 500
  (a 404 still means "this is an HTTP server"). Raw-TCP services (databases,
  RPC) fail the HTTP probe and are never registered.
- If the service is already reachable via the host's tailnet IP (bound to
  ``0.0.0.0`` or the tailnet interface) it is registered directly.
- Loopback-only services (the common dev-server default) get an
  *agent forwarder*: an ephemeral listener bound to the tailnet IP that
  byte-proxies to ``127.0.0.1:<port>`` (same protocol-transparent pipe as the
  gateway router). The forwarder's port is what gets registered.
- When a tracked service disappears the lease is closed and the forwarder
  stopped, freeing the ``dev:<port>`` port fleet-wide.

Private-by-default invariants
-----------------------------
- Forwarders bind the host's tailnet IP only (never ``0.0.0.0``, never a
  public address).
- Control-plane traffic uses the existing ``FabricClient`` (tailnet URL +
  bearer token via ``HERDR_ENGINEERING_FABRIC_TOKEN`` / fabric-token file).
- The agent never touches Herdr-native state paths; its own store lives under
  the herdr-engineering state dir (``fabric-agent.json``).
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import HerdrEngineeringError

log = logging.getLogger(__name__)


# Ports that answer HTTP but are NOT dev services — well-known infrastructure
# daemons. Registering them would only pollute the fabric with links nobody
# asked for. Config ``fabric.agent_exclude_ports`` extends this set.
DEFAULT_DENY_PORTS: frozenset[int] = frozenset({
    631,    # CUPS (IPP)
    1933,   # restic REST server
    9090,   # Prometheus / Cockpit
    9091,   # Alertmanager / alternate web
    9100,   # node_exporter
    9180,   # windows_exporter
    9200,   # Elasticsearch
    9300,   # Elasticsearch cluster transport
    9500,   # Netdata
})
# Ports below ``AgentConfig.web_port_min`` that are still web services.
COMMON_WEB_PORTS: frozenset[int] = frozenset({80, 443, 8080, 8443})


# ---------------------------------------------------------------------------
# Listener scanning (per-OS, best effort, no dependencies)
# ---------------------------------------------------------------------------

def _scan_linux() -> dict[int, dict[str, Any]]:
    """Parse ``ss -ltnpH`` into {port: {"addrs": set, "process": str|None}}."""
    exe = shutil.which("ss")
    if exe is None:
        log.warning("ss not found; listener scan disabled on this host")
        return {}
    try:
        out = subprocess.run([exe, "-ltnpH"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ss scan failed: %s", exc)
        return {}
    result: dict[int, dict[str, Any]] = {}
    proc_re = re.compile(r'\(\("([^"]+)",pid=')
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[3]
        proc = proc_re.search(line)
        entry = result.setdefault(_port_of(local),
                                  {"addrs": set(), "process": None})
        if entry["process"] is None and proc:
            entry["process"] = proc.group(1)
        entry["addrs"].add(_host_of(local))
    return result


def _scan_macos() -> dict[int, dict[str, Any]]:
    """Parse ``lsof -nP -iTCP -sTCP:LISTEN`` into the same shape."""
    exe = shutil.which("lsof")
    if exe is None:
        log.warning("lsof not found; listener scan disabled on this host")
        return {}
    try:
        out = subprocess.run([exe, "-nP", "-iTCP", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("lsof scan failed: %s", exc)
        return {}
    result: dict[int, dict[str, Any]] = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        name = parts[8]  # e.g. "127.0.0.1:5678", "*:8080", "[::1]:8080"
        port = _port_of(name)
        proc = parts[0] or None
        entry = result.setdefault(port, {"addrs": set(), "process": None})
        if entry["process"] is None and proc:
            entry["process"] = proc
        entry["addrs"].add(_host_of(name))
    return result


def _port_of(addr: str) -> int:
    """Extract the port from a 'host:port' / '[v6]:port' / '*:port' string."""
    host, _, port = addr.rpartition(":")
    try:
        return int(port)
    except ValueError:
        return -1


def _host_of(addr: str) -> str:
    host = addr.rpartition(":")[0]
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host or "*"


def scan_listening_ports() -> dict[int, dict[str, Any]]:
    """All listening TCP listeners on this host.

    Returns ``{port: {"addrs": set[str], "process": str | None}}``.
    ``addrs`` holds bind addresses; ``"*"``/``0.0.0.0``/``"::"`` mean
    "all interfaces". Best effort: an empty dict when the scanner is
    unavailable (the agent simply finds nothing that cycle).
    """
    if os.uname().sysname == "Darwin":
        return _scan_macos()
    return _scan_linux()


# ---------------------------------------------------------------------------
# Web-service probe
# ---------------------------------------------------------------------------

def probe_web(port: int, timeout: float = 2.0) -> bool:
    """True when 127.0.0.1:<port> speaks HTTP (any status < 500).

    A raw-TCP service (database, RPC) fails the HTTP handshake and is
    reported as not a web service.
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                     method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500  # 404/405 still means "HTTP server here"
    except http.client.HTTPException:
        # Banner-first non-HTTP services (SSH, FTP, SMTP, ...) make the HTTP
        # parser raise BadStatusLine/RemoteDisconnected — not a web service.
        return False
    except (OSError, ValueError):
        return False


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Agent forwarder (loopback-only apps -> tailnet-reachable ephemeral port)
# ---------------------------------------------------------------------------

class AgentForwarder:
    """Binds ``bind_host:0`` (OS-allocated port) and byte-proxies to
    ``127.0.0.1:<target_port>``. Bound to the tailnet IP only — never
    public, never 0.0.0.0.
    """

    def __init__(self, bind_host: str, target_port: int,
                 bind_port: int = 0, private_only: bool = True) -> None:
        self.bind_host = bind_host
        self.target_port = target_port
        # bind_port=0 => OS-allocated ephemeral port; a specific bind_port
        # makes the forwarder reuse the SAME port so port-matching works
        # (localhost:4040 -> dev.lan:4040). Callers fall back to 0 on EADDRINUSE.
        self.bind_port = bind_port
        self.private_only = private_only
        self._listener: socket.socket | None = None
        self._stopping = threading.Event()

    @property
    def port(self) -> int:
        if self._listener is None:
            raise HerdrEngineeringError("forwarder not started")
        return self._listener.getsockname()[1]

    def start(self) -> AgentForwarder:
        if self.private_only:
            from .observability import _is_public_ip
            if _is_public_ip(self.bind_host):
                raise HerdrEngineeringError(
                    f"refusing public bind address {self.bind_host} "
                    "(dev fabric is private-only)")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_host, self.bind_port))
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
                threading.Thread(target=self._proxy, args=(client,),
                                 daemon=True).start()

        threading.Thread(target=_accept_loop, daemon=True).start()
        return self

    def _proxy(self, client: socket.socket) -> None:
        target: socket.socket | None = None
        try:
            target = socket.create_connection(
                ("127.0.0.1", self.target_port), timeout=5)
            from .devfabric import proxy_pair
            proxy_pair(client, target)
        except OSError:
            pass  # app gone: drop the connection
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


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    tailnet_ip: str
    store_path: Path
    exclude_ports: frozenset[int] = frozenset()
    label_prefix: str = ""
    interval: float = 5.0
    probe_timeout: float = 2.0
    # Noise filter for auto-registration. A port is a candidate only when it
    # is a non-privileged (>= web_port_min, or a common web port) non-ephemeral
    # (< ephemeral_min) port that is not in exclude_ports or DEFAULT_DENY_PORTS.
    # Dev servers pick ports in the registered (>=1024) range below the OS
    # ephemeral range; system daemons and debug/agent endpoints fall outside.
    web_port_min: int = 1024
    ephemeral_min: int = 32768
    deny_ports: frozenset[int] = DEFAULT_DENY_PORTS
    # Gateway lease TTL (seconds). Tracked leases are heartbeated at half this
    # interval so they never expire; a lease the gateway dropped (restart or
    # TTL lapse) is transparently re-registered. Matches the gateway default.
    heartbeat_ttl: float = 90.0


class FabricAgent:
    """Detects web services on this host and keeps the gateway's lease set in
    sync with them (register on appear, close on disappear).
    """

    def __init__(self, client: Any, config: AgentConfig) -> None:
        self._client = client
        self.cfg = config
        self._machine = socket.gethostname()
        self._store: dict[str, dict[str, Any]] = self._load_store()
        self._forwards: dict[int, AgentForwarder] = {}
        self._lock = threading.Lock()
        # Restore forwarders for loopback-only services survived a restart.
        for port_s, entry in self._store.items():
            if entry.get("fwd_port"):
                fwd = AgentForwarder(config.tailnet_ip, int(port_s))
                try:
                    fwd.start()
                    # Rebind to the remembered port when still free so the
                    # registered target keeps working across restarts.
                    if fwd.port != entry["fwd_port"]:
                        fwd.stop()
                        fwd = self._rebind(fwd, entry["fwd_port"], int(port_s))
                        if fwd is None:
                            continue
                    self._forwards[int(port_s)] = fwd
                except OSError as exc:
                    log.warning("could not restore forwarder for port %s: %s",
                                port_s, exc)

    # -- store ----------------------------------------------------------

    def _load_store(self) -> dict[str, dict[str, Any]]:
        if self.cfg.store_path.exists():
            try:
                data = json.loads(self.cfg.store_path.read_text())
                if isinstance(data, dict):
                    return data
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("agent store unreadable (%s); starting fresh", exc)
        return {}

    def _save_store(self) -> None:
        self.cfg.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._store, indent=2))
        tmp.replace(self.cfg.store_path)

    def _rebind(self, fwd: AgentForwarder, port: int,
                target_port: int) -> AgentForwarder | None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.cfg.tailnet_ip, port))
            sock.close()
        except OSError:
            log.warning("forward port %d no longer free; re-registering", port)
            return None
        # Bind the exact port via a dedicated listener.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.cfg.tailnet_ip, port))
        sock.listen(64)
        sock.settimeout(0.5)
        fwd._listener = sock  # noqa: SLF001 - reuse accept-loop mechanics

        def _accept_loop() -> None:
            while not fwd._stopping.is_set():  # noqa: SLF001
                try:
                    client, _addr = fwd._listener.accept()  # noqa: SLF001
                except TimeoutError:
                    continue
                except OSError:
                    break
                threading.Thread(target=fwd._proxy, args=(client,),  # noqa: SLF001
                                 daemon=True).start()

        threading.Thread(target=_accept_loop, daemon=True).start()
        return fwd

    # -- scan -----------------------------------------------------------

    def scan_once(self) -> dict[str, list[str]]:
        """One detect/register/close cycle. Returns a summary."""
        listeners = scan_listening_ports()
        live_ports = set(listeners)
        registered: list[str] = []
        closed: list[str] = []

        # Ports the gateway itself binds (its dev:<port> front-door range) are
        # never adoption candidates. On the GATEWAY host these are live local
        # HTTP listeners (the router proxies them); registering one would make
        # the agent proxy its own front door — a feedback loop. Best effort:
        # an empty set when the gateway is momentarily unreachable.
        gateway_ext_ports: set[int] = set()
        try:
            for lease in self._client.list().get("leases", []):
                ep = lease.get("external_port")
                if ep:
                    gateway_ext_ports.add(int(ep))
        except Exception as exc:  # noqa: BLE001 - best effort
            log.debug("gateway external-port lookup failed: %s", exc)

        # Ports owned by this agent's own forwarders are never candidates —
        # they proxy an already-tracked service (double-registration guard).
        with self._lock:
            fwd_ports = {e["fwd_port"] for e in self._store.values()
                         if e.get("fwd_port")}
            # 1) release vanished services
            for port_s in list(self._store):
                port = int(port_s)
                if port not in live_ports:
                    self._release(port_s)
                    closed.append(port_s)

        # 2) adopt new web services
        for port in sorted(live_ports):
            if port <= 0 or not self._is_dev_web_port(port):
                continue
            if port in gateway_ext_ports:
                # Skip only when the local listener is already reachable on the
                # tailnet (gateway router front-door / a 0.0.0.0-bound service).
                # A loopback-only dev server on a port that another host also
                # uses must still be adopted (it needs its own forwarder).
                addrs = (listeners[port].get("addrs") or set())
                if ("*" in addrs or "0.0.0.0" in addrs or "::" in addrs
                        or self.cfg.tailnet_ip in addrs):
                    continue
            with self._lock:
                if (str(port) in self._store
                        or port in fwd_ports):
                    continue
            if not probe_web(port, timeout=self.cfg.probe_timeout):
                continue
            entry = self._adopt(port, listeners[port])
            if entry:
                registered.append(entry["label"])

        # 3) keepalive: heartbeat leases that are due; if the gateway dropped
        # one (restart or TTL lapse), transparently re-register the same
        # target so the dev:<port> mapping never goes stale.
        now = time.monotonic()
        with self._lock:
            due = [(p, e) for p, e in self._store.items()
                   if now - e.get("last_hb", 0.0) >= self.cfg.heartbeat_ttl / 2
                   and e.get("lease_id")]
        for port_s, entry in due:
            self._keepalive(port_s, entry)

        self._save_store()
        return {"registered": registered, "closed": closed}

    def _keepalive(self, port_s: str, entry: dict[str, Any]) -> None:
        """Heartbeat a tracked lease; re-register it if the gateway lost it."""
        lease_id = entry.get("lease_id")
        try:
            # The gateway answers 200 {"ok": false} for an unknown/expired
            # lease (it does NOT 404), so success must be read from the body,
            # not inferred from the absence of an exception.
            res = self._client.heartbeat(lease_id)
            if res.get("ok", True):
                entry["last_hb"] = time.monotonic()
                return
            log.info("lease %s (port %s) gone (gateway: no such lease); "
                     "re-registering", lease_id, port_s)
        except Exception as exc:
            log.info("lease %s (port %s) not alive (%s); re-registering",
                     lease_id, port_s, exc)
        # Lease gone (gateway restart / TTL lapse). Re-register the SAME target
        # (forwarder, if any, stays bound) so the dev:<port> URL is preserved.
        try:
            result = self._client.register(
                target_machine_id=self._machine,
                target_host=entry["target_host"],
                target_port=entry["target_port"],
                protocol="http", label=entry.get("label", ""),
                owner_process_id=entry.get("process") or "",
                lease_id=lease_id)
            lease = result.get("lease") or result
            entry["lease_id"] = lease.get("id", lease_id)
            entry["external_port"] = lease.get("external_port",
                                               entry.get("external_port"))
            entry["url"] = result.get("url", entry.get("url"))
            entry["last_hb"] = time.monotonic()
            log.info("re-registered port %s -> dev port %s",
                     port_s, entry["external_port"])
        except Exception as exc:  # gateway still down: retry next cycle
            log.warning("re-register failed for port %s: %s", port_s, exc)

    def _is_dev_web_port(self, port: int) -> bool:
        """Noise filter: is this port a candidate dev web service?

        Excludes explicit/config exclusions and well-known infrastructure
        daemons; excludes privileged ports (system services) except common
        web ports; excludes the OS ephemeral range (debug/agent endpoints).
        """
        if port in self.cfg.exclude_ports or port in self.cfg.deny_ports:
            return False
        if port in COMMON_WEB_PORTS:
            return True
        if port < self.cfg.web_port_min:
            return False
        if port >= self.cfg.ephemeral_min:
            return False
        return True

    def _adopt(self, port: int,
               info: dict[str, Any]) -> dict[str, Any] | None:
        process = info.get("process")
        label = (f"{self.cfg.label_prefix}"
                 f"{process or 'web'} on {self._machine} (port {port})")
        fwd: AgentForwarder | None = None
        if tcp_open(self.cfg.tailnet_ip, port):
            target_host, target_port = self.cfg.tailnet_ip, port
        else:
            # Loopback-only: put a tailnet-bound forwarder in front of it.
            # Prefer reusing the SAME port (port-matching), falling back to an
            # ephemeral port when that exact tailnet port is already bound.
            for bind_port in (port, 0):
                try:
                    fwd = AgentForwarder(self.cfg.tailnet_ip, port,
                                         bind_port=bind_port).start()
                    break
                except OSError:
                    fwd = None
                    continue
            if fwd is None:
                log.warning("no forwarder for loopback service on %d", port)
                return None
            target_host, target_port = self.cfg.tailnet_ip, fwd.port

        try:
            result = self._client.register(
                target_machine_id=self._machine, target_host=target_host,
                target_port=target_port, protocol="http", label=label,
                owner_process_id=process or "")
        except Exception as exc:  # gateway down / rejected: retry next cycle
            log.warning("register failed for port %d: %s", port, exc)
            if fwd is not None:
                fwd.stop()
            return None

        # The gateway nests the lease under "lease" and adds a stable "url".
        lease = result.get("lease") or result
        with self._lock:
            if fwd is not None:
                self._forwards[port] = fwd
            self._store[str(port)] = {
                "lease_id": lease.get("id"),
                "external_port": lease.get("external_port"),
                "url": result.get("url"),
                "target_host": target_host,
                "target_port": target_port,
                "fwd_port": fwd.port if fwd is not None else None,
                "label": label,
                "process": process,
                "last_hb": time.monotonic(),
            }
        log.info("auto-registered %s -> dev port %s",
                 label, lease.get("external_port"))
        return self._store[str(port)]

    def _release(self, port_s: str) -> None:
        entry = self._store.pop(port_s, None)
        port = int(port_s)
        fwd = self._forwards.pop(port, None)
        if fwd is not None:
            fwd.stop()
        if entry and entry.get("lease_id"):
            try:
                self._client.close(entry["lease_id"])
                log.info("auto-closed lease %s (port %s gone)",
                         entry["lease_id"], port_s)
            except Exception as exc:  # already expired, etc.
                log.warning("close failed for %s: %s", entry["lease_id"], exc)

    # -- run loop ---------------------------------------------------------

    def run(self, stop: threading.Event | None = None,
            cycle_timeout: float = 300.0, poll: float = 5.0) -> None:
        """Main scan loop, guarded by a watchdog.

        If a single scan cycle fails to complete within ``cycle_timeout``
        seconds (e.g. an unbounded DNS lookup wedges the thread), the watchdog
        dumps every thread's stack to the log -- so the exact blocking frame
        is captured -- and hard-exits so the service manager (systemd
        ``Restart=on-failure`` / launchd ``KeepAlive``) respawns a healthy
        agent. This both self-heals and records the evidence for diagnosis.
        """
        stop = stop or threading.Event()
        self._cycle_last = time.monotonic()
        threading.Thread(target=self._watchdog, args=(stop, cycle_timeout, poll),
                         daemon=True, name="agent-watchdog").start()
        while not stop.is_set():
            try:
                self.scan_once()
            except Exception:  # never die on a bad cycle
                log.exception("agent cycle failed")
            finally:
                # Updated even on a raised cycle; NOT updated when scan_once
                # is itself wedged, so the watchdog can detect the stall.
                self._cycle_last = time.monotonic()
            stop.wait(self.cfg.interval)

    def _watchdog(self, stop: threading.Event, cycle_timeout: float,
                  poll: float) -> None:
        import faulthandler
        while not stop.is_set():
            time.sleep(poll)
            if stop.is_set():
                return
            elapsed = time.monotonic() - self._cycle_last
            if elapsed > cycle_timeout:
                log.error(
                    "agent scan cycle stuck for %.0fs (>= %.0fs); dumping "
                    "thread stacks and exiting so the service manager "
                    "respawns a healthy agent",
                    elapsed, cycle_timeout)
                try:
                    faulthandler.dump_traceback()
                except Exception:  # noqa: BLE001 - best-effort diagnostics
                    pass
                os._exit(1)

    def stop(self) -> None:
        with self._lock:
            for fwd in self._forwards.values():
                fwd.stop()
            self._forwards.clear()
