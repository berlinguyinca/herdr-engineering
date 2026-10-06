import asyncio

from herdr_engineering.control_plane import db


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


def test_control_plane_config_defaults():
    c = db.control_plane_config({})
    assert c["postgres_dsn"].startswith("postgresql://")
    assert c["rustfs_bucket"] == "herdr-artifacts"
    assert c["rustfs_region"] == "us-east-1"


def test_control_plane_config_overrides():
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
