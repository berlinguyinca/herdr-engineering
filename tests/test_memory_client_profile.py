"""Private deployment artifacts for the existing canonical memory client."""
import io
import json
import os
import stat
import subprocess
import tarfile

import pytest

from herdr_engineering.memory_client_profile import (
    MAX_KEY,
    ProfileError,
    check_archive,
    clear_profile,
    install_profile,
    policy_info,
    prepare_profile,
    receive_profile,
    verify_runtime,
)
from herdr_engineering.memory_install import PINNED_CLIENT_VERSION


@pytest.fixture
def profile(tmp_path):
    source = tmp_path / 'source'
    source.mkdir(mode=0o700)
    key = source / 'project.key'
    key.write_text('project-fixture-credential')
    key.chmod(0o600)
    global_key = source / 'global.key'
    global_key.write_text('global-fixture-credential')
    global_key.chmod(0o600)
    data = {'url': 'https://memory.example.test', 'default_project': 'atlas',
            'cache_dir': '/operator/private-cache',
            'projects': {
                'atlas': {'paths': ['/operator/repository'], 'tenant': 'engineering',
                          'credentials': {'codex': str(key), 'pi': '/do-not-read/pi.key'}},
                'personal': {'credentials': {'codex': '/do-not-read/personal.key'}}},
            'scopes': {'global': {'credentials': {'codex': str(global_key)}},
                       'infra': {'credentials': {'codex': '/do-not-read/infra.key'}}}}
    config = source / 'config.json'
    config.write_text(json.dumps(data))
    config.chmod(0o600)
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        result = {'client': PINNED_CLIENT_VERSION, 'contract': 1} if '--version' in argv else {
            'contract': 1, 'client': PINNED_CLIENT_VERSION, 'valid': True}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(result), stderr='')

    return config, data, key, runner, calls


def policy(projects=None, scopes=None):
    return {'enabled': True, 'viewers': {
        'owner': {'projects': ['atlas'] if projects is None else projects,
                  'scopes': [] if scopes is None else scopes}}}


def contents(path):
    with tarfile.open(path) as archive:
        return {member.name: (member, archive.extractfile(member).read())
                for member in archive.getmembers()}


def test_private_archive_selects_engineering_bindings_without_user_cache_or_personal_keys(profile, tmp_path):
    source, _, _, runner, calls = profile
    output = tmp_path / 'prepared' / 'client.tar'
    receipt = prepare_profile(source, policy(scopes=['global']), output, runner=runner)
    files = contents(output)
    config = json.loads(files['config.json'][1])
    assert receipt['prepared'] is True
    assert receipt['projects'] == ['atlas'] and receipt['scopes'] == ['global']
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert all(m.mode == 0o600 and m.uid == m.gid == 0 for m, _ in files.values())
    assert set(config['projects']) == {'atlas'} and set(config['scopes']) == {'global'}
    assert set(config['projects']['atlas']['credentials']) == {'codex'}
    assert 'paths' not in config['projects']['atlas']
    assert config['cache_dir'] == '/state/herdr-memory-explorer'
    assert '/operator/private-cache' not in json.dumps(config)
    assert 'fixture-credential' not in json.dumps(receipt)
    assert len(files) == 3 and any('--version' in argv for argv, _ in calls)
    assert any('validate-config' in argv for argv, _ in calls)
    assert not any('status' in argv for argv, _ in calls)
    assert receipt['source_client_validated'] is True
    assert 'source_cli_healthy' not in receipt


@pytest.mark.parametrize('settings', [{}, {'enabled': False, 'viewers': policy()['viewers']},
                                      {'enabled': True, 'viewers': {}}])
def test_disabled_or_ungranted_memory_does_not_open_source_or_emit_credentials(tmp_path, settings):
    output = tmp_path / 'unused.tar'
    receipt = prepare_profile(tmp_path / 'nonexistent.json', settings, output)
    assert receipt['prepared'] is False and not output.exists()


@pytest.mark.parametrize('target', ['config', 'key'])
def test_public_permissions_and_symlinks_fail_before_archive_creation(profile, tmp_path, target):
    source, _, key, runner, _ = profile
    selected = source if target == 'config' else key
    selected.chmod(0o644)
    output = tmp_path / 'bad.tar'
    with pytest.raises(ProfileError):
        prepare_profile(source, policy(), output, runner=runner)
    assert not output.exists()
    selected.chmod(0o600)
    original = selected.with_suffix('.original')
    selected.rename(original)
    selected.symlink_to(original)
    with pytest.raises(ProfileError):
        prepare_profile(source, policy(), output, runner=runner)
    assert not output.exists()


def test_fifo_credential_is_rejected_without_blocking(profile, tmp_path):
    source, _, key, runner, _ = profile
    key.unlink()
    os.mkfifo(key, mode=0o600)
    with pytest.raises(ProfileError):
        prepare_profile(source, policy(), tmp_path / 'bad.tar', runner=runner)


def test_wrong_source_owner_is_rejected(profile, tmp_path, monkeypatch):
    source, _, _, runner, calls = profile
    uid = os.getuid()
    monkeypatch.setattr(os, 'getuid', lambda: uid + 1)
    with pytest.raises(ProfileError):
        prepare_profile(source, policy(), tmp_path / 'bad.tar', runner=runner)
    assert calls == []


def test_personal_grant_is_never_exported(profile, tmp_path):
    source, _, _, runner, _ = profile
    with pytest.raises(ProfileError):
        prepare_profile(source, policy(projects=['personal']), tmp_path / 'bad.tar', runner=runner)


def test_global_only_viewer_uses_nonpersonal_identity_anchor_without_changing_browser_grants(profile, tmp_path):
    source, _, _, runner, _ = profile
    output = tmp_path / 'global' / 'client.tar'
    receipt = prepare_profile(source, policy(projects=[], scopes=['global']), output, runner=runner)
    config = json.loads(contents(output)['config.json'][1])
    assert receipt['identity_project'] == 'atlas'
    assert receipt['viewer_projects'] == []
    assert config['default_project'] == 'atlas'
    assert set(config['projects']) == {'atlas'} and set(config['scopes']) == {'global'}


def test_cli_validation_rejection_is_sanitized_and_creates_no_archive(profile, tmp_path):
    source, _, _, _, _ = profile
    def reject(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout='private fixture-credential', stderr='secret detail')
    output = tmp_path / 'bad.tar'
    with pytest.raises(ProfileError) as error:
        prepare_profile(source, policy(), output, runner=reject)
    assert 'fixture-credential' not in str(error.value) and 'secret detail' not in str(error.value)
    assert not output.exists()


def test_profile_requires_the_pinned_client_not_only_a_claimed_contract(profile, tmp_path):
    source, _, _, _, _ = profile
    def old_client(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout='{"client":"0.1.5","contract":1}', stderr='')
    with pytest.raises(ProfileError):
        prepare_profile(source, policy(), tmp_path / 'bad.tar', runner=old_client)


def test_concurrent_source_configuration_change_is_not_silently_exported(profile, tmp_path):
    source, config, _, runner, _ = profile
    def change_during_validation(argv, **kwargs):
        if 'validate-config' in argv:
            config['url'] = 'https://changed.example.test'
            source.write_text(json.dumps(config))
        return runner(argv, **kwargs)
    output = tmp_path / 'bad.tar'
    with pytest.raises(ProfileError):
        prepare_profile(source, policy(), output, runner=change_during_validation)
    assert not output.exists()


def test_receive_writes_private_files_and_no_operator_outbox(profile, tmp_path):
    source, _, _, runner, _ = profile
    archive = tmp_path / 'prepared' / 'client.tar'
    prepare_profile(source, policy(), archive, runner=runner)
    target = tmp_path / 'runtime'
    receive_profile(archive.read_bytes(), target=target)
    config = json.loads((target / 'config.json').read_text())
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / 'config.json').stat().st_mode) == 0o600
    assert all(p.stat().st_uid == os.getuid() for p in target.rglob('*'))
    assert not any(p.name == 'delivery.sqlite3' for p in target.rglob('*'))
    assert config['cache_dir'] == '/state/herdr-memory-explorer'


@pytest.mark.parametrize('ending', ['', '\n', '\r\n'])
def test_maximum_token_round_trips_without_growing_the_archive_member(profile, tmp_path, ending):
    source, _, key, runner, _ = profile
    key.write_bytes(('k' * MAX_KEY + ending).encode())
    archive = tmp_path / 'prepared' / 'client.tar'
    prepare_profile(source, policy(), archive, runner=runner)
    receive_profile(archive.read_bytes(), target=tmp_path / 'runtime')
    keys = [data for name, (_, data) in contents(archive).items() if name.startswith('credentials/')]
    assert keys == [b'k' * MAX_KEY]


def test_oversized_normalized_token_is_rejected(profile, tmp_path):
    source, _, key, runner, _ = profile
    key.write_bytes(b'k' * (MAX_KEY + 1))
    with pytest.raises(ProfileError):
        prepare_profile(source, policy(), tmp_path / 'bad.tar', runner=runner)


@pytest.mark.parametrize('output_kind', ['source', 'credential'])
def test_archive_output_never_overwrites_its_source_inputs(profile, output_kind):
    source, _, key, runner, _ = profile
    output = source if output_kind == 'source' else key
    before = output.read_bytes()
    with pytest.raises(ProfileError):
        prepare_profile(source, policy(), output, runner=runner)
    assert output.read_bytes() == before


@pytest.mark.parametrize('member_name,kind', [('../outside', 'regular'),
    ('/outside', 'regular'), ('credentials/key', 'symlink'), ('extra.env', 'regular')])
def test_receive_rejects_unsafe_archive_members_without_partial_install(tmp_path, member_name, kind):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        member = tarfile.TarInfo(member_name)
        member.mode = 0o600
        if kind == 'symlink':
            member.type = tarfile.SYMTYPE
            member.linkname = '/outside'
            archive.addfile(member)
        else:
            member.size = 1
            archive.addfile(member, io.BytesIO(b'x'))
    target = tmp_path / 'runtime'
    with pytest.raises(ProfileError):
        receive_profile(stream.getvalue(), target=target)
    assert not target.exists()


def test_docker_install_sends_archive_via_stdin_never_argv(profile, tmp_path):
    source, _, _, runner, _ = profile
    output = tmp_path / 'prepared' / 'client.tar'
    prepare_profile(source, policy(), output, runner=runner)
    calls = []
    def docker(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b'', stderr=b'')
    install_profile(output, 'owned-test-container', runner=docker)
    argv, kwargs = calls[0]
    assert argv[:3] == ['docker', 'exec', '-i']
    assert 'owned-test-container' in argv and 'receive' in argv
    assert kwargs['input'] == output.read_bytes()
    assert 'fixture-credential' not in ' '.join(argv)


def test_clear_removes_only_managed_runtime_profile_not_source_or_cache(profile, tmp_path):
    source, _, key, runner, _ = profile
    original = key.read_bytes()
    archive = tmp_path / 'prepared' / 'client.tar'
    prepare_profile(source, policy(), archive, runner=runner)
    target = tmp_path / 'runtime'
    receive_profile(archive.read_bytes(), target=target)
    cache = tmp_path / 'separate-cache'
    cache.mkdir()
    (cache / 'delivery.sqlite3').write_bytes(b'accepted outbox state')
    clear_profile(target=target)
    assert not target.exists()
    assert key.read_bytes() == original
    assert (cache / 'delivery.sqlite3').read_bytes() == b'accepted outbox state'
    clear_profile(target=target)
    assert not target.exists()


def test_clear_preserves_unmanaged_directory_contents(tmp_path):
    target = tmp_path / 'runtime'
    target.mkdir(mode=0o700)
    custom = target / 'config.json'
    custom.write_text('private user data')
    custom.chmod(0o600)
    with pytest.raises(ProfileError):
        clear_profile(target=target)
    assert custom.read_text() == 'private user data'


def test_docker_removal_uses_no_archive_or_credentials():
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b'', stderr=b'')
    install_profile(None, 'owned-test-container', runner=runner)
    argv, kwargs = calls[0]
    assert argv[-1] == 'clear' and kwargs['input'] == b''


def test_runtime_version_check_is_local_and_does_not_read_credentials():
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({
            'client': PINNED_CLIENT_VERSION, 'contract': 1}), stderr='')
    verify_runtime('owned-test-container', runner=runner)
    assert calls == [['docker', 'exec', 'owned-test-container', 'herdr-memory', '--version']]


def test_runtime_version_failure_is_sanitized():
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout='private fixture-credential',
                                           stderr='private token detail')
    with pytest.raises(ProfileError) as error:
        verify_runtime('owned-test-container', runner=runner)
    assert 'fixture-credential' not in str(error.value) and 'token detail' not in str(error.value)


def test_interrupted_first_activation_can_be_cleared_without_losing_sources(profile, tmp_path, monkeypatch):
    from herdr_engineering import memory_client_profile as module
    source, _, key, runner, _ = profile
    archive = tmp_path / 'prepared' / 'client.tar'
    prepare_profile(source, policy(), archive, runner=runner)
    original_atomic = module._atomic
    def fail_config(path, data):
        if path.name == 'config.json':
            raise ProfileError('simulated I/O failure')
        return original_atomic(path, data)
    target = tmp_path / 'runtime'
    monkeypatch.setattr(module, '_atomic', fail_config)
    with pytest.raises(ProfileError):
        receive_profile(archive.read_bytes(), target=target)
    clear_profile(target=target)
    assert not target.exists()
    assert key.read_text() == 'project-fixture-credential'


@pytest.mark.parametrize('settings,expected', [
    ({}, {'enabled': False, 'mode': 'clear'}),
    ({'enabled': True, 'viewers': {}}, {'enabled': False, 'mode': 'clear'}),
    (policy(), {'enabled': True, 'mode': 'install'}),
    ({**policy(), 'command': ['custom-memory-wrapper']}, {'enabled': False, 'mode': 'unmanaged'}),
    ({**policy(), 'command': ['custom-memory-wrapper'], 'client_config_source': '/private/operator.json'},
     {'enabled': True, 'mode': 'install'}),
])
def test_policy_info_is_only_runtime_selection_metadata(settings, expected):
    settings['private_backend_secret'] = 'never-print-this-value'
    assert policy_info(settings) == expected
    assert 'never-print-this-value' not in json.dumps(policy_info(settings))


def test_explicit_profile_override_can_manage_a_custom_command():
    assert policy_info({**policy(), 'command': ['custom']}, profile_override=True) == {
        'enabled': True, 'mode': 'install'}


def test_archive_check_is_read_only_and_never_returns_credentials(profile, tmp_path):
    source, _, _, runner, _ = profile
    archive = tmp_path / 'prepared' / 'client.tar'
    prepare_profile(source, policy(), archive, runner=runner)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    receipt = check_archive(archive)
    assert receipt['valid'] is True
    assert 'fixture-credential' not in json.dumps(receipt)
    assert len(receipt['sha256']) == 64
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before
