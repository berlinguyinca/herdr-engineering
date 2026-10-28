"""Explicit identity reconciliation retains the journal through full/checkpoint recovery."""
import asyncio
import copy
from datetime import timedelta

import pytest

from herdr_engineering.control_plane.agent_identity import AgentIdentityRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_postgres_recovery import _insert, _reference, _row
from tests.control_plane.test_recovery_checkpoint import _build, _checkpoint_source
from tests.control_plane.test_recovery_checkpoint import pg as checkpoint_pg

asyncpg = pytest.importorskip('asyncpg')
pg = checkpoint_pg


@pytest.fixture
def db(pg):
    pg.query('TRUNCATE fabric_events')
    pg.query('TRUNCATE recovery_checkpoint_manifests CASCADE')
    return pg


def seed_conflicts(db):
    records = []
    for identity in ('a', 'b'):
        registry = AgentIdentityRegistry()
        registry.allocate(identity, 'Ada')
        records.append(_row('agent_identity', identity, 'agent_identity.updated',
                            copy.deepcopy(registry._records[identity]), len(records)))
        records.append(_row('session', identity, 'SessionStarted', {'status': 'active'}, len(records)))
    _insert(db, records)


def run(db, function):
    async def execute():
        conn = await asyncpg.connect(db.dsn)
        try:
            return await function(conn)
        finally:
            await conn.close()
    return asyncio.run(execute())


def test_explicit_repair_preserves_history_and_restores_full_checkpoint_export(db):
    from herdr_engineering.control_plane.agent_identity_repair_cli import (
        apply_plan,
        create_plan,
    )

    seed_conflicts(db)
    _build(db)
    original_ids = {r['event_id'] for r in db.query('SELECT event_id FROM fabric_events')}
    plan = run(db, create_plan)
    report = run(db, lambda conn: apply_plan(conn, plan))
    assert report['status'] == 'applied' and report['corrected_identities'] == 2
    assert original_ids <= {r['event_id'] for r in db.query('SELECT event_id FROM fabric_events')}
    full, _ = _reference(db)
    restored, _, metrics = _checkpoint_source(db)
    assert metrics['mode'] == 'checkpoint'
    assert full.agent_identities.list() == restored.agent_identities.list()
    assert len({s['agent_handle'] for s in restored.list_sessions()}) == 2
    assert all(s['display_name'] != 'Ada' for s in restored.list_sessions())
    backup = restored.export_state()
    imported = ControlPlaneRepo()
    imported.import_state(backup)
    assert imported.agent_identities.list() == restored.agent_identities.list()
    assert imported.agent_identities.allocate('new', 'Ada')['display_name'] != 'Ada'
    assert not any(e['entity_type'].startswith('agent_identity') for e in restored.recent_events())
    before = db.query('SELECT count(*) AS n FROM fabric_events')[0]['n']
    assert run(db, lambda conn: apply_plan(conn, plan))['status'] == 'already_applied'
    assert db.query('SELECT count(*) AS n FROM fabric_events')[0]['n'] == before


def test_stale_or_tampered_plan_is_rejected_without_writing(db):
    from herdr_engineering.control_plane.agent_identity_repair_cli import (
        apply_plan,
        create_plan,
    )

    seed_conflicts(db)
    plan = run(db, create_plan)
    tampered = copy.deepcopy(plan)
    tampered['records'][0]['display_name'] = 'Tampered'
    before = db.query('SELECT count(*) AS n FROM fabric_events')[0]['n']
    with pytest.raises(ValueError):
        run(db, lambda conn: apply_plan(conn, tampered))
    assert db.query('SELECT count(*) AS n FROM fabric_events')[0]['n'] == before
    registry = AgentIdentityRegistry()
    registry.allocate('c', 'Grace')
    late = _row('agent_identity', 'c', 'agent_identity.updated', registry._records['c'], 5)
    late['source_timestamp'] -= timedelta(seconds=100)
    late['ingest_timestamp'] -= timedelta(seconds=100)
    _insert(db, [late])
    with pytest.raises(ValueError, match='stale'):
        run(db, lambda conn: apply_plan(conn, plan))
    assert db.query('SELECT count(*) AS n FROM fabric_events')[0]['n'] == before + 1
