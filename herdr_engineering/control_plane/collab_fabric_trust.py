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

FABRIC_TRUST_KEYS = frozenset({"enabled", "kill_switch", "machines", "whois_cache_seconds", "tailnet_suffix", "agent_ttl_seconds",
                               "listen_host", "listen_port", "whois_socket", "auto_join"})
AGENT_TTL_DEFAULT, AGENT_TTL_MIN, AGENT_TTL_MAX = 2592000, 300, 7776000      # 30 days; 5 minutes..90 days
FABRIC_PEER_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))   # tailnet addresses only
_SUFFIX = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\Z")
_SHORT_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")
FABRIC_JOIN_PER_HOUR, FABRIC_JOIN_MAX_AGENTS = 30, 200       # per machine: NEW agents an hour; names ever used (revoked ones count)
FABRIC_JOIN_MAX_TOTAL = 1000                                  # across all machines; separate from the operator's own max_agents_per_user
FABRIC_JOIN_AUDIT_WINDOW, FABRIC_JOIN_AUDIT_MAX = 300, 500    # one refusal audit per (machine, reason) per window; lifetime cap in the log
FABRIC_RENEW_AFTER = 86400                                    # an auto-joined agent's sliding renewal is written once a day at most
FABRIC_JOIN_AGENT_LIFE = 10 * 365 * 86400                     # the agent record itself; the fabric window is what matters
JOIN_ORIGIN = "fabric_join"
JOIN_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")           # THE name rule: the server, `rooms join-fabric` and join.sh all follow it
JOIN_NAME_RULE = "1-64 characters: lowercase letters, digits, dot, underscore, hyphen, starting with a letter or digit"
_NAME = "control_plane.collab.fabric_trust"
_MAX_CACHE = 1024


def validate_listen_host(host: Any, name: str = f"{_NAME}.listen_host") -> str:
    """The dedicated fabric listener binds a tailnet address only (100.64.0.0/10 or fd7a:115c:a1e0::/48): never loopback, a LAN
    address, a wildcard or a name. Returns the normalized literal; raises ValueError otherwise."""
    try:
        ip = ipaddress.ip_address(host) if isinstance(host, str) and host == host.strip() else None
    except ValueError:
        ip = None
    if ip is None or not any(ip in net for net in FABRIC_PEER_NETS):
        raise ValueError(f"{name} must be a tailnet address (100.64.0.0/10 or fd7a:115c:a1e0::/48)")
    return str(ip)


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
                   "tailnet_suffix": None, "agent_ttl_seconds": AGENT_TTL_DEFAULT,
                   "listen_host": None, "listen_port": None, "whois_socket": None, "auto_join": False}
    for key, value in raw.items():
        name = f"{_NAME}.{key}"
        if key in ("enabled", "kill_switch", "auto_join"):
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
        elif key == "listen_host":
            value = validate_listen_host(value, name)
        elif key == "listen_port":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise ValueError(f"{name} must be an integer 1-65535")
        elif key == "whois_socket":
            if (not isinstance(value, str) or not value.startswith("/") or "\0" in value or len(value) > 107
                    or ".." in value.split("/")):
                raise ValueError(f"{name} must be an absolute unix socket path")
        elif key == "agent_ttl_seconds":
            if isinstance(value, bool) or not isinstance(value, int) or not AGENT_TTL_MIN <= value <= AGENT_TTL_MAX:
                raise ValueError(f"{name} must be an integer {AGENT_TTL_MIN}-{AGENT_TTL_MAX}")
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3600:
                raise ValueError(f"{name} must be an integer 0-3600")
        cfg[key] = value
    if (cfg["listen_host"] is None) != (cfg["listen_port"] is None):
        raise ValueError(f"{_NAME}.listen_host and listen_port must be given together")
    if cfg["auto_join"] and not cfg["enabled"]:
        raise ValueError(f"{_NAME}.auto_join requires enabled: true")
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
        self.whois = whois                                     # injected (tests); else chosen per config: sidecar or tailscaled
        self._sidecars: dict[str, TailscaleWhois] = {}
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
            fqdn = self._node_name(ip, float(cfg["whois_cache_seconds"]), cfg.get("whois_socket"))
            suffix = cfg["tailnet_suffix"]
            if not fqdn or not suffix:
                return None
            for machine in cfg["machines"]:                    # the FULL name must match: a first-label match is spoofable
                if fqdn == f"{machine}.{suffix}":
                    return machine
        except Exception:                                      # noqa: BLE001 - any failure is "not the fabric"
            return None
        return None

    def _whois_for(self, socket_path: str | None) -> TailscaleWhois:
        if self.whois is not None:
            return self.whois
        if not socket_path:
            return self._sidecars.setdefault("", TailscaleWhois())
        return self._sidecars.setdefault(socket_path, TailscaleWhois(sockets=(socket_path,), proxy=True))

    def _node_name(self, ip: str, ttl: float, socket_path: str | None = None) -> str | None:
        t = self.now()
        with self._lock:
            hit = self._cache.get(ip)
        if hit and t - hit[0] < ttl:
            return hit[1]
        ident = self._whois_for(socket_path).lookup(ip)                          # OSError / bad JSON propagates: not cached, caller fails closed
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

    def fabric_auto_join_active(self) -> bool:
        """Fabric trust is on, the kill switch is off AND ``auto_join`` is true (read per request)."""
        cfg = self.fabric_trust_cfg()
        return bool(cfg["enabled"] and not cfg["kill_switch"] and cfg["auto_join"])


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
        self._fabric_join_index: dict[tuple[str, str], str] = {}   # (machine, name) -> agent_id of every auto-joined agent, any status
        self.fabric_join_per_hour = FABRIC_JOIN_PER_HOUR
        self.fabric_join_max_agents = FABRIC_JOIN_MAX_AGENTS
        self.fabric_join_max_total = FABRIC_JOIN_MAX_TOTAL
        self.fabric_join_audit_max = FABRIC_JOIN_AUDIT_MAX
        self._fabric_join_denied_total = 0       # refusal audit rows in the log (rebuilt on replay): bounded by fabric_join_audit_max

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
        if agent.get("origin") == JOIN_ORIGIN and not self.fabric_auto_join_active():
            return "fabric_auto_join_off"                         # the one switch governs the agents it made
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
            self._fabric_renew(aid, agent, machine)
            pr = self._fabric_actor(aid, agent["owner"], machine)
        pr.peer = peer
        return pr

    def _fabric_renew_due(self, agent: dict) -> float | None:
        """The new expiry when an auto-joined agent's window is worth writing now, else None. The window slides, but a durable event
        is written at most once a day (a quarter of the window when that is shorter): a polling agent costs about one event a day."""
        ttl = self.fabric_trust_cfg()["agent_ttl_seconds"]
        expires = self._now() + ttl
        if expires - agent["fabric_expires_at"] < min(FABRIC_RENEW_AFTER, ttl / 4):
            return None
        return expires

    def _fabric_renew(self, aid: str, agent: dict, machine: str) -> None:
        """Sliding renewal for an agent made by ``fabric-join`` (see ``_fabric_renew_due``). Best effort: a full log must not fail
        authentication. Never rebinds the machine. Caller holds the lock."""
        if agent.get("origin") != JOIN_ORIGIN:
            return
        expires = self._fabric_renew_due(agent)
        if expires is None:
            return
        from .collab import CollabError
        try:
            self._commit("agent_fabric_set", self._join_actor(agent["owner"], machine) | {
                "agent_id": aid, "fabric": True, "machine": machine, "fabric_expires_at": expires, "origin": JOIN_ORIGIN,
                "action": "agent.fabric.renew", "outcome": "allowed", "project_ids": [], "resource_id": aid})
        except CollabError:
            pass

    @staticmethod
    def _join_actor(owner: str, machine: str) -> dict:
        return {"actor_user_id": owner, "actor_agent_id": None, "actor_kind": "user", "via": "fabric", "machine": machine}

    def _join_denied(self, owner: str, machine: str, reason: str, name: str, aid: str | None = None) -> None:
        """Audit a refused join. Throttled per (machine, reason) - never per name, which a caller controls - and capped in total, so
        a loop over fresh names cannot fill the capped event log. Refusals past either limit are only counted."""
        now = self._now()
        gate = ("fabric_join", machine, reason)
        if (now - self._denied_at.get(gate, -1e18) < FABRIC_JOIN_AUDIT_WINDOW
                or self._fabric_join_denied_total >= self.fabric_join_audit_max):
            self.stats["audit_throttled"] += 1
            return
        if len(self._denied_at) > 10_000:
            self._denied_at = {k: t for k, t in self._denied_at.items() if now - t < 60}
        self._denied_at[gate] = now
        from .collab import CollabError
        try:
            self._commit("audit_denied", self._join_actor(owner, machine) | {
                "action": "agent.fabric_join", "outcome": "denied", "reason": reason, "room_id": None,
                "resource_id": aid or name, "project_ids": [], "agent_id": aid})
        except CollabError:
            pass

    def fabric_join(self, body: dict, peer_ip: str | None, claimed: str | None, owner: str | None) -> tuple[int, dict]:
        """``POST agents/fabric-join`` (dedicated listener only). The tailnet machine vouches, no token and no owner click: creates
        (or renews) a normal agent of ``owner`` (the collab operator) bound to the machine whois found, with the fabric bit on and NO
        room grants. Idempotent per (machine, name). A revoked (machine, name) is never resurrected."""
        from .collab import CollabError, _digest, _machine, ids, secrets
        if self.degraded or not owner or not self.fabric_auto_join_active():
            raise CollabError(404, "not found")
        peer = _real_peer(peer_ip)
        machine = self.fabric_resolver.resolve(peer) if peer else None     # fails closed on every error
        if machine is None:
            self._fabric_fail()
        if claimed is not None and claimed.strip().lower() != machine:
            self._fabric_fail()
        if set(body) - {"name", "machine"}:
            raise CollabError(400, "unknown field")
        said = _machine(body.get("machine"))
        if said is not None and said != machine:
            self._fabric_fail()
        name = body.get("name")
        if not isinstance(name, str) or not JOIN_NAME.fullmatch(name):
            raise CollabError(400, "name must be " + JOIN_NAME_RULE)
        with self._lock:
            if not self._ok(owner):
                raise CollabError(404, "not found")
            aid = self._fabric_join_index.get((machine, name))
            if aid is not None:
                agent = self._agents[aid]
                if agent["status"] != "active" or not agent.get("fabric") or agent["owner"] != owner:
                    self._join_denied(owner, machine, "fabric_join_revoked", name, aid)   # never silently resurrected
                    raise CollabError(403, "forbidden")
                if agent.get("machine") != machine:                  # the owner rebound it elsewhere: this is not that machine's agent
                    self._join_denied(owner, machine, "fabric_join_machine_rebound", name, aid)
                    raise CollabError(403, "forbidden")
                expires = self._fabric_renew_due(agent)             # not throttled: it creates nothing, and is written once a day at most
                if expires is not None:
                    self._commit("agent_fabric_set", self._join_actor(owner, machine) | {
                        "agent_id": aid, "fabric": True, "machine": machine, "fabric_expires_at": expires, "origin": JOIN_ORIGIN,
                        "action": "agent.fabric.renew", "outcome": "allowed", "project_ids": [], "resource_id": aid})
                return 200, {"agent_id": aid, "name": name, "machine": machine, "fabric_expires_at": agent["fabric_expires_at"]}
            # a new agent. Bounds first (a refusal costs the caller's budget nothing), then the per-machine creation rate.
            names = [m for (m, _n) in self._fabric_join_index]
            if names.count(machine) >= self.fabric_join_max_agents:          # revoked names count too: the index stays bounded
                self._join_denied(owner, machine, "fabric_join_machine_cap", name)
                raise CollabError(429, "fabric join agent limit reached for this machine")
            if len(names) >= self.fabric_join_max_total:
                self._join_denied(owner, machine, "fabric_join_total_cap", name)
                raise CollabError(429, "fabric join agent limit reached")
            try:
                self._throttle("fabricjoin:" + machine, self.fabric_join_per_hour, 3600.0, "fabric join")
            except CollabError:
                self._join_denied(owner, machine, "fabric_join_rate_limited", name)
                raise
            expires = self._now() + self.fabric_trust_cfg()["agent_ttl_seconds"]
            aid = ids.new_id("agent")
            self._commit("agent_enrolled", self._join_actor(owner, machine) | {      # the slot is chosen by _commit, like any agent's
                "agent_id": aid, "owner_user_id": owner, "code_digest": None, "join_digest": None,
                "device_id": "fabric-" + machine, "harness_type": "agent", "name": name,
                "parent_agent_id": None, "instance_id": None, "machine": machine,
                "secret_digest": _digest(secrets.token_urlsafe(32)),        # nobody holds this secret: no credential exists
                "expires_at_epoch": self._now() + FABRIC_JOIN_AGENT_LIFE, "origin": JOIN_ORIGIN,
                "fabric": True, "fabric_expires_at": expires,
                "action": "agent.fabric_join", "outcome": "allowed", "project_ids": [], "resource_id": aid})
        return 201, {"agent_id": aid, "name": name, "machine": machine, "fabric_expires_at": expires}

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
