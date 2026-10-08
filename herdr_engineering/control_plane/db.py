"""Control-plane configuration normalization."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_DEFAULTS = {
    "postgres_dsn": "postgresql://herdr:herdr@127.0.0.1:5432/herdr",
    "rustfs_endpoint": "http://127.0.0.1:9000",
    "rustfs_bucket": "herdr-artifacts",
    "rustfs_region": "us-east-1",
}

_DSN_RE = re.compile(r"^(postgres(?:ql)?://)(?:[^@/]*@)?([^@/]+.*)$")


def control_plane_config(cfg: dict[str, Any]) -> dict[str, str]:
    """Return normalized control-plane settings from a config dict.

    The Postgres DSN credentials are taken from ``HERDR_CP_PG_USER`` /
    ``HERDR_CP_PG_PASSWORD`` when present, so a compose stack that supplies
    random strong credentials via ``env_file: .env`` still connects without
    hardcoding them in a committed ``config.yaml``. The DSN host/port/db come
    from config (e.g. the ``postgres`` service name on the bridge network).
    """
    cp = cfg.get("control_plane") or {}
    out = dict(_DEFAULTS)
    for key in _DEFAULTS:
        if key in cp and cp[key]:
            out[key] = str(cp[key])
    dsn = out["postgres_dsn"]
    user = os.environ.get("HERDR_CP_PG_USER")
    password = os.environ.get("HERDR_CP_PG_PASSWORD")
    if user and password:
        m = _DSN_RE.match(dsn)
        if m:
            out["postgres_dsn"] = f"{m.group(1)}{user}:{password}@{m.group(2)}"
    return out


class Migrator:
    """Apply versioned SQL migrations idempotently, in filename order.

    ``conn`` is any object exposing async ``execute(sql, *args)`` and
    ``fetch(sql, *args)`` (a real asyncpg connection or a test fake).
    """

    def __init__(self, conn: Any):
        self._conn = conn

    async def ensure_schema(self) -> None:
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version text PRIMARY KEY,"
            "  applied_at timestamptz NOT NULL DEFAULT now()"
            ")"
        )

    async def applied(self) -> list[str]:
        rows = await self._conn.fetch(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        return [r["version"] for r in rows]

    async def apply(self, migrations_dir: str) -> list[str]:
        await self.ensure_schema()
        already = set(await self.applied())
        files = sorted(p.name for p in Path(migrations_dir).glob("*.sql"))
        applied: list[str] = []
        for name in files:
            if name in already:
                continue
            sql = Path(migrations_dir, name).read_text(encoding="utf-8")
            await self._conn.execute("BEGIN")
            try:
                await self._conn.execute(sql)
                await self._conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", name
                )
                await self._conn.execute("COMMIT")
            except Exception:
                await self._conn.execute("ROLLBACK")
                raise
            applied.append(name)
        return applied
