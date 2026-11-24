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

# These migrations were published under overlapping numeric prefixes before
# consolidation. Keep their definitions and reconcile the renamed receipts;
# replaying their CREATE TABLE statements would break existing installations.
MIGRATION_ALIASES = {
    '0008_artifact_registry.sql': '0003_artifact_registry.sql',
    '0009_artifact_publications.sql': '0004_artifact_publications.sql',
    '0010_artifact_binding_context.sql': '0005_artifact_binding_context.sql',
    '0011_fabric_scheduler.sql': '0005_fabric_scheduler.sql',
    '0012_artifact_acl.sql': '0006_artifact_acl.sql',
    '0013_handoffs.sql': '0007_handoffs.sql',
}


# One migrator at a time per database: the session-level advisory lock key (any fixed bigint nothing else in the
# control plane uses), and how long a migration may wait for a lock (this one or a table's) before failing loudly.
MIGRATION_LOCK_KEY = 0x4845524452434D47    # "HERDRCMG"
MIGRATION_LOCK_TIMEOUT_MS = 60_000
# How long a starter waits for a sibling's migration (the advisory lock) before giving up: far longer than one DDL's lock wait.
MIGRATION_WAIT_TIMEOUT_MS = 600_000


def control_plane_config(cfg: dict[str, Any]) -> dict[str, str]:
    """Return normalized control-plane settings from a config dict.

    The DSN comes from config. The compose stack passes the fleet file's
    ``control_plane.pg_dsn`` in (see ``cli._cp_config``); nothing is read from env.
    """
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

    def __init__(
        self,
        conn: Any,
        lock_timeout_ms: int = MIGRATION_LOCK_TIMEOUT_MS,
        wait_timeout_ms: int = MIGRATION_WAIT_TIMEOUT_MS,
    ):
        self._conn = conn
        self._lock_timeout_ms = lock_timeout_ms
        self._wait_timeout_ms = wait_timeout_ms

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
        """Apply what is pending, holding one advisory lock so two starters never migrate at once.

        The lock is per session (taken and released on this connection). Waiting for a sibling's migration to finish is
        bounded by ``wait_timeout_ms`` (long, so a slow migration does not fail the second starter); every wait for a
        table lock while applying (behind a long transaction) fails after ``lock_timeout_ms`` instead of hanging the start.
        """
        try:
            await self._conn.execute(f"SET lock_timeout = {int(self._wait_timeout_ms)}")
            await self._conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
            try:
                await self._conn.execute(f"SET lock_timeout = {int(self._lock_timeout_ms)}")
                return await self._apply_locked(migrations_dir)
            finally:
                await self._conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        finally:
            await self._conn.execute("RESET lock_timeout")

    async def _apply_locked(self, migrations_dir: str) -> list[str]:
        await self.ensure_schema()
        already = set(await self.applied())
        files = sorted(p.name for p in Path(migrations_dir).glob("*.sql"))
        applied: list[str] = []
        for name in files:
            if name in already:
                continue
            sql = Path(migrations_dir, name).read_text(encoding="utf-8")
            previous = MIGRATION_ALIASES.get(name)
            alias_applied = previous in already if previous else False
            if alias_applied:
                legacy = Path(migrations_dir, previous)
                if legacy.is_file() and legacy.read_text(encoding='utf-8') != sql:
                    raise ValueError(f'migration alias content differs: {name}')
            await self._conn.execute("BEGIN")
            try:
                if not alias_applied:
                    await self._conn.execute(sql)
                await self._conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", name
                )
                await self._conn.execute("COMMIT")
            except Exception:
                await self._conn.execute("ROLLBACK")
                raise
            applied.append(name)
            already.add(name)
        return applied
