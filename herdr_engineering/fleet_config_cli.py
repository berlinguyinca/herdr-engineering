"""Operator tooling for the central fleet config: init, validate, token,
render-compose, and the host-credential helpers `fleet join` shares.

Secrets are never printed, never placed in argv, and every file that holds one
is created 0600 atomically (os.open with the mode, then rename).
"""
from __future__ import annotations

import fcntl
import getpass
import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .fleet_config import (CONTROL_PLANE_SECTION, SCHEMA_VERSION, FleetConfig,
                           FleetConfigError, _parse, load_fleet_config)

DEFAULT_CONFIG_PATH = Path("~/.config/herdr-engineering/fleet-config.yaml")
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


def compose_env(cfg: FleetConfig) -> dict[str, str]:
    """Validated ``control_plane.compose`` mapping (names and stringified values)."""
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
    return out


def _quote_env_value(value: str) -> str:
    """Quote for docker compose ``env_file``.

    Single quotes are literal (no interpolation, no escapes, raw newlines allowed),
    so they carry any value that has no single quote. A value with a single quote
    is double-quoted instead: ``\\`` ``"`` ``$`` get a backslash and a newline
    becomes ``\\n``.
    """
    if "'" not in value:
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
# init
# --------------------------------------------------------------------------

def _ask(answers: dict, key: str, label: str, prompt: Callable, secret_prompt: Callable,
         *, secret: bool = False, default: str | None = None) -> str:
    if key in answers and answers[key] not in (None, ""):
        return str(answers[key])
    suffix = f" [{default}]" if default else ""
    asker = secret_prompt if secret else prompt
    reply = str(asker(f"{label}{suffix}: ")).strip()
    if not reply and default is not None:
        return default
    if not reply:
        raise FleetConfigError(f"{key} is required")
    return reply


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
        hid = str(prompt("host id (blank to finish): ")).strip()
        if not hid:
            return hosts
        roles = str(prompt(f"roles for {hid} (comma separated, default host): ")).strip()
        hosts[hid] = [r.strip() for r in roles.split(",") if r.strip()] or ["host"]


def init_config(path: Path, answers: dict | None = None, prompt: Callable = input,
                secret_prompt: Callable = getpass.getpass) -> Path:
    """Create a new fleet config. Generates DB/MinIO credentials and API keys.

    Hosts get a placeholder hash of a discarded token: they cannot authenticate
    until ``fleet join`` / ``fleet config token`` installs a real one.
    """
    path = _expand(path)
    if path.exists():
        raise FleetConfigError(f"{path} already exists; refusing to overwrite")
    a = dict(answers or {})
    ask = lambda k, label, **kw: _ask(a, k, label, prompt, secret_prompt, **kw)  # noqa: E731
    public_url = ask("public_url", "public URL of the control plane")
    llm_url = ask("llm_upstream_url", "LLM upstream address")
    llm_key = ask("llm_upstream_key", "LLM upstream key", secret=True)
    ci_url = ask("ci_url", "CI address")
    ci_token = ask("ci_token", "CI token", secret=True)
    web_bind = ask("web_bind", "web bind address", default="127.0.0.1")
    web_port = ask("web_port", "web public port", default="80")
    pg_port = str(a.get("pg_port") or "5432")
    hosts = _collect_hosts(a, prompt)

    pg_user, pg_pass = f"herdr_{secrets.token_hex(4)}", secrets.token_hex(16)
    minio_user, minio_pass = f"herdr_{secrets.token_hex(4)}", secrets.token_hex(16)
    api_key = secrets.token_urlsafe(32)
    dsn = f"postgresql://{pg_user}:{pg_pass}@postgres:5432/herdr"
    bucket = "herdr-artifacts"
    data = {
        "version": SCHEMA_VERSION,
        "sections": {
            CONTROL_PLANE_SECTION: {"roles": ["control-plane"], "values": {
                "public_url": public_url,
                "pg_dsn": dsn,
                "host_aliases": str(a.get("host_aliases") or ""),
                "api_keys": {api_key: "operator"},
                "rustfs_endpoint": "http://rustfs:9000",
                "rustfs_bucket": bucket,
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
                    "HERDR_CP_PG_DSN": dsn,
                    "HERDR_CP_RUSTFS_BUCKET": bucket,
                    "HERDR_CP_HOST_ALIASES": str(a.get("host_aliases") or ""),
                    "FABRIC_LLM_UPSTREAM_KEY": llm_key,
                    "HERDR_ENGINEERING_CI_URL": ci_url,
                },
            }},
            "llm": {"roles": ["builder"], "values": {"upstream_url": llm_url, "upstream_key": llm_key}},
            "ci": {"roles": ["builder", "ci"], "values": {"url": ci_url, "token": ci_token}},
        },
        "hosts": {hid: {"roles": roles, "token_sha256": mint_host_token()[1], "overrides": {}}
                  for hid, roles in hosts.items()},
    }
    _parse(data)
    compose_env(_parse(data))  # reject unrepresentable values before writing
    write_private_file(path, yaml.safe_dump(data, sort_keys=False), exclusive=True)
    load_fleet_config(path)  # final check of what is on disk
    return path


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------

def validate_config(path: Path) -> dict:
    """Load, check compose renderability if a compose map exists. No values."""
    cfg = load_fleet_config(_expand(path))
    try:
        has_compose = bool(cfg.section(CONTROL_PLANE_SECTION).get("compose"))
    except FleetConfigError:
        has_compose = False
    if has_compose:
        compose_env(cfg)
    return {"revision": cfg.revision, "hosts": len(cfg.status()["hosts"])}


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


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    write_private_file(dest, path.read_bytes())
    return dest


def set_host(path: Path, host_id: str, token_sha256: str, roles: list[str] | None = None) -> None:
    """Add or replace ``host_id``'s hash in the file (backup first, validated, atomic)."""
    path = _expand(path)
    lock = path.with_name(path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(os.open(lock, os.O_RDWR | os.O_CREAT, 0o600), "r+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        load_fleet_config(path)  # refuses unsafe/invalid input
        data = yaml.safe_load(path.read_text())
        hosts = data.setdefault("hosts", {})
        old = hosts.get(host_id) or {}
        entry = {"roles": list(roles) if roles is not None else list(old.get("roles", ["host"])),
                 "token_sha256": token_sha256,
                 "overrides": old.get("overrides", {})}
        hosts[host_id] = entry
        _parse(data)
        _backup(path)
        write_private_file(path, yaml.safe_dump(data, sort_keys=False))


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
