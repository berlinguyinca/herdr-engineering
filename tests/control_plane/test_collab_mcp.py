# ruff: noqa: F811  (pytest fixtures imported from sibling test modules)
"""MCP upload results keep grants out of model-visible text."""
import json

import pytest

from herdr_engineering.control_plane import collab_mcp
from tests.control_plane.test_collab_store import database  # noqa: F401
from tests.control_plane.test_collab_uploads import ROOM, manifest, setup, upload_env  # noqa: F401


def test_upload_mcp_tools_are_declared():
    names = {tool['name'] for tool in collab_mcp.TOOLS}
    assert {'room_upload_preview', 'room_upload_start', 'room_upload_status',
            'room_upload_finalize', 'room_upload_cancel', 'room_upload_request_access'} <= names


def test_upload_mcp_aggregates_status_and_hides_signed_grants(upload_env):
    _, _, _, _, _, _, pr, _ = upload_env
    service = setup(upload_env)
    result = collab_mcp._call(service, pr, 'room_upload_start', {'room_id': ROOM, 'manifest': manifest()})
    text = result['content'][0]['text']
    assert 'signature' not in text and 'https://' not in text
    grant = result['_meta']['herdr/upload-grant']
    assert grant['files'][0]['parts'][0]['url'].startswith('https://')
    result = collab_mcp._call(service, pr, 'room_upload_status', {'upload_id': grant['upload_id']})
    status = json.loads(result['content'][0]['text'])
    assert status['file_count'] == 1 and status['total_bytes'] == 3 and status['uploaded_bytes'] == 0
    assert 'files' not in status


@pytest.mark.parametrize('args', [{'upload_id': []}, {'upload_id': 'x', 'url': 'https://evil'}, {}])
def test_upload_mcp_rejects_malformed_arguments(upload_env, args):
    from herdr_engineering.control_plane.collab import CollabError
    service = setup(upload_env)
    with pytest.raises(CollabError) as exc:
        collab_mcp._call(service, upload_env[6], 'room_upload_status', args)
    assert exc.value.status == 400


# Exercise the real HTTP body/authentication boundary with the existing public server.
from tests.control_plane.test_collab_public import connect, rpc, stack  # noqa: E402, F401


def test_upload_mcp_accepts_large_authenticated_manifests_without_relaxing_a2a(stack):
    registered = connect(stack.main, 'alice')
    files = [manifest(path=f'file-{n}.csv')['files'][0] for n in range(500)]
    params = {'name': 'room_upload_preview', 'arguments': {'room_id': ROOM, 'manifest': {'files': files}}}
    assert len(json.dumps(params)) > 64 * 1024
    status, response = rpc(stack.pub, 'tools/call', params, agent=registered)
    # Reaches the service boundary, which fails closed until a relational backend is injected.
    assert status == 200
    assert json.loads(response['result']['content'][0]['text'])['status'] == 503
    assert rpc(stack.pub, 'tools/call', params)[0] == 401
    params['name'] = 'room_post'
    assert rpc(stack.pub, 'tools/call', params, agent=registered)[0] == 413
