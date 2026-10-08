import asyncio
from pathlib import Path

from herdr_engineering.control_plane import db

REQUIRED_TABLES = {
    "hosts", "host_samples", "missions", "mission_events", "mission_stage_runs",
    "plans", "plan_revisions", "sessions", "session_events", "agents", "agent_runs",
    "services", "service_events", "artifacts", "artifact_bindings",
    "artifact_materializations", "token_usage", "resource_usage", "test_runs",
    "test_results", "reviews", "review_findings", "pull_requests", "audit_events",
    "fabric_events", "state_snapshots",
}


_MIGRATION = Path(__file__).parents[2] / "deploy" / "control-plane" / "migrations" / "0001_control_plane.sql"


def test_phase1_schema_contains_all_spec_tables():
    sql = _MIGRATION.read_text()
    for table in REQUIRED_TABLES:
        assert f"CREATE TABLE {table}" in sql, f"missing table {table}"


def test_phase1_schema_indexes_heavily_queried_fields():
    sql = _MIGRATION.read_text()
    assert "CREATE INDEX" in sql
    for col in ("mission_id", "session_id", "host_id", "event_type"):
        assert col in sql


class _FakeConn:
    def __init__(self):
        self.log = []
        self._applied = []

    async def execute(self, sql, *a):
        self.log.append(("exec", sql, a))

    async def fetch(self, sql, *a):
        if "FROM schema_migrations" in sql:
            return [{"version": v} for v in self._applied]
        return []

    async def fetchrow(self, sql, *a):
        return None


def _run(coro):
    return asyncio.run(coro)


def test_control_plane_config_defaults(monkeypatch):
    monkeypatch.delenv("HERDR_CP_PG_USER", raising=False)
    monkeypatch.delenv("HERDR_CP_PG_PASSWORD", raising=False)
    c = db.control_plane_config({})
    assert c["postgres_dsn"].startswith("postgresql://")
    assert c["rustfs_bucket"] == "herdr-artifacts"
    assert c["rustfs_region"] == "us-east-1"


def test_control_plane_config_uses_env_credentials(monkeypatch):
    """When HERDR_CP_PG_USER/PASSWORD are set (compose env_file: .env), the
    DSN credentials are substituted in while host/port/db come from config."""
    monkeypatch.setenv("HERDR_CP_PG_USER", "herdr_abcd1234")
    monkeypatch.setenv("HERDR_CP_PG_PASSWORD", "s3cr3t-password")
    c = db.control_plane_config({"control_plane": {
        "postgres_dsn": "postgresql://herdr:herdr@postgres:5432/herdr",
    }})
    assert c["postgres_dsn"] == (
        "postgresql://herdr_abcd1234:s3cr3t-password@postgres:5432/herdr")


def test_control_plane_config_env_substitutes_without_existing_creds(monkeypatch):
    """Env creds are inserted even when the configured DSN has no user:pass."""
    monkeypatch.setenv("HERDR_CP_PG_USER", "u")
    monkeypatch.setenv("HERDR_CP_PG_PASSWORD", "p")
    c = db.control_plane_config({"control_plane": {
        "postgres_dsn": "postgresql://postgres:5432/herdr",
    }})
    assert c["postgres_dsn"] == "postgresql://u:p@postgres:5432/herdr"


def test_control_plane_config_overrides(monkeypatch):
    monkeypatch.delenv("HERDR_CP_PG_USER", raising=False)
    monkeypatch.delenv("HERDR_CP_PG_PASSWORD", raising=False)
    c = db.control_plane_config({"control_plane": {
        "postgres_dsn": "postgresql://x:y@h:5432/db",
        "rustfs_endpoint": "http://127.0.0.1:9000",
        "rustfs_bucket": "artifacts",
    }})
    assert c["postgres_dsn"] == "postgresql://x:y@h:5432/db"
    assert c["rustfs_endpoint"] == "http://127.0.0.1:9000"
    assert c["rustfs_bucket"] == "artifacts"


def test_migrator_applies_in_order_and_skips_applied(tmp_path):
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "0001_a.sql").write_text("CREATE TABLE a (id int);")
    (mdir / "0002_b.sql").write_text("CREATE TABLE b (id int);")
    fc = _FakeConn()
    fc._applied = ["0001_a.sql"]
    mig = db.Migrator(fc)
    done = _run(mig.apply(str(mdir)))
    assert done == ["0002_b.sql"]
    sqls = [s for k, s, _ in fc.log if k == "exec"]
    assert any("CREATE TABLE IF NOT EXISTS schema_migrations" in s for s in sqls)
    inserted = [
        a[0]
        for k, s, a in fc.log
        if k == "exec" and "INSERT INTO schema_migrations" in s
    ]
    assert inserted == ["0002_b.sql"]
    assert "0001_a.sql" not in inserted
