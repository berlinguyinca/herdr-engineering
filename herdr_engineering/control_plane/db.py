"""Control-plane configuration normalization."""
from __future__ import annotations

from pathlib import Path
from typing import Any

_DEFAULTS = {
    "postgres_dsn": "postgresql://herdr:herdr@127.0.0.1:5432/herdr",
    "rustfs_endpoint": "http://127.0.0.1:9000",
    "rustfs_bucket": "herdr-artifacts",
    "rustfs_region": "us-east-1",
}


def control_plane_config(cfg: dict[str, Any]) -> dict[str, str]:
    """Return normalized control-plane settings from a config dict."""
    cp = cfg.get("control_plane") or {}
    out = dict(_DEFAULTS)
    for key in _DEFAULTS:
        if key in cp and cp[key]:
            out[key] = str(cp[key])
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
