"""Tailnet-only network guard for the assistant hub (stdlib only, no DNS)."""

from __future__ import annotations

import ipaddress
import re
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


def _clean(value: str) -> bool:
    """Refuse scope ids (%) and any surrounding whitespace before parsing."""
    return isinstance(value, str) and "%" not in value and value == value.strip()


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not _clean(value):
        return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _in(ip, networks) -> bool:
    return any(ip.version == n.version and ip in n for n in networks)


def bind_allowed(host: str, networks) -> bool:
    """Only IP literals inside the allowed networks; wildcards and hostnames are refused."""
    if not _clean(host):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_unspecified:
        return False
    # IPv4-mapped bind addresses are deliberately refused (v6 never matches v4 nets); peers are unwrapped.
    return _in(ip, networks)


def peer_allowed(peer: str, networks) -> bool:
    ip = _parse_ip(peer or "")
    return ip is not None and _in(ip, networks)


_PORT = r"(?::[0-9]{1,5})?"
_BRACKETED = re.compile(r"\[([0-9A-Fa-f:.]+)\]" + _PORT + r"\Z")
_PLAIN = re.compile(r"([A-Za-z0-9._-]+)" + _PORT + r"\Z")


def _normalize_host(value: str) -> str | None:
    """Strictly extract the lowercased host from `host[:port]` / `[v6][:port]`; None if malformed."""
    if not isinstance(value, str) or not value:
        return None
    m = _BRACKETED.match(value)
    if m:
        try:
            return str(ipaddress.IPv6Address(m.group(1)))
        except ValueError:
            return None
    m = _PLAIN.match(value)
    if not m:
        return None
    host = m.group(1).lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or host.endswith("."):
        return None
    return host


def _normalize_entry(entry: str) -> str | None:
    host = _normalize_host(entry)
    if host is not None:
        return host
    if _clean(entry):
        try:
            return str(ipaddress.IPv6Address(entry))  # bare IPv6 literal entry
        except ValueError:
            return None
    return None


def host_header_allowed(header: str | None, allowed_hosts: list[str]) -> bool:
    if not header:
        return False
    host = _normalize_host(header)
    if host is None:
        return False
    names = {n for n in map(_normalize_entry, allowed_hosts) if n} | LOOPBACK_NAMES
    return host in names
