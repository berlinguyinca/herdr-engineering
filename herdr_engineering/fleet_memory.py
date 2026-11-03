"""Hindsight shared memory in the central fleet config (``control_plane.memory``).

The memory backend is an optional compose profile (``memory``) of the control-plane
project. Its secrets are handled exactly like the Postgres/MinIO ones: generated ON THE
HOST, kept in the 0600 central file, rendered into the transient 0600 compose env file,
never printed, never placed in argv, validated before anything is started.

Schema (``sections.control_plane.values.memory``)::

    enabled: false                       # strict bool; nothing renders unless true
    image_tag: "5a672f0c..."             # tag of herdr-memory-hindsight (built by deploy.sh --with-memory)
    model_dir: /abs/path/to/e5-model     # pinned ONNX model directory, mounted read-only
    port: 8888                           # loopback-only host publish
    endpoint: https://memory.dev.lan     # what OTHER fabric hosts use (HTTPS through the fabric gateway)
    db_password: <generated>             # database password (applied to an EMPTY volume only)
    tenants: {project: <key>, global: <key>, infra: <key>, personal: <key>}

``personal`` is the personal-assistant tenant: its own key, its own tenant, never shared.
The non-secret endpoint is also published to every host through ``sections.memory``
(roles ``[all]``, only ``url``), which is how ``herdr-eng memory install`` learns it.

Errors name keys, never values.
"""
from __future__ import annotations

import ipaddress
import os
import re
import secrets
import stat
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .fleet_config import CONTROL_PLANE_SECTION, FleetConfig, FleetConfigError

TENANTS = ("project", "global", "infra", "personal")
PUBLIC_SECTION = "memory"           # roles [all]; only {url}
DEFAULT_PORT = 8888
# The default is the revision the herdr-memory deployment candidate pins for its image.
DEFAULT_IMAGE_TAG = "5a672f0cdb89f4a3b841770cce2036a5b28b799d"
MEMORY_KEYS = frozenset({"enabled", "image_tag", "model_dir", "port", "endpoint", "db_password", "tenants"})
MEMORY_COMPOSE_VARS = ("HERDR_MEMORY_DB_PASSWORD", "HERDR_MEMORY_TENANT_USERS", "HERDR_MEMORY_IMAGE_TAG",
                       "HERDR_MEMORY_MODEL_DIR", "HERDR_MEMORY_PORT")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{32,128}$")      # URL/CSV safe: no ':' ',' '@' '/' '$' or quotes
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# Host ports other services of the project already publish or the gateway owns.
_RESERVED_PORTS = frozenset({80, 443, 5432, 8080, 8088, 8089, 8787, 9000, 9001, 29999})


def _err(msg: str) -> FleetConfigError:
    return FleetConfigError(f"control_plane.memory: {msg}")


def _endpoint(value: Any, where: str = "control_plane.memory.endpoint") -> str:
    """An https origin (bare: no credentials, path, query or fragment). Plain http is refused:
    the herdr-memory client refuses it off loopback and we do not weaken that."""
    if not isinstance(value, str) or not value:
        raise FleetConfigError(f"{where} must be a non-empty https URL")
    try:
        p = urllib.parse.urlsplit(value)
        port = p.port
    except ValueError:
        raise FleetConfigError(f"{where} is not a valid URL") from None
    if p.scheme != "https":
        raise FleetConfigError(f"{where} must be an https URL (the client refuses plain HTTP off loopback)")
    if not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
        raise FleetConfigError(f"{where} must be a bare https origin (host and optional port only)")
    try:
        if ipaddress.ip_address(p.hostname).is_unspecified:
            raise FleetConfigError(f"{where} must not be an unspecified address")
    except ValueError:
        pass
    if port is not None and not 1 <= port <= 65535:
        raise FleetConfigError(f"{where} has an invalid port")
    return value.rstrip("/")


def parse_memory_section(raw: Any) -> dict[str, Any]:
    """Validate ``control_plane.memory`` (structure and every value present; completeness only when
    enabled). Returns a normalised copy. Never echoes a value."""
    if not isinstance(raw, dict):
        raise _err("must be a mapping")
    unknown = sorted(str(k) for k in raw if k not in MEMORY_KEYS)
    if unknown:
        raise _err("unknown keys: " + ", ".join(unknown))
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise _err("enabled must be true or false")
    out: dict[str, Any] = {"enabled": enabled}
    if "image_tag" in raw:
        if not isinstance(raw["image_tag"], str) or not _TAG.match(raw["image_tag"]):
            raise _err("image_tag must be an image tag ([A-Za-z0-9_.-], at most 128 characters)")
        out["image_tag"] = raw["image_tag"]
    if "model_dir" in raw:
        md = raw["model_dir"]
        if (not isinstance(md, str) or not os.path.isabs(md) or ":" in md
                or any(ord(c) < 32 or c == "$" for c in md)):
            raise _err("model_dir must be an absolute path without ':', '$' or control characters")
        out["model_dir"] = md
    if "port" in raw:
        port = raw["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535 \
                or port in _RESERVED_PORTS:
            raise _err("port must be an integer 1024-65535 that no other fabric service uses")
        out["port"] = port
    if "endpoint" in raw:
        out["endpoint"] = _endpoint(raw["endpoint"])
    if "db_password" in raw:
        if not isinstance(raw["db_password"], str) or not _SECRET.match(raw["db_password"]):
            raise _err("db_password must be 32-128 characters of [A-Za-z0-9_-] (generate it with "
                       "`herdr-eng fleet config memory init`)")
        out["db_password"] = raw["db_password"]
    if "tenants" in raw:
        tenants = raw["tenants"]
        if not isinstance(tenants, dict):
            raise _err("tenants must be a mapping of tenant: key")
        bad = sorted(str(t) for t in tenants if t not in TENANTS)
        if bad:
            raise _err(f"unknown tenants: {', '.join(bad)} (allowed: {', '.join(TENANTS)})")
        for t, key in tenants.items():
            if not isinstance(key, str) or not _SECRET.match(key):
                raise _err(f"tenants.{t} must be 32-128 characters of [A-Za-z0-9_-]")
        out["tenants"] = dict(tenants)
    secrets_seen = [v for v in [out.get("db_password"), *out.get("tenants", {}).values()] if v]
    if len(set(secrets_seen)) != len(secrets_seen):
        raise _err("the database password and every tenant key must all be different "
                   "(a shared key would merge tenants)")
    if enabled:
        missing = [k for k in ("image_tag", "model_dir", "port", "endpoint", "db_password") if k not in out]
        missing += [f"tenants.{t}" for t in TENANTS if t not in out.get("tenants", {})]
        if missing:
            raise _err("enabled but missing: " + ", ".join(missing)
                       + " (run `herdr-eng fleet config memory init`)")
    return out


def parse_public_section(values: Any) -> dict[str, str]:
    """``sections.memory`` (readable by every host): only the non-secret endpoint URL."""
    if not isinstance(values, dict) or set(values) - {"url"}:
        raise FleetConfigError("section memory: only a non-secret `url` is allowed (it is served to every host)")
    return {"url": _endpoint(values["url"], "section memory: url")} if "url" in values else {}


def memory_values(cp_values: dict) -> dict[str, Any] | None:
    """The validated memory block of a control_plane mapping, or None when absent."""
    if "memory" not in cp_values or cp_values["memory"] in (None, {}):
        return None
    return parse_memory_section(cp_values["memory"])


def memory_enabled(cfg: FleetConfig) -> bool:
    try:
        m = memory_values(cfg.section(CONTROL_PLANE_SECTION))
    except FleetConfigError as exc:
        if str(exc).startswith("unknown section"):
            return False
        raise
    return bool(m and m["enabled"])


def memory_compose_env(cp_values: dict, compose: dict[str, str] | None = None) -> dict[str, str]:
    """Compose variables for the ``memory`` profile; ``{}`` unless enabled. ``compose`` (the rendered
    control_plane.compose mapping) is used to refuse a port that collides with the project's own."""
    compose = compose or {}
    clash = sorted(k for k in compose if k.startswith("HERDR_MEMORY_"))
    if clash:
        raise _err("control_plane.compose must not set " + ", ".join(clash)
                   + " (they come from control_plane.memory)")
    m = memory_values(cp_values)
    if not m or not m["enabled"]:
        return {}
    taken = {compose.get(k) for k in ("HERDR_CP_PG_PORT", "HERDR_CP_WEB_PUBLIC_PORT", "HERDR_LLM_PROXY_PORT",
                                      "HERDR_LLM_GATEWAY_PORT", "HERDR_LLM_RELAY_PORT")}
    if str(m["port"]) in taken:
        raise _err("port collides with another published port of the control-plane project")
    secrets_all = [str(cp_values.get(k) or "") for k in ("minio_password",)] + \
                  [compose.get("HERDR_CP_PG_PASSWORD", ""), compose.get("HERDR_CP_MINIO_PASSWORD", "")]
    for v in [m["db_password"], *m["tenants"].values()]:
        if v and v in secrets_all:
            raise _err("a memory secret equals a Postgres/MinIO secret; generate separate ones")
    return {
        "HERDR_MEMORY_DB_PASSWORD": m["db_password"],
        "HERDR_MEMORY_TENANT_USERS": ",".join(f"{t}:{m['tenants'][t]}" for t in TENANTS),
        "HERDR_MEMORY_IMAGE_TAG": m["image_tag"],
        "HERDR_MEMORY_MODEL_DIR": m["model_dir"],
        "HERDR_MEMORY_PORT": str(m["port"]),
    }


def endpoint_url(cfg: FleetConfig) -> str | None:
    """The non-secret endpoint other hosts use (control_plane.memory.endpoint), or None."""
    m = memory_values(cfg.section(CONTROL_PLANE_SECTION))
    return m.get("endpoint") if m else None


# --------------------------------------------------------------------------
# generation (host side, 0600, never printed)
# --------------------------------------------------------------------------

def _new_secret() -> str:
    return secrets.token_urlsafe(32)        # 43 chars of [A-Za-z0-9_-]


def init_memory(path: Path, *, enable: bool = False, endpoint: str | None = None, model_dir: str | None = None,
                image_tag: str | None = None, port: int | None = None, rotate: tuple[str, ...] = (),
                edit: Callable | None = None) -> list[str]:
    """Create or complete ``control_plane.memory`` in the central file: missing secrets are generated,
    existing ones are kept (the database applies its password only to an EMPTY volume), ``rotate`` names
    tenants whose key is regenerated. Backup first, validated, atomic. Returns the NAMES it generated."""
    from .fleet_config_cli import _edit_central
    bad = [t for t in rotate if t not in TENANTS]
    if bad:
        raise FleetConfigError(f"cannot rotate unknown tenant(s): {', '.join(bad)}")
    generated: list[str] = []

    def mutate(data: dict) -> None:
        cp = data["sections"][CONTROL_PLANE_SECTION]["values"]
        mem = dict(cp.get("memory") or {})
        mem.setdefault("enabled", False)
        if enable:
            mem["enabled"] = True
        if endpoint is not None:
            mem["endpoint"] = endpoint
        if model_dir is not None:
            mem["model_dir"] = model_dir
        mem["image_tag"] = image_tag or mem.get("image_tag") or DEFAULT_IMAGE_TAG
        mem["port"] = port if port is not None else mem.get("port", DEFAULT_PORT)
        if "db_password" not in mem:
            mem["db_password"] = _new_secret()
            generated.append("db_password")
        tenants = dict(mem.get("tenants") or {})
        for t in TENANTS:
            if t not in tenants or t in rotate:
                tenants[t] = _new_secret()
                generated.append(f"tenants.{t}")
        mem["tenants"] = tenants
        cp["memory"] = mem
        parse_memory_section(mem)                       # refuse before writing
        if mem.get("endpoint"):                          # the non-secret copy every host may read
            data["sections"][PUBLIC_SECTION] = {"roles": ["all"], "values": {"url": mem["endpoint"]}}

    (edit or _edit_central)(path, mutate)
    return generated


def export_key(cfg: FleetConfig, tenant: str, out: Path) -> None:
    """Write one tenant's key to ``out`` (0600) inside a 0700 directory, for the host's herdr-memory
    client (``projects.*.credentials`` / ``scopes.*.credentials``). Never printed."""
    from .fleet_config_cli import write_private_file
    if tenant not in TENANTS:
        raise FleetConfigError(f"unknown tenant: {tenant} (allowed: {', '.join(TENANTS)})")
    m = memory_values(cfg.section(CONTROL_PLANE_SECTION))
    if not m or tenant not in m.get("tenants", {}):
        raise FleetConfigError(f"no key for tenant {tenant}: run `herdr-eng fleet config memory init`")
    out = Path(out).expanduser()
    parent = out.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
        os.chmod(parent, 0o700)
    elif stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise FleetConfigError(f"{parent} must not be accessible by group or other (chmod 700)")
    write_private_file(out, m["tenants"][tenant])


def status_summary(cfg: FleetConfig) -> dict[str, Any]:
    """Presence only: no value of any secret."""
    try:
        m = memory_values(cfg.section(CONTROL_PLANE_SECTION))
    except FleetConfigError as exc:
        if str(exc).startswith("unknown section"):
            m = None
        else:
            raise
    if not m:
        return {"configured": False, "enabled": False}
    return {"configured": True, "enabled": m["enabled"], "port": m.get("port"), "endpoint": m.get("endpoint"),
            "image_tag": m.get("image_tag"), "model_dir": m.get("model_dir"),
            "db_password": "set" if "db_password" in m else "missing",
            "tenants": {t: ("set" if t in m.get("tenants", {}) else "missing") for t in TENANTS}}
