import asyncio

import pytest

from herdr_engineering.control_plane.db import Migrator


class Connection:
    def __init__(self, applied=()):
        self.versions = list(applied)
        self.statements = []

    async def execute(self, sql, *args):
        self.statements.append(sql)
        if sql.startswith('INSERT INTO schema_migrations'):
            self.versions.append(args[0])

    async def fetch(self, _sql):
        return [{'version': version} for version in self.versions]


def definitions(path, changed=False):
    (path / '0003_artifact_registry.sql').write_text('CREATE TABLE artifacts_once();')
    (path / '0008_artifact_registry.sql').write_text(
        'CREATE TABLE different();' if changed else 'CREATE TABLE artifacts_once();')


def test_renamed_migration_records_receipt_without_recreating_existing_tables(tmp_path):
    definitions(tmp_path)
    conn = Connection(['0003_artifact_registry.sql'])
    assert asyncio.run(Migrator(conn).apply(str(tmp_path))) == ['0008_artifact_registry.sql']
    assert not any(sql == 'CREATE TABLE artifacts_once();' for sql in conn.statements)
    assert '0008_artifact_registry.sql' in conn.versions


def test_fresh_install_executes_each_schema_definition_once(tmp_path):
    definitions(tmp_path)
    conn = Connection()
    asyncio.run(Migrator(conn).apply(str(tmp_path)))
    assert conn.statements.count('CREATE TABLE artifacts_once();') == 1
    assert conn.versions == ['0003_artifact_registry.sql', '0008_artifact_registry.sql']


def test_alias_with_different_content_fails_before_running_ddl(tmp_path):
    definitions(tmp_path, changed=True)
    conn = Connection(['0003_artifact_registry.sql'])
    with pytest.raises(ValueError, match='alias content differs'):
        asyncio.run(Migrator(conn).apply(str(tmp_path)))
    assert 'CREATE TABLE different();' not in conn.statements
