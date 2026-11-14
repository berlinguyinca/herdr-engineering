"""``control_plane.collab.members``: the one parser and the one limits type for the member channel.

Dependency free on purpose (like ``https_origin``): ``fleet_config_cli`` runs on the deploy host and the control plane
reads the very same section, so a value this refuses never reaches runtime.  Errors are ``ValueError`` messages that
start with ``control_plane.collab.members`` and name the key; callers add their own prefix (``fleet config``).

Layout (every limit key optional)::

    members:
      enabled: true
      group: staff
      session_secret: <at least 32 bytes>
      groups_ttl: 86400
      removed_grace: 3600
      max_artifact_bytes: null   max_member_bytes: null   max_artifacts: null
      share_limits: {max_share_bytes: null, max_total_bytes: null}
      share_creates_per_hour: 20
      request_limits: {http_chunks_per_request, max_publish_files, max_message_chars, max_upload_bytes,
                       result_wait_seconds}
      rate_limits: {chunks_per_minute, writes_per_minute, session_exchanges_per_minute}
      staging: {staging_bytes_per_member}

``public_host`` is NOT authored here: it is ``control_plane.share.public_host`` (the deploy tooling copies it into the
collab slice, where this parser reads it back).
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

WHERE: Final = "control_plane.collab.members"
MIN_SECRET_BYTES: Final = 32
_TIB: Final = 1 << 40
HOST_RE: Final = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)

TOP_KEYS: Final = frozenset(
    {
        "enabled",
        "group",
        "session_secret",
        "groups_ttl",
        "removed_grace",
        "max_artifact_bytes",
        "max_member_bytes",
        "max_artifacts",
        "share_limits",
        "share_creates_per_hour",
        "request_limits",
        "rate_limits",
        "staging",
        "public_host",
        "session_seconds",
        "secrets_gid",
    }
)
REQUEST_KEYS: Final = frozenset(
    {
        "http_chunks_per_request",
        "max_publish_files",
        "max_message_chars",
        "max_upload_bytes",
        "result_wait_seconds",
    }
)
RATE_KEYS: Final = frozenset(
    {"chunks_per_minute", "writes_per_minute", "session_exchanges_per_minute"}
)
STAGING_KEYS: Final = frozenset({"staging_bytes_per_member"})
SHARE_LIMIT_KEYS: Final = frozenset({"max_share_bytes", "max_total_bytes"})


# Upper bounds (config load).  ``http_chunks_per_request`` x 512 KiB plus framing must fit the edge's 17m
# ``client_max_body_size`` (member-http.conf), so 32 is a hard ceiling; the rest keep a typo from disabling a limit.
MAX_HTTP_CHUNKS: Final = 32
_BOUNDS: Final = {
    "http_chunks_per_request": MAX_HTTP_CHUNKS,
    "max_publish_files": 10_000,
    "max_message_chars": 10_000,
    "chunks_per_minute": 100_000,
    "writes_per_minute": 10_000,
    "share_creates_per_hour": 10_000,
    "session_exchanges_per_minute": 10_000,
    "max_upload_bytes": _TIB,
    "staging_bytes_per_member": _TIB,
}
MAX_SESSION_SECONDS: Final = 7 * 86400


_NULLABLE: Final = frozenset({"max_upload_bytes", "staging_bytes_per_member"})


def _positive(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class MembersConfig:
    """The sanity, rate and staging limits of the member channel (plan 5, Global Constraints).  ``max_upload_bytes`` is the
    per-file limit handed to ``validate_chunk``; ``staging_bytes_per_member`` is the member's staging cap (reserved by
    declared size), on top of the whole-volume ``staging_bytes_total`` of the bridge limits."""

    max_upload_bytes: int | None = (
        _TIB  # None: no member cap, only the bridge limits and the chunk index bound
    )
    http_chunks_per_request: int = 32
    max_publish_files: int = 200
    max_message_chars: int = 500
    chunks_per_minute: int = 1200
    writes_per_minute: int = 30
    share_creates_per_hour: int = 20
    staging_bytes_per_member: int | None = (
        _TIB  # None: only the bridge's staging_bytes_per_agent applies
    )
    result_wait_seconds: float = 5.0
    session_exchanges_per_minute: int = 20

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            if f.name == "result_wait_seconds":
                v = self.result_wait_seconds
                if (
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    or not 0 <= v <= 20
                ):
                    raise ValueError("result_wait_seconds must be 0 to 20")
            elif f.name in _NULLABLE and getattr(self, f.name) is None:
                continue
            else:
                value = _positive(f.name, getattr(self, f.name))
                if value > _BOUNDS[f.name]:
                    raise ValueError(f"{f.name} must be at most {_BOUNDS[f.name]}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> MembersConfig:
        raw = dict(raw or {})
        known = {f.name for f in dataclasses.fields(cls)}
        if unknown := sorted(set(raw) - known):
            raise ValueError("unknown member limit(s): " + ", ".join(unknown))
        return cls(**raw)


def valid_public_host(value: object) -> bool:
    """A lowercase DNS name: no scheme, port, path, userinfo, uppercase, trailing dot or non-ASCII."""
    return (
        isinstance(value, str)
        and value.isascii()
        and HOST_RE.fullmatch(value) is not None
        and not value.rsplit(".", 1)[-1].isdigit()
    )  # an IPv4 literal is not a DNS name


def _section(raw: Mapping[str, Any], key: str, allowed: frozenset[str]) -> dict[str, Any]:
    section = raw.get(key)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ValueError(f"{WHERE}.{key} must be a mapping")
    if unknown := sorted(set(section) - allowed):
        raise ValueError(f"{WHERE}.{key}: unknown keys: " + ", ".join(unknown))
    return dict(section)


def _limits(raw: Mapping[str, Any], per_hour: int) -> MembersConfig:
    merged = {
        **_section(raw, "request_limits", REQUEST_KEYS),
        **_section(raw, "rate_limits", RATE_KEYS),
        **_section(raw, "staging", STAGING_KEYS),
        "share_creates_per_hour": per_hour,
    }
    for key, value in merged.items():
        try:
            MembersConfig.from_mapping({key: value})
        except ValueError as exc:
            raise ValueError(f"{WHERE}.{key}: {exc}") from None
    return MembersConfig.from_mapping(merged)


def parse_members(
    raw: object, *, public_host: object = None, session_seconds: object = None
) -> dict[str, Any] | None:
    """The validated section, or None unless ``enabled`` is exactly true.  Raises ``ValueError``.

    A disabled section is validated too (types, unknown keys, bounds), so a mistake shows now and not when someone
    flips it on; only ``group``, ``session_secret`` and the host are required just when enabled.  ``public_host`` and
    ``session_seconds`` (the share's) win over the authored keys, which are the slice form."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"{WHERE} must be a mapping")
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ValueError(f"{WHERE}.enabled must be true or false")
    on = raw.get("enabled") is True
    if unknown := sorted(set(raw) - TOP_KEYS):
        raise ValueError(f"{WHERE}: unknown keys: " + ", ".join(unknown))
    group = raw.get("group")
    if (on or group is not None) and (not isinstance(group, str) or not group.strip()):
        raise ValueError(f"{WHERE}.group must be a non-empty string")
    secret = raw.get("session_secret")
    if (on or secret is not None) and (
        not isinstance(secret, str)
        or not secret.isascii()
        or len(secret.encode()) < MIN_SECRET_BYTES
    ):
        raise ValueError(
            f"{WHERE}.session_secret must be a string of at least {MIN_SECRET_BYTES} bytes"
        )
    host = public_host if public_host is not None else raw.get("public_host")
    if host is None and on:
        raise ValueError(f"{WHERE} needs control_plane.share.public_host (the member web host)")
    if host is not None and not valid_public_host(host):
        raise ValueError(
            "control_plane.share.public_host must be a lowercase DNS name (no scheme, port or path)"
        )
    seconds = session_seconds if session_seconds is not None else raw.get("session_seconds", 14400)
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or not 60 <= seconds <= MAX_SESSION_SECONDS
    ):
        raise ValueError("control_plane.share.session_seconds must be an integer, 60 to 604800")
    ttl = raw.get("groups_ttl", 24 * 3600.0)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not ttl > 0:
        raise ValueError(f"{WHERE}.groups_ttl must be a positive number")
    grace = raw.get("removed_grace", 3600.0)
    if (
        isinstance(grace, bool)
        or not isinstance(grace, (int, float))
        or not 0 <= grace <= 30 * 86400
    ):
        raise ValueError(f"{WHERE}.removed_grace must be a number of seconds, 0 to 2592000")
    defaults: dict[str, Any] = {}
    for key in ("max_artifact_bytes", "max_member_bytes", "max_artifacts"):
        value = raw.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"{WHERE}.{key} must be a positive integer or null")
        defaults[key] = value
    share_limits = raw.get("share_limits", {})
    share_limits = {} if share_limits is None else share_limits
    if not isinstance(share_limits, Mapping) or (
        bad := sorted(set(share_limits) - SHARE_LIMIT_KEYS)
    ):
        raise ValueError(
            f"{WHERE}.share_limits must be a mapping with max_share_bytes and max_total_bytes"
            + (": unknown keys: " + ", ".join(bad) if isinstance(share_limits, Mapping) else "")
        )
    for key, value in share_limits.items():
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"{WHERE}.share_limits.{key} must be a positive integer or null")
    per_hour = raw.get("share_creates_per_hour", 20)
    if isinstance(per_hour, bool) or not isinstance(per_hour, int) or per_hour < 1:
        raise ValueError(f"{WHERE}.share_creates_per_hour must be a positive integer")
    limits = _limits(raw, per_hour)
    if not on:
        return None
    gid = raw.get("secrets_gid")
    if gid is not None and (isinstance(gid, bool) or not isinstance(gid, int) or gid < 0):
        raise ValueError(
            f"{WHERE}.secrets_gid must be a non-negative integer (the collab-public group id) or null"
        )
    return {
        "secrets_gid": gid,
        "group": group,
        "groups_ttl": float(ttl),
        "removed_grace": float(grace),
        "defaults": defaults,
        "share_limits": dict(share_limits),
        "share_creates_per_hour": per_hour,
        "session_seconds": seconds,
        "session_secret": secret,
        "public_host": host,
        "limits": limits,
    }
