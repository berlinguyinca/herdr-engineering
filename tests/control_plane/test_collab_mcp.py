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


from tests.control_plane.test_skill_registry import agent, env, publish, user  # noqa: E402, F401
from tests.control_plane.test_skill_transfer import service, transfer  # noqa: E402, F401


def test_skill_tools_are_declared():
    assert {'skills_search', 'skills_get', 'skills_download', 'skills_install'} <= {
        t['name'] for t in collab_mcp.TOOLS}


@pytest.mark.parametrize('name', ['skills_search', 'skills_get', 'skills_download', 'skills_install'])
def test_skill_mcp_scopes_checksums_and_harness_only_credentials(transfer, env, name):
    _, v = publish(env)
    svc = service(transfer)
    args = {'query': 'csv', 'scope': 'private'} if name == 'skills_search' else {
        'skill_id': v.skill_id, 'version': v.version}
    result = collab_mcp._call(svc, agent(), name, args)
    text = result['content'][0]['text']
    assert collab_mcp.UNTRUSTED in text
    assert v.skill_id in text and v.sha256 in text
    assert all(secret not in text for secret in ['SECRET', 'https://', 'storage_key', 'signature'])
    if name in ('skills_download', 'skills_install'):
        grant = result['_meta']['herdr/skill-package']
        assert grant['url'].startswith('https://') and grant['sha256'] == v.sha256
        assert 'installed' not in text
    else:
        assert '_meta' not in result
    if name == 'skills_search':
        assert v.skill_id not in collab_mcp._call(svc, agent('bob'), name, args)['content'][0]['text']
    else:
        from herdr_engineering.control_plane.collab import CollabError
        with pytest.raises(CollabError):
            collab_mcp._call(svc, agent('bob'), name, args)


@pytest.mark.parametrize('name,args', [
    ('skills_search', {'query': '', 'scope': 'all'}),
    ('skills_search', {'query': [], 'scope': 'private'}),
    ('skills_get', {'skill_id': '', 'version': '1'}),
    ('skills_download', {'skill_id': 'x', 'version': '1', 'url': 'https://evil'}),
    ('skills_install', {'skill_id': 'x', 'version': '1', 'path': '/tmp/evil'}),
    ('skills_install', {}),
])
def test_skill_mcp_rejects_invalid_arguments(transfer, name, args):
    from herdr_engineering.control_plane.collab import CollabError
    with pytest.raises(CollabError) as exc:
        collab_mcp._call(service(transfer), agent(), name, args)
    assert exc.value.status == 400


def test_skill_provider_errors_never_escape_to_mcp(transfer, env):
    _, v = publish(env)
    def fail(*args, **kwargs):
        raise RuntimeError('https://storage.invalid/?SECRET')
    transfer[3].generate_presigned_url = fail
    from herdr_engineering.control_plane.collab import CollabError
    with pytest.raises(CollabError) as exc:
        collab_mcp._call(service(transfer), agent(), 'skills_download', {
            'skill_id': v.skill_id, 'version': v.version})
    assert exc.value.status == 503 and 'SECRET' not in str(exc.value)


def test_skill_mcp_missing_backend_fails_closed(stack):
    registered = connect(stack.main, 'alice')
    status, response = rpc(stack.pub, 'tools/call', {
        'name': 'skills_download', 'arguments': {'skill_id': 'unknown', 'version': '1'}}, agent=registered)
    assert status == 200
    assert json.loads(response['result']['content'][0]['text'])['status'] == 503
