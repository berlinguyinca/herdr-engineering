"""Canonical URL resolution for agent output (spec: herdr-fabric-url-resolution-20261028).

Pure functions, no I/O. ``resolve_url`` maps a URL an agent printed (``http://localhost:3188/x``)
to the canonical fabric URL (``https://dev.lan:3188/x``) when exactly one active lease owns it
*as seen from the machine the agent ran on*. Nothing is guessed: no lease, an ambiguous lease,
or a non-loopback URL leaves ``canonical`` unset and says why. The original text is never altered.
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

_LOOPBACK_NAMES = frozenset({"localhost"})
_MAX_URL = 2048
_UNSAFE_CHARS = re.compile(r"[\s\x00-\x1f\x7f<>\"`\\]")
# fd00:ec2::254 is the AWS IPv6 metadata endpoint; ULA otherwise passes as private fabric space.
_METADATA_NETS = tuple(ipaddress.ip_network(n) for n in ("169.254.0.0/16", "fd00:ec2::/32"))
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class UnsafeTarget(ValueError):
    """A lease target that would turn the gateway into an open proxy."""


@dataclass(frozen=True)
class UrlResolution:
    original: str
    canonical: str | None
    status: str  # resolved|unmapped|ambiguous|expired|unhealthy|not_loopback|invalid
    reason: str = ""
    host: str | None = None
    port: int | None = None
    service: str | None = None
    session: str | None = None
    source_machine: str | None = None
    lease_id: str | None = None
    access: str = "private-fabric"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _loopback_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def _is_loopback_host(host: str) -> bool:
    return host in _LOOPBACK_NAMES or _loopback_ip(host)


def _lease_port(lease: dict[str, Any]) -> int:
    return int((lease.get("target") or {}).get("port", lease.get("target_port", -1)))


def _lease_machine(lease: dict[str, Any]) -> str | None:
    t = lease.get("target") or {}
    return t.get("machine_id", lease.get("target_machine_id"))


def _lease_host(lease: dict[str, Any]) -> str | None:
    t = lease.get("target") or {}
    return t.get("host", lease.get("target_host"))


def _fail(url: str, status: str, reason: str, **kw: Any) -> UrlResolution:
    return UrlResolution(original=url, canonical=None, status=status, reason=reason, **kw)


def resolve_url(url: str, leases: Iterable[dict[str, Any]], *, url_base: str, source_machine: str | None = None,
                scheme: str = "https") -> UrlResolution:
    """Resolve ``url`` against ``leases`` as seen from ``source_machine``.

    A lease owned by ``source_machine`` always wins over another machine's lease on the same port.
    With an unknown source, a fleet-unique match resolves and more than one is ``ambiguous``.
    A known source with no lease of its own is ``unmapped``: it never borrows another host's service.
    """
    if not isinstance(url, str) or len(url) > _MAX_URL or _UNSAFE_CHARS.search(url):
        return _fail(url if isinstance(url, str) else "", "invalid", "malformed URL")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return _fail(url, "invalid", "unparsable authority")
    if parts.scheme.lower() not in ("http", "https") or not host:
        return _fail(url, "not_loopback", "not an http(s) URL")
    if parts.username is not None or parts.password is not None:
        return _fail(url, "invalid", "userinfo not allowed")
    host = host.lower()
    fabric_name = host == url_base.lower() or host.endswith("." + url_base.lower())
    if not (_is_loopback_host(host) or fabric_name):
        return _fail(url, "not_loopback", "external host; no explicit mapping")
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    if not 0 < port < 65536:
        return _fail(url, "invalid", "port out of range")

    pool = list(leases)
    # a fabric-addressed URL names the *external* port; a loopback URL names the target port
    if fabric_name:
        matches = [lz for lz in pool if int(lz.get("external_port", -1)) == port]
    else:
        matches = [lz for lz in pool if _lease_port(lz) == port]
        if source_machine is not None:
            matches = [lz for lz in matches if _lease_machine(lz) == source_machine]
    if not matches:
        return _fail(url, "unmapped", f"no active lease for port {port}", port=port, source_machine=source_machine)
    machines = {_lease_machine(lz) for lz in matches}
    if len(matches) > 1 and (len(machines) > 1 or fabric_name):
        return _fail(url, "ambiguous", f"port {port} is served by {sorted(m for m in machines if m)}",
                     port=port, source_machine=source_machine)
    lz = matches[0]
    owner = lz.get("owner") or {}
    ident = dict(host=_lease_host(lz), port=_lease_port(lz), service=lz.get("label"),
                 session=owner.get("herdr_session_id", lz.get("owner_herdr_session_id")),
                 source_machine=_lease_machine(lz), lease_id=lz.get("id"))
    if lz.get("state") not in (None, "active"):
        return _fail(url, "expired", f"lease is {lz.get('state')}", **ident)
    if lz.get("health_last_error"):
        return _fail(url, "unhealthy", f"service unreachable: {lz['health_last_error']}", **ident)
    tail = url[len(parts.scheme) + 3 + len(parts.netloc):]  # path?query#fragment, byte-for-byte
    canonical = f"{scheme}://{url_base}:{int(lz['external_port'])}{tail}"
    return UrlResolution(original=url, canonical=canonical, status="resolved", **ident)


def validate_target_host(host: str, extra_networks: Iterable[str] = ()) -> None:
    """Accept only loopback and tailnet IP literals. Names other than
    ``localhost`` are refused: resolving them at connect time is how SSRF re-binds."""
    if not isinstance(host, str) or not host or len(host) > 255:
        raise UnsafeTarget("empty target host")
    if host.lower() == "localhost":
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise UnsafeTarget(f"target host {host!r} is not an IP literal") from None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if any(ip in net for net in _METADATA_NETS):
        raise UnsafeTarget(f"target {host} is a cloud metadata address")
    if ip.is_loopback and not ip.is_unspecified:
        return
    if ip.is_unspecified or ip.is_multicast or ip.is_link_local or ip.is_reserved:
        raise UnsafeTarget(f"target {host} is not routable service space")
    if ip in _TAILNET_V4 or ip in _TAILNET_V6:
        return
    if any(ip in ipaddress.ip_network(n, strict=False) for n in extra_networks):
        return  # operator-declared fabric networks (e.g. a LAN the gateway may route to)
    raise UnsafeTarget(f"target {host} is outside loopback and the tailnet")
