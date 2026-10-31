"""Tailnet-only network guard for the assistant hub (stdlib only, no DNS)."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

DEFAULT_NETWORKS: tuple[str, ...] = (
    "127.0.0.0/8",
    "::1/128",
    "100.64.0.0/10",
    "fd7a:115c:a1e0::/48",
)

LOOPBACK_NAMES: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def parse_networks(cidrs: Iterable[str]) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(c, strict=False) for c in cidrs)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _in(ip, networks) -> bool:
    return any(ip.version == n.version and ip in n for n in networks)


def bind_allowed(host: str, networks) -> bool:
    """Only IP literals inside the allowed networks; wildcards and hostnames are refused."""
    try:
        ip = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    if ip.is_unspecified:
        return False
    return _in(ip, networks)


def peer_allowed(peer: str, networks) -> bool:
    ip = _parse_ip(peer or "")
    return ip is not None and _in(ip, networks)


def _strip_port(header: str) -> str:
    h = header.strip()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end != -1 else h
    if h.count(":") == 1:
        return h.split(":", 1)[0]
    return h


def host_header_allowed(header: str | None, allowed_hosts: list[str]) -> bool:
    if not header:
        return False
    host = _strip_port(header).lower().rstrip(".")
    if not host:
        return False
    names = {h.strip().lower().rstrip(".") for h in allowed_hosts} | LOOPBACK_NAMES
    return host in names
