"""Fabric trust, slice 1: peer identity; slice 2: the ``Fabric`` auth scheme (``FabricTrustMixin`` below).

Design: ``docs/specs/fabric-trust-agent-auth-20261104/SPEC.md`` (sections 3.1, 3.5, 4 slice 1). A request is "from the fabric"
only if the real TCP peer address resolves, through the local tailscaled LocalAPI ``whois`` (``fabric_access.TailscaleWhois``),
to a node whose MagicDNS short name is listed in ``control_plane.collab.fabric_trust.machines``.

Everything sits behind ``control_plane.collab.fabric_trust.enabled`` (YAML only, no env vars, absent means off, read per request,
fail closed); ``kill_switch: true`` turns it off at once. This slice only identifies the peer. The ``Fabric`` auth scheme is slice 2.
The resolver takes the socket peer address and nothing else: ``X-Forwarded-For`` and every other header can never reach it.
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from ..fabric_access import TailscaleWhois

FABRIC_TRUST_KEYS = frozenset({"enabled", "kill_switch", "machines", "whois_cache_seconds", "tailnet_suffix", "agent_ttl_seconds"})
AGENT_TTL_DEFAULT, AGENT_TTL_MIN, AGENT_TTL_MAX = 2592000, 300, 7776000      # 30 days; 5 minutes..90 days
FABRIC_PEER_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))   # tailnet addresses only
_SUFFIX = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\Z")
_SHORT_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")
_NAME = "control_plane.collab.fabric_trust"
_MAX_CACHE = 1024


def normalize_fabric_trust(raw: Any) -> dict:
    """Validate ``control_plane.collab.fabric_trust`` (YAML only). Unknown keys and wrong types raise ValueError. Absent means
    OFF. ``machines`` are MagicDNS short names (no dots), lower-cased; enabling with no machines, or without ``tailnet_suffix``
    (the tailnet's MagicDNS domain, e.g. ``tail0c50da.ts.net``), is an error. A node is a machine only when its FULL name equals
    ``<machine>.<tailnet_suffix>``. ``agent_ttl_seconds`` is how long one owner "set the bit" stays valid."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{_NAME} must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - FABRIC_TRUST_KEYS)
    if unknown:
        raise ValueError(f"{_NAME}: unknown keys: " + ", ".join(unknown))
    cfg: dict[str, Any] = {"enabled": False, "kill_switch": False, "machines": [], "whois_cache_seconds": 30,
                   "tailnet_suffix": None, "agent_ttl_seconds": AGENT_TTL_DEFAULT}
    for key, value in raw.items():
        name = f"{_NAME}.{key}"
        if key in ("enabled", "kill_switch"):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        elif key == "machines":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{name} must be a list of MagicDNS short names")
            value = [v.strip().lower() for v in value]
            if not all(_SHORT_NAME.match(v) for v in value):
                raise ValueError(f"{name} entries must be MagicDNS short names (letters, digits, hyphens; no dots)")
        elif key == "tailnet_suffix":
            if not isinstance(value, str) or not _SUFFIX.match(value.strip().strip(".").lower()):
                raise ValueError(f"{name} must be the tailnet MagicDNS domain, e.g. tail0c50da.ts.net")
            value = value.strip().strip(".").lower()
        elif key == "agent_ttl_seconds":
            if isinstance(value, bool) or not isinstance(value, int) or not AGENT_TTL_MIN <= value <= AGENT_TTL_MAX:
                raise ValueError(f"{name} must be an integer {AGENT_TTL_MIN}-{AGENT_TTL_MAX}")
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3600:
                raise ValueError(f"{name} must be an integer 0-3600")
        cfg[key] = value
    if cfg["enabled"] and not cfg["machines"]:
        raise ValueError(f"{_NAME}.machines must list at least one machine when enabled")
    if cfg["enabled"] and not cfg["tailnet_suffix"]:
        raise ValueError(f"{_NAME}.tailnet_suffix is required when enabled")
    return cfg


class FabricPeerResolver:
    """Maps a real TCP peer ip to an allow-listed machine short name, or None. Fails closed on every error.

    ``config`` is called on every resolve (hot config, like the other collab blocks) and returns a normalized config; if it raises,
    the answer is None. Only the whois identity is cached (per ip, ``whois_cache_seconds``); the flags and the allow-list are
    re-checked on every call, so a kill switch or an edited machine list takes effect immediately. Lookup errors are not cached."""

    def __init__(self, config: Callable[[], dict], *, whois: TailscaleWhois | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.whois = whois if whois is not None else TailscaleWhois()
        self.now = now
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    def resolve(self, peer_ip: str | None) -> str | None:
        try:
            cfg = self.config()
            if not cfg["enabled"] or cfg["kill_switch"] or not cfg["machines"]:
                return None
            if not peer_ip:
                return None
            ip = str(ipaddress.ip_address(peer_ip.split("%")[0]))
            fqdn = self._node_name(ip, float(cfg["whois_cache_seconds"]))
            suffix = cfg["tailnet_suffix"]
            if not fqdn or not suffix:
                return None
            for machine in cfg["machines"]:                    # the FULL name must match: a first-label match is spoofable
                if fqdn == f"{machine}.{suffix}":
                    return machine
        except Exception:                                      # noqa: BLE001 - any failure is "not the fabric"
            return None
        return None

    def _node_name(self, ip: str, ttl: float) -> str | None:
        t = self.now()
        with self._lock:
            hit = self._cache.get(ip)
        if hit and t - hit[0] < ttl:
            return hit[1]
        ident = self.whois.lookup(ip)                          # OSError / bad JSON propagates: not cached, caller fails closed
        fqdn = ident.node.rstrip(".").lower() if ident and ident.node else None
        with self._lock:
            if len(self._cache) >= _MAX_CACHE:
                self._cache.clear()
            self._cache[ip] = (t, fqdn)
        return fqdn


class FabricTrustMixin:
    """Config state for ``CollabService``: ``fabric_trust`` (startup value) and ``fabric_trust_source`` (hot, per request)."""

    def _init_fabric_trust(self) -> None:
        self.fabric_trust = normalize_fabric_trust(None)
        self.fabric_trust_source: Callable[[], Any] | None = None   # callable -> raw YAML mapping, read per request

    def fabric_trust_cfg(self) -> dict:
        """Effective settings NOW. A source that fails or returns something invalid means OFF."""
        if self.fabric_trust_source is None:
            return self.fabric_trust
        try:
            return normalize_fabric_trust(self.fabric_trust_source())
        except Exception:                                      # noqa: BLE001 - any failure is OFF
            return {**normalize_fabric_trust(None), "enabled": False, "kill_switch": True}

    def fabric_trust_active(self) -> bool:
        cfg = self.fabric_trust_cfg()
        return bool(cfg["enabled"] and not cfg["kill_switch"])


def _real_peer(peer_ip: str | None) -> str | None:
    """The normalized peer address, or None for anything that can never be a tailnet node: absent, malformed, loopback
    (every peer behind ``tailscale serve`` or a local proxy), unspecified, multicast. IPv4-mapped IPv6 is unwrapped first so
    ``::ffff:127.0.0.1`` cannot slip past."""
    try:
        ip = ipaddress.ip_address((peer_ip or "").split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return None
    if not any(ip in net for net in FABRIC_PEER_NETS):        # only a tailnet address (100.64.0.0/10, fd7a:115c:a1e0::/48)
        return None
    return str(ip)


class FabricAuthMixin:
    """``Authorization: Fabric <agent_id>`` for ``CollabService`` (spec section 3.2). Every refusal is the same generic 401 and
    counts as an auth failure; a refusal for a peer that the transport check already accepted (an allow-listed machine) is also
    audited under its own reason code. Unauthenticated callers are never written to the log."""

    fabric_resolver: FabricPeerResolver

    def _init_fabric_auth(self) -> None:
        self.fabric_resolver = FabricPeerResolver(self.fabric_trust_cfg)

    def _fabric_fail(self, reason: str | None = None, agent: dict | None = None, aid: str | None = None,
                     machine: str | None = None) -> None:
        self.stats["auth_failures"] += 1
        if reason and agent is not None and aid:
            self._audit_denied(self._fabric_actor(aid, agent["owner"], machine), "auth.agent", reason,
                               resource_id=aid, agent_id=aid)
        from .collab import CollabError  # lazy: collab imports this module
        raise CollabError(401, "unauthorized")

    @staticmethod
    def _fabric_actor(aid: str, owner: str, machine: str | None = None):
        from .collab import Principal
        pr = Principal("agent", aid, owner)
        pr.via, pr.machine = "fabric", machine
        return pr

    def _fabric_agent_problem(self, aid: str, agent: dict, machine: str) -> str | None:
        """Why this agent may not use Fabric auth from ``machine`` right now (None = it may). Caller holds the lock."""
        if agent["status"] != "active":
            return "fabric_agent_inactive"
        if agent.get("external") or aid in self._helper_minted or aid in self._helper_by_agent:
            return "fabric_agent_kind"
        if not self._ok(agent["owner"]):
            return "fabric_owner_disabled"
        if agent.get("machine") != machine:
            return "fabric_machine_unbound"
        if not agent.get("fabric"):
            return "fabric_not_enabled"
        expires = agent.get("fabric_expires_at")                     # absent (older log) = expired: fail closed
        if not isinstance(expires, (int, float)) or self._now() >= expires:
            return "fabric_expired"
        return None

    def _fabric_authenticate(self, aid: str, peer_ip: str | None, claimed: str | None):
        if self.degraded or not self.fabric_trust_active() or not aid:
            self._fabric_fail()
        peer = _real_peer(peer_ip)
        machine = self.fabric_resolver.resolve(peer) if peer else None     # fails closed on every error
        if machine is None:
            self._fabric_fail()
        with self._lock:
            agent = self._agents.get(aid)
            if agent is None:
                self._fabric_fail()
            if claimed is not None and claimed.strip().lower() != machine:  # the header is informational; whois decides
                self._fabric_fail("fabric_header_mismatch", agent, aid, machine)
            reason = self._fabric_agent_problem(aid, agent, machine)
            if reason:
                self._fabric_fail(reason, agent, aid, machine)
            pr = self._fabric_actor(aid, agent["owner"], machine)
        pr.peer = peer
        return pr

    def _fabric_still_valid(self, pr) -> bool:
        """Everything ``_fabric_authenticate`` checked, again, for a stream that is still open. tailscaled is asked outside the
        service lock (it is cached by ``whois_cache_seconds``)."""
        if self.degraded or not self.fabric_trust_active() or not pr.peer:
            return False
        if self.fabric_resolver.resolve(pr.peer) != pr.machine:
            return False
        with self._lock:
            agent = self._agents.get(pr.id)
            return agent is not None and self._fabric_agent_problem(pr.id, agent, pr.machine) is None
