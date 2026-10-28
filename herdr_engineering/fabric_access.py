"""Data-plane access control for the fabric gateway (spec: herdr-fabric-url-resolution-20261028, D7).

The control API has always required a bearer token; the per-lease listeners did not, so any tailnet peer could
open any leased port. With a policy configured, every connection is checked *before* TLS or HTTP is spoken:
the peer's tailnet identity (tailscaled LocalAPI ``whois``) must match an allowed user or tag. The default is
deny, lookups that fail close the connection (fail closed), and guest/shared nodes are denied simply because
nobody lists them. Loopback peers (the gateway host itself) are trusted unless ``trust_loopback=False``.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import quote

DEFAULT_SOCKETS = ("/var/run/tailscale/tailscaled.sock", "/run/tailscale/tailscaled.sock")


@dataclass(frozen=True)
class Identity:
    user: str | None
    tags: tuple[str, ...] = ()
    node: str | None = None


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("local-tailscaled.sock", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class TailscaleWhois:
    """Identify a tailnet peer through tailscaled's LocalAPI (works from a host-network container with the socket mounted)."""

    def __init__(self, sockets: Iterable[str] = DEFAULT_SOCKETS, timeout: float = 2.0) -> None:
        self.sockets = tuple(sockets)
        self.timeout = timeout

    def lookup(self, ip: str) -> Identity | None:
        """The peer's identity, or None when tailscaled does not know the address. Raises OSError if unreachable."""
        last: Exception | None = None
        for path in self.sockets:
            try:
                conn = _UnixHTTP(path, self.timeout)
                conn.request("GET", f"/localapi/v0/whois?addr={quote(ip + ':1')}", headers={"Host": "local-tailscaled.sock"})
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
            except OSError as exc:
                last = exc
                continue
            if resp.status == 404:
                return None
            if resp.status != 200:
                raise OSError(f"tailscaled whois returned {resp.status}")
            data = json.loads(body)
            node, profile = data.get("Node") or {}, data.get("UserProfile") or {}
            login = profile.get("LoginName")
            return Identity(user=None if login in (None, "", "tagged-devices") else login.lower(),
                            tags=tuple(sorted(node.get("Tags") or ())), node=(node.get("Name") or "").rstrip(".") or None)
        raise OSError(f"tailscaled LocalAPI unreachable: {last}")


@dataclass
class AccessPolicy:
    users: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    trust_loopback: bool = True
    whois: Callable[[str], Identity | None] = field(default_factory=lambda: TailscaleWhois().lookup)
    cache_seconds: float = 30.0
    now: Callable[[], float] = time.monotonic
    _cache: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    denied: int = 0

    def __post_init__(self) -> None:
        self.users = frozenset(u.lower() for u in self.users)
        self.tags = frozenset(self.tags)

    @property
    def enforcing(self) -> bool:
        return bool(self.users or self.tags)

    def decide(self, peer_ip: str | None) -> tuple[bool, str]:
        if not self.enforcing:
            return True, "no policy"
        if not peer_ip:
            return self._deny("unknown peer")
        try:
            ip = ipaddress.ip_address(peer_ip.split("%")[0])
        except ValueError:
            return self._deny("malformed peer address")
        if ip.is_loopback:
            return (True, "loopback") if self.trust_loopback else self._deny("loopback not trusted")
        key, t = str(ip), self.now()
        with self._lock:
            hit = self._cache.get(key)
        if hit and t - hit[0] < self.cache_seconds:
            return hit[1]
        try:
            ident = self.whois(key)
        except Exception as exc:   # fail closed; a flapping tailscaled must not open the door
            return self._deny(f"identity lookup failed: {exc}")  # not cached: retry on the next connection
        if ident is None:
            verdict = self._deny("peer is not a tailnet node")
        elif ident.user and ident.user.lower() in self.users:
            verdict = (True, f"user {ident.user}")
        elif set(ident.tags) & self.tags:
            verdict = (True, f"tag {sorted(set(ident.tags) & self.tags)[0]}")
        else:
            verdict = self._deny(f"{ident.user or ident.node or 'peer'} is not on the allow-list")
        with self._lock:
            if len(self._cache) > 1024:
                self._cache.clear()
            self._cache[key] = (t, verdict)
        return verdict

    def _deny(self, reason: str) -> tuple[bool, str]:
        self.denied += 1
        return False, reason
