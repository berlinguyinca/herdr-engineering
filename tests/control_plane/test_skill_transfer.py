"""Transfer authorization uses real Postgres; only the S3 transport is doubled."""
# ruff: noqa: F811
import hashlib
import importlib
import json
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from herdr_engineering.control_plane.collab import (
    CollabError,
    CollabService,
    StaticTokenAuthenticator,
)
from tests.control_plane.test_skill_registry import agent, env, publish, user  # noqa: F401


@pytest.fixture
def transfer(env):
    module = importlib.import_module('herdr_engineering.control_plane.skill_transfer')
    registry, run, conn, s3, _, invalid = env
    s3.grants = []
    def presign(operation, *, Params, ExpiresIn, HttpMethod):
        s3.grants.append((operation, Params, ExpiresIn, HttpMethod))
        return 'https://storage.invalid/package?X-Amz-Credential=SECRET&X-Amz-Signature=SECRET'
    s3.generate_presigned_url = presign
    return module.SkillTransfer(registry), run, conn, s3, invalid


def service(transfer):
    backend, run, *_ = transfer
    return CollabService(StaticTokenAuthenticator({}), skill_service=backend, skill_runner=run)


def test_search_filters_scope_query_and_live_authorization(transfer, env):
    backend, run, conn, *_ = transfer
    _, private = publish(env)
    _, room = publish(env, 'room')
    _, shared = publish(env, 'shared', user('maintainer'))
    for scope, ids in [('private', []), ('room', [room.skill_id]), ('shared', [shared.skill_id])]:
        result = run(backend.search_skills(agent('bob'), 'csv', scope))
        assert [s['skill_id'] for s in result] == ids
    assert run(backend.search_skills(agent('bob'), 'not present', 'shared')) == []
    assert run(backend.search_skills(agent('bob'), '%', 'shared')) == []
    assert run(backend.search_skills(agent(), '', 'private'))[0]['skill_id'] == private.skill_id
    run(conn.execute("UPDATE collab_agent_room_attachments SET status='revoked'"))
    assert run(backend.search_skills(agent('bob'), '', 'room')) == []
    run(conn.execute("UPDATE collab_skill_versions SET status='revoked'"))
    assert run(backend.search_skills(agent(), '', 'private')) == []


@pytest.mark.parametrize('method', ['get_skill', 'download_skill', 'install_skill'])
def test_transfer_cannot_read_another_owners_private_package(transfer, env, method):
    backend, run, _, s3, _ = transfer
    _, v = publish(env)
    with pytest.raises(CollabError) as exc:
        run(getattr(backend, method)(agent('bob'), v.skill_id, v.version))
    assert exc.value.status == 404
    assert not s3.grants


def test_download_is_short_lived_verified_and_not_an_install(transfer, env):
    backend, run, conn, s3, _ = transfer
    _, v = publish(env)
    ref = run(backend.download_skill(agent(), v.skill_id, v.version))
    data = s3.objects['skills', v.storage_key]
    assert ref.sha256 == v.sha256
    assert ref.transport_sha256 == hashlib.sha256(data).hexdigest()
    assert ref.size_bytes == len(data)
    assert 0 < ref.expires_at - backend.now() <= 60
    assert s3.grants == [('get_object', {'Bucket': 'skills', 'Key': v.storage_key}, 60, 'GET')]
    assert 'SECRET' not in repr(ref)
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_installations')) == 0
    audit = run(conn.fetch('SELECT * FROM collab_audit_events'))
    assert any(row['action'] == 'skill.download_requested' for row in audit)
    assert 'SECRET' not in str(audit)


@pytest.mark.parametrize('method', ['get_skill', 'download_skill', 'install_skill'])
def test_corrupted_storage_is_rejected_before_issuing_grants(transfer, env, method):
    backend, run, _, s3, _ = transfer
    _, v = publish(env)
    s3.objects['skills', v.storage_key] = b'{"secret":"provider-secret"}'
    with pytest.raises(CollabError) as exc:
        run(getattr(backend, method)(agent(), v.skill_id, v.version))
    assert exc.value.status == 409 and 'provider-secret' not in str(exc.value)
    assert not s3.grants


@pytest.mark.parametrize('method', ['search_skills', 'get_skill', 'download_skill', 'install_skill'])
def test_live_revocation_denies_every_operation(transfer, env, method):
    backend, run, _, s3, invalid = transfer
    _, v = publish(env)
    invalid.add(agent().key)
    args = ('', 'private') if method == 'search_skills' else (v.skill_id, v.version)
    with pytest.raises(CollabError) as exc:
        run(getattr(backend, method)(agent(), *args))
    assert exc.value.status == 403
    assert not s3.grants


def test_revocation_during_presigning_discards_grant_and_audit(transfer, env):
    backend, run, conn, s3, invalid = transfer
    _, v = publish(env)
    def revoked(*args, **kwargs):
        invalid.add(agent().key)
        return 'https://storage.invalid/?SECRET'
    s3.generate_presigned_url = revoked
    with pytest.raises(CollabError) as exc:
        run(backend.install_skill(agent(), v.skill_id, v.version))
    assert exc.value.status == 403
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action LIKE '%requested'")) == 0


def test_install_records_request_only_and_confirmed_harness_receipt(transfer, env):
    backend, run, conn, *_ = transfer
    _, v = publish(env)
    ref = run(backend.install_skill(agent(), v.skill_id, v.version))
    assert ref.sha256 == v.sha256
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_installations')) == 0
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='skill.install_requested'")) == 1
    for _ in range(2):
        run(backend.record_installation(agent(), v.skill_id, v.version, ref.sha256))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_installations')) == 1
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='skill.install_reported'")) == 1
    with pytest.raises(CollabError):
        run(backend.record_installation(agent('bob'), v.skill_id, v.version, ref.sha256))
    with pytest.raises(CollabError):
        run(backend.record_installation(agent(), v.skill_id, v.version, '0' * 64))


@pytest.fixture
def harness(transfer, env, tmp_path):
    from herdr_engineering.control_plane.skill_transfer import HarnessSkillInstaller
    backend, run, _, s3, _ = transfer
    _, v = publish(env)
    ref = run(backend.install_skill(agent(), v.skill_id, v.version))
    installer = HarnessSkillInstaller(tmp_path, trusted_keys=env[0].trusted_keys)
    return installer, ref, s3.objects['skills', v.storage_key]


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
    assert 'SECRET' not in ''.join(p.read_text() for p in tmp_path.rglob('*') if p.is_file())


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


def test_failed_activation_preserves_previous_version(harness, transfer, env, tmp_path, monkeypatch):
    import os
    installer, ref, data = harness
    first = installer.install(ref, data)
    _, v = publish(env, skill_id=ref.skill_id, version='2.0.0')
    backend, run, _, s3, _ = transfer
    second = run(backend.install_skill(agent(), v.skill_id, v.version))
    def fail(*args, **kwargs):
        raise OSError('disk unavailable')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        installer.install(second, s3.objects['skills', v.storage_key])
    assert [p.resolve() for p in (tmp_path / 'installed').iterdir()] == [first]
    assert not list(tmp_path.rglob('*.tmp'))


def test_search_rechecks_room_access_before_returning_batch(transfer, env, monkeypatch):
    backend, run, conn, *_ = transfer
    publish(env, 'room')
    original = backend.get_skill
    async def expire_after_load(*args):
        result = await original(*args)
        await conn.execute("UPDATE collab_room_memberships SET expires_at=1 WHERE user_id='bob'")
        return result
    monkeypatch.setattr(backend, 'get_skill', expire_after_load)
    assert run(backend.search_skills(agent('bob'), '', 'room')) == []


def test_harness_rejects_nonprivate_root(harness, tmp_path):
    installer, ref, data = harness
    tmp_path.chmod(0o755)
    try:
        with pytest.raises(ValueError, match='private'):
            installer.install(ref, data)
        assert not list(tmp_path.iterdir())
    finally:
        tmp_path.chmod(0o700)


def test_harness_activation_failure_after_staging_preserves_old_link(harness, transfer, env, tmp_path, monkeypatch):
    import os
    installer, ref, data = harness
    first = installer.install(ref, data)
    _, v = publish(env, skill_id=ref.skill_id, version='2.0.0')
    backend, run, _, s3, _ = transfer
    second = run(backend.install_skill(agent(), v.skill_id, v.version))
    original = os.replace
    def fail_pointer(source, target, **kwargs):
        import stat
        info = os.stat(source, dir_fd=kwargs.get('src_dir_fd'), follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise OSError('activation unavailable')
        return original(source, target, **kwargs)
    monkeypatch.setattr(os, 'replace', fail_pointer)
    with pytest.raises(OSError):
        installer.install(second, s3.objects['skills', v.storage_key])
    assert [p.resolve() for p in (tmp_path / 'installed').iterdir()] == [first]
    assert not list(tmp_path.rglob('*.tmp'))


@pytest.mark.parametrize('mutation', [
    "UPDATE collab_room_memberships SET status='removed' WHERE user_id='bob'",
    "UPDATE collab_room_memberships SET expires_at=1 WHERE user_id='bob'",
    "UPDATE collab_agent_room_attachments SET status='revoked'",
    "UPDATE collab_agent_room_attachments SET expires_at=1",
    "UPDATE collab_agent_room_attachments SET actions='{}'",
    "UPDATE collab_rooms SET status='archiving'",
])
def test_room_search_and_grants_follow_revocation(transfer, env, mutation):
    backend, run, conn, s3, _ = transfer
    _, v = publish(env, 'room')
    run(conn.execute(mutation))
    assert run(backend.search_skills(agent('bob'), '', 'room')) == []
    for method in (backend.get_skill, backend.download_skill, backend.install_skill):
        with pytest.raises(CollabError) as exc:
            run(method(agent('bob'), v.skill_id, v.version))
        assert exc.value.status == 404
    assert not s3.grants


@pytest.mark.parametrize('url', ['http://storage.invalid/secret', 'https://user:secret@storage.invalid/'])
def test_insecure_presign_is_not_returned(transfer, env, url):
    backend, run, conn, s3, _ = transfer
    _, v = publish(env)
    s3.generate_presigned_url = lambda *a, **kw: url
    with pytest.raises(CollabError) as exc:
        run(backend.download_skill(agent(), v.skill_id, v.version))
    assert exc.value.status == 503 and 'secret' not in str(exc.value)
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='skill.download_requested'")) == 0


def test_human_cannot_request_or_report_agent_installation(transfer, env):
    backend, run, conn, *_ = transfer
    _, v = publish(env)
    for method, args in [(backend.install_skill, ()), (backend.record_installation, (v.sha256,))]:
        with pytest.raises(CollabError) as exc:
            run(method(user(), v.skill_id, v.version, *args))
        assert exc.value.status == 403
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_installations')) == 0


def test_harness_swaps_active_version_and_retains_immutable_cache(harness, transfer, env, tmp_path):
    installer, ref, data = harness
    first = installer.install(ref, data)
    _, v = publish(env, skill_id=ref.skill_id, version='2.0.0')
    backend, run, _, s3, _ = transfer
    second = run(backend.install_skill(agent(), v.skill_id, v.version))
    latest = installer.install(second, s3.objects['skills', v.storage_key])
    assert first != latest and first.exists()
    assert [p.resolve() for p in (tmp_path / 'installed').iterdir()] == [latest]


def test_harness_rejects_symlink_cache_root(harness, tmp_path):
    installer, ref, data = harness
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'cache').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        installer.install(ref, data)
    assert not list(outside.iterdir())


def test_harness_rejects_signed_file_directory_collision(transfer, env, tmp_path):
    from herdr_engineering.control_plane.skill_transfer import HarnessSkillInstaller
    backend, run, _, s3, _ = transfer
    _, v = publish(env, files={'SKILL.md': b'# skill', 'requirements.lock': b'',
                              'fixtures': b'file', 'fixtures/input.csv': b'a\n1\n'})
    ref = run(backend.install_skill(agent(), v.skill_id, v.version))
    with pytest.raises(ValueError):
        HarnessSkillInstaller(tmp_path, trusted_keys=env[0].trusted_keys).install(ref, s3.objects['skills', v.storage_key])
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
    import os
    installer, ref, data = harness
    parent = tmp_path / 'foreign'
    parent.mkdir(mode=0o755)
    parent.chmod(0o1777 if sticky else 0o755)
    installer.root = parent / 'skills'
    installer.root.mkdir(mode=0o700)
    inode = parent.stat().st_ino
    stat, fstat = os.stat, os.fstat
    def foreign_owner(st):
        if st.st_ino != inode:
            return st
        fields = list(st)
        fields[4] = 987654  # Ownership metadata only; no elevated chown required.
        return os.stat_result(fields)
    monkeypatch.setattr(os, 'stat', lambda *a, **kw: foreign_owner(stat(*a, **kw)))
    monkeypatch.setattr(os, 'fstat', lambda fd: foreign_owner(fstat(fd)))
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
    import shutil
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


@pytest.mark.parametrize('existing', [False, True])
def test_harness_anchors_activation_when_root_moves_at_replace(harness, transfer, env, tmp_path, monkeypatch, existing):
    import os
    import shutil
    import stat
    installer, ref, data = harness
    installer.root = tmp_path / 'skills'
    active = installer.root / 'installed'
    slot = hashlib.sha256(ref.skill_id.encode()).hexdigest()
    old = None
    if existing:
        installer.install(ref, data)
        old = os.readlink(active / slot)
        _, version = publish(env, skill_id=ref.skill_id, version='2.0.0')
        backend, run, _, s3, _ = transfer
        ref = run(backend.install_skill(agent(), version.skill_id, version.version))
        data = s3.objects['skills', version.storage_key]
    detached = tmp_path / 'detached'
    replace_file = os.replace
    swapped = False
    def swap_at_activation(source, destination, **kwargs):
        nonlocal swapped
        info = os.stat(source, dir_fd=kwargs.get('src_dir_fd'), follow_symlinks=False)
        if not swapped and stat.S_ISLNK(info.st_mode):
            swapped = True
            installer.root.rename(detached)
            shutil.copytree(detached, installer.root, symlinks=True)
            # The untrusted replacement must not receive the real activation.
            (active / slot).unlink(missing_ok=True)
        return replace_file(source, destination, **kwargs)
    monkeypatch.setattr(os, 'replace', swap_at_activation)
    with pytest.raises(ValueError, match='changed'):
        installer.install(ref, data)
    assert swapped
    assert not (active / slot).is_symlink()
    if existing:
        assert os.readlink(detached / 'installed' / slot) == old
    else:
        assert not (detached / 'installed' / slot).is_symlink()
    # The replacement copy is attacker-owned data; only anchored original temps are removed.
    assert not list((detached / 'installed').glob('*.tmp'))
