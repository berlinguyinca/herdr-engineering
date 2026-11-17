"""`control-plane checkpoint` names the reason for any ValueError, redacted; real Postgres and CLI."""
import asyncio
import json
import logging
import zlib
from pathlib import Path

import asyncpg
import pytest
import yaml

from herdr_engineering import cli
from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering.control_plane.db import Migrator
from herdr_engineering.control_plane.projection_refresh import _safe_message
from herdr_engineering.control_plane.projection_store import encode_checkpoint

from ._private_pg import private_postgres

MIGRATIONS = Path(__file__).parents[2] / "deploy/control-plane/migrations"
ANSWERS = {
    "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
    "llm_upstream_key": "placeholder-llm-value", "ci_url": "https://ci.example.test",
    "ci_token": "placeholder-ci-value", "web_bind": "127.0.0.1", "web_port": "8080",
    "gateway_tailnet_ip": "127.0.0.1", "hosts": {"bender": ["builder"]},
}


def _fleet_file(tmp_path, dsn):
    path = fcc.init_config(tmp_path / "fleet-config.yaml", dict(ANSWERS))
    data = yaml.safe_load(path.read_text())
    data["sections"]["control_plane"]["values"]["pg_dsn"] = dsn
    path.unlink()
    fcc.write_private_file(path, yaml.safe_dump(data, sort_keys=False))
    return path


def test_safe_message_redacts_urls_and_truncates():
    text = _safe_message(ValueError("bad postgresql://u:p@h/db and https://x.test/y here"))
    assert "://" not in text and "<url>" in text
    assert len(_safe_message(ValueError("x" * 900))) == 500


def test_checkpoint_cli_prints_reason_for_value_error(tmp_path, capsys):
    # A real ValueError from the real migrator: an aliased migration whose content differs.
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0005_artifact_binding_context.sql").write_text("SELECT 1;")
    (migrations / "0010_artifact_binding_context.sql").write_text("SELECT 2;")
    with private_postgres() as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")

        async def seed():
            conn = await asyncpg.connect(dsn)
            await Migrator(conn).ensure_schema()
            await conn.execute("INSERT INTO schema_migrations(version) VALUES('0005_artifact_binding_context.sql')")
            await conn.close()

        asyncio.run(seed())
        code = cli.main(["control-plane", "checkpoint", "--fleet-config", str(_fleet_file(tmp_path, dsn)),
                         "--migrations", str(migrations)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False and out["error"] == "ValueError"
    assert out["reason"] == "migration alias content differs: 0010_artifact_binding_context.sql"
    assert len(out["reason"]) <= 500 and "://" not in json.dumps(out)


def test_restore_rejection_warning_carries_safe_message(caplog):
    # A stored checkpoint with a valid checksum and frontier but a schema-invalid body.
    with private_postgres() as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")
        from herdr_engineering.control_plane.bridge import PostgresSource, load_into
        from herdr_engineering.control_plane.projection_refresh import refresh_checkpoint
        from herdr_engineering.control_plane.repo import ControlPlaneRepo

        async def prepare():
            conn = await asyncpg.connect(dsn)
            await Migrator(conn).apply(str(MIGRATIONS))
            await conn.execute(
                "INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,source_timestamp) "
                "VALUES('e1',0,'ServiceRegistered','service','s','{}',now())")
            await conn.close()

        async def corrupt():
            conn = await asyncpg.connect(dsn)
            state = json.loads(zlib.decompress(await conn.fetchval("SELECT state FROM projection_checkpoints")))
            state["repo"]["source_ledger"] = {"version": 99}
            packed, checksum = encode_checkpoint(state)
            await conn.execute("UPDATE projection_checkpoints SET state=$1, checksum=$2", packed, checksum)
            await conn.close()

        asyncio.run(prepare())
        refresh_checkpoint(dsn)
        asyncio.run(corrupt())
        caplog.set_level(logging.WARNING)
        source = PostgresSource(dsn=dsn)
        try:
            load_into(ControlPlaneRepo(), source)
        finally:
            source.close()
    rejected = [r.getMessage() for r in caplog.records if "restore rejected" in r.getMessage()]
    assert rejected and all("://" not in m for m in rejected), caplog.text
    assert "ValueError: invalid projection checkpoint schema" in rejected[0], rejected
