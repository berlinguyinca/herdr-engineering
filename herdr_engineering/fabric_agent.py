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
import select
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

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

# Host processes that are container/VM networking shims rather than the dev
# service itself. They surface as meaningless, mostly-stale port bindings
# (e.g. rootless podman's slirp4netns) that do not correspond to a service the
# user can actually open, so they are never adopted/registered.
_IGNORED_PROCESSES: frozenset[str] = frozenset({
    "slirp4netns",        # rootless container port-mapping shim
    "vpnkit",             # Docker Desktop VM port forwarder
    "com.docker.backend", # Docker Desktop backend
    "com.docker.vpnkit",
    "kube-proxy",
})


# ---------------------------------------------------------------------------
# Listener scanning (per-OS, best effort, no dependencies)
# ---------------------------------------------------------------------------

class ListenerInventory(dict):
    """Dict-compatible listener scan with explicit completeness for absence checks."""

    def __init__(self, *args, complete=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.complete = complete


def _apply_listener_pids(result, owners, unknown):
    for port, entry in result.items():
        pids = owners.get(port, set())
        if len(pids) == 1 and port not in unknown:
            entry["pid"] = next(iter(pids))
        elif len(pids) > 1 or (pids and port in unknown):
            entry["pid_ambiguous"] = True
    return result


def _scan_linux() -> dict[int, dict[str, Any]]:
    """Parse ``ss -ltnpH`` into {port: {"addrs": set, "process": str|None}}."""
    exe = shutil.which("ss")
    if exe is None:
        log.warning("ss not found; listener scan disabled on this host")
        return ListenerInventory()
    try:
        scan = subprocess.run([exe, "-ltnpH"], capture_output=True, text=True, timeout=10)
        out = scan.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ss scan failed: %s", exc)
        return ListenerInventory()
    result = ListenerInventory(complete=getattr(scan, "returncode", 0) == 0)
    owners, unknown = {}, set()
    proc_re = re.compile(r'\("([^"]+)",pid=(\d+)')
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 5:
            result.complete = False
            continue
        local = parts[3]
        processes = proc_re.findall(line)
        port = _port_of(local)
        if not 0 < port <= 65535:
            result.complete = False
            continue
        entry = result.setdefault(port, {"addrs": set(), "process": None})
        if entry["process"] is None and processes:
            entry["process"] = processes[0][0]
        pids = {int(pid) for _, pid in processes if int(pid) > 0}
        owners.setdefault(port, set()).update(pids)
        if not pids or len(re.findall(r'\("[^"]+"', line)) > len(processes):
            unknown.add(port)
        entry["addrs"].add(_host_of(local))
    return _apply_listener_pids(result, owners, unknown)


def _scan_macos() -> dict[int, dict[str, Any]]:
    """Parse ``lsof -nP -iTCP -sTCP:LISTEN`` into the same shape."""
    exe = shutil.which("lsof")
    if exe is None:
        log.warning("lsof not found; listener scan disabled on this host")
        return ListenerInventory()
    try:
        scan = subprocess.run([exe, "-nP", "-iTCP", "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=15)
        out = scan.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("lsof scan failed: %s", exc)
        return ListenerInventory()
    result = ListenerInventory(complete=getattr(scan, "returncode", 0) == 0)
    owners, unknown = {}, set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 9:
            result.complete = False
            continue
        name = parts[8]  # e.g. "127.0.0.1:5678", "*:8080", "[::1]:8080"
        port = _port_of(name)
        if not 0 < port <= 65535:
            result.complete = False
            continue
        proc = parts[0] or None
        entry = result.setdefault(port, {"addrs": set(), "process": None})
        if entry["process"] is None and proc:
            entry["process"] = proc
        if parts[1].isdigit() and int(parts[1]) > 0:
            owners.setdefault(port, set()).add(int(parts[1]))
        else:
            unknown.add(port)
        entry["addrs"].add(_host_of(name))
    return _apply_listener_pids(result, owners, unknown)


def _port_of(addr: str) -> int:
    """Extract the port from a 'host:port' / '[v6]:port' / '*:port' string."""
    host, _, port = addr.rpartition(":")
    try:
        return int(port)
    except ValueError:
        return -1


def _listener_cwd(pid: int | None, *, timeout: float = 2.0) -> str | None:
    """PID-scoped cwd stays local; never use it as ownership evidence."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if os.uname().sysname == "Darwin":
            exe = shutil.which("lsof")
            if not exe:
                return None
            result = subprocess.run([exe, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                    capture_output=True, text=True, timeout=timeout)
            paths = [line[1:] for line in result.stdout.splitlines() if line.startswith("n/")]
            path = paths[0] if len(paths) == 1 else None
        else:
            path = os.readlink(f"/proc/{pid}/cwd")
        return path if path and os.path.isabs(path) and len(path) <= 4096 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _listener_project(pid: int | None, *, timeout: float = 2.0) -> str | None:
    path = _listener_cwd(pid, timeout=timeout)
    name = Path(path).name if path else ""
    return name[:120] if name and all(ch.isprintable() for ch in name) else None


@lru_cache(maxsize=1)
def _linux_boot_identity():
    with open("/proc/sys/kernel/random/boot_id") as stream:
        value = stream.read(80).strip()
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", value):
        raise ValueError("host boot identity unavailable")
    return value


def _process_identity(pid, *, timeout=0.2):
    """Read parent/start identity only; never capture argv or environment."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if os.uname().sysname == "Darwin":
            result = subprocess.run(["ps", "-p", str(pid), "-o", "ppid=", "-o", "lstart="],
                                    capture_output=True, text=True, timeout=timeout)
            parts = result.stdout.strip().split(maxsplit=1)
            return {"parent_pid": int(parts[0]), "start": parts[1]} if len(parts) == 2 else None
        with open(f"/proc/{pid}/stat") as stream:
            parts = stream.read(4096).rsplit(")", 1)[1].split()
        return {"parent_pid": int(parts[1]), "start": f"{_linux_boot_identity()}:{parts[19]}"}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _system_unit(pid):
    if os.uname().sysname == "Darwin":
        return None  # No launchd ownership is inferred from process names.
    try:
        with open(f"/proc/{pid}/cgroup") as stream:
            text = stream.read(4096)
        units = re.findall(r"/system\.slice/([A-Za-z0-9_.@:-]+\.service)(?:/|$)", text, re.MULTILINE)
        return units[0] if len(set(units)) == 1 else None
    except OSError:
        return None


class ListenerOwnership:
    """Bounded native-pane root inventory plus fresh process ancestry checks."""

    def __init__(self, host):
        self.host = host
        self._roots = []
        self._expires = 0.0

    @staticmethod
    def _json(args, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {}
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=min(0.5, remaining))
            if result.returncode or len(result.stdout) > 1024 * 1024:
                return {}
            data = json.loads(result.stdout)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, subprocess.SubprocessError):
            return {}

    def _refresh(self, deadline):
        self._roots = []
        self._expires = time.monotonic() + 20
        executable = shutil.which("herdr")
        if not executable:
            return
        inventory = self._json([executable, "session", "list", "--json"], deadline)
        sessions = inventory.get("sessions")
        if not isinstance(sessions, list):
            return
        for session in sessions[:8]:
            if not isinstance(session, dict):
                continue
            if time.monotonic() >= deadline:
                break
            name = session.get("name")
            if (session.get("running") is not True or not isinstance(name, str)
                    or not name or name.startswith("-") or len(name) > 200):
                continue
            prefix = [executable, "--session", name]
            data = self._json(prefix + ["api", "snapshot"], deadline)
            result = data.get("result")
            snapshot = result.get("snapshot") if isinstance(result, dict) else None
            panes = snapshot.get("panes", []) if isinstance(snapshot, dict) else []
            if not isinstance(panes, list):
                continue
            for pane in panes[:128]:
                if not isinstance(pane, dict):
                    continue
                if time.monotonic() >= deadline:
                    break
                pane_id, terminal = pane.get("pane_id"), pane.get("terminal_id")
                if not all(isinstance(value, str) and value and not value.startswith("-")
                           and len(value) <= 200 for value in (pane_id, terminal)):
                    continue
                process = self._json(prefix + ["pane", "process-info", "--pane", pane_id], deadline)
                result = process.get("result")
                info = result.get("process_info") if isinstance(result, dict) else None
                if not isinstance(info, dict) or info.get("pane_id") != pane_id:
                    continue
                shell = info.get("shell_pid")
                identity = _process_identity(shell, timeout=max(0.01, min(0.2, deadline - time.monotonic())))
                if identity is None:
                    continue
                self._roots.append({"host_id": self.host, "herdr_session": name,
                    "pane_id": pane_id, "terminal_id": terminal, "shell_pid": shell,
                    "shell_start": identity["start"],
                    "session_id": "herdr:" + ":".join(quote(value, safe="") for value in (self.host, name, pane_id))})

    def classify(self, pid, *, timeout=0.75):
        if type(pid) is not int or pid <= 0 or timeout <= 0:
            return None
        deadline = time.monotonic() + min(1.0, timeout)
        initial = _process_identity(pid, timeout=max(0.01, min(0.2, deadline - time.monotonic())))
        if initial is None:
            return None
        if time.monotonic() >= self._expires:
            self._refresh(deadline)
        chain, current, observed = [], pid, {}
        while current > 1 and len(chain) < 64 and time.monotonic() < deadline:
            if current in chain:
                return None
            chain.append(current)
            identity = _process_identity(current, timeout=max(0.01, min(0.2, deadline - time.monotonic())))
            if identity is None:
                return None
            observed[current] = dict(identity)
            roots = [root for root in self._roots if root["shell_pid"] == current]
            if roots:
                if len(roots) != 1 or identity["start"] != roots[0]["shell_start"]:
                    return None
                for ancestor, expected in observed.items():
                    if time.monotonic() >= deadline or _process_identity(
                            ancestor, timeout=max(0.01, min(0.2, deadline - time.monotonic()))) != expected:
                        return None
                if observed[pid] != initial:
                    return None
                return {**roots[0], "kind": "herdr_process_ancestry", "listener_pid": pid,
                        "listener_start": initial["start"], "ancestor_pids": chain,
                        "observed_at": datetime.now(UTC).isoformat()}
            current = identity["parent_pid"]
        # Reparented/detached containers lack launch proof and stay unassigned;
        # a genuine system cgroup is separate evidence for the System view.
        unit = _system_unit(pid)
        if unit and _process_identity(pid) == initial:
            return {"kind": "system_unit", "host_id": self.host, "unit": unit,
                    "listener_pid": pid, "listener_start": initial["start"],
                    "observed_at": datetime.now(UTC).isoformat()}
        return None


class ListenerGitMetadata:
    """Small checkout cache keyed by actual listener identity, never its agent."""

    def __init__(self):
        self._cache = {}

    @staticmethod
    def _git(executable, cwd, args, deadline):
        if deadline <= time.monotonic():
            return None
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        process = None
        try:
            process = subprocess.Popen([executable, *args], cwd=cwd, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            raw = bytearray()
            while len(raw) <= 4096:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    return None
                chunk = os.read(process.stdout.fileno(), 4097 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > 4096 or process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                return None
            return raw.decode("utf-8").strip()
        except (OSError, UnicodeError, subprocess.SubprocessError):
            return None
        finally:
            if process:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
                process.stdout.close()

    def read(self, pid, ownership, *, timeout=0.5):
        if (not isinstance(ownership, dict) or ownership.get("kind") != "herdr_process_ancestry"
                or ownership.get("listener_pid") != pid or timeout <= 0):
            return {}
        deadline = time.monotonic() + min(timeout, 1.0)
        identity = _process_identity(pid, timeout=min(0.2, timeout))
        if not identity or identity["start"] != ownership.get("listener_start"):
            return {}
        cwd = _listener_cwd(pid, timeout=max(0.01, min(0.2, deadline - time.monotonic())))
        if not cwd:
            return {}
        key = (pid, identity["start"], cwd)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < 30:
            return dict(cached[1])
        result = {}
        executable = shutil.which("git")
        if executable:
            from .control_plane.project_links import canonical_repository
            origin = canonical_repository(self._git(executable, cwd,
                ["config", "--local", "--get", "remote.origin.url"], deadline))
            head = self._git(executable, cwd, ["rev-parse", "--verify", "HEAD"], deadline) if origin else None
            remaining = max(0.01, min(0.2, deadline - time.monotonic()))
            if (origin and isinstance(head, str) and re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", head)
                    and time.monotonic() < deadline
                    and _process_identity(pid, timeout=remaining) == identity
                    and _listener_cwd(pid, timeout=remaining) == cwd):
                result = {"repository_id": origin, "commit_sha": head.lower()}
        self._cache.pop(key, None)
        self._cache[key] = (time.monotonic(), result)
        while len(self._cache) > 128:
            self._cache.pop(next(iter(self._cache)))
        return dict(result)


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
                 bind_port: int = 0, private_only: bool = True,
                 fabric_names: frozenset[str] = frozenset()) -> None:
        self.bind_host = bind_host
        self.target_port = target_port
        # Host names the fabric itself owns (the control-plane host, the
        # tailnet IP). A request addressed to one of them reaches a loopback
        # app as ``Host: 127.0.0.1:<port>``, the name that app's own
        # DNS-rebinding guard accepts, so nobody configures the app with the
        # fabric's names. Any other Host is forwarded untouched.
        self.fabric_names = frozenset(n.lower() for n in fabric_names)
        # bind_port=0 => OS-allocated ephemeral port; a specific bind_port
        # makes the forwarder reuse the SAME port so port-matching works
        # (localhost:4040 -> dev.lan:4040). Callers fall back to 0 on EADDRINUSE.
        self.bind_port = bind_port
        self.private_only = private_only
        self._listener: socket.socket | None = None
        self._stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(self.MAX_CONNECTIONS)

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

    _HEAD_LIMIT = 16384
    _HEAD_DEADLINE = 2.0
    MAX_CONNECTIONS = 256

    def _proxy(self, client: socket.socket) -> None:
        target: socket.socket | None = None
        # Bounded: a flood of idle or trickling connections cannot hold every
        # thread, descriptor and loopback slot.
        if not self._slots.acquire(blocking=False):
            try:
                client.close()
            except OSError:
                pass
            return
        try:
            # The head is read BEFORE the app is dialled, so a client that
            # never speaks holds nothing on the app.
            head = self._read_head(client)
            target = socket.create_connection(
                ("127.0.0.1", self.target_port), timeout=5)
            from .devfabric import proxy_pair
            if head:
                target.sendall(self._rewrite_head(head))
            proxy_pair(client, target)
        except OSError:
            pass  # app gone: drop the connection
        finally:
            self._slots.release()
            for s in (client, target):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _read_head(self, client: socket.socket) -> bytes:
        """What the client sent first, through the blank line if it arrives.

        One overall deadline, not one per read, so a trickle cannot hold the
        connection. Whatever arrived is returned and forwarded either way.
        """
        if not self.fabric_names:
            return b""
        buf = b""
        deadline = time.monotonic() + self._HEAD_DEADLINE
        try:
            while b"\r\n\r\n" not in buf and len(buf) < self._HEAD_LIMIT:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                client.settimeout(left)
                chunk = client.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except (OSError, TimeoutError):
            pass
        return buf

    def _rewrite_head(self, data: bytes) -> bytes:
        """Point a fabric-addressed ``Host`` at the loopback target.

        Deliberately narrow, because the app's own Host guard is what keeps a
        website from using the browser against it: only a plain ``GET`` or
        ``HEAD`` (no state change, and a cross-site page cannot read the
        answer), a head that parses unambiguously, no bytes after the head
        (nothing pipelined behind it), no upgrade, and exactly one ``Host``
        naming a fabric-owned host. Anything else is forwarded as received and
        the app decides. ``Connection: close`` is forced on a rewritten request.
        """
        end = data.find(b"\r\n\r\n")
        if end < 0 or len(data) != end + 4:
            return data
        head = data[:end]
        # Ambiguity a lenient backend could read differently: a bare LF or CR.
        if b"\n" in head.replace(b"\r\n", b"") or b"\r" in head.replace(b"\r\n", b""):
            return data
        lines = head.split(b"\r\n")
        first = lines[0].split(b" ")
        if (len(first) != 3 or first[0] not in (b"GET", b"HEAD")
                or not first[2].startswith(b"HTTP/1.")):
            return data
        host_at = None
        host = ""
        for i, line in enumerate(lines[1:], 1):
            if line[:1] in (b" ", b"\t"):  # obs-fold continuation
                return data
            name, sep, value = line.partition(b":")
            if not sep or name != name.strip() or not name:
                return data  # no colon, or whitespace before it
            lname = name.lower()
            if lname in (b"upgrade", b"content-length", b"transfer-encoding"):
                return data
            if lname == b"host":
                if host_at is not None:
                    return data  # two Host headers: not ours to interpret
                host_at = i
                v = value.strip().decode("latin-1").lower()
                if not v or any(c in v for c in " \t@/\\"):
                    return data
                host = (v[1:].split("]")[0] if v.startswith("[")
                        else v.split(":")[0])
                host = host.rstrip(".")
        if host_at is None or host not in self.fabric_names:
            return data
        lines[host_at] = f"Host: 127.0.0.1:{self.target_port}".encode()
        lines = [ln for ln in lines
                 if ln.partition(b":")[0].strip().lower() != b"connection"]
        lines.append(b"Connection: close")
        return b"\r\n".join(lines) + b"\r\n\r\n"

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
    # Base URL of the control plane (e.g. http://dev.lan) to report discovered
    # services to, so every dev:<port> lease shows up on the fleet Services
    # page with a clickable route. Empty = reporting disabled.
    control_plane_url: str = ""

    def fabric_names(self) -> frozenset[str]:
        """The host names the fabric owns: its control-plane host and this
        machine's tailnet IP. A request for one of them is addressed to the
        fabric, never to an attacker's domain."""
        names = {self.tailnet_ip}
        if self.control_plane_url:
            from urllib.parse import urlparse
            host = urlparse(self.control_plane_url).hostname
            if host:
                names.add(host)
        return frozenset(n.lower() for n in names if n)


class FabricAgent:
    """Detects web services on this host and keeps the gateway's lease set in
    sync with them (register on appear, close on disappear).
    """

    def __init__(self, client: Any, config: AgentConfig) -> None:
        self._client = client
        self.cfg = config
        self._machine = socket.gethostname()
        self._ownership = ListenerOwnership(self._machine)
        self._git_metadata = ListenerGitMetadata()
        self._store: dict[str, dict[str, Any]] = self._load_store()
        self._forwards: dict[int, AgentForwarder] = {}
        self._warned_unleased: set[int] = set()
        self._cp_last_report: dict[str, float] = {}
        self._cp_unavailable: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        # Restore forwarders for loopback-only services survived a restart.
        for port_s, entry in self._store.items():
            if entry.get("fwd_port"):
                fwd = AgentForwarder(config.tailnet_ip, int(port_s),
                                     fabric_names=config.fabric_names())
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
        # Report every currently-tracked service to the control plane so the
        # fleet Services page is fully populated across agent restarts (a
        # restart re-heartbeats these leases; it does not re-adopt them, so
        # without this the pre-existing leases would never surface).
        self._report_services()
        # Keep recorded launch history until a fresh scan observes the current
        # listener; that scan must not wait for the normal heartbeat interval.
        self._cp_last_report.clear()

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
        # The caller stops the throwaway forwarder (setting _stopping) before
        # rebinding, so reset the stop flag or the accept loop below would exit
        # immediately and leave this listener bound-but-dead.
        fwd._stopping = threading.Event()  # noqa: SLF001

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
        gateway_front: set[int] = set()
        try:
            # The gateway binds its WHOLE front-door range (dev.lan:18000-
            # 28999), not just currently-leased ports. Exclude the entire range
            # so its own idle front doors are never self-registered as services
            # (feedback-loop guard). Fall back to the leased subset if the
            # gateway predates the range in /healthz.
            hz = self._client.healthz()
            lo, hi = hz.get("port_lo"), hz.get("port_hi")
            if lo is not None and hi is not None:
                gateway_front = set(range(int(lo), int(hi) + 1))
            else:
                for lease in self._client.list().get("leases", []):
                    ep = lease.get("external_port")
                    if ep:
                        gateway_ext_ports.add(int(ep))
        except Exception as exc:  # noqa: BLE001 - best effort
            log.debug("gateway front-door lookup failed: %s", exc)

        # Ports owned by this agent's own forwarders are never candidates —
        # they proxy an already-tracked service (double-registration guard).
        with self._lock:
            fwd_ports = {e["fwd_port"] for e in self._store.values()
                         if e.get("fwd_port")}
            # 1) release vanished services, plus any previously-registered
            # noise that is no longer adoptable: gateway front-door listeners
            # (feedback-loop guard) and container/VM networking shims.
            for port_s in list(self._store):
                port = int(port_s)
                entry = self._store[port_s]
                process = entry.get("process") or ""
                if (port not in live_ports
                        or port in gateway_front
                        or process in _IGNORED_PROCESSES):
                    self._release(port_s)
                    closed.append(port_s)

        # 2) adopt new web services
        for port in sorted(live_ports):
            if port <= 0 or not self._is_dev_web_port(port):
                continue
            if port in gateway_front:
                # Reserved gateway front-door range: the gateway binds these on
                # its bind hosts regardless of leases. Never adopt them.
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
                self._cp_unavailable.pop(entry["label"], None)

        # 3) keepalive: heartbeat leases that are due; if the gateway dropped
        # one (restart or TTL lapse), transparently re-register the same
        # target so the dev:<port> mapping never goes stale.
        now = time.monotonic()
        with self._lock:
            due = [(p, e) for p, e in self._store.items()
                   if now - e.get("last_hb", 0.0) >= self.cfg.heartbeat_ttl / 2
                   and e.get("lease_id")]
        for port_s, entry in due:
            entry["source_port"] = int(port_s)
            self._keepalive(port_s, entry)

        # Reporting must never delay the next lease's gateway heartbeat. It has
        # its own slower cadence and a bounded batch/failure budget.
        self._report_services(listeners)

        # 4) surface listeners that look like dev services but hold no lease
        # (probe failed, gateway rejected, no forwarder possible): warn once per
        # port so "every service gets registered and rewritten" cannot fail
        # silently. Cleared when the port goes away or gets a lease.
        unleased: list[int] = []
        with self._lock:
            tracked = set(self._store) | {str(p) for p in fwd_ports}
        for port in sorted(live_ports):
            if (port <= 0 or port in gateway_front or str(port) in tracked
                    or not self._is_dev_web_port(port)):
                continue
            info = listeners[port]
            if (info.get("process") or "") in _IGNORED_PROCESSES:
                continue
            if port in gateway_ext_ports:
                addrs = (info.get("addrs") or set())
                if ("*" in addrs or "0.0.0.0" in addrs or "::" in addrs
                        or self.cfg.tailnet_ip in addrs):
                    continue
            unleased.append(port)
        self._warned_unleased &= set(unleased)
        for port in unleased:
            if port not in self._warned_unleased:
                self._warned_unleased.add(port)
                log.warning("port %d (%s) is listening on %s but has no fabric lease; its "
                            "URLs will not be rewritten (not HTTP, or registration failed)",
                            port, listeners[port].get("process") or "unknown process",
                            self._machine)
        self._save_store()
        return {"registered": registered, "closed": closed,
                "unleased": [str(p) for p in unleased]}

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
        if process and process in _IGNORED_PROCESSES:
            log.debug("ignoring container/VM networking shim %r on port %s",
                      process, port)
            return None
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
                                         bind_port=bind_port,
                                         fabric_names=self.cfg.fabric_names()
                                         ).start()
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
                "project": None,
                "source_port": port,
                "last_hb": time.monotonic(),
            }
        log.info("auto-registered %s -> dev port %s",
                 label, lease.get("external_port"))
        return self._store[str(port)]

    @staticmethod
    def _describe_service(entry: dict[str, Any], host: str) -> str:
        """Human one-liner for a discovered service: what it is, on which
        system it runs, and how to reach it."""
        process = (entry.get("process") or "").strip()
        kind = process or "web"
        host = (host or "").strip() or "an unknown host"
        port = entry.get("external_port")
        url = entry.get("url")
        desc = f"{kind} service on {host}"
        if entry.get("project"):
            desc += f", working directory {entry['project']}"
        if entry.get("source_port"):
            desc += f", listener port {entry['source_port']}"
        if port:
            desc += f" (port {port})"
        if url:
            desc += f", reachable at {url}"
        return desc

    def _report_services(self, listeners: dict[int, dict[str, Any]] | None = None) -> None:
        """At most 4 reports/2 seconds per cycle; live reports at least 5 min apart.

        A control-plane outage stops the batch on its first failure. Successful
        reports rotate behind older entries, so recovery drains fairly.
        """
        if not self.cfg.control_plane_url:
            return
        now = time.monotonic()
        deadline = now + 2.0
        pending = [(entry, "unavailable") for entry in self._cp_unavailable.values()]
        pending += [(entry, "healthy") for entry in sorted(
            self._store.values(), key=lambda e: self._cp_last_report.get(e["label"], 0.0))
            if now - self._cp_last_report.get(entry["label"], -300.0) >= 300.0]
        for entry, status in pending[:4]:
            if time.monotonic() >= deadline:
                break
            if status == "healthy" and listeners is not None:
                info = listeners.get(entry.get("source_port"), {})
                previous_origin = entry.get("ownership") or {}
                observed = self._ownership.classify(
                    info.get("pid"), timeout=max(0.0, min(0.75, deadline - time.monotonic())))
                # A pane may end while its child keeps serving. Retain already
                # observed origin only for the exact same process birth identity.
                if ((observed or {}).get("kind") != "herdr_process_ancestry"
                        and previous_origin.get("kind") == "herdr_process_ancestry"
                        and previous_origin.get("listener_pid") == info.get("pid")):
                    current = _process_identity(info.get("pid"), timeout=max(0.01, min(0.2, deadline - time.monotonic())))
                    if current and current["start"] == previous_origin.get("listener_start"):
                        observed = previous_origin
                entry["ownership"] = observed
                metadata = self._git_metadata.read(info.get("pid"), entry["ownership"],
                    timeout=max(0.0, min(0.5, deadline - time.monotonic())))
                entry.update(repository_id=metadata.get("repository_id"), commit_sha=metadata.get("commit_sha"))
                entry["project"] = _listener_project(
                    info.get("pid"), timeout=max(0.01, min(0.2, deadline - time.monotonic())))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            label = entry["label"]
            if not self._report_service(entry, status,
                                        heartbeat=status == "healthy" and label in self._cp_last_report,
                                        timeout=min(1.0, remaining)):
                break
            self._cp_last_report[label] = time.monotonic()
            if status == "unavailable":
                self._cp_unavailable.pop(label, None)
                self._cp_last_report.pop(label, None)

    def _report_service(self, entry: dict[str, Any], status: str,
                        *, heartbeat: bool = False, timeout: float = 2.0) -> bool:
        """Best-effort report of a discovered service to the control plane.

        Emits a ``service`` envelope to ``{control_plane_url}/api/v1/ingest`` so
        the app appears on the fleet Services page (visible from any host) with
        a clickable ``dev:<port>`` route and a human description of what it is
        and where it runs. The control-plane repo keys by (host_id,
        service_name), so re-reporting the same label updates in place instead
        of duplicating. Best-effort: a down control plane is never fatal.
        """
        if not self.cfg.control_plane_url:
            return False
        payload = {
            "service_name": entry.get("label") or "",
            "host_id": self._machine,
            "port": entry.get("external_port") or 0,
            "route": entry.get("url") or "",
            "status": status,
            "owner": entry.get("process") or "",
            "description": self._describe_service(entry, self._machine),
            "project": entry.get("project"),
            "source_port": entry.get("source_port"),
            "ownership": entry.get("ownership"),
            "session_id": (entry.get("ownership") or {}).get("session_id"),
            "repository_id": entry.get("repository_id"), "commit_sha": entry.get("commit_sha"),
        }
        body = {
            "source": "fabric-agent",
            "entity_type": "service",
            "entity_id": entry.get("lease_id")
                or f"{self._machine}:{entry.get('external_port')}",
            "event_type": ("service.heartbeat" if heartbeat
                           else "service.registered" if status == "healthy"
                            else "service.unavailable"),
            "payload": payload,
        }
        url = f"{self.cfg.control_plane_url.rstrip('/')}/api/v1/ingest"
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout):
                pass
            return True
        except Exception as exc:  # control plane down: never break the agent
            log.warning("control-plane report failed for %s: %s",
                        entry.get("label"), exc)
            return False

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
        # Mirror the lifecycle into the control plane so the service leaves the
        # fleet UI when the app stops.
        if entry:
            self._cp_unavailable[entry["label"]] = entry
            # Bound retry memory during a prolonged control-plane outage.
            if len(self._cp_unavailable) > 1000:
                self._cp_unavailable.pop(next(iter(self._cp_unavailable)))

    # -- run loop ---------------------------------------------------------

    def retarget(self, client: Any, control_plane_url: str) -> None:
        """Swap the gateway client / control-plane URL used from the next pass."""
        self._client = client
        self.cfg = replace(self.cfg, control_plane_url=control_plane_url)
        # Live forwarders rewrite a Host naming the control-plane host (#117); a
        # fleet-config refresh that moves it must reach them too, not only the
        # forwarders created after it.
        names = self.cfg.fabric_names()
        for fwd in list(self._forwards.values()):
            fwd.fabric_names = names

    def run(self, stop: threading.Event | None = None,
            cycle_timeout: float = 300.0, poll: float = 5.0,
            after_cycle: Callable[[], None] | None = None) -> None:
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
            if after_cycle is not None:
                after_cycle()  # may raise HostConfigUnavailable (revoked): ends the run
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
