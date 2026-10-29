"""Rewrite loopback URLs to the fabric address of the service that owns them.

A service on host A listening on ``127.0.0.1:3000`` is only reachable from host B as
``http://dev.lan:<external_port>``. Callers hand this module the active leases and the
machine the reference was written on; a loopback reference is rewritten only when that
resolves to exactly one lease. Ambiguity (two machines, same port) is never guessed.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"})

_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?[^\s\"'<>)\]]*")


class AmbiguousTarget(Exception):
    """Two or more leases match a loopback reference and nothing disambiguates."""


@dataclass(frozen=True)
class Resolution:
    lease: dict[str, Any]
    url_base: str

    @property
    def external(self) -> str:
        return f"{self.url_base}:{self.lease['external_port']}"


def _port(lease: dict[str, Any]) -> int:
    if lease.get("origin_port"):
        return int(lease["origin_port"])
    t = lease.get("target") or {}
    return int(t.get("port", lease.get("target_port", -1)))


def _machine(lease: dict[str, Any]) -> str | None:
    t = lease.get("target") or {}
    return t.get("machine_id", lease.get("target_machine_id"))


def _names(lease: dict[str, Any]) -> set[Any]:
    o = lease.get("owner") or {}
    return {lease.get("label"), o.get("herdr_session_id"), lease.get("owner_herdr_session_id"),
            _machine(lease)} - {None}


def resolve_loopback(port: int, leases: Iterable[dict[str, Any]], *, from_machine: str | None = None,
                     url_base: str, service: str | None = None) -> Resolution | None:
    """Return the single lease owning loopback ``port`` as seen from ``from_machine``.

    A lease on ``from_machine`` itself means the reference is already correct and is
    not rewritten (None). ``service`` narrows by lease label / owner session.
    """
    matches = [lz for lz in leases if _port(lz) == port]
    if service:
        matches = [lz for lz in matches if service in _names(lz)]
    if from_machine and any(_machine(lz) == from_machine for lz in matches):
        return None
    machines = {_machine(lz) for lz in matches}
    if len(machines) > 1:
        raise AmbiguousTarget(f"port {port} is served by {sorted(m for m in machines if m)}")
    if not matches:
        return None
    return Resolution(matches[0], url_base)


def rewrite_url(url: str, leases: Iterable[dict[str, Any]], *, from_machine: str | None, url_base: str,
                service: str | None = None) -> str:
    parts = urlsplit(url)
    host = parts.hostname
    if not host or host.lower() not in {h.strip("[]") for h in LOOPBACK_HOSTS} or not parts.port:
        return url
    res = resolve_loopback(parts.port, list(leases), from_machine=from_machine, url_base=url_base,
                           service=service)
    if res is None:
        return url
    return urlunsplit((parts.scheme, res.external, parts.path, parts.query, parts.fragment))


def rewrite_text(text: str, leases: Iterable[dict[str, Any]], *, from_machine: str | None,
                 url_base: str) -> tuple[str, list[str]]:
    """Rewrite loopback URLs in free text; return (text, notes about ambiguous ones left alone)."""
    leases = list(leases)
    notes: list[str] = []

    def sub(m: re.Match[str]) -> str:
        try:
            return rewrite_url(m.group(0), leases, from_machine=from_machine, url_base=url_base)
        except AmbiguousTarget as exc:
            notes.append(f"{m.group(0)}: {exc}")
            return m.group(0)

    return _URL_RE.sub(sub, text), notes


# ---- opt-in response-header rewriting for the gateway's HTTP leases ----------
_LOOPBACK_NAMES = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
MAX_HEAD = 64 * 1024


def _fix_value(value: str, port: int, external: str, scheme: str | None = None) -> str:
    # ``scheme`` pins the result's scheme (a TLS-terminated lease must hand out https links);
    # without it the scheme the origin wrote is kept.
    for name in _LOOPBACK_NAMES:
        value = re.sub(rf"(https?)://{re.escape(name)}:{port}(?![0-9])",
                       lambda m: f"{scheme or m.group(1)}://{external}", value)
    return value


def rewrite_response_head(head: bytes, port: int, external: str, scheme: str | None = None) -> bytes:
    """Rewrite loopback references in Location / Access-Control-Allow-Origin /
    Set-Cookie (Domain) of one HTTP response head. ``head`` ends at the blank line.
    Bodies are never touched (a URL inside a body may be signed or built by JS)."""
    lines = head.split(b"\r\n")
    host_only = external.rsplit(":", 1)[0]
    for i, raw in enumerate(lines[1:], start=1):
        name, sep, value = raw.partition(b":")
        if not sep:
            continue
        lname = name.strip().lower()
        text = value.decode("latin-1")
        if lname in (b"location", b"access-control-allow-origin", b"content-location"):
            new = _fix_value(text, port, external, scheme)
        elif lname == b"set-cookie":
            new = re.sub(r"(?i)(;\s*domain=)(?:localhost|127\.0\.0\.1)", rf"\g<1>{host_only}", text)
        else:
            continue
        if new != text:
            lines[i] = name + b":" + new.encode("latin-1")
    return b"\r\n".join(lines)


def force_close(head: bytes) -> bytes:
    """Ask the origin to close after this exchange so every response of a
    rewritten lease passes through the head filter (no keep-alive reuse)."""
    lines = head.split(b"\r\n")
    kept = [lines[0]] + [ln for ln in lines[1:] if not ln.lower().startswith(b"connection:")]
    kept.insert(1, b"Connection: close")
    return b"\r\n".join(kept)


def is_upgrade(head: bytes) -> bool:
    return b"\r\nupgrade:" in head.lower()


def inject_request_headers(head: bytes, pairs: dict[str, str]) -> bytes:
    """Set (replacing any client-supplied copy of) forwarding headers on one request head.

    ``head`` ends before the blank line. Client-sent X-Forwarded-* values are dropped, not appended:
    the gateway is the first trusted hop and a caller must not be able to claim another origin."""
    lines = head.split(b"\r\n")
    drop = {k.lower().encode() for k in pairs}
    kept = [lines[0]] + [ln for ln in lines[1:] if ln.split(b":", 1)[0].strip().lower() not in drop]
    kept += [f"{k}: {v}".encode("latin-1") for k, v in pairs.items()]
    return b"\r\n".join(kept)
