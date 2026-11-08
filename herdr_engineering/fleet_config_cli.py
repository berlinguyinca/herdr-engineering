"""Operator tooling for the central fleet config: init, validate, token, revoke,
render-compose, render-collab, and the host-credential helpers `fleet join` shares.

Secrets are never printed, never placed in argv, and every file that holds one
is created 0600 atomically (os.open with the mode, then rename).

Layout: the central file lives alone in a dedicated 0700 directory
(``~/.config/herdr-engineering/fleet/fleet-config.yaml``). deploy.sh bind-mounts
that DIRECTORY read-only, so a rename-replace (our atomic writes, ``sed -i``, vim)
is visible inside the containers; a single-file mount would keep the old inode.
Dated backups go to the sibling ``fleet-backups/`` directory (never into the
mounted one), newest ``BACKUP_KEEP`` kept. Rewriting the YAML drops comments.
"""
from __future__ import annotations

import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .fleet_config import (
    CONTROL_PLANE_SECTION,
    SCHEMA_VERSION,
    FleetConfig,
    FleetConfigError,
    _parse,
    load_fleet_config,
)
from .https_origin import https_origin, origin_hostname

DEFAULT_CONFIG_PATH = Path("~/.config/herdr-engineering/fleet/fleet-config.yaml")
# Where the file lived before it got its own directory: init leaves a symlink here.
LEGACY_CONFIG_PATH = Path("~/.config/herdr-engineering/fleet-config.yaml")
BACKUP_KEEP = 10
HOST_JSON_REL = ".config/herdr-engineering/host.json"
LOCAL_TARGET = "local"
TOKEN_PLACEHOLDER = "<written to host>"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _expand(path: Path | str | None) -> Path:
    return Path(path if path else DEFAULT_CONFIG_PATH).expanduser()


# --------------------------------------------------------------------------
# atomic 0600 writes
# --------------------------------------------------------------------------

def write_private_file(path: Path, data: str | bytes, *, exclusive: bool = False) -> None:
    """Write ``data`` to ``path`` with mode 0600, never readable by others even
    briefly: a 0600 temp file is created with O_EXCL, then renamed over ``path``.
    With ``exclusive`` the final step refuses to replace an existing file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode() if isinstance(data, str) else data
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        if exclusive:
            try:
                os.link(tmp, path)  # fails if path exists
            except FileExistsError:
                raise FleetConfigError(f"{path} already exists; refusing to overwrite") from None
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def mint_host_token() -> tuple[str, str]:
    """(token, sha256 hex). 32 random bytes, urlsafe."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------
# compose env_file rendering
# --------------------------------------------------------------------------

def _compose_scalar(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if "\x00" in value or "\r" in value:
            raise FleetConfigError(
                f"control_plane.compose.{key}: value contains a NUL or carriage "
                "return, which compose env_file cannot represent")
        return value
    raise FleetConfigError(f"control_plane.compose.{key}: value must be a string, number or bool")


# Every variable compose.yaml substitutes with no default (or that deploy.sh reads).
# Extra names are allowed (e.g. HERDR_LLM_PROXY_PORT); these must be present.
COMPOSE_REQUIRED = ("HERDR_CP_PG_USER", "HERDR_CP_PG_PASSWORD", "HERDR_CP_MINIO_USER",
                    "HERDR_CP_MINIO_PASSWORD", "HERDR_CP_WEB_BIND", "HERDR_CP_WEB_PUBLIC_PORT",
                    "HERDR_CP_PG_PORT", "GATEWAY_TAILNET_IP")
LOOPBACK = "127.0.0.1"


def compose_env(cfg: FleetConfig) -> dict[str, str]:
    """Validated ``control_plane.compose`` mapping (names and stringified values).

    Also derives GATEWAY_BIND (the gateway's --bind list): loopback plus the tailnet
    address, or just loopback when they are the same, so no duplicate bind is emitted."""
    try:
        values = cfg.section(CONTROL_PLANE_SECTION)
    except FleetConfigError:
        raise FleetConfigError("no control_plane section in the fleet config") from None
    compose = values.get("compose")
    if not isinstance(compose, dict) or not compose:
        raise FleetConfigError("control_plane.compose must be a non-empty mapping of ENV_NAME: value")
    out: dict[str, str] = {}
    for key, value in compose.items():
        if not isinstance(key, str) or not _ENV_NAME.match(key):
            raise FleetConfigError(f"control_plane.compose: {key!r} is not a valid environment variable name")
        out[key] = _compose_scalar(key, value)
    missing = [k for k in COMPOSE_REQUIRED if not out.get(k)]
    if missing:
        raise FleetConfigError("control_plane.compose is missing required keys: " + ", ".join(missing))
    ip = out["GATEWAY_TAILNET_IP"]
    try:
        if "%" in ip:  # no zone ids: the value is substituted into a URL and a --bind list
            raise ValueError
        addr = ipaddress.ip_address(ip)  # one literal: no commas, spaces or host names
    except ValueError:
        raise FleetConfigError("control_plane.compose.GATEWAY_TAILNET_IP must be one IPv4 or "
                               "IPv6 address literal (e.g. the output of `tailscale ip -4`)") from None
    out["GATEWAY_BIND"] = LOOPBACK if ip == LOOPBACK else f"{LOOPBACK},{ip}"
    # host part of the gateway healthcheck URL (an IPv6 literal needs brackets)
    out["GATEWAY_HEALTH_HOST"] = f"[{ip}]" if addr.version == 6 else ip
    # Optional Hindsight memory profile: variables exist only while control_plane.memory.enabled is true.
    from .fleet_memory import memory_compose_env
    out.update(memory_compose_env(values, out))
    return out


def _quote_env_value(value: str) -> str:
    """Quote for docker compose ``env_file``.

    Single quotes are literal (no interpolation, no escapes, raw newlines allowed),
    so they carry any value that has no single quote and does not end in a
    backslash (compose rejects ``'abc\\'``). A value with a single quote
    is double-quoted instead: ``\\`` ``"`` ``$`` get a backslash and a newline
    becomes ``\\n``.
    """
    if "'" not in value and not value.endswith("\\"):
        return f"'{value}'"
    esc = (value.replace("\\", "\\\\").replace('"', '\\"')
           .replace("$", "\\$").replace("\n", "\\n"))
    return f'"{esc}"'


def render_compose(cfg: FleetConfig, out: Path) -> None:
    """Write the compose env file (0600, created atomically)."""
    env = compose_env(cfg)
    lines = [f"{k}={_quote_env_value(v)}" for k, v in env.items()]
    write_private_file(Path(out), "\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# collab slice (the public collab container's only configuration)
# --------------------------------------------------------------------------

# Public identifiers only: the slice is handed to the internet-facing collab container,
# so an unknown key (which could be a secret) is refused rather than passed along.
COLLAB_KEYS = frozenset({"cognito_pool", "cognito_clients", "cognito_group", "cognito_web_client",
                         "cognito_domain", "public_url", "operator_user", "operator_email",
                         "open_signup", "approver_groups", "dashboard_hosts", "replay_history", "steering",
                         "contextual", "insights", "team_rooms", "team_roles", "coordinators",
                         "fabric_trust",   # machine short names only, no secret
                         "skills",
                         "summary_fix",   # summary_fix: ids of the operator's summary-fix room and agent, no secret
                         "delegation_enabled",
                         "members",       # plan 5 member web: carries session_secret (the slice is 0600, collab-public only)
                         "task_lease_seconds"})   # a number of seconds, no secret


def collab_values(cfg: FleetConfig) -> dict:
    """Validated ``control_plane.collab`` mapping (optional section; required only
    when the collaboration network is enabled on this host)."""
    try:
        values = cfg.section(CONTROL_PLANE_SECTION)
    except FleetConfigError:
        raise FleetConfigError("no control_plane section in the fleet config") from None
    collab = values.get("collab")
    if not isinstance(collab, dict) or not collab:
        raise FleetConfigError("control_plane.collab is not set (needed when the collaboration "
                               "network is enabled: cognito_pool, cognito_clients, ...)")
    unknown = sorted(str(k) for k in set(collab) - COLLAB_KEYS)
    if unknown:
        raise FleetConfigError("control_plane.collab: unknown keys: " + ", ".join(unknown))
    pool = collab.get("cognito_pool")
    if not isinstance(pool, str) or not pool:
        raise FleetConfigError("control_plane.collab.cognito_pool is required")
    # collab-public always runs the local owner's no-sign-in listener, which acts as this
    # Cognito username; refusing here beats a container that crashes at startup.
    user = collab.get("operator_user")
    if not isinstance(user, str) or not user:
        raise FleetConfigError("control_plane.collab.operator_user is required (the Cognito username "
                               "the dashboard's no-sign-in collaboration acts as)")
    lease = collab.get("task_lease_seconds")
    if lease is not None and (isinstance(lease, bool) or not isinstance(lease, (int, float)) or not 60 <= lease <= 7 * 86400):
        raise FleetConfigError("control_plane.collab.task_lease_seconds must be a number of seconds, 60 to 604800")
    history = collab.get("replay_history", "since_join")
    if history not in ("full", "since_join"):
        raise FleetConfigError("control_plane.collab.replay_history must be 'full' or 'since_join'")
    from .control_plane.collab_workbench import normalize_steering
    try:
        normalize_steering(collab.get("steering"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    from .control_plane.collab_context import normalize_contextual
    try:
        normalize_contextual(collab.get("contextual"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    from .control_plane.collab_team_rooms import normalize_team_rooms
    try:
        normalize_team_rooms(collab.get("team_rooms"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    from .control_plane.collab_team_roles import normalize_team_roles
    try:
        normalize_team_roles(collab.get("team_roles"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    from .control_plane.collab_coordinators import normalize_coordinators
    try:
        normalize_coordinators(collab.get("coordinators"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    from .control_plane.collab_fabric_trust import normalize_fabric_trust
    try:
        normalize_fabric_trust(collab.get("fabric_trust"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    from .control_plane.collab import summary_fix_config
    try:
        summary_fix_config(collab.get("summary_fix"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    _check_members(collab, _share_mapping(cfg)[0], cfg)
    return dict(collab)


def _check_members(collab: dict, share: dict, cfg: FleetConfig | None = None) -> None:
    """Validate ``control_plane.collab.members`` (the member web host comes from ``control_plane.share.public_host``).

    Messages name keys, never values: the section carries ``session_secret``."""
    host = share.get("public_host")
    if host is not None and host == _loose_hostname(collab.get("public_url")):
        raise FleetConfigError("control_plane.share.public_host must be a host other than control_plane.collab."
                               "public_url's: the member web and share links live there, MCP stays on the collab host")
    members = collab.get("members")
    if members is None:
        return
    from .control_plane.members_config import parse_members
    for key in ("public_host", "session_seconds"):
        if isinstance(members, dict) and key in members:
            raise FleetConfigError(f"control_plane.collab.members.{key} is not authored here: set "
                                   f"control_plane.share.{key} (the deploy tooling copies it into the slice)")
    try:
        parse_members(members, public_host=host, session_seconds=share.get("session_seconds"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    if members.get("enabled") is True and cfg is not None:
        _check_members_prerequisites(cfg, share)


def _check_members_prerequisites(cfg: FleetConfig, share: dict) -> None:
    """Members that cannot work are refused here instead of failing silently at runtime (404 everywhere, no routes)."""
    try:
        values = cfg.section(CONTROL_PLANE_SECTION)
    except FleetConfigError:
        return
    if share.get("enabled") is not True:
        raise FleetConfigError("control_plane.collab.members.enabled needs control_plane.share.enabled: true "
                               "(the member host is routed by the share edge)")
    bridge = values.get("collab_artifacts")
    if not (isinstance(bridge, dict) and bridge.get("enabled") is True):
        raise FleetConfigError("control_plane.collab.members.enabled needs control_plane.collab_artifacts.enabled: "
                               "true (members publish through the collab artifact bridge)")
    keys = values.get("api_keys")
    if not keys:
        raise FleetConfigError("control_plane.collab.members.enabled needs control_plane.api_keys: without keys the "
                               "API treats every caller as the operator, who may read and share member artifacts")


def _members_with_host(values: dict, share: dict) -> dict:
    """``values`` with the share host and session lifetime copied into an ENABLED members section (collab-public only
    loads this slice, never ``control_plane.share``, so it reads both there)."""
    members = values.get("members")
    if isinstance(members, dict) and members.get("enabled") is True:
        slice_ = {**members, "public_host": share.get("public_host")}
        if share.get("session_seconds") is not None:
            slice_["session_seconds"] = share["session_seconds"]
        return {**values, "members": slice_}
    return values


def _collab_artifacts_slice(cfg: FleetConfig) -> dict:
    """The one thing collab-public needs of ``control_plane.collab_artifacts``: whether the bridge is on. Exactly
    ``enabled: true`` renders true; anything else (absent, false, "true", 1) renders false. No limit travels here: the
    worker publishes its limits in grants.json."""
    try:
        section = cfg.section(CONTROL_PLANE_SECTION).get("collab_artifacts")
    except FleetConfigError:
        section = None
    enabled = isinstance(section, dict) and section.get("enabled") is True
    return {"collab_artifacts": {"enabled": enabled}}


def fabric_listener(cfg: FleetConfig) -> tuple[str, int] | None:
    """(host, port) of control_plane.collab.fabric_trust's dedicated listener, or None when it is not configured. The
    values were validated (tailnet address only) by ``collab_values``; deploy.sh publishes exactly this address and port."""
    from .control_plane.collab_fabric_trust import normalize_fabric_trust
    trust = normalize_fabric_trust(collab_values(cfg).get("fabric_trust"))
    return (trust["listen_host"], trust["listen_port"]) if trust["listen_host"] else None


def render_collab(cfg: FleetConfig, out: Path) -> None:
    """Write a minimal fleet file (JSON, valid YAML, 0600) holding only control_plane.collab and the
    ``collab_artifacts: {enabled: <bool>}`` switch."""
    data = {"version": SCHEMA_VERSION,
            "sections": {CONTROL_PLANE_SECTION: {"roles": ["control-plane"],
                                                 "values": {"collab": _members_with_host(collab_values(cfg), _share_mapping(cfg)[0]),
                                                        **_collab_artifacts_slice(cfg)}}},
            "hosts": {}}
    _parse(data)
    write_private_file(Path(out), json.dumps(data, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# share slice (the public share container's only configuration)
# --------------------------------------------------------------------------

SHARE_SECTION_KEY = "share"
# control_plane.share as authored; an unknown key (which could be a secret meant elsewhere) is refused.
SHARE_KEYS = frozenset({"enabled", "max_share_bytes", "max_total_bytes", "max_files", "default_public_days",
                        "session_seconds", "signing_secret", "shares_dir", "events_dir", "marker_dir",
                        "annotations", "owner_emails", "public_url", "public_host"})
# What share-public gets: no directories (its mounts are fixed), nothing else from the central file.
SHARE_SLICE_KEYS = ("enabled", "max_share_bytes", "max_total_bytes", "max_files", "default_public_days",
                    "session_seconds", "signing_secret", "annotations", "public_url", "public_host")
# Cognito identifiers share-public needs for lab sign-in (public values; no group, no operator).
SHARE_COLLAB_KEYS = ("cognito_pool", "cognito_clients", "cognito_web_client", "cognito_domain", "public_url")
SHARE_FIXED_DIRS = {"shares_dir": "/shares", "events_dir": "/share-events",
                    "marker_dir": "/var/lib/herdr/share-markers"}
SHARE_CAP_DEFAULTS = {"max_share_bytes": 512 * 1024 * 1024, "max_files": 20000,
                      "max_total_bytes": 2 * 1024 * 1024 * 1024, "default_public_days": 30}
_SECRET_HINT = ("generate one locally (e.g. `openssl rand -hex 32`) and store it in the fleet YAML as "
                "control_plane.share.signing_secret; it is never printed")


def _share_mapping(cfg: FleetConfig) -> tuple[dict, dict]:
    """(control_plane.share, control_plane.collab) as mappings; either may be empty."""
    try:
        values = cfg.section(CONTROL_PLANE_SECTION)
    except FleetConfigError:
        return {}, {}
    share = values.get(SHARE_SECTION_KEY)
    if share is None:
        share = {}
    if not isinstance(share, dict):
        raise FleetConfigError("control_plane.share must be a mapping")
    collab = values.get("collab") if isinstance(values.get("collab"), dict) else {}
    return share, collab


def share_enabled(cfg: FleetConfig) -> bool:
    """True only when control_plane.share.enabled is exactly true."""
    return _share_mapping(cfg)[0].get("enabled") is True


def _loose_hostname(value: object) -> str | None:
    """Lowercase hostname of whatever the collab URL is (port, userinfo, trailing dot ignored); None if unparsable."""
    if not isinstance(value, str):
        return None
    try:
        host = urllib.parse.urlsplit(value.strip()).hostname
    except ValueError:
        return None
    return host.rstrip(".") if host else None


def share_values(cfg: FleetConfig) -> dict:
    """The validated share slice values ``{collab: {...}, share: {...}}``.

    Disabled (the default): only ``share.enabled: false`` is rendered and nothing else is required. Enabled: needs a
    signing secret of at least 32 bytes, an https ``control_plane.share.public_url`` origin of its own (not the
    MCP host's; the viewer's redirect is fixed at ``/s/callback`` at its root), the Cognito pool, and caps that the control plane itself accepts."""
    share, collab = _share_mapping(cfg)
    unknown = sorted(str(k) for k in set(share) - SHARE_KEYS)
    if unknown:
        raise FleetConfigError("control_plane.share: unknown keys: " + ", ".join(unknown))
    for key, fixed in SHARE_FIXED_DIRS.items():
        if key in share and share[key] != fixed:
            raise FleetConfigError(f"control_plane.share.{key} must be {fixed} (or left out): it is a container "
                                   f"path and deploy.sh's mounts are fixed")
    from .control_plane.annotation_anchor import annotation_settings, clean_owner_emails
    try:
        annotation_settings(share.get("annotations"))
        clean_owner_emails(share.get("owner_emails"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    if share.get("public_host") is not None:
        from .control_plane.members_config import valid_public_host
        host = share["public_host"]
        if not valid_public_host(host):
            raise FleetConfigError("control_plane.share.public_host must be a lowercase DNS name (no scheme, port or "
                                   "path): share links and the member web live there")
        if share.get("public_url") is not None and https_origin(share["public_url"]) != f"https://{host}":
            raise FleetConfigError("control_plane.share.public_url disagrees with control_plane.share.public_host: "
                                   "set only public_host")
        share = {**share, "public_url": f"https://{host}"}
    if share.get("enabled") is not True:
        return {"share": {"enabled": False}}
    from .control_plane.share_session import MIN_SECRET_BYTES
    secret = share.get("signing_secret")
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise FleetConfigError(f"control_plane.share.enabled is true but control_plane.share.signing_secret is "
                               f"missing or shorter than {MIN_SECRET_BYTES} bytes: {_SECRET_HINT}")
    origin = https_origin(share.get("public_url"))
    # Stricter than the server's parser on purpose: the lab redirect_uri is built from the raw value, and Cognito
    # matches callback URLs exactly, so the authored value must already be the lowercase origin.
    if (origin is None or share["public_url"].strip().rstrip("/") != origin
            or origin_hostname(origin) == _loose_hostname(collab.get("public_url"))):
        raise FleetConfigError("control_plane.share.public_url must be a bare https origin (lowercase ASCII host, "
                               "optional port, empty path or '/', no userinfo, query or fragment) on a host other "
                               "than control_plane.collab.public_url's: share links and the lab sign-in callback "
                               "/s/callback live at the root of the share host; MCP stays on "
                               "control_plane.collab.public_url")
    if not isinstance(collab.get("cognito_pool"), str) or not collab.get("cognito_pool"):
        raise FleetConfigError("control_plane.share needs control_plane.collab.cognito_pool")
    # The caps the control plane enforces (artifact_share_api.DEFAULTS; a test keeps the two in step). Not imported:
    # deploy.sh runs this on the host, where the control plane's database driver is not installed.
    for name in SHARE_CAP_DEFAULTS:
        value = share.get(name, SHARE_CAP_DEFAULTS[name])
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise FleetConfigError(f"control_plane.share.{name} must be a positive integer")
    if share.get("default_public_days", SHARE_CAP_DEFAULTS["default_public_days"]) > 90:
        raise FleetConfigError("control_plane.share.default_public_days must be 1-90")
    seconds = share.get("session_seconds", 14400)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 60:
        raise FleetConfigError("control_plane.share.session_seconds must be an integer >= 60")
    return {"collab": {k: collab[k] for k in SHARE_COLLAB_KEYS if k in collab},
            "share": {k: share[k] for k in SHARE_SLICE_KEYS if k in share}}


def render_share(cfg: FleetConfig, out: Path) -> None:
    """Write the share slice (JSON, valid YAML, 0600): only control_plane.share's serving keys and the Cognito
    identifiers share-public needs. Refuses (writing nothing) when share is enabled but incomplete."""
    data = {"version": SCHEMA_VERSION,
            "sections": {CONTROL_PLANE_SECTION: {"roles": ["control-plane"], "values": share_values(cfg)}},
            "hosts": {}}
    _parse(data)
    write_private_file(Path(out), json.dumps(data, indent=2, sort_keys=True) + "\n")


def check_share_slice(cfg: FleetConfig) -> None:
    """Refuse anything but a share slice (share-public must never get the central file)."""
    extra = sorted(set(cfg.section_names()) - {CONTROL_PLANE_SECTION})
    values = cfg.section(CONTROL_PLANE_SECTION) if CONTROL_PLANE_SECTION in cfg.section_names() else {}
    collab = values.get("collab") or {}
    share = values.get(SHARE_SECTION_KEY) or {}
    if (extra or set(values) - {"collab", SHARE_SECTION_KEY} or cfg.status()["hosts"]
            or not isinstance(collab, dict) or set(collab) - set(SHARE_COLLAB_KEYS)
            or not isinstance(share, dict) or set(share) - set(SHARE_SLICE_KEYS)):
        raise FleetConfigError("share-public reads only the render-share slice (control_plane.share serving keys + "
                               "Cognito identifiers); this file carries more (mount the slice, not the central file)")


# --------------------------------------------------------------------------
# artifact host slice (the host's only configuration: one JSON file, mounted as a compose secret)
# --------------------------------------------------------------------------

ARTIFACT_HOST_KEY = "artifact_host"
ARTIFACT_HOST_KEYS = frozenset(
    {
        "enabled",
        "public_host",
        "mcp_public_hosts",
        "session_seconds",
        "session_secret",
        "claim_key",
        "health_port",
        "db_password",
    }
)
ARTIFACT_HOST_SCHEMA_VERSION = (
    1  # herdr_artifact_host.host_config.SCHEMA_VERSION (a test keeps the two in step)
)
ARTIFACT_HOST_MIN_SECRET_BYTES = 32
ARTIFACT_HOST_MIN_PASSWORD = 16
ARTIFACT_HOST_MIN_DISTINCT = 10  # distinct characters a secret must hold ("a" * 32 and "ab" * 16 are refused)
# Fixed, not configurable: these must match deploy/artifact-host/compose.yaml (a test keeps them in step).
ARTIFACT_HOST_INTERNAL_BIND = "172.31.252.18"
ARTIFACT_HOST_SLICE_FILES = ("public.json", "internal.json", "migrate.json")
ARTIFACT_HOST_ADMIN_PASSWORD_FILE = "db-admin-password"
ARTIFACT_HOST_ROLES_SQL = "roles.sql"
ARTIFACT_HOST_ROLE_DB_USERS = {
    "public": "artifact_public",
    "internal": "artifact_internal",
    "migrate": "artifact_owner",
}
ARTIFACT_HOST_DEFAULT_HEALTH_PORT = 8092
_ARTIFACT_HOST_SECRET_HINT = "generate one locally (e.g. `openssl rand -hex 32`) and store it in the fleet YAML; it is never printed"


def artifact_host_db_passwords(db_password: str) -> dict[str, str]:
    """One database password per role (and the bootstrap admin's), each HMAC-SHA256(db_password, role).

    The fleet YAML therefore holds ONE value to generate and rotate, yet no container's slice holds a password that works
    for another role (an HMAC does not reveal its key or its siblings).  Rotating db_password changes every role's
    password at the next deploy (roles.sql re-applies them)."""
    import hmac

    return {
        role: hmac.new(
            db_password.encode("utf-8"), f"artifact-host-db:{role}".encode(), hashlib.sha256
        ).hexdigest()
        for role in (*ARTIFACT_HOST_ROLE_DB_USERS, "admin")
    }


def _distinct_enough(value: str) -> bool:
    return len(set(value)) >= ARTIFACT_HOST_MIN_DISTINCT


def _artifact_host_mapping(cfg: FleetConfig) -> dict:
    try:
        values = cfg.section(CONTROL_PLANE_SECTION)
    except FleetConfigError:
        return {}
    section = values.get(ARTIFACT_HOST_KEY)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise FleetConfigError("control_plane.artifact_host must be a mapping")
    return section


def artifact_host_enabled(cfg: FleetConfig) -> bool:
    """True only when control_plane.artifact_host.enabled is exactly true."""
    return _artifact_host_mapping(cfg).get("enabled") is True


def artifact_host_public_host(cfg: FleetConfig) -> str | None:
    """The host's public name when it is enabled (the edge's server_name), else None."""
    return _artifact_host_mapping(cfg).get("public_host") if artifact_host_enabled(cfg) else None


def artifact_host_slices(cfg: FleetConfig) -> dict[str, dict] | None:
    """The host's per-role configuration slices (herdr_artifact_host.host_config schema), or None when it is disabled.

    Cognito identifiers come from control_plane.collab; the dashboard's bearer key is control_plane.artifact_library.host_key
    (only its sha256 digest travels into the host).  Errors name keys, never values."""
    section = _artifact_host_mapping(cfg)
    unknown = sorted(str(k) for k in set(section) - ARTIFACT_HOST_KEYS)
    if unknown:
        raise FleetConfigError("control_plane.artifact_host: unknown keys: " + ", ".join(unknown))
    if section.get("enabled") is not True:
        return None
    if share_enabled(cfg):
        raise FleetConfigError(
            "control_plane.artifact_host and control_plane.share cannot both be enabled: the host "
            "serves /s/ and the member web itself and replaces share-public (set share.enabled: false)"
        )
    from .control_plane.members_config import valid_public_host

    host = section.get("public_host")
    if not valid_public_host(host):
        raise FleetConfigError(
            "control_plane.artifact_host.public_host must be a lowercase DNS name (no scheme, port "
            "or path)"
        )
    mcp_hosts = section.get("mcp_public_hosts", [])
    if (
        not isinstance(mcp_hosts, list)
        or not all(valid_public_host(h) for h in mcp_hosts)
        or host in mcp_hosts
    ):
        raise FleetConfigError(
            "control_plane.artifact_host.mcp_public_hosts must be a list of lowercase DNS names "
            "other than public_host (the names agents reach /mcp/artifacts on)"
        )
    secrets_: dict[str, str] = {}
    for key in ("session_secret", "claim_key"):
        value = section.get(key)
        if (
            not isinstance(value, str)
            or len(value.encode("utf-8")) < ARTIFACT_HOST_MIN_SECRET_BYTES
        ):
            raise FleetConfigError(
                f"control_plane.artifact_host.{key} is missing or shorter than "
                f"{ARTIFACT_HOST_MIN_SECRET_BYTES} bytes: {_ARTIFACT_HOST_SECRET_HINT}"
            )
        if not _distinct_enough(value):
            raise FleetConfigError(
                f"control_plane.artifact_host.{key} is too repetitive (fewer than "
                f"{ARTIFACT_HOST_MIN_DISTINCT} distinct characters): {_ARTIFACT_HOST_SECRET_HINT}"
            )
        secrets_[key] = value
    if secrets_["session_secret"] == secrets_["claim_key"]:
        raise FleetConfigError(
            "control_plane.artifact_host.session_secret and claim_key must differ"
        )
    password = section.get("db_password")
    if not isinstance(password, str) or len(password) < ARTIFACT_HOST_MIN_PASSWORD:
        raise FleetConfigError(
            "control_plane.artifact_host.db_password is missing or shorter than "
            f"{ARTIFACT_HOST_MIN_PASSWORD} characters: {_ARTIFACT_HOST_SECRET_HINT}"
        )
    if not _distinct_enough(password):
        raise FleetConfigError(
            "control_plane.artifact_host.db_password is too repetitive (fewer than "
            f"{ARTIFACT_HOST_MIN_DISTINCT} distinct characters): {_ARTIFACT_HOST_SECRET_HINT}"
        )
    seconds = section.get("session_seconds", 14400)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 60 <= seconds <= 7 * 86400:
        raise FleetConfigError(
            "control_plane.artifact_host.session_seconds must be an integer from 60 to 604800"
        )
    health = section.get("health_port", ARTIFACT_HOST_DEFAULT_HEALTH_PORT)
    if (
        isinstance(health, bool)
        or not isinstance(health, int)
        or not 1024 <= health <= 65535
        or health in (8090, 8091)
    ):
        raise FleetConfigError(
            "control_plane.artifact_host.health_port must be a port from 1024 to 65535 other than "
            "8090 and 8091"
        )
    collab = _share_mapping(cfg)[1]
    taken = {_loose_hostname(collab.get("public_url"))}
    dashboard_hosts = collab.get("dashboard_hosts")
    if isinstance(dashboard_hosts, list):
        taken |= {_loose_hostname(h) for h in dashboard_hosts}
    if host in taken:
        raise FleetConfigError(
            "control_plane.artifact_host.public_host must differ from the collaboration MCP host "
            "(control_plane.collab.public_url) and control_plane.collab.dashboard_hosts: the edge "
            "serves each name with its own server block"
        )
    cognito = {
        "pool_id": collab.get("cognito_pool"),
        "client_ids": collab.get("cognito_clients"),
        "web_client_id": collab.get("cognito_web_client"),
        "domain": collab.get("cognito_domain"),
        "group": collab.get("cognito_group"),
    }
    clients = cognito["client_ids"]
    missing = sorted(
        k for k, v in cognito.items() if not v or (k == "client_ids" and not isinstance(v, list))
    )
    if missing:
        raise FleetConfigError(
            "control_plane.artifact_host needs control_plane.collab.cognito_pool, cognito_clients "
            "(a list), cognito_web_client, cognito_domain and cognito_group; missing: "
            + ", ".join(missing)
        )
    del clients
    library = cfg.section(CONTROL_PLANE_SECTION).get("artifact_library")
    key = library.get("host_key") if isinstance(library, dict) else None
    if not isinstance(key, str) or not key:
        raise FleetConfigError(
            "control_plane.artifact_host needs control_plane.artifact_library.host_key (the "
            "dashboard's bearer key; only its sha256 digest reaches the host)"
        )
    passwords = artifact_host_db_passwords(password)
    users = ARTIFACT_HOST_ROLE_DB_USERS

    def database(role: str) -> dict[str, str]:
        return {"user": users[role], "password": passwords[role]}

    return {
        "public": {
            "version": ARTIFACT_HOST_SCHEMA_VERSION,
            "role": "public",
            "public_host": host,
            "public_url": f"https://{host}",
            "mcp_public_hosts": list(mcp_hosts),
            "session_seconds": seconds,
            "cognito": cognito,
            "session_secret": secrets_["session_secret"],
            "claim_key": secrets_["claim_key"],
            "health_port": health,
            "database": database("public"),
        },
        # Only the digest of the dashboard's key, and no session or claim key: this role serves the dashboard.
        "internal": {
            "version": ARTIFACT_HOST_SCHEMA_VERSION,
            "role": "internal",
            "internal_key_digest": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "bind": ARTIFACT_HOST_INTERNAL_BIND,
            "database": database("internal"),
        },
        "migrate": {
            "version": ARTIFACT_HOST_SCHEMA_VERSION,
            "role": "migrate",
            "database": database("migrate"),
        },
    }


def artifact_host_roles_sql(cfg: FleetConfig) -> str:
    """The idempotent role/grant script deploy.sh pipes into the database container as its bootstrap admin on every deploy.

    Three non-superuser login roles: ``artifact_owner`` owns the database and runs the migrations (DDL),
    ``artifact_public`` gets DML only, ``artifact_internal`` gets SELECT only (and ``default_transaction_read_only``).  The
    script runs before the migration so default privileges apply to every table the owner creates."""
    slices = artifact_host_slices(cfg)
    if slices is None:
        raise FleetConfigError("control_plane.artifact_host is not enabled")
    users = {r: slices[r]["database"] for r in ARTIFACT_HOST_ROLE_DB_USERS}
    owner, public, internal = users["migrate"], users["public"], users["internal"]
    lines = [
        "-- generated by `herdr-eng fleet config render-artifact-host`; idempotent; contains role passwords",
        "\\set ON_ERROR_STOP on",
    ]
    for u in (owner, public, internal):
        name, pw = u["user"], u["password"]
        lines += [
            f"SELECT 'CREATE ROLE {name} LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{name}')\\gexec",
            f"ALTER ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{pw}';",
        ]
    o, p, i = owner["user"], public["user"], internal["user"]
    lines += [
        f"ALTER DATABASE artifact_host OWNER TO {o};",
        "REVOKE ALL ON DATABASE artifact_host FROM PUBLIC;",
        f"GRANT CONNECT ON DATABASE artifact_host TO {o}, {p}, {i};",
        f"ALTER SCHEMA public OWNER TO {o};",
        "REVOKE ALL ON SCHEMA public FROM PUBLIC;",
        f"GRANT USAGE ON SCHEMA public TO {p}, {i};",
        f"ALTER ROLE {i} SET default_transaction_read_only = on;",
        f"ALTER ROLE {p} SET default_transaction_read_only = off;",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {o} IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {p};",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {o} IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {p};",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {o} IN SCHEMA public GRANT SELECT ON TABLES TO {i};",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {p};",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {p};",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {i};",
        "",
    ]
    return "\n".join(lines)


def render_artifact_host(cfg: FleetConfig, out: Path) -> None:
    """Write the host's files into the directory ``out`` (created 0700; every file 0600) when it is enabled; when disabled
    write nothing and remove nothing: one JSON slice per role, the bootstrap admin's password and roles.sql."""
    slices = artifact_host_slices(cfg)
    if slices is None:
        return
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    for role, name in zip(ARTIFACT_HOST_ROLE_DB_USERS, ARTIFACT_HOST_SLICE_FILES, strict=True):
        write_private_file(out / name, json.dumps(slices[role], indent=2, sort_keys=True) + "\n")
    section = _artifact_host_mapping(cfg)
    admin = artifact_host_db_passwords(section["db_password"])["admin"]
    write_private_file(out / ARTIFACT_HOST_ADMIN_PASSWORD_FILE, admin)
    write_private_file(out / ARTIFACT_HOST_ROLES_SQL, artifact_host_roles_sql(cfg))


# Marker line in collab-edge/nginx.conf replaced by the share snippet (zones, upstream and the share host's own
# server block) when shares are enabled. Disabled, the rendered file is the template byte for byte (and names no
# share-public upstream nginx would have to resolve).
EDGE_SHARE_MARKER = "  # @share-http@ (deploy.sh: share-http.conf when control_plane.share.enabled: zones, upstream, the share host's server)\n"
EDGE_SHARE_SNIPPET = "share-http.conf"
EDGE_SHARE_HOST_MARKER = "@share_host@"


EDGE_MEMBER_MARKER = "    # @member-locations@\n"
EDGE_MEMBER_SNIPPET = "member-http.conf"
EDGE_MEMBER_ZONES_MARKER = "  # @member-zones@\n"
EDGE_MEMBER_ZONES = (
    "  # The member web UI and API: their own zones (the share zones are for /s/).\n"
    "  limit_req_zone $remote_addr zone=member_req:10m rate=10r/s;\n"
    "  limit_conn_zone $remote_addr zone=member_conn:10m;\n")


EDGE_ARTIFACT_HOST_SNIPPET = "artifact-host-http.conf"
EDGE_ARTIFACT_MCP_MARKER = "    # @artifact-mcp@ (render-edge: artifact-host-mcp.conf when control_plane.artifact_host.enabled: POST /mcp/artifacts)\n"
EDGE_ARTIFACT_MCP_SNIPPET = "artifact-host-mcp.conf"
EDGE_ARTIFACT_HOST_MARKER = "@artifact_host@"


def render_edge_conf(
    template: str,
    *,
    share: bool,
    share_host: str | None = None,
    snippets_dir: Path | None = None,
    members: bool = False,
    artifact_host: str | None = None,
) -> str:
    """The edge's nginx.conf: the template, plus the share host's zones, upstream and server when ``share`` is true.

    ``share_host`` (required then) is the share origin's host, lowercase, a port allowed; the server answers to the
    hostname (nginx matches ``server_name`` without the port).  ``artifact_host`` (a lowercase DNS name) renders the
    artifact host's server INSTEAD of the share host's, plus its /mcp/artifacts location on the MCP server."""
    if artifact_host is not None:
        if share:
            raise FleetConfigError(
                "the edge serves either share-public or the artifact host, not both"
            )
        from .control_plane.members_config import valid_public_host

        if not valid_public_host(artifact_host):
            raise FleetConfigError(
                f"the edge needs the artifact host's DNS name, got {artifact_host!r}"
            )
        base = (
            Path(snippets_dir)
            if snippets_dir
            else (Path(__file__).resolve().parents[1] / "deploy" / "control-plane" / "collab-edge")
        )
        for marker in (EDGE_SHARE_MARKER, EDGE_ARTIFACT_MCP_MARKER):
            if template.count(marker) != 1:
                raise FleetConfigError(
                    f"edge template must hold the marker line {marker.strip()!r} exactly once"
                )
        server = (base / EDGE_ARTIFACT_HOST_SNIPPET).read_text(encoding="utf-8")
        if EDGE_ARTIFACT_HOST_MARKER not in server:
            raise FleetConfigError(
                f"{EDGE_ARTIFACT_HOST_SNIPPET} must hold {EDGE_ARTIFACT_HOST_MARKER}"
            )
        mcp = (base / EDGE_ARTIFACT_MCP_SNIPPET).read_text(encoding="utf-8")
        return template.replace(
            EDGE_SHARE_MARKER, server.replace(EDGE_ARTIFACT_HOST_MARKER, artifact_host)
        ).replace(EDGE_ARTIFACT_MCP_MARKER, mcp)
    if not share:
        return template
    if (
        not isinstance(share_host, str)
        or https_origin("https://" + share_host) != "https://" + share_host
    ):
        raise FleetConfigError(
            "the edge needs the share host (a lowercase hostname, port allowed) when shares are "
            f"enabled, got {share_host!r}"
        )
    base = (
        Path(snippets_dir)
        if snippets_dir
        else (Path(__file__).resolve().parents[1] / "deploy" / "control-plane" / "collab-edge")
    )
    if template.count(EDGE_SHARE_MARKER) != 1:
        raise FleetConfigError(
            f"edge template must hold the marker line {EDGE_SHARE_MARKER.strip()!r} exactly once"
        )
    snippet = (base / EDGE_SHARE_SNIPPET).read_text(encoding="utf-8")
    if EDGE_SHARE_HOST_MARKER not in snippet:
        raise FleetConfigError(f"{EDGE_SHARE_SNIPPET} must hold {EDGE_SHARE_HOST_MARKER}")
    snippet = snippet.replace(EDGE_SHARE_HOST_MARKER, share_host.split(":")[0])
    if snippet.count(EDGE_MEMBER_MARKER) != 1:
        raise FleetConfigError(
            f"{EDGE_SHARE_SNIPPET} must hold the marker line {EDGE_MEMBER_MARKER.strip()!r} once"
        )
    member = (base / EDGE_MEMBER_SNIPPET).read_text(encoding="utf-8") if members else ""
    if snippet.count(EDGE_MEMBER_ZONES_MARKER) != 1:
        raise FleetConfigError(
            f"{EDGE_SHARE_SNIPPET} must hold the marker line {EDGE_MEMBER_ZONES_MARKER.strip()!r} once"
        )
    snippet = snippet.replace(EDGE_MEMBER_ZONES_MARKER, EDGE_MEMBER_ZONES if members else "")
    snippet = snippet.replace(
        EDGE_MEMBER_MARKER, member
    )  # members off: the share host serves /s/ and nothing else
    return template.replace(EDGE_SHARE_MARKER, snippet)


def _members_enabled(cfg: FleetConfig) -> bool:
    collab = cfg.section("control_plane").get("collab")
    members = collab.get("members") if isinstance(collab, dict) else None
    return isinstance(members, dict) and members.get("enabled") is True


def render_edge(cfg: FleetConfig, template: Path, out: Path) -> None:
    """Write the edge nginx.conf (0644: it holds no secret) rendered for this fleet config."""
    template = Path(template)
    share = share_enabled(cfg)
    share_host = None
    if share:
        origin = https_origin(share_values(cfg)["share"]["public_url"])
        share_host = origin.removeprefix("https://") if origin else None
    text = render_edge_conf(template.read_text(encoding="utf-8"), share=share, share_host=share_host,
                            snippets_dir=template.parent, members=share and _members_enabled(cfg),
                            artifact_host=artifact_host_public_host(cfg))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    out.chmod(0o644)


# --------------------------------------------------------------------------
# llm slice (the llm-proxy container's only configuration)
# --------------------------------------------------------------------------

LLM_SECTION = "llm"
LLM_PROXY_KEY = "llm_proxy"


def llm_slice(cfg: FleetConfig) -> dict:
    """A minimal fleet file holding only the ``llm`` section and
    ``control_plane.llm_proxy`` (tuning): no DSN, no API keys, no host hashes."""
    try:
        llm = cfg.section(LLM_SECTION)
    except FleetConfigError:
        raise FleetConfigError("no llm section in the fleet config (llm.upstream_url / upstream_key)") from None
    try:
        tuning = cfg.section(CONTROL_PLANE_SECTION).get(LLM_PROXY_KEY)
    except FleetConfigError:
        tuning = None
    if tuning is not None and not isinstance(tuning, dict):
        raise FleetConfigError("control_plane.llm_proxy must be a mapping")
    sections: dict[str, dict] = {LLM_SECTION: {"roles": ["builder"], "values": llm}}
    if tuning:
        sections[CONTROL_PLANE_SECTION] = {"roles": ["control-plane"],
                                           "values": {LLM_PROXY_KEY: tuning}}
    data = {"version": SCHEMA_VERSION, "sections": sections, "hosts": {}}
    _parse(data)
    return data


def check_llm_slice(cfg: FleetConfig) -> None:
    """Refuse anything but an llm slice (the llm-proxy must never get the central file)."""
    extra = sorted(set(cfg.section_names()) - {LLM_SECTION, CONTROL_PLANE_SECTION})
    cp_keys: set = set()
    if CONTROL_PLANE_SECTION in cfg.section_names():
        cp_keys = set(cfg.section(CONTROL_PLANE_SECTION)) - {LLM_PROXY_KEY}
    if extra or cp_keys or cfg.status()["hosts"]:
        raise FleetConfigError(
            "the llm-proxy reads only the render-llm slice (llm + control_plane.llm_proxy); "
            "this file carries more (mount the slice, not the central file)")


def render_llm(cfg: FleetConfig, out: Path) -> None:
    """Write the llm slice (JSON, valid YAML, 0600)."""
    write_private_file(Path(out), json.dumps(llm_slice(cfg), indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------

def _ask(answers: dict, key: str, label: str, prompt: Callable, secret_prompt: Callable,
         *, secret: bool = False, default: str | None = None) -> str:
    if key in answers and answers[key] not in (None, ""):
        return str(answers[key])
    suffix = f" [{default}]" if default else ""
    asker = secret_prompt if secret else prompt
    try:
        reply = str(asker(f"{label}{suffix}: ")).strip()
    except EOFError:
        raise FleetConfigError(f"{key} is required: not in --from-env/--answers and there is "
                               "no terminal to ask") from None
    if not reply and default is not None:
        return default
    if not reply:
        raise FleetConfigError(f"{key} is required")
    return reply


def _tailscale_ip() -> str | None:
    """First line of `tailscale ip -4`, or None when tailscale is absent or fails."""
    try:
        proc = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.strip().splitlines() if proc.returncode == 0 else []
    return lines[0].strip() or None if lines else None


def _collect_hosts(answers: dict, prompt: Callable) -> dict[str, list[str]]:
    raw = answers.get("hosts")
    hosts: dict[str, list[str]] = {}
    if raw is not None:
        items = raw.items() if isinstance(raw, dict) else [
            (h["id"], h.get("roles", ["host"])) if isinstance(h, dict) else (h, ["host"]) for h in raw]
        for hid, roles in items:
            hosts[str(hid)] = list(roles)
        return hosts
    while True:
        try:
            hid = str(prompt("host id (blank to finish): ")).strip()
        except EOFError:  # non-interactive: no hosts now, add them later with fleet join
            return hosts
        if not hid:
            return hosts
        try:
            roles = str(prompt(f"roles for {hid} (comma separated, default host): ")).strip()
        except EOFError:
            roles = ""
        hosts[hid] = [r.strip() for r in roles.split(",") if r.strip()] or ["host"]


API_KEYS_WARNING = (
    "warning: --with-api-keys turns on X-API-Key auth for the control plane. The browser "
    "dashboard, the llm-proxy and the emit hook do not send keys yet, so they will get "
    "401 until they do. Leave control_plane.api_keys empty ({}) to keep the tailnet-open "
    "posture.")


OPTIONAL_SECRET_LABELS = {"github_token": "GitHub token"}


def init_config(path: Path, answers: dict | None = None, prompt: Callable = input,
                secret_prompt: Callable | None = None, *, with_api_keys: bool = False,
                optional_secrets: tuple[str, ...] = ()) -> Path:
    """Create a new fleet config. Generates DB/MinIO credentials.

    The control plane stays open on the tailnet (``control_plane.api_keys: {}``) and
    ``ingest.api_key`` / ``agent.api_key`` are empty, so clients send no X-API-Key.
    ``with_api_keys`` generates operator/ingest/agent keys instead (keyed mode; the
    dashboard, llm-proxy and emit hook do not support keys yet - see API_KEYS_WARNING).

    Hosts get a placeholder hash of a discarded token: they cannot authenticate
    until ``fleet join`` / ``fleet config token`` installs a real one.
    """
    path = _expand(path)
    if path.exists():
        raise FleetConfigError(f"{path} already exists; refusing to overwrite")
    a = dict(answers or {})
    secret_prompt = secret_prompt or getpass.getpass  # resolved per call (tests patch it)
    ask = lambda k, label, **kw: _ask(a, k, label, prompt, secret_prompt, **kw)  # noqa: E731
    for key in optional_secrets:  # asked only when missing; blank or no terminal = none
        if not a.get(key):
            try:
                a[key] = str(secret_prompt(f"{OPTIONAL_SECRET_LABELS.get(key, key)} (blank = none): ")).strip()
            except EOFError:
                a[key] = ""
    public_url = ask("public_url", "public URL of the control plane")
    llm_url = ask("llm_upstream_url", "LLM upstream address")
    llm_key = ask("llm_upstream_key", "LLM upstream key", secret=True)
    ci_url = ask("ci_url", "CI address")
    ci_token = ask("ci_token", "CI token", secret=True)
    web_bind = ask("web_bind", "web bind address", default="127.0.0.1")
    web_port = ask("web_port", "web public port", default="80")
    pg_port = str(a.get("pg_port") or "5432")
    # fabric-gateway (host network) binds this address: default to this machine's
    # tailnet IP when tailscale answers, otherwise the operator must say.
    gateway_ip = ask("gateway_tailnet_ip", "gateway tailnet IP (127.0.0.1 = loopback only)",
                     default=_tailscale_ip())
    hosts = _collect_hosts(a, prompt)

    # Existing credentials (imported with --from-env/--answers) are kept: Postgres and
    # MinIO apply their users only to an EMPTY volume, so new ones would not work.
    pg_user = str(a.get("pg_user") or "") or f"herdr_{secrets.token_hex(4)}"
    pg_pass = str(a.get("pg_password") or "") or secrets.token_hex(16)
    minio_user = str(a.get("minio_user") or "") or f"herdr_{secrets.token_hex(4)}"
    minio_pass = str(a.get("minio_password") or "") or secrets.token_hex(16)
    api_keys: dict[str, str] = {}
    ingest_key = agent_key = ""  # empty: hosts send no X-API-Key (open control plane)
    if with_api_keys:
        ingest_key = secrets.token_urlsafe(32)  # role ingest: every host posts events with it
        agent_key = secrets.token_urlsafe(32)   # role agent: ingest + LLM-key mint/revoke + reviews
        api_keys = {secrets.token_urlsafe(32): "operator", ingest_key: "ingest", agent_key: "agent"}
    dsn = build_pg_dsn(pg_user, pg_pass)
    bucket = str(a.get("rustfs_bucket") or "") or "herdr-artifacts"
    ci_actions = str(a.get("ci_actions", "")).strip().lower() in ("1", "true", "yes", "on")
    extra_compose = a.get("compose_extra") or {}
    if not isinstance(extra_compose, dict):
        raise FleetConfigError("compose_extra must be a mapping of ENV_NAME: value")
    data = {
        "version": SCHEMA_VERSION,
        "sections": {
            CONTROL_PLANE_SECTION: {"roles": ["control-plane"], "values": {
                "public_url": public_url,
                "pg_dsn": dsn,
                "host_aliases": str(a.get("host_aliases") or ""),
                # {} = open on the tailnet (today's posture). Keyed mode also needs the
                # dashboard, llm-proxy and emit hook to carry keys (not supported yet).
                "api_keys": api_keys,
                "rustfs_endpoint": "http://rustfs:9000",
                "rustfs_bucket": bucket,
                "ci_actions": ci_actions,  # true lets operators retry/cancel CI pipelines from the UI
                "minio_user": minio_user,
                "minio_password": minio_pass,
                "compose": {
                    "HERDR_CP_PG_USER": pg_user,
                    "HERDR_CP_PG_PASSWORD": pg_pass,
                    "HERDR_CP_MINIO_USER": minio_user,
                    "HERDR_CP_MINIO_PASSWORD": minio_pass,
                    "HERDR_CP_WEB_BIND": web_bind,
                    "HERDR_CP_WEB_PUBLIC_PORT": web_port,
                    "HERDR_CP_PG_PORT": pg_port,
                    "GATEWAY_TAILNET_IP": gateway_ip,
                    **{str(k): str(v) for k, v in extra_compose.items()},
                },
            }},
            "ingest": {"roles": ["all"], "values": {"api_key": ingest_key}},
            "agent": {"roles": ["all"], "values": {"api_key": agent_key}},
            "llm": {"roles": ["builder"], "values": {"upstream_url": llm_url, "upstream_key": llm_key}},
            "ci": {"roles": ["builder", "ci"], "values": {"url": ci_url, "token": ci_token}},
            "github": {"roles": ["ci"], "values": {"token": str(a.get("github_token") or "")}},
            "fabric": {"roles": ["all"], "values": {"url": str(a.get("fabric_url") or ""),
                                                    "token": str(a.get("fabric_token") or "")}},
            "inspector": {"roles": ["all"], "values": {"enabled": False, "reasoning": "off"}},
        },
        "hosts": {hid: {"roles": roles, "token_sha256": mint_host_token()[1], "overrides": {}}
                  for hid, roles in hosts.items()},
    }
    _parse(data)
    compose_env(_parse(data))  # reject unrepresentable values before writing
    check_credentials_consistent(_parse(data))
    created = not path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if created:
        os.chmod(path.parent, 0o700)
    write_private_file(path, yaml.safe_dump(data, sort_keys=False), exclusive=True)
    load_fleet_config(path)  # final check of what is on disk
    _link_legacy_path(path)
    return path


def build_pg_dsn(user: str, password: str, host: str = "postgres", port: int = 5432,
                 db: str = "herdr") -> str:
    """The web/worker DSN. User and password are percent-encoded, so imported
    credentials containing @ : / ? # or spaces stay unambiguous."""
    q = lambda v: urllib.parse.quote(v, safe="")  # noqa: E731
    return f"postgresql://{q(user)}:{q(password)}@{host}:{port}/{db}"


def _dsn_credentials(dsn: str) -> tuple[str, str, str] | None:
    """(user, password, host) of a postgres DSN, decoded; None when unparseable."""
    try:
        parts = urllib.parse.urlsplit(dsn)
        if parts.scheme not in ("postgresql", "postgres") or parts.username is None:
            return None
        return (urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or ""),
                parts.hostname or "")
    except ValueError:
        return None


def check_credentials_consistent(cfg: FleetConfig) -> None:
    """pg_dsn, minio_* and compose.HERDR_CP_{PG,MINIO}_* must name the same users and
    passwords (when the DSN points at the compose `postgres` service). Names only."""
    try:
        cp = cfg.section(CONTROL_PLANE_SECTION)
    except FleetConfigError:
        return
    compose = cp.get("compose") if isinstance(cp.get("compose"), dict) else {}
    bad: list[str] = []
    creds = _dsn_credentials(str(cp.get("pg_dsn") or ""))
    if creds and creds[2] == "postgres":
        if "HERDR_CP_PG_USER" in compose and str(compose["HERDR_CP_PG_USER"]) != creds[0]:
            bad.append("pg_dsn user vs compose.HERDR_CP_PG_USER")
        if "HERDR_CP_PG_PASSWORD" in compose and str(compose["HERDR_CP_PG_PASSWORD"]) != creds[1]:
            bad.append("pg_dsn password vs compose.HERDR_CP_PG_PASSWORD")
    for key, ckey in (("minio_user", "HERDR_CP_MINIO_USER"), ("minio_password", "HERDR_CP_MINIO_PASSWORD")):
        if key in cp and ckey in compose and str(cp[key]) != str(compose[ckey]):
            bad.append(f"{key} vs compose.{ckey}")
    if bad:
        raise FleetConfigError("control_plane credentials disagree: " + "; ".join(bad))


# --------------------------------------------------------------------------
# importing an existing setup (init --from-env / --answers)
# --------------------------------------------------------------------------

# env name -> init answer key. Values are never printed; reports carry names only.
ENV_IMPORT_MAP = {
    "HERDR_CP_PG_USER": "pg_user", "HERDR_CP_PG_PASSWORD": "pg_password",
    "HERDR_CP_MINIO_USER": "minio_user", "HERDR_CP_MINIO_PASSWORD": "minio_password",
    "HERDR_CP_WEB_BIND": "web_bind", "HERDR_CP_WEB_PUBLIC_PORT": "web_port",
    "HERDR_CP_PG_PORT": "pg_port", "GATEWAY_TAILNET_IP": "gateway_tailnet_ip",
    "FABRIC_LLM_UPSTREAM_KEY": "llm_upstream_key", "FABRIC_LLM_UPSTREAM_URL": "llm_upstream_url",
    "HERDR_CP_HOST_ALIASES": "host_aliases", "HERDR_CP_RUSTFS_BUCKET": "rustfs_bucket",
    "HERDR_ENGINEERING_CI_URL": "ci_url", "HERDR_ENGINEERING_CI_TOKEN": "ci_token",
    "HERDR_ENGINEERING_CI_ACTIONS": "ci_actions", "HERDR_ENGINEERING_GITHUB_TOKEN": "github_token",
    "HERDR_ENGINEERING_FABRIC_URL": "fabric_url", "HERDR_CP_URL": "public_url",
}
# env name -> answer key whose value is the CONTENT of the named file
ENV_IMPORT_FILE_MAP = {
    "FABRIC_LLM_UPSTREAM_KEY_FILE": "llm_upstream_key",
    "HERDR_ENGINEERING_CI_TOKEN_FILE": "ci_token", "HERDR_ENGINEERING_CI_TOKEN_HOST_FILE": "ci_token",
    "HERDR_ENGINEERING_GITHUB_TOKEN_FILE": "github_token",
    "HERDR_ENGINEERING_GITHUB_TOKEN_HOST_FILE": "github_token",
}
# carried into control_plane.compose as-is (docker-level settings)
ENV_COMPOSE_PASSTHROUGH = ("HERDR_LLM_PROXY_PORT", "HERDR_COLLAB_CERT_DIR", "HERDR_COLLAB_CERT_UID",
                           "HERDR_COLLAB_EDGE_BIND", "HERDR_COLLAB_OPERATOR_PORT")


def parse_env_file(text: str) -> tuple[dict[str, str], list[int]]:
    r"""Parse a docker-compose style env file without any shell evaluation.

    Handles blank lines, ``#`` comments, an ``export`` prefix, CRLF, ``=`` inside
    values, single quotes (literal, may span lines), double quotes (``\\ \" \n \$``
    escapes, may span lines) and unquoted values with an inline `` #`` comment.
    Returns (values, line numbers that were not NAME=value). Never raises on content.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: dict[str, str] = {}
    bad: list[int] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        lineno, line = i + 1, lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
        name, sep, rest = line.partition("=")
        name = name.strip()
        if not sep or not _ENV_NAME.match(name):
            bad.append(lineno)
            continue
        rest = rest.lstrip()
        if rest[:1] in ("'", '"'):
            q, buf, body = rest[0], [], rest[1:]
            while True:
                closed, k = False, 0
                while k < len(body):
                    c = body[k]
                    if q == '"' and c == "\\" and k + 1 < len(body):
                        n = body[k + 1]
                        buf.append({"n": "\n", "t": "\t"}.get(n, n))
                        k += 2
                        continue
                    if c == q:
                        closed = True
                        break
                    buf.append(c)
                    k += 1
                if closed:
                    break
                if i >= len(lines):     # unterminated quote: drop the entry
                    buf = None
                    break
                buf.append("\n")
                body = lines[i]
                i += 1
            if buf is None:
                bad.append(lineno)
                continue
            out[name] = "".join(buf)
        else:
            value = rest
            hash_at = value.find(" #")
            if hash_at >= 0:
                value = value[:hash_at]
            out[name] = value.rstrip()
    return out, bad


def import_env_file(path: Path) -> tuple[dict, dict]:
    """(answers, report) from an old env file such as control-plane.env. The report
    lists variable NAMES only: imported, unknown, empty, unreadable files, bad lines."""
    path = Path(path).expanduser()
    try:
        values, bad_lines = parse_env_file(path.read_text())
    except (OSError, UnicodeDecodeError):
        raise FleetConfigError(f"cannot read {path}") from None
    answers: dict[str, Any] = {}
    report: dict[str, list] = {"imported": [], "unknown": [], "empty": [], "unreadable_files": [],
                               "bad_lines": bad_lines}
    compose_extra: dict[str, str] = {}
    dsn = values.get("HERDR_CP_PG_DSN")
    for name, value in values.items():
        if name == "HERDR_CP_PG_DSN":
            continue
        if value == "":
            report["empty"].append(name)
            continue
        if name in ENV_IMPORT_MAP:
            answers[ENV_IMPORT_MAP[name]] = value
            report["imported"].append(name)
        elif name in ENV_IMPORT_FILE_MAP:
            try:
                content = Path(value).expanduser().read_text().strip()
            except (OSError, UnicodeDecodeError):
                report["unreadable_files"].append(name)
                continue
            if content:
                answers.setdefault(ENV_IMPORT_FILE_MAP[name], content)
                report["imported"].append(name)
            else:
                report["empty"].append(name)
        elif name in ENV_COMPOSE_PASSTHROUGH:
            compose_extra[name] = value
            report["imported"].append(name)
        else:
            report["unknown"].append(name)
    if dsn:
        creds = _dsn_credentials(dsn)
        if creds is None:
            report["unknown"].append("HERDR_CP_PG_DSN")
        else:
            for key, got in (("pg_user", creds[0]), ("pg_password", creds[1])):
                if key in answers and answers[key] != got:
                    raise FleetConfigError(
                        f"{path}: HERDR_CP_PG_DSN and HERDR_CP_{'PG_USER' if key == 'pg_user' else 'PG_PASSWORD'} "
                        "disagree; fix the file (values not shown)")
                answers.setdefault(key, got)
            report["imported"].append("HERDR_CP_PG_DSN")
    if compose_extra:
        answers["compose_extra"] = compose_extra
    # The pre-feature stack kept these tokens in fixed files the env file never named
    # (compose.github.yaml / compose.integrations.yaml defaults): fall back to them.
    report["tokens"] = {}
    for key, (dotted, rel) in LEGACY_TOKEN_FILES.items():
        if not answers.get(key):
            try:
                content = (Path.home() / rel).read_text().strip()
            except (OSError, UnicodeDecodeError):
                content = ""
            if content:
                answers[key] = content
        report["tokens"][dotted] = "imported" if answers.get(key) else "missing"
    return answers, report


LEGACY_TOKEN_FILES = {"github_token": ("github.token", ".config/herdr-engineering/github-token"),
                      "ci_token": ("ci.token", ".config/herdr-engineering/ci-token")}


ANSWER_KEYS = frozenset(set(ENV_IMPORT_MAP.values()) | set(ENV_IMPORT_FILE_MAP.values()) | {
    "public_url", "llm_upstream_url", "llm_upstream_key", "ci_url", "ci_token", "web_bind",
    "web_port", "pg_port", "gateway_tailnet_ip", "host_aliases", "github_token", "fabric_url",
    "fabric_token", "hosts", "compose_extra"})


def load_answers_file(path: Path) -> dict:
    """YAML/JSON mapping of init answers. Must not be group/other accessible (0600)."""
    path = Path(path).expanduser()
    try:
        mode = path.stat().st_mode
    except OSError:
        raise FleetConfigError(f"cannot read {path}") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise FleetConfigError(f"{path} must not be accessible by group or other (chmod 600)")
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise FleetConfigError(f"{path} is not a readable YAML/JSON mapping") from None
    if not isinstance(data, dict):
        raise FleetConfigError(f"{path} must hold a mapping of answer keys")
    unknown = sorted(str(k) for k in set(data) - ANSWER_KEYS)
    if unknown:
        raise FleetConfigError(f"{path}: unknown answer keys: " + ", ".join(unknown))
    return data


def _link_legacy_path(path: Path) -> None:
    """When the file is created at the default location, leave a relative symlink at
    the old path (if nothing is there) so tools pointed at it keep working."""
    if path != _expand(DEFAULT_CONFIG_PATH):
        return
    legacy = _expand(LEGACY_CONFIG_PATH)
    if os.path.lexists(legacy):
        return
    try:
        os.symlink(os.path.relpath(path, legacy.parent), legacy)
    except OSError:
        pass  # a convenience only


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------

def validate_config(path: Path) -> dict:
    """Load and require a renderable control_plane.compose map. Names only, no values."""
    cfg = load_fleet_config(_expand(path))
    compose_env(cfg)
    check_credentials_consistent(cfg)
    from .control_plane.graph_merge_gate import validate_section
    try:
        validate_section((cfg.section("control_plane") or {}).get("graph_merge_gate"))
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None
    check_artifact_library(cfg)
    artifact_host_slices(cfg)   # names the missing or weak key when the host is enabled but incomplete
    return {"revision": cfg.revision, "hosts": len(cfg.status()["hosts"])}


def check_artifact_library(cfg: FleetConfig) -> None:
    """Validate ``control_plane.artifact_library`` (host_url, host_key, timeout_seconds).  The key is never echoed."""
    from .control_plane.artifact_host_client import parse_library_host
    section = (cfg.section(CONTROL_PLANE_SECTION) if CONTROL_PLANE_SECTION in cfg.section_names() else {}).get("artifact_library")
    try:
        parse_library_host(section)
    except ValueError as exc:
        raise FleetConfigError(str(exc)) from None


# --------------------------------------------------------------------------
# host credentials (shared by `fleet join` and `fleet config token`)
# --------------------------------------------------------------------------

def _install_script() -> str:
    return ('umask 077; d="$HOME/.config/herdr-engineering"; mkdir -p "$d" && '
            'cat > "$d/host.json.new" && chmod 600 "$d/host.json.new" && '
            'mv "$d/host.json.new" "$d/host.json"')


def install_host_json(target: str, payload: dict, *, runner: Callable | None = None) -> None:
    """Put host.json on ``target`` (0600). The token travels on stdin only."""
    body = json.dumps(payload)
    if target == LOCAL_TARGET:
        write_private_file(Path.home() / HOST_JSON_REL, body)
        return
    proc = (runner or subprocess.run)(["ssh", "-o", "BatchMode=yes", "--", target, _install_script()],
                  input=body, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        # stderr from ssh/cat never contains the token (it is only on stdin)
        raise FleetConfigError(f"installing host.json on {target} failed (rc={proc.returncode}): "
                               f"{(proc.stderr or '').strip()[-200:]}")


def backup_dir(path: Path) -> Path:
    """``<dir>-backups`` beside the file's directory (``fleet`` -> ``fleet-backups``):
    never inside the directory deploy.sh mounts into the containers."""
    real = Path(os.path.realpath(_expand(path)))
    return real.parent.with_name(real.parent.name + "-backups")


def _private_dir(d: Path) -> Path:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _backup(path: Path, keep: int = BACKUP_KEEP) -> Path | None:
    """Dated 0600 copy in backup_dir(path); prunes all but the newest ``keep``."""
    if not path.exists():
        return None
    d = _private_dir(backup_dir(path))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = d / f"{path.name}.bak-{stamp}"
    write_private_file(dest, path.read_bytes())
    olds = sorted(d.glob(f"{path.name}.bak-*"))  # the stamp sorts chronologically
    for stale in olds[:-keep] if keep > 0 else olds:
        try:
            stale.unlink()
        except OSError:
            pass
    return dest


def _edit_central(path: Path, mutate: Callable[[dict], None]) -> Path | None:
    """Locked read-modify-write of the central file: validate before and after,
    dated backup, then an atomic write-new + fsync + rename onto the REAL file
    (symlinks resolved, so the compatibility link stays a link). Returns the backup."""
    real = Path(os.path.realpath(_expand(path)))
    lock = _private_dir(backup_dir(real)) / ".fleet-config.lock"
    with open(os.open(lock, os.O_RDWR | os.O_CREAT, 0o600), "r+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        load_fleet_config(real)  # refuses unsafe/invalid input
        data = yaml.safe_load(real.read_text())
        mutate(data)
        _parse(data)
        dest = _backup(real)
        write_private_file(real, yaml.safe_dump(data, sort_keys=False))
        return dest


def set_host(path: Path, host_id: str, token_sha256: str, roles: list[str] | None = None) -> None:
    """Add or replace ``host_id``'s hash in the file (backup first, validated, atomic rename)."""
    def mutate(data: dict) -> None:
        hosts = data.setdefault("hosts", {})
        old = hosts.get(host_id) or {}
        hosts[host_id] = {"roles": list(roles) if roles is not None else list(old.get("roles", ["host"])),
                          "token_sha256": token_sha256,
                          "overrides": old.get("overrides", {})}
    _edit_central(path, mutate)


def revoke_host(path: Path, host_id: str) -> Path | None:
    """Remove ``host_id`` (its token stops working on the server's next read).
    Backup first, validated, atomic rename. Returns the backup path."""
    def mutate(data: dict) -> None:
        hosts = data.get("hosts")
        if not isinstance(hosts, dict) or host_id not in hosts:
            raise FleetConfigError(f"unknown host: {host_id}")
        del hosts[host_id]
    return _edit_central(path, mutate)


def issue_host_credentials(config_path: Path, host_id: str, target: str, *,
                           roles: list[str] | None = None, control_plane_url: str | None = None,
                           must_exist: bool = False, runner: Callable | None = None) -> dict:
    """Mint a token, install it on ``target``, and only then persist its hash."""
    config_path = _expand(config_path)
    cfg = load_fleet_config(config_path)
    if must_exist and host_id not in cfg.status()["hosts"]:
        raise FleetConfigError(f"unknown host: {host_id}")
    url = control_plane_url
    if not url:
        try:
            url = cfg.section(CONTROL_PLANE_SECTION).get("public_url")
        except FleetConfigError:
            url = None
    if not url:
        raise FleetConfigError("control plane URL unknown: set control_plane.public_url or pass it")
    token, digest = mint_host_token()
    install_host_json(target, {"control_plane_url": url, "host_id": host_id, "token": token},
                      runner=runner)
    set_host(config_path, host_id, digest, roles)
    return {"ok": True, "host": host_id, "target": target, "token": TOKEN_PLACEHOLDER,
            "config": str(config_path)}
