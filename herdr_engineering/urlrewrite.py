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
