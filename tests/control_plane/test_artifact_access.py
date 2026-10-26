import hashlib
import http.client
import json
import threading
from contextlib import contextmanager

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_jobs import ArtifactJobManager
from herdr_engineering.control_plane.artifact_reviews import ArtifactReviewManager
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def principal(key):
    return 'api-key:' + hashlib.sha256(key.encode()).hexdigest()[:16] + ':operator'


@contextmanager
def running(**kwargs):
    server = ControlPlaneServer(ControlPlaneRepo(), artifact_acl=ArtifactAcl(),
        api_keys={'owner': 'operator', 'other': 'operator'}, **kwargs)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(3)


def call(server, method, path, body=None, key='owner', raw=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
    payload = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=payload, headers={
        'X-API-Key': key, 'Content-Type': 'application/octet-stream' if raw else 'application/json'})
    response = conn.getresponse()
    content = response.read()
    status = response.status
    conn.close()
    return status, content


def test_upload_owner_can_list_read_and_download_but_other_operator_cannot():
    with running() as server:
        status, data = call(server, 'POST', '/api/v1/artifacts?filename=private.txt', raw=b'private')
        assert status == 200
        artifact = json.loads(data)
        aid = artifact['artifact_id']
        assert artifact['created_by'] == principal('owner')
        assert aid in call(server, 'GET', '/api/v1/artifacts')[1].decode()
        assert call(server, 'GET', f'/api/v1/artifacts/{aid}/download') == (200, b'private')
        assert call(server, 'GET', f'/api/v1/artifacts/{aid}', key='other')[0] == 404
        assert json.loads(call(server, 'GET', '/api/v1/artifacts', key='other')[1])['artifacts'] == []


def test_direct_review_and_job_reads_do_not_bypass_artifact_acl():
    jobs = ArtifactJobManager()
    reviews = ArtifactReviewManager()
    job = jobs.submit(profile='web', artifact_id='private', base_version_id=None,
                      request={'source': 'secret'}, requested_by=principal('owner'))
    review = reviews.request(artifact_id='private', version_id='v1', requested_by=principal('owner'))
    with running(artifact_job_manager=jobs, artifact_review_manager=reviews) as server:
        server.artifact_acl.grant('private', principal('owner'), {'admin'})
        for path in (f'/api/v1/artifact-jobs/{job["job_id"]}',
                     f'/api/v1/artifact-reviews/{review["review_id"]}'):
            assert call(server, 'GET', path)[0] == 200
            assert call(server, 'GET', path, key='other')[0] == 404


def test_other_operator_cannot_self_grant_or_mutate_private_artifact():
    reviews = ArtifactReviewManager()
    review = reviews.request(artifact_id='private', version_id='v1', requested_by=principal('owner'))
    with running(artifact_review_manager=reviews) as server:
        server.artifact_acl.grant('private', principal('owner'), {'admin'})
        changes = [('/api/v1/artifacts/private/acl', {'principal_id': principal('other'), 'permissions': ['admin']}),
                   ('/api/v1/artifacts/private/acl/revoke', {'principal_id': principal('owner')}),
                   (f'/api/v1/artifact-reviews/{review["review_id"]}/complete', {'decision': 'approved'})]
        for path, payload in changes:
            assert call(server, 'POST', path, payload, key='other')[0] in {403, 404}
        assert not server.artifact_acl.can('private', principal('other'), 'read')
        assert reviews.get(review['review_id'])['state'] == 'requested'


def test_durable_upload_is_visible_and_downloadable_without_the_legacy_projection():
    class Blobs:
        def __init__(self):
            self.data = {}

        def put(self, data):
            digest = hashlib.sha256(data).hexdigest()
            key = 'sha256/' + digest
            self.data[key] = data
            return key, digest

        def get(self, key, digest):
            data = self.data[key]
            assert hashlib.sha256(data).hexdigest() == digest
            return data

    blobs = Blobs()

    class Durable:
        def __init__(self):
            self.records = {}

        def sync_call(self, method, *args, **kwargs):
            if method == 'register':
                aid = 'artifact_durable'
                key, digest = blobs.put(kwargs['data'])
                self.records[aid] = {'artifact_id': aid, 'created_by': kwargs['created_by'],
                    'storage_key': key, 'sha256': digest, 'mime_type': kwargs['mime_type'],
                    'size_bytes': len(kwargs['data']), 'original_filename': kwargs['original_filename']}
                return aid
            if method == 'get_record':
                record = self.records.get(args[0])
                return record if record and record['created_by'] == kwargs['principal_id'] else None
            if method == 'list_records':
                return [r for r in self.records.values() if r['created_by'] == kwargs['principal_id']]
            raise AssertionError(method)

    with running(artifact_store=Durable(), rustfs=blobs) as server:
        status, data = call(server, 'POST', '/api/v1/artifacts?filename=durable.txt', raw=b'durable')
        assert status == 200
        aid = json.loads(data)['artifact_id']
        assert aid == 'artifact_durable'
        server.repo._artifacts.clear()
        assert aid in call(server, 'GET', '/api/v1/artifacts')[1].decode()
        assert call(server, 'GET', f'/api/v1/artifacts/{aid}')[0] == 200
        assert call(server, 'GET', f'/api/v1/artifacts/{aid}/download') == (200, b'durable')
        assert call(server, 'GET', f'/api/v1/artifacts/{aid}', key='other')[0] == 404


def test_native_delivery_includes_artifact_references_after_identity_verification(monkeypatch):
    import socket
    from types import SimpleNamespace

    from herdr_engineering.herdr_adapter import NativeHerdrAdapter

    received = []
    monkeypatch.setattr(NativeHerdrAdapter, 'list_agents', lambda *_a, **_k: [
        SimpleNamespace(terminal_id='term1', agent_session='conversation1',
                        interactive_ready=True, launch_pending=False)])
    monkeypatch.setattr(NativeHerdrAdapter, 'send_input',
                        lambda _self, target, message, **_kwargs: received.append((target, message)))
    server = ControlPlaneServer(ControlPlaneRepo())
    server._dispatch_native_message({'terminal_id': 'term1', 'agent_session': 'conversation1',
        'host_id': socket.gethostname(), 'herdr_session': 'workspace1'}, 'Please inspect', ['artifact_1'])
    assert received == [('term1', 'Please inspect\n\nAttached HerdR artifact references:\n- herdr-artifact://artifact_1')]
