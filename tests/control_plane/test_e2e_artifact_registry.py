"""Opt-in durable artifact HTTP checks against a dedicated test Postgres."""
import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.db import Migrator
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_artifact_access import call

DSN = os.environ.get('HERDR_E2E_PG_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='requires dedicated HERDR_E2E_PG_DSN')


class Blobs:
    def __init__(self):
        self.objects = {}

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        key = f'sha256/{digest[:2]}/{digest[2:4]}/{digest}'
        self.objects[key] = data
        return key, digest

    def get(self, key, digest):
        data = self.objects[key]
        assert hashlib.sha256(data).hexdigest() == digest
        return data


def test_upload_catalog_acl_and_bytes_survive_http_server_restart():
    import asyncpg

    schema = 'artifact_test_' + uuid4().hex
    blobs = Blobs()

    async def prepare():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f'CREATE SCHEMA {schema}')
            await conn.execute(f'SET search_path TO {schema}')
            await Migrator(conn).apply(str(Path(__file__).parents[2] / 'deploy/control-plane/migrations'))
        finally:
            await conn.close()

    async def factory():
        conn = await asyncpg.connect(DSN, server_settings={'search_path': schema})
        return ArtifactStore(conn, blobs)

    async def cleanup():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f'DROP SCHEMA {schema} CASCADE')
        finally:
            await conn.close()

    asyncio.run(prepare())
    artifact_id = None
    try:
        for restart in range(2):
            adapter = SyncArtifactAdapter(factory)
            acl = ArtifactAcl(on_change=lambda action, payload: adapter('record_acl_event', action, payload))
            acl.load(adapter('load_acl'))
            server = ControlPlaneServer(ControlPlaneRepo(), artifact_store=adapter,
                artifact_acl=acl, rustfs=blobs, api_keys={'owner': 'operator', 'other': 'operator'})
            thread = threading.Thread(target=server.serve, daemon=True)
            thread.start()
            server.wait_until_ready()
            try:
                if not restart:
                    status, content = call(server, 'POST', '/api/v1/artifacts?filename=durable.txt', raw=b'durable bytes')
                    assert status == 200, content
                    artifact_id = json.loads(content)['artifact_id']
                status, listing = call(server, 'GET', '/api/v1/artifacts')
                assert status == 200, listing
                assert artifact_id in {row['artifact_id'] for row in json.loads(listing)['artifacts']}
                assert call(server, 'GET', f'/api/v1/artifacts/{artifact_id}/download') == (200, b'durable bytes')
                assert call(server, 'GET', f'/api/v1/artifacts/{artifact_id}', key='other')[0] == 404
            finally:
                server.shutdown()
                thread.join(3)
                adapter.close()
    finally:
        asyncio.run(cleanup())
