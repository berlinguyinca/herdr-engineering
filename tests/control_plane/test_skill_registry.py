"""Skill registry on registry artifacts: service, transfer, harness installer and HTTP routes (flag-gated)."""
# ruff: noqa: F811
import base64
import hashlib
import json
import os
import shutil
import stat
import threading
import urllib.error
import urllib.request
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from herdr_engineering.control_plane import skill_registry as sr
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.skill_registry import SkillError, SkillRegistry
from herdr_engineering.control_plane.skill_routes import build_skill_registry, skill_registry_config
from herdr_engineering.control_plane.skill_transfer import HarnessSkillInstaller, SkillTransfer

PERMS = {'read', 'edit', 'review', 'share', 'admin'}


class FakeRustFS:
    def __init__(self):
        self.objects = {}

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        key = f'sha256/{digest[:2]}/{digest[2:4]}/{digest}'
        self.objects[key] = data
        return key, digest

    def get(self, key, sha256):
        return self.objects[key]


class FakeStore:
    """Registry semantics the skill service relies on: owner/grant ACL, CAS head, immutable versions."""

    def __init__(self):
        self.artifacts, self.versions, self.grants = {}, {}, {}
        self.n = 0

    def __call__(self, method, *args, **kwargs):
        return getattr(self, method)(*args, **kwargs)

    def register_logical(self, *, original_filename, artifact_type='other', created_by=None, mime_type=None,
                         workspace_id=None):
        self.n += 1
        aid = f'artifact_{self.n}'
        self.artifacts[aid] = {'artifact_id': aid, 'original_filename': original_filename, 'created_by': created_by,
                               'artifact_type': artifact_type, 'current_version_id': None}
        self.versions[aid] = []
        return dict(self.artifacts[aid])

    def commit_version(self, aid, *, base_version_id, source_manifest_blob, output_manifest, provenance=None,
                       created_by=None, **_):
        art = self.artifacts[aid]
        if art['current_version_id'] != base_version_id:
            raise RuntimeError('stale head')
        v = {'version_id': f'version_{aid}_{len(self.versions[aid]) + 1}', 'artifact_id': aid,
             'version_number': len(self.versions[aid]) + 1, 'state': 'ready', 'output_manifest': output_manifest,
             'provenance': provenance or {}, 'created_by': created_by}
        self.versions[aid].insert(0, v)
        art['current_version_id'] = v['version_id']
        return dict(v)

    def list_versions(self, aid):
        return list(self.versions.get(aid, []))

    def report_permission(self, aid, principal, permission):
        art = self.artifacts.get(aid)
        if art is None:
            return False
        if art['created_by'] == principal:
            return True
        g = self.grants.get((aid, principal))
        return bool(g and (permission in g or 'admin' in g))

    def list_records(self, *, principal_id, query=None, artifact_type=None, status=None, limit=200, offset=0):
        out = [a for a in self.artifacts.values()
               if (a['created_by'] == principal_id or (a['artifact_id'], principal_id) in self.grants)
               and (artifact_type is None or a['artifact_type'] == artifact_type)
               and (not query or query.lower() in a['original_filename'].lower())]
        return out[offset:offset + limit]

    def record_acl_event(self, action, grant):
        key = (grant['artifact_id'], grant['principal_id'])
        if action == 'grant':
            self.grants[key] = set(grant['permissions'])
        else:
            self.grants.pop(key, None)


@pytest.fixture
def env():
    key = Ed25519PrivateKey.generate()
    store, fs, events = FakeStore(), FakeRustFS(), []

    def on_grant(aid, pid, perms, by):
        store('record_acl_event', 'grant', {'artifact_id': aid, 'principal_id': pid, 'permissions': sorted(perms)})

    registry = SkillRegistry(store, fs, signing_key=key, key_id='test-key',
                             trusted_keys={'test-key': key.public_key()}, on_grant=on_grant)
    transfer = SkillTransfer(registry, record_event=lambda *a: events.append(a))
    return registry, transfer, store, fs, events


def metadata(version='1.0.0', files=None, **over):
    files = files or {'SKILL.md': b'# CSV summary', 'requirements.lock': b'',
                      'tests/case.json': b'{}', 'scripts/run.py': b'raise RuntimeError("never execute")'}
    base = {'name': 'csv-summary', 'version': version, 'input_schema': {'type': 'object'},
            'output_schema': {'type': 'object'}, 'capabilities': ['read_files'], 'supported_file_types': ['csv'],
            'fixtures': [], 'tests': ['tests/case.json'], 'dependency_lock': 'requirements.lock',
            'provenance': {'author': 'alice'}, 'files': files}
    return {**base, **over}


def publish(env, who='alice', request_key='r1', **kw):
    share = kw.pop('share_with', ())
    skill_id = kw.pop('skill_id', None)
    return env[0].publish(who, metadata(**kw), request_key=request_key, skill_id=skill_id, share_with=share)


# ----------------------------------------------------------------------------------------------- registry mapping
def test_skill_is_a_registry_artifact_with_immutable_versions(env):
    registry, _, store, fs, _ = env
    v1 = publish(env)
    art = store.artifacts[v1.skill_id]
    assert art['artifact_type'] == 'skill' and art['created_by'] == 'alice'
    stored = store.versions[v1.skill_id][0]
    assert stored['output_manifest'][0]['storage_key'] == v1.storage_key
    assert stored['provenance']['skill']['signature'] and stored['provenance']['skill']['key_id'] == 'test-key'
    assert v1.storage_key in fs.objects
    v2 = publish(env, skill_id=v1.skill_id, version='2.0.0', request_key='r2')
    assert v2.skill_id == v1.skill_id and v2.version_id != v1.version_id
    assert [v['provenance']['skill']['version'] for v in store.versions[v1.skill_id]] == ['2.0.0', '1.0.0']
    # v1 stays loadable and unchanged after v2.
    assert registry.load('alice', v1.skill_id, '1.0.0').sha256 == v1.sha256


def test_republish_same_content_is_idempotent_but_changed_content_is_immutable(env):
    v1 = publish(env)
    again = publish(env, skill_id=v1.skill_id)
    assert again.version_id == v1.version_id and len(env[2].versions[v1.skill_id]) == 1
    with pytest.raises(SkillError) as exc:
        publish(env, skill_id=v1.skill_id, files={'SKILL.md': b'changed', 'requirements.lock': b'',
                                                  'tests/case.json': b'{}'})
    assert exc.value.status == 409


# ----------------------------------------------------------------------------------------------------------- ACL
def test_private_skill_is_invisible_to_others_everywhere_with_one_404(env):
    registry, transfer, *_ = env
    v = publish(env)
    missing = 'artifact_does_not_exist'
    for call in (lambda p, s: registry.load(p, s, '1.0.0'), lambda p, s: transfer.get_skill(p, s, '1.0.0'),
                 lambda p, s: transfer.grant(p, s, '1.0.0'), lambda p, s: transfer.record_installation(p, s, '1.0.0', v.sha256)):
        with pytest.raises(SkillError) as forbidden:
            call('bob', v.skill_id)
        with pytest.raises(SkillError) as absent:
            call('bob', missing)
        assert (forbidden.value.status, str(forbidden.value)) == (absent.value.status, str(absent.value)) == (404, 'not found')
    assert registry.search('bob', '') == [] and registry.search('bob', 'csv') == []
    assert [s['skill_id'] for s in registry.search('alice', 'csv')] == [v.skill_id]


def test_sharing_is_an_acl_grant_read_only(env):
    registry, transfer, store, *_ = env
    v = publish(env, share_with=['bob'])
    assert store.grants[(v.skill_id, 'bob')] == {'read'}
    assert transfer.get_skill('bob', v.skill_id, '1.0.0')['sha256'] == v.sha256
    assert [s['skill_id'] for s in registry.search('bob', '')] == [v.skill_id]
    with pytest.raises(SkillError) as exc:        # read does not allow publishing a new version
        publish(env, 'bob', skill_id=v.skill_id, version='9.9.9', request_key='x')
    assert exc.value.status == 404
    store.grants.pop((v.skill_id, 'bob'))         # revocation is effective on the next read
    with pytest.raises(SkillError):
        registry.load('bob', v.skill_id, '1.0.0')
    assert registry.search('bob', '') == []


def test_search_authorizes_before_limit_so_hidden_skills_cannot_crowd_results(env):
    registry, *_ = env
    for i in range(60):
        publish(env, 'carol', request_key=f'c{i}')
    mine = publish(env, 'dave', request_key='d')
    assert [s['skill_id'] for s in registry.search('dave', '')] == [mine.skill_id]


# --------------------------------------------------------------------------------------- tamper / size / verify
def test_tampered_storage_is_rejected_on_load(env):
    registry, transfer, _, fs, _ = env
    v = publish(env)
    obj = json.loads(fs.objects[v.storage_key])
    obj['files']['SKILL.md'] = base64.b64encode(b'# evil').decode()
    fs.objects[v.storage_key] = json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()
    for call in (registry.load, transfer.get_skill, transfer.grant):
        with pytest.raises(SkillError) as exc:
            call('alice', v.skill_id, '1.0.0')
        assert exc.value.status == 409


def test_bad_signature_and_untrusted_key_fail_verification(env):
    registry, *_ = env
    v = publish(env)
    pkg = registry.load('alice', v.skill_id, '1.0.0')
    assert registry.verify_skill_package(pkg).ok
    assert not registry.verify_skill_package(replace(pkg, signature='00' * 64)).ok
    assert not registry.verify_skill_package(replace(pkg, key_id='other')).ok
    other = Ed25519PrivateKey.generate().public_key()
    assert not sr.verify_skill_package(pkg, {'test-key': other}).ok


def test_size_cap_and_content_rules(env, monkeypatch):
    monkeypatch.setattr(sr, 'MAX_PACKAGE_BYTES', 64)
    with pytest.raises(SkillError) as exc:
        publish(env, files={'SKILL.md': b'x' * 100, 'requirements.lock': b'', 'tests/case.json': b'{}'})
    assert exc.value.status == 400
    monkeypatch.undo()
    for bad in ({'requirements.lock': b'', 'tests/case.json': b'{}'},                    # no SKILL.md
                {'SKILL.md': b'a', '../x': b'', 'requirements.lock': b'', 'tests/case.json': b'{}'}):
        with pytest.raises(SkillError):
            publish(env, files=bad)
    with pytest.raises(SkillError):
        env[0].publish('alice', metadata(), request_key='')
    with pytest.raises(SkillError):
        env[0].publish('alice', {**metadata(), 'extra': 1}, request_key='k')


# ------------------------------------------------------------------------------------------------- transfer
def test_transfer_is_short_lived_single_use_and_bound_to_principal(env):
    registry, transfer, _, _, events = env
    v = publish(env, share_with=['bob'])
    now = [1000.0]
    transfer.now = lambda: now[0]
    ref = transfer.grant('alice', v.skill_id, '1.0.0', install=True)
    assert ref.expires_at - now[0] == 60 and ref.url.startswith('/api/v1/skills/download/')
    assert '/skills/download' not in repr(ref) and ref.size_bytes
    token = ref.url.rsplit('/', 1)[1]
    with pytest.raises(SkillError):
        transfer.redeem('bob', token)                       # wrong principal
    ref = transfer.grant('alice', v.skill_id, '1.0.0')
    token = ref.url.rsplit('/', 1)[1]
    data = transfer.redeem('alice', token)
    assert hashlib.sha256(data).hexdigest() == ref.transport_sha256
    with pytest.raises(SkillError):
        transfer.redeem('alice', token)                     # single use
    ref = transfer.grant('alice', v.skill_id, '1.0.0')
    now[0] += 61
    with pytest.raises(SkillError):
        transfer.redeem('alice', ref.url.rsplit('/', 1)[1])  # expired
    actions = [e[2] for e in events]
    assert 'skill.install_requested' in actions and 'skill.download_requested' in actions
    assert '/api/v1' not in json.dumps(events) and token not in json.dumps(events)   # no URL/token in audit


def test_revocation_between_issue_and_redeem_denies(env):
    _, transfer, store, *_ = env
    v = publish(env, share_with=['bob'])
    ref = transfer.grant('bob', v.skill_id, '1.0.0')
    store.grants.pop((v.skill_id, 'bob'))
    with pytest.raises(SkillError) as exc:
        transfer.redeem('bob', ref.url.rsplit('/', 1)[1])
    assert exc.value.status == 404


def test_installation_receipt_checks_checksum_and_is_audited(env):
    _, transfer, _, _, events = env
    v = publish(env)
    transfer.record_installation('alice', v.skill_id, '1.0.0', v.sha256)
    assert events[-1][2] == 'skill.install_reported'
    with pytest.raises(SkillError) as exc:
        transfer.record_installation('alice', v.skill_id, '1.0.0', '0' * 64)
    assert exc.value.status == 409


# --------------------------------------------------------------------------------------- harness installer
def issue(env, version='1.0.0', skill_id=None, files=None, who='alice', key='k'):
    v = env[0].publish(who, metadata(version, files), request_key=key + version, skill_id=skill_id)
    ref = env[1].grant(who, v.skill_id, version, install=True)
    data = env[1].redeem(who, ref.url.rsplit('/', 1)[1])
    return v, ref, data


@pytest.fixture
def harness(env, tmp_path):
    _, ref, data = issue(env)
    return HarnessSkillInstaller(tmp_path, trusted_keys=env[0].trusted_keys), ref, data


def test_harness_verifies_caches_and_activates_without_executing(harness, tmp_path):
    installer, ref, data = harness
    installed = installer.install(ref, data)
    assert installed == tmp_path / 'cache' / ref.sha256
    assert (installed / 'SKILL.md').read_bytes() == b'# CSV summary'
    assert (installed / 'scripts/run.py').read_bytes() == b'raise RuntimeError("never execute")'
    pointers = list((tmp_path / 'installed').iterdir())
    assert len(pointers) == 1 and pointers[0].resolve() == installed
    assert installer.install(ref, data) == installed
    assert not list(tmp_path.rglob('*.tmp'))
    assert not (installed / 'scripts/run.py').stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize('change', ['checksum', 'signature', 'key', 'identity', 'expired', 'size', 'traversal'])
def test_harness_rejects_invalid_packages_before_writing(harness, tmp_path, change):
    installer, ref, data = harness
    if change == 'expired':
        ref = replace(ref, expires_at=0)
    elif change == 'identity':
        ref = replace(ref, skill_id='../other')
    elif change == 'size':
        ref = replace(ref, size_bytes=len(data) + 1)
    elif change == 'checksum':
        data += b' '
    else:
        obj = json.loads(data)
        if change == 'signature':
            obj['signature'] = '00' * 64
        elif change == 'key':
            installer.trusted_keys = {'test-key': Ed25519PrivateKey.generate().public_key()}
        else:
            obj['files']['../escape'] = 'YQ=='
        data = json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()
        ref = replace(ref, transport_sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    with pytest.raises(ValueError):
        installer.install(ref, data)
    assert not list(tmp_path.iterdir())


def test_harness_rejects_tampered_cache_and_symlink_paths(harness, tmp_path):
    installer, ref, data = harness
    installed = installer.install(ref, data)
    (installed / 'SKILL.md').write_text('tampered')
    with pytest.raises(ValueError):
        installer.install(ref, data)
    (installed / 'SKILL.md').unlink()
    outside = tmp_path / 'outside'
    outside.write_text('do not overwrite')
    (installed / 'SKILL.md').symlink_to(outside)
    with pytest.raises(ValueError):
        installer.install(ref, data)
    assert outside.read_text() == 'do not overwrite'


def test_harness_rejects_nonprivate_root(harness, tmp_path):
    installer, ref, data = harness
    tmp_path.chmod(0o755)
    try:
        with pytest.raises(ValueError, match='private'):
            installer.install(ref, data)
        assert not list(tmp_path.iterdir())
    finally:
        tmp_path.chmod(0o700)


def test_harness_swaps_active_version_and_retains_immutable_cache(env, harness, tmp_path):
    installer, ref, data = harness
    first = installer.install(ref, data)
    _, ref2, data2 = issue(env, '2.0.0', ref.skill_id)
    latest = installer.install(ref2, data2)
    assert first != latest and first.exists()
    assert [p.resolve() for p in (tmp_path / 'installed').iterdir()] == [latest]


def test_failed_activation_preserves_previous_version(env, harness, tmp_path, monkeypatch):
    installer, ref, data = harness
    first = installer.install(ref, data)
    _, ref2, data2 = issue(env, '2.0.0', ref.skill_id)
    original = os.replace

    def fail_pointer(source, target, **kwargs):
        info = os.stat(source, dir_fd=kwargs.get('src_dir_fd'), follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise OSError('activation unavailable')
        return original(source, target, **kwargs)
    monkeypatch.setattr(os, 'replace', fail_pointer)
    with pytest.raises(OSError):
        installer.install(ref2, data2)
    assert [p.resolve() for p in (tmp_path / 'installed').iterdir()] == [first]
    assert not list(tmp_path.rglob('*.tmp'))


def test_harness_rejects_symlink_cache_root(harness, tmp_path):
    installer, ref, data = harness
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'cache').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        installer.install(ref, data)
    assert not list(outside.iterdir())


def test_harness_rejects_signed_file_directory_collision(env, tmp_path):
    _, ref, data = issue(env, files={'SKILL.md': b'# skill', 'requirements.lock': b'',
                                     'tests/case.json': b'{}', 'tests': b'file'})
    with pytest.raises(ValueError):
        HarnessSkillInstaller(tmp_path, trusted_keys=env[0].trusted_keys).install(ref, data)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('mode', [0o777, 0o775])
def test_harness_rejects_writable_nonsticky_ancestor(harness, tmp_path, mode):
    installer, ref, data = harness
    parent = tmp_path / 'unsafe'
    parent.mkdir(mode=mode)
    parent.chmod(mode)
    installer.root = parent / 'private' / 'skills'
    installer.root.mkdir(parents=True, mode=0o700)
    with pytest.raises(ValueError, match='ancestor'):
        installer.install(ref, data)
    assert not list(installer.root.iterdir())


@pytest.mark.parametrize('sticky', [False, True])
def test_harness_rejects_foreign_owned_ancestor(harness, tmp_path, monkeypatch, sticky):
    installer, ref, data = harness
    parent = tmp_path / 'foreign'
    parent.mkdir(mode=0o755)
    parent.chmod(0o1777 if sticky else 0o755)
    installer.root = parent / 'skills'
    installer.root.mkdir(mode=0o700)
    inode = parent.stat().st_ino
    real_stat, real_fstat = os.stat, os.fstat

    def foreign_owner(st):
        if st.st_ino != inode:
            return st
        fields = list(st)
        fields[4] = 987654
        return os.stat_result(fields)
    monkeypatch.setattr(os, 'stat', lambda *a, **kw: foreign_owner(real_stat(*a, **kw)))
    monkeypatch.setattr(os, 'fstat', lambda fd: foreign_owner(real_fstat(fd)))
    with pytest.raises(ValueError, match='ancestor'):
        installer.install(ref, data)
    assert not list(installer.root.iterdir())


def test_harness_accepts_owned_sticky_ancestor(harness, tmp_path):
    installer, ref, data = harness
    parent = tmp_path / 'sticky'
    parent.mkdir()
    parent.chmod(0o1777)
    installer.root = parent / 'skills'
    assert (installer.install(ref, data) / 'SKILL.md').read_bytes() == b'# CSV summary'


@pytest.mark.parametrize('part', ['ancestor', 'root', 'cache', 'installed'])
def test_harness_rejects_directory_replacement_before_activation(harness, tmp_path, monkeypatch, part):
    installer, ref, data = harness
    installer.root = tmp_path / 'parent' / 'skills'
    installer.install(ref, data)
    directory = (installer.root.parent if part == 'ancestor' else
                 installer.root if part == 'root' else installer.root / part)
    detached = tmp_path / 'detached'
    now = installer.now
    calls = 0

    def swap_after_verification():
        nonlocal calls
        calls += 1
        if calls == 2:
            directory.rename(detached)
            shutil.copytree(detached, directory, symlinks=True)
            if part != 'installed':
                cached = (directory / 'skills' / 'cache' if part == 'ancestor' else
                          directory / 'cache' if part == 'root' else directory)
                (cached / ref.sha256 / 'SKILL.md').write_text('UNVERIFIED replacement')
        return now()
    monkeypatch.setattr(installer, 'now', swap_after_verification)
    with pytest.raises(ValueError, match='changed'):
        installer.install(ref, data)
    assert not list(tmp_path.rglob('*.tmp'))


# ------------------------------------------------------------------------------------------ flag and HTTP
def test_flag_defaults_off_and_constructs_nothing():
    assert skill_registry_config(None)['enabled'] is False
    assert build_skill_registry(None, artifact_store=None, rustfs=None) == (None, None)
    assert build_skill_registry({'enabled': False}, artifact_store=FakeStore(), rustfs=FakeRustFS()) == (None, None)
    with pytest.raises(ValueError):
        skill_registry_config({'enabled': 'yes'})
    with pytest.raises(ValueError):
        skill_registry_config({'enabled': True})                       # key file required
    with pytest.raises(ValueError):
        build_skill_registry({'enabled': True, 'signing_key_file': '/x'}, artifact_store=None, rustfs=None)


def _serve(cfg, tmp_path):
    repo = ControlPlaneRepo()
    store, fs = FakeStore(), FakeRustFS()
    svr = ControlPlaneServer(repo, rustfs=fs, artifact_store=store, artifact_acl=ArtifactAcl(),
                             api_keys={'k-alice': 'operator', 'k-bob': 'operator'}, skill_registry=cfg)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr, f'http://127.0.0.1:{svr.port}', store


def call(base, method, path, key, body=None, ctype='application/json'):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={'X-API-Key': key, 'Content-Type': ctype})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_routes_404_and_no_capability_when_flag_off(tmp_path):
    svr, base, _ = _serve(None, tmp_path)
    try:
        assert call(base, 'GET', '/api/v1/skills', 'k-alice')[0] == 404
        assert call(base, 'POST', '/api/v1/skills', 'k-alice', {})[0] == 404
        health = json.loads(call(base, 'GET', '/api/v1/health', 'k-alice')[1])
        assert 'skill_registry_v1' not in health['capabilities']
    finally:
        svr.shutdown()


def test_http_publish_share_transfer_download_with_acl(tmp_path):
    keyfile = tmp_path / 'signing.key'
    keyfile.write_text(Ed25519PrivateKey.generate().private_bytes_raw().hex())
    keyfile.chmod(0o600)
    svr, base, store = _serve({'enabled': True, 'signing_key_file': str(keyfile), 'key_id': 'k1'}, tmp_path)
    try:
        health = json.loads(call(base, 'GET', '/api/v1/health', 'k-alice')[1])
        assert 'skill_registry_v1' in health['capabilities']
        md = metadata()
        body = {**{k: v for k, v in md.items() if k != 'files'}, 'request_key': 'rk1',
                'files': {p: base64.b64encode(b).decode() for p, b in md['files'].items()}}
        status, raw = call(base, 'POST', '/api/v1/skills', 'k-alice', body)
        assert status == 201, raw
        sid = json.loads(raw)['skill_id']
        # bob (no grant): list empty, get/transfer/download all 404, same body as a missing skill
        assert json.loads(call(base, 'GET', '/api/v1/skills', 'k-bob')[1]) == {'skills': []}
        missing = call(base, 'GET', '/api/v1/skills/artifact_nope/versions/1.0.0', 'k-bob')
        assert call(base, 'GET', f'/api/v1/skills/{sid}/versions/1.0.0', 'k-bob') == missing and missing[0] == 404
        assert call(base, 'POST', f'/api/v1/skills/{sid}/versions/1.0.0/transfer', 'k-bob', {})[0] == 404
        # alice transfers; the URL redeems once, only for alice
        status, raw = call(base, 'POST', f'/api/v1/skills/{sid}/versions/1.0.0/transfer', 'k-alice', {'install': True})
        assert status == 200
        ref = json.loads(raw)
        assert call(base, 'GET', ref['url'], 'k-bob')[0] == 404
        status, raw = call(base, 'GET', ref['url'], 'k-alice')
        assert status == 404                                        # bob's failed attempt consumed the capability
        ref = json.loads(call(base, 'POST', f'/api/v1/skills/{sid}/versions/1.0.0/transfer', 'k-alice', {})[1])
        status, data = call(base, 'GET', ref['url'], 'k-alice')
        assert status == 200 and hashlib.sha256(data).hexdigest() == ref['transport_sha256']
        assert call(base, 'GET', ref['url'], 'k-alice')[0] == 404   # single use
        # content type and unknown fields are refused
        assert call(base, 'POST', '/api/v1/skills', 'k-alice', body, ctype='text/plain')[0] == 415
        assert call(base, 'POST', '/api/v1/skills', 'k-alice', {**body, 'created_by': 'x'})[0] == 400
        # sharing: publish a v2 shared with bob
        md2 = metadata('2.0.0')
        body2 = {**body, 'version': '2.0.0', 'request_key': 'rk2', 'skill_id': sid, 'share_with': ['api-key:bob'],
                 'files': {p: base64.b64encode(b).decode() for p, b in md2['files'].items()}}
        assert call(base, 'POST', '/api/v1/skills', 'k-alice', body2)[0] == 201
    finally:
        svr.shutdown()
