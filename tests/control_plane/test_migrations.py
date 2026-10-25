"""Migration ordering and upgrade safety for the collaboration data platform (0003-0009+).

Real Postgres (HERDR_E2E_PG_DSN). The Migrator skips by filename, so each historical state a
database could be in is applied first, then the current chain, and the resulting schema must be
identical to a fresh install. Nothing here touches a deployed database."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from herdr_engineering.control_plane.db import Migrator

HEAD = Path(__file__).parents[2] / 'deploy/control-plane/migrations'
CHECKPOINTS = Path(__file__).parent / 'migration_checkpoints'
NAMES = sorted(p.name for p in HEAD.glob('*.sql'))


def test_migrations_are_contiguous_unique_and_ordered():
    numbers = [int(n.split('_', 1)[0]) for n in NAMES]
    assert numbers == list(range(1, len(NAMES) + 1))
    assert all(re.fullmatch(r'\d{4}_[a-z0-9_]+\.sql', n) for n in NAMES)
    assert {'0003', '0004', '0005', '0006', '0007', '0008', '0009'} <= {n[:4] for n in NAMES}


def test_collaboration_migrations_are_transaction_safe_and_non_destructive():
    for name in NAMES[2:]:                      # 0003 onward
        sql = re.sub(r'--[^\n]*', '', (HEAD / name).read_text(encoding='utf-8'))
        # Migrator wraps each file in BEGIN/COMMIT: these cannot run inside it.
        assert not re.search(r'\b(COMMIT|ROLLBACK|VACUUM)\b|CONCURRENTLY', sql, re.I), name
        # Rooms, files and audit history are never dropped or truncated by an upgrade.
        assert not re.search(r'\b(DROP\s+(TABLE|SCHEMA|COLUMN)|TRUNCATE|DELETE\s+FROM)\b', sql, re.I), name


@pytest.fixture
def database():
    dsn = os.environ.get('HERDR_E2E_PG_DSN')
    if not dsn:
        pytest.skip('set HERDR_E2E_PG_DSN to a throwaway Postgres')
    import asyncpg
    with asyncio.Runner() as runner:
        conn = runner.run(asyncpg.connect(dsn))
        schemas = []

        async def fresh_schema():
            schema = 'migrate_' + uuid4().hex
            schemas.append(schema)
            await conn.execute(f'CREATE SCHEMA {schema}')
            await conn.execute(f'SET search_path TO {schema}')
            return schema
        try:
            yield conn, runner.run, fresh_schema
        finally:
            runner.run(conn.execute('SET search_path TO public'))
            for schema in schemas:
                runner.run(conn.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
            runner.run(conn.close())


async def fingerprint(conn, schema):
    """Everything an upgrade must preserve, independent of column order and schema name."""
    queries = {
        'columns': ("SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                    "pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d "
                    "ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE n.nspname=$1 AND c.relkind='r' "
                    "AND a.attnum>0 AND NOT a.attisdropped"),
        'constraints': ("SELECT c.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid) "
                        "FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1"),
        'indexes': "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname=$1",
        'triggers': ("SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
                     "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                     "WHERE n.nspname=$1 AND NOT t.tgisinternal"),
        'functions': ("SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p "
                      "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=$1"),
    }
    result = {}
    for name, sql in queries.items():
        rows = await conn.fetch(sql, schema)
        result[name] = sorted(tuple(str(v).replace(schema, '<schema>') for v in row.values()) for row in rows)
    return result


def prefix(tmp_path, count, name='prefix'):
    target = tmp_path / name
    target.mkdir()
    for source in sorted(HEAD.glob('*.sql'))[:count]:
        shutil.copy(source, target / source.name)
    return target


def checkpoint(tmp_path, variant, upto):
    """The first `upto` files of the chain, with 0003 replaced by a historical edit."""
    target = prefix(tmp_path, upto, 'checkpoint')
    shutil.copy(CHECKPOINTS / variant, target / '0003_collaboration_data_platform.sql')
    return target


def assert_same(actual, expected):
    for kind in expected:
        assert actual[kind] == expected[kind], (
            kind, [x for x in expected[kind] if x not in actual[kind]],
            [x for x in actual[kind] if x not in expected[kind]])


def upgrade_to_head(database, setup_dir, applied_before):
    conn, run, fresh_schema = database
    expected_schema = run(fresh_schema())
    run(Migrator(conn).apply(str(HEAD)))
    expected = run(fingerprint(conn, expected_schema))
    schema = run(fresh_schema())
    assert len(run(Migrator(conn).apply(str(setup_dir)))) == applied_before
    upgraded = run(Migrator(conn).apply(str(HEAD)))
    assert upgraded == NAMES[applied_before:]
    assert run(Migrator(conn).apply(str(HEAD))) == []                    # a second run changes nothing
    assert_same(run(fingerprint(conn, schema)), expected)


@pytest.mark.parametrize('count', range(2, len(NAMES)))
def test_every_prefix_of_the_chain_upgrades_to_the_current_schema(database, tmp_path, count):
    upgrade_to_head(database, prefix(tmp_path, count), count)


def test_database_that_applied_0003_with_the_skill_registry_converges(database, tmp_path):
    upgrade_to_head(database, checkpoint(tmp_path, '0003_dce994e_with_registry.sql', 5), 5)


@pytest.mark.xfail(strict=True, reason='KNOWN GAP: a database that applied the 10-minute f5e1dad draft of 0003 '
                   'silently lacks grant/expiry/sequence columns and the audit request_fingerprint; '
                   'no repair migration exists. Rebuild such a dev database or add an idempotent 0010.')
def test_database_that_applied_the_first_0003_draft_converges(database, tmp_path):
    upgrade_to_head(database, checkpoint(tmp_path, '0003_f5e1dad_first_draft.sql', 3), 3)
