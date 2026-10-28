"""Stable service-name URLs: ``https://<service>.local.metabolomics.us`` on one well-known port.

The per-port front doors (``https://local.metabolomics.us:3188``) change whenever a lease is re-allocated. A named
front is a single listener (443 by default) that reads the host name the client asked for, from the TLS ClientHello's
SNI or, for plain HTTP, the ``Host`` header, and hands the connection to the lease that owns that name. Everything
behind that decision (TLS termination, the access policy, header rewriting) is the router's existing path, so a named
URL is exactly as protected as a port URL.

Names are ``<label>`` when the lease was registered with a label that is a valid DNS label, else ``<machine>-<port>``
(the machine and the port the app listens on: stable across restarts and re-registration). A collision is never
guessed: the older lease keeps the name and the newer one falls back to its automatic name.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger("herdr.fabric.named")

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$")
_RESERVED = frozenset({"www", "api", "gateway", "control", "admin", "localhost"})


def _slug(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:41].strip("-")


def automatic_name(lease: dict[str, Any]) -> str:
    target = lease.get("target") or {}
    machine = _slug(str(target.get("machine_id", lease.get("target_machine_id", "")) or "svc")) or "svc"
    port = lease.get("origin_port") or target.get("port", lease.get("target_port"))
    return f"{machine}-{int(port)}"[:63]


def assign_names(leases: Iterable[dict[str, Any]]) -> dict[str, str]:
    """{lease id: name}. Deterministic: the oldest lease claims a requested label, later ones fall back."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for lz in sorted(leases, key=lambda x: (str(x.get("created_at") or ""), str(x.get("id")))):
        label = lz.get("label")
        want = label if isinstance(label, str) and _LABEL.match(label) and label not in _RESERVED else None
        name = want if want and want not in taken else automatic_name(lz)
        base, n = name, 2
        while name in taken:       # two apps on the same machine and port in different namespaces
            name = f"{base[:58]}-{n}"
            n += 1
        taken.add(name)
        out[str(lz["id"])] = name
    return out


def parse_sni(data: bytes) -> str | None:
    """Server name from a TLS ClientHello (None when absent or malformed). Bounds-checked: input is hostile."""
    try:
        if len(data) < 6 or data[0] != 0x16 or data[5] != 0x01:
            return None
        pos = 5 + 4 + 2 + 32                                  # record hdr, handshake hdr, version, random
        pos += 1 + data[pos]                                  # session id
        pos += 2 + int.from_bytes(data[pos:pos + 2], "big")   # cipher suites
        pos += 1 + data[pos]                                  # compression
        end = min(len(data), pos + 2 + int.from_bytes(data[pos:pos + 2], "big"))
        pos += 2
        while pos + 4 <= end:
            kind = int.from_bytes(data[pos:pos + 2], "big")
            size = int.from_bytes(data[pos + 2:pos + 4], "big")
            body = data[pos + 4:pos + 4 + size]
            if kind == 0 and len(body) >= 5 and body[2] == 0:
                n = int.from_bytes(body[3:5], "big")
                name = body[5:5 + n]
                if len(name) == n and n:
                    return name.decode("ascii").lower().rstrip(".")
                return None
            pos += 4 + size
    except (IndexError, UnicodeDecodeError, ValueError):
        return None
    return None


def parse_host(head: bytes) -> str | None:
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"host:"):
            v = line[5:].strip().decode("latin-1").lower()
            host = v[1:].split("]")[0] if v.startswith("[") else v.split(":")[0]
            return host.rstrip(".") or None
        if not line:
            break
    return None


def peek_name(client: socket.socket, deadline: float = 5.0) -> tuple[str | None, bool]:
    """(requested host name, is_tls) without consuming a byte, so the normal path still sees the whole stream."""
    end = time.monotonic() + deadline
    need = 5
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return None, False
        client.settimeout(left)
        data = client.recv(16384, socket.MSG_PEEK)
        if not data:
            return None, False
        if data[0] == 0x16:
            if len(data) >= 5:
                need = 5 + int.from_bytes(data[3:5], "big")
            if len(data) >= min(need, 16384):
                return parse_sni(data), True
        else:
            if b"\r\n\r\n" in data or len(data) >= 8192:
                return parse_host(data), False
        time.sleep(0.005)


class NamedFront:
    """One listener that routes by requested host name to the lease that owns it."""

    def __init__(self, router, leases: Callable[[], list[dict[str, Any]]], domain: str, port: int = 443,
                 bind_hosts: list[str] | None = None, apex_hosts: Iterable[str] = (),
                 apex_target: tuple[str, int] | None = None,
                 host_routes: dict[str, tuple[str, int]] | None = None) -> None:
        self.router, self.leases_fn = router, leases
        # The bare fleet names (https://dev.lan, https://local.metabolomics.us) are the dashboard, not a lease:
        # they go to one configured upstream. Without an upstream the bare names are dropped like any unknown name.
        self.apex_hosts = frozenset(h.lower().strip(".") for h in apex_hosts if h)
        self.apex_target = apex_target
        # Exact host name -> upstream, for names that are not leases and not the dashboard (e.g. this node's
        # ts.net name, which `tailscale serve` used to proxy to a localhost-only service).
        self.host_routes = {h.lower().strip("."): t for h, t in (host_routes or {}).items()}
        self.domain = domain.lower().strip(".")
        self.port = port
        self.bind_hosts = [h for h in (bind_hosts or router._bind_hosts) if h]
        self._listeners: list[socket.socket] = []
        self._stop = threading.Event()

    def name_of(self, lease_id: str) -> str | None:
        names = assign_names(self.leases_fn())
        return names.get(lease_id)

    def host_of(self, lease_id: str) -> str | None:
        n = self.name_of(lease_id)
        return f"{n}.{self.domain}" if n else None

    def netloc_of(self, lease_id: str, tls: bool) -> str | None:
        h = self.host_of(lease_id)
        if not h:
            return None
        default = 443 if tls else 80
        return h if self.port == default else f"{h}:{self.port}"

    def lookup(self, host: str | None) -> dict[str, Any] | None:
        if not host or not host.endswith("." + self.domain):
            return None
        label = host[: -len(self.domain) - 1]
        if "." in label:
            return None
        leases = [lz for lz in self.leases_fn() if lz.get("state") in (None, "active")]
        for lz in leases:
            if assign_names(leases).get(str(lz["id"])) == label:
                return lz
        return None

    def start(self) -> None:
        for host in self.bind_hosts:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, self.port))
                s.listen(128)
                s.settimeout(0.5)
            except OSError as exc:
                s.close()
                self.stop()
                raise OSError(f"named front cannot bind {host}:{self.port}: {exc}") from exc
            self._listeners.append(s)
            threading.Thread(target=self._accept, args=(s,), daemon=True, name=f"named-front-{host}").start()

    def stop(self) -> None:
        self._stop.set()
        for s in self._listeners:
            try:
                s.close()
            except OSError:
                pass
        self._listeners = []

    def _accept(self, listener: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            host, tls = peek_name(client)
            lease = self.lookup(host)
        except OSError:
            host, lease, tls = None, None, False
        upstream = (self.host_routes.get(host) if host else None) or (
            self.apex_target if host in self.apex_hosts else None)
        if lease is None and upstream:
            try:
                client.settimeout(None)
            except OSError:
                return
            self.router._proxy_one(client, upstream[0], upstream[1], None, None, self.port, external=host)
            return
        if lease is None:
            # unknown name: say nothing about what exists
            try:
                client.close()
            except OSError:
                pass
            return
        try:
            client.settimeout(None)
        except OSError:
            return
        target = lease.get("target") or {}
        self.router._proxy_one(client, target.get("host", lease.get("target_host")),
                               int(target.get("port", lease.get("target_port"))), lease.get("routes") or None,
                               None, int(lease["external_port"]),
                               external=self.netloc_of(str(lease["id"]), tls))
