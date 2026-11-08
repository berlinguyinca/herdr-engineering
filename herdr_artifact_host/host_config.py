"""The host's configuration: one JSON slice PER ROLE (rendered by ``herdr-eng fleet config render-artifact-host``).

Each role's container mounts only its own slice as a compose secret, so the dashboard-facing ``internal`` role never holds the
session or claim keys and the one-shot ``migrate`` role holds only the schema owner's database credentials.  Nothing is read
from the environment.  This module is dependency free (no asyncpg, no aiohttp) so the deploy-side validation can be kept in
step with it by a test.  Errors are ``ValueError`` messages that name keys and never values (the files carry secrets).
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

SCHEMA_VERSION: Final = 1
DEFAULT_CONFIG_PATH: Final = "/run/secrets/artifact-host-config"
PUBLIC_PORT: Final = (
    8090  # the edge's upstream port (deploy/control-plane/collab-edge/artifact-host-http.conf)
)
INTERNAL_PORT: Final = (
    8091  # the relay's upstream (deploy/artifact-host/relay/nginx.conf); herdr-cp-web reaches it as artifact-host-relay:8091
)
DEFAULT_HEALTH_PORT: Final = 8092
MIN_SECRET_BYTES: Final = 32
SYNC_DIR: Final = "/sync"
BLOB_DIR: Final = "/var/lib/artifact-host/blobs"
DB_HOST: Final = "artifact-host-db"
DB_NAME: Final = "artifact_host"
# The three database roles (none is a superuser): owner = DDL, used only by migrate; public = DML; internal = SELECT only.
ROLE_DB_USER: Final = {
    "public": "artifact_public",
    "internal": "artifact_internal",
    "migrate": "artifact_owner",
}
ROLES: Final = tuple(ROLE_DB_USER)

_DIGEST = re.compile(r"[0-9a-f]{64}")
_DNS = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
COMMON_KEYS: Final = frozenset({"version", "role", "database"})
ROLE_KEYS: Final = {
    "public": COMMON_KEYS
    | {
        "public_host",
        "public_url",
        "mcp_public_hosts",
        "session_seconds",
        "cognito",
        "session_secret",
        "claim_key",
        "health_port",
    },
    "internal": COMMON_KEYS | {"internal_key_digest", "bind"},
    "migrate": COMMON_KEYS,
}
COGNITO_KEYS: Final = frozenset({"pool_id", "client_ids", "web_client_id", "domain", "group"})
DATABASE_KEYS: Final = frozenset({"user", "password"})


def _dns(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and _DNS.fullmatch(value) is not None
        and not value.rsplit(".", 1)[-1].isdigit()
    )


def _secret(name: str, value: object) -> bytes:
    if not isinstance(value, str) or len(value.encode("utf-8")) < MIN_SECRET_BYTES:
        raise ValueError(f"{name} must be a string of at least {MIN_SECRET_BYTES} bytes")
    return value.encode("utf-8")


def _port(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise ValueError(f"{name} must be a port from 1024 to 65535")
    if value in (PUBLIC_PORT, INTERNAL_PORT):
        raise ValueError(f"{name} must differ from the public and internal listener ports")
    return value


@dataclass(frozen=True)
class HostConfig:
    """One role's slice; the fields a role does not hold keep their empty defaults."""

    role: str
    db_user: str
    db_password: str = field(repr=False)
    public_host: str = ""
    public_url: str = ""
    mcp_public_hosts: tuple[str, ...] = ()
    session_seconds: int = 0
    pool_id: str = ""
    client_ids: tuple[str, ...] = ()
    web_client_id: str = ""
    cognito_domain: str = ""
    group: str = ""
    session_secret: bytes = field(default=b"", repr=False)
    claim_key: bytes = field(default=b"", repr=False)
    internal_key_digest: str = ""
    bind: str = ""
    health_port: int = DEFAULT_HEALTH_PORT

    @property
    def dsn(self) -> str:
        from urllib.parse import quote

        return (
            f"postgresql://{quote(self.db_user, safe='')}:{quote(self.db_password, safe='')}"
            f"@{DB_HOST}:5432/{DB_NAME}"
        )


def _database(raw: Mapping[str, Any], role: str) -> tuple[str, str]:
    db = raw.get("database")
    if not isinstance(db, Mapping) or set(db) != DATABASE_KEYS:
        raise ValueError("database must hold exactly: user, password")
    if db["user"] != ROLE_DB_USER[role]:
        raise ValueError(f"database.user must be {ROLE_DB_USER[role]} for the {role} role")
    if not isinstance(db["password"], str) or len(db["password"]) < 16:
        raise ValueError("database.password must be a string of at least 16 characters")
    return db["user"], db["password"]


def _bind(value: object) -> str:
    try:
        addr = ipaddress.IPv4Address(value) if isinstance(value, str) else None
    except ValueError:
        addr = None
    if addr is None or not addr.is_private or addr.is_unspecified or addr.is_loopback:
        raise ValueError("bind must be the container's private IPv4 address on the internal network")
    return str(addr)


def _public(raw: Mapping[str, Any]) -> dict[str, Any]:
    host = raw.get("public_host")
    if not _dns(host):
        raise ValueError("public_host must be a lowercase DNS name")
    assert isinstance(host, str)
    if raw.get("public_url") != f"https://{host}":
        raise ValueError("public_url must be https://<public_host>")
    mcp = raw.get("mcp_public_hosts", [])
    if not isinstance(mcp, list) or not all(_dns(h) for h in mcp) or host in mcp:
        raise ValueError(
            "mcp_public_hosts must be a list of lowercase DNS names other than public_host"
        )
    seconds = raw.get("session_seconds", 14400)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 60 <= seconds <= 7 * 86400:
        raise ValueError("session_seconds must be an integer from 60 to 604800")
    cognito = raw.get("cognito")
    if not isinstance(cognito, Mapping) or set(cognito) != COGNITO_KEYS:
        raise ValueError("cognito must hold exactly: " + ", ".join(sorted(COGNITO_KEYS)))
    clients = cognito["client_ids"]
    if (
        not isinstance(clients, list)
        or not clients
        or not all(isinstance(c, str) and c for c in clients)
    ):
        raise ValueError("cognito.client_ids must be a non-empty list of strings")
    for key in ("pool_id", "web_client_id", "domain", "group"):
        if not isinstance(cognito[key], str) or not cognito[key]:
            raise ValueError(f"cognito.{key} must be a non-empty string")
    session_secret = _secret("session_secret", raw.get("session_secret"))
    claim_key = _secret("claim_key", raw.get("claim_key"))
    if session_secret == claim_key:
        raise ValueError("session_secret and claim_key must differ")
    return {
        "public_host": host,
        "public_url": f"https://{host}",
        "mcp_public_hosts": tuple(mcp),
        "session_seconds": seconds,
        "pool_id": cognito["pool_id"],
        "client_ids": tuple(clients),
        "web_client_id": cognito["web_client_id"],
        "cognito_domain": cognito["domain"],
        "group": cognito["group"],
        "session_secret": session_secret,
        "claim_key": claim_key,
        "health_port": _port("health_port", raw.get("health_port", DEFAULT_HEALTH_PORT)),
    }


def parse(raw: object) -> HostConfig:
    if not isinstance(raw, Mapping):
        raise ValueError("artifact host config must be a mapping")
    role = raw.get("role")
    if role not in ROLE_KEYS:
        raise ValueError("role must be one of: " + ", ".join(ROLES))
    if unknown := sorted(str(k) for k in set(raw) - ROLE_KEYS[role]):
        raise ValueError(f"artifact host config ({role}): unknown keys: " + ", ".join(unknown))
    if raw.get("version") != SCHEMA_VERSION:
        raise ValueError(f"artifact host config: version must be {SCHEMA_VERSION}")
    user, password = _database(raw, role)
    extra: dict[str, Any] = {}
    if role == "public":
        extra = _public(raw)
    elif role == "internal":
        digest = raw.get("internal_key_digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("internal_key_digest must be a lowercase hex sha256")
        extra = {"internal_key_digest": digest, "bind": _bind(raw.get("bind"))}
    return HostConfig(role=role, db_user=user, db_password=password, **extra)


def load(path: str | Path) -> HostConfig:
    try:
        data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read the artifact host config ({type(exc).__name__})") from None
    except ValueError:
        raise ValueError("the artifact host config is not valid JSON") from None
    return parse(data)
