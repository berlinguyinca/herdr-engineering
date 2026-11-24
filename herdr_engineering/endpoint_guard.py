"""Validation of a URL the LLM proxy will connect to on behalf of a registered custom model.

A registered ``base_url`` makes the proxy container open connections, so it must not be a way to reach the cloud
metadata service, a link-local address, or a URL that smuggles credentials. Private and loopback services stay
allowed on purpose: a custom model exists to point at a private endpoint (a compose service, a tailnet host).
"""
import ipaddress
import re
import socket
from urllib.parse import urlsplit

from .fabric_urls import _METADATA_NETS

_MAX_URL = 2048
_UNSAFE_CHARS = re.compile(r"[\s\x00-\x1f\x7f<>\"`\\]")
_NUMERIC_LABEL = re.compile(r"(?:0[xX][0-9a-fA-F]*|[0-9]+)")
_METADATA_NAMES = frozenset({"metadata", "metadata.google.internal", "instance-data", "instance-data.ec2.internal"})
_EXTRA_METADATA = tuple(ipaddress.ip_network(n) for n in ("100.100.100.200/32", "fd00:ec2::/32"))


def _check_ip(ip, shown: str) -> None:
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is not None:
            ip = inner
    if any(ip in net for net in _METADATA_NETS + _EXTRA_METADATA):
        raise ValueError(f"base_url host {shown} is a cloud metadata address")
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        raise ValueError(f"base_url host {shown} is a link-local, unspecified or multicast address")


def validate_model_endpoint(url, *, resolve: bool = False) -> str:
    """Return ``url`` when the proxy may connect to it, else raise ValueError.

    http or https only, no credentials, a plain host and port, and no metadata or link-local address, whether it is
    written as a literal or (with ``resolve``) is what the name resolves to now. An unresolvable name passes: the
    private service may simply be down."""
    if not isinstance(url, str) or not url or len(url) > _MAX_URL or _UNSAFE_CHARS.search(url):
        raise ValueError("base_url must be a plain http(s) URL")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("base_url must be http(s)")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("base_url must not contain credentials")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("base_url has an invalid port") from None
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("base_url has no host")
    if host in _METADATA_NAMES:
        raise ValueError(f"base_url host {host} is a cloud metadata name")
    try:
        _check_ip(ipaddress.ip_address(host), host)
        return url
    except ValueError as exc:
        if "base_url" in str(exc):
            raise
    # Not an IP literal. A name whose last label is a number is an IPv4 address in another spelling
    # (2852039166, 0xa9fea9fe, 169.254.43518): refuse it rather than guess how a resolver reads it.
    if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
        raise ValueError("base_url host looks like a numeric IP address in an unusual spelling")
    if resolve:
        try:
            infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError:
            return url
        for info in infos:
            _check_ip(ipaddress.ip_address(info[4][0].split("%")[0]), f"{host} ({info[4][0]})")
    return url
