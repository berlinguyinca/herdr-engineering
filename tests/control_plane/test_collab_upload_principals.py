"""Upload ownership contracts used by the browser-to-registered-agent handoff."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.collab import CollabError, Principal
from herdr_engineering.control_plane.collab_uploads import CollabUploads

ROOM = '00000000-0000-4000-8000-000000000001'
UPLOAD = '00000000-0000-4000-8000-000000000002'
MANIFEST = {'files': [{'relative_path': 'data.csv', 'size_bytes': 3, 'sha256': 'a' * 64,
                       'parts': [{'size_bytes': 3, 'sha256': 'a' * 64}]}]}


class Store:
    """Replace only persistence; use real upload authorization and operations."""
    def __init__(self, principal):
        self.row = SimpleNamespace(upload_id=UPLOAD, room_id=ROOM, created_by='owner',
                                   status='available', manifest={**MANIFEST, 'principal': principal})

    @asynccontextmanager
    async def lifecycle_lock(self):
        yield

    async def get_upload(self, upload_id):
        assert upload_id == UPLOAD
        return self.row

    async def upload_files(self, upload_id):
        assert upload_id == UPLOAD
        return [{'file_id': 'published', 'status': 'available'}]


def service(principal):
    # These paths touch no provider: use real service methods with a persistence fixture.
    uploads = object.__new__(CollabUploads)
    uploads.store = Store(principal)
    return uploads


async def authorize(*args):
    return True


@pytest.mark.parametrize('operation', ['resume', 'status', 'finalize'])
@pytest.mark.parametrize(('stored', 'caller'), [
    ('user:owner', Principal('agent', 'registered', 'owner')),
    ('agent:registered', Principal('user', 'owner', 'owner')),
    ('agent:registered', Principal('agent', 'other-agent', 'owner')),
])
def test_upload_operations_reject_other_principals_even_with_same_owner(stored, caller, operation):
    async def run():
        uploads = service(stored)
        with pytest.raises(CollabError) as exc:
            if operation == 'resume':
                await uploads.start_upload(caller, ROOM, MANIFEST, {'upload_id': UPLOAD}, authorize)
            elif operation == 'status':
                await uploads.get_upload_status(caller, UPLOAD, authorize)
            else:
                await uploads.finalize_upload(caller, UPLOAD, {'data.csv': 'a' * 64}, authorize)
        assert exc.value.status == 404
    asyncio.run(run())


def test_creating_agent_can_read_and_finalize_its_available_upload():
    async def run():
        uploads = service('agent:registered')
        agent = Principal('agent', 'registered', 'owner')
        status = await uploads.get_upload_status(agent, UPLOAD, authorize)
        assert status.status == 'available' and status.uploaded_bytes == 3
        assert await uploads.finalize_upload(agent, UPLOAD, {'data.csv': 'a' * 64}, authorize) == [
            {'file_id': 'published', 'status': 'available'}]
    asyncio.run(run())
