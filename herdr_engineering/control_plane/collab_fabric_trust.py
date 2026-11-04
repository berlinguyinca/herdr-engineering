"""Fabric trust, slice 1: peer identity.

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

FABRIC_TRUST_KEYS = frozenset({"enabled", "kill_switch", "machines", "whois_cache_seconds"})
_SHORT_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")
_NAME = "control_plane.collab.fabric_trust"
_MAX_CACHE = 1024


def normalize_fabric_trust(raw: Any) -> dict:
    """Validate ``control_plane.collab.fabric_trust`` (YAML only). Unknown keys and wrong types raise ValueError. Absent means
    OFF. ``machines`` are MagicDNS short names (no dots), lower-cased; enabling with no machines is an error."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{_NAME} must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - FABRIC_TRUST_KEYS)
    if unknown:
        raise ValueError(f"{_NAME}: unknown keys: " + ", ".join(unknown))
    cfg: dict[str, Any] = {"enabled": False, "kill_switch": False, "machines": [], "whois_cache_seconds": 30}
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
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3600:
                raise ValueError(f"{name} must be an integer 0-3600")
        cfg[key] = value
    if cfg["enabled"] and not cfg["machines"]:
        raise ValueError(f"{_NAME}.machines must list at least one machine when enabled")
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
            short = self._short_name(ip, float(cfg["whois_cache_seconds"]))
        except Exception:                                      # noqa: BLE001 - any failure is "not the fabric"
            return None
        return short if short in cfg["machines"] else None

    def _short_name(self, ip: str, ttl: float) -> str | None:
        t = self.now()
        with self._lock:
            hit = self._cache.get(ip)
        if hit and t - hit[0] < ttl:
            return hit[1]
        ident = self.whois.lookup(ip)                          # OSError / bad JSON propagates: not cached, caller fails closed
        short = ident.node.split(".")[0].lower() if ident and ident.node else None
        with self._lock:
            if len(self._cache) >= _MAX_CACHE:
                self._cache.clear()
            self._cache[ip] = (t, short)
        return short


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
