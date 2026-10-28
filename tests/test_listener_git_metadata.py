"""Service Git identity belongs to the owned listener's checkout."""
import os
import subprocess
from unittest.mock import Mock

import pytest

import herdr_engineering.fabric_agent as fa


def checkout(path, origin):
    path.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    def git(*args):
        return subprocess.check_output(['git', '-C', str(path), *args], env=env, stderr=subprocess.DEVNULL, text=True).strip()
    git('init')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    git('remote', 'add', 'origin', origin)
    git('commit', '--allow-empty', '-m', 'fixture')
    return git('rev-parse', 'HEAD')


def listener(monkeypatch, cwd):
    monkeypatch.setattr(fa, '_listener_cwd', lambda pid, **kwargs: str(cwd))
    monkeypatch.setattr(fa, '_process_identity', lambda pid, **kwargs: {'parent_pid': 100, 'start': 'birth'})
    return {'kind': 'herdr_process_ancestry', 'listener_pid': 300, 'listener_start': 'birth'}


def test_listener_checkout_wins_over_inherited_agent_git_environment(tmp_path, monkeypatch):
    frontend, backend = tmp_path / 'frontend', tmp_path / 'backend'
    checkout(frontend, 'https://github.com/org/frontend.git')
    sha = checkout(backend, 'git@github.com:Org/Backend.git')
    proof = listener(monkeypatch, backend)
    monkeypatch.setenv('GIT_DIR', str(frontend / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(frontend))
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'remote.origin.url')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'https://evil.example/other/repo')
    metadata = fa.ListenerGitMetadata().read(300, proof, timeout=1)
    assert metadata == {'repository_id': 'github.com/org/backend', 'commit_sha': sha}


def test_unowned_listener_never_probes_git_or_cwd(monkeypatch):
    cwd = Mock(side_effect=AssertionError('must not inspect unowned cwd'))
    monkeypatch.setattr(fa, '_listener_cwd', cwd)
    reader = fa.ListenerGitMetadata()
    assert reader.read(300, None) == {}
    assert reader.read(300, {'kind': 'system_unit'}) == {}
    cwd.assert_not_called()


def test_missing_git_or_non_checkout_is_optional_metadata(tmp_path, monkeypatch):
    proof = listener(monkeypatch, tmp_path)
    assert fa.ListenerGitMetadata().read(300, proof) == {}
    monkeypatch.setattr(fa.shutil, 'which', lambda command: None)
    assert fa.ListenerGitMetadata().read(300, proof) == {}


def test_pid_reuse_never_reuses_previous_git_cache(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    checkout(repo, 'https://github.com/org/app.git')
    proof = listener(monkeypatch, repo)
    reader = fa.ListenerGitMetadata()
    assert reader.read(300, proof)['repository_id'] == 'github.com/org/app'
    monkeypatch.setattr(fa, '_process_identity', lambda pid, **kwargs: {'parent_pid': 100, 'start': 'reused'})
    assert reader.read(300, proof) == {}


def test_listener_git_metadata_handles_high_numbered_pipe(tmp_path, monkeypatch):
    fcntl = pytest.importorskip('fcntl')
    resource = pytest.importorskip('resource')
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 1024:
        pytest.skip('process descriptor limit cannot exercise descriptors above select FD_SETSIZE')
    repo = tmp_path / 'repo'
    sha = checkout(repo, 'https://github.com/org/app.git')
    proof = listener(monkeypatch, repo)
    popen = subprocess.Popen
    descriptors = []
    pipes = []

    def high_pipe(*args, **kwargs):
        process = popen(*args, **kwargs)
        descriptor = fcntl.fcntl(process.stdout.fileno(), fcntl.F_DUPFD, 1024)
        os.set_inheritable(descriptor, False)
        process.stdout.close()
        process.stdout = os.fdopen(descriptor, 'rb')
        descriptors.append(descriptor)
        pipes.append(process.stdout)
        return process

    monkeypatch.setattr(fa.subprocess, 'Popen', high_pipe)
    assert fa.ListenerGitMetadata().read(300, proof, timeout=1) == {
        'repository_id': 'github.com/org/app', 'commit_sha': sha,
    }
    assert len(descriptors) == 2 and all(descriptor >= 1024 for descriptor in descriptors)
    assert all(pipe.closed for pipe in pipes)


def test_live_report_without_proof_clears_old_git_metadata(tmp_path, monkeypatch):
    agent = fa.FabricAgent(Mock(), fa.AgentConfig(tailnet_ip='127.0.0.1', store_path=tmp_path / 'state', control_plane_url='http://test'))
    agent._store['8080'] = {'label': 'app', 'source_port': 8080,
                           'repository_id': 'github.com/old/app', 'commit_sha': 'a' * 40}
    agent._ownership.classify = lambda *args, **kwargs: None
    agent._report_service = Mock(return_value=True)
    monkeypatch.setattr(fa, '_listener_project', lambda *args, **kwargs: 'descriptive-folder')
    agent._report_services({8080: {'pid': 300}})
    sent = agent._report_service.call_args.args[0]
    assert sent['repository_id'] is None and sent['commit_sha'] is None


def test_unavailable_service_keeps_recorded_launch_and_git_identity(tmp_path, monkeypatch):
    agent = fa.FabricAgent(Mock(), fa.AgentConfig(tailnet_ip='127.0.0.1', store_path=tmp_path / 'state', control_plane_url='http://test'))
    proof = {'kind': 'herdr_process_ancestry', 'session_id': 'native-session'}
    entry = {'label': 'app', 'source_port': 8080, 'ownership': proof,
             'repository_id': 'github.com/org/backend', 'commit_sha': 'b' * 40}
    agent._cp_unavailable['app'] = entry
    agent._ownership.classify = Mock(side_effect=AssertionError('unavailable service has no live process'))
    agent._report_service = Mock(return_value=True)
    agent._report_services({})
    sent, status = agent._report_service.call_args.args
    assert status == 'unavailable'
    assert sent['ownership'] == proof
    assert sent['repository_id'] == 'github.com/org/backend' and sent['commit_sha'] == 'b' * 40


def test_git_metadata_read_limits_output_and_stalled_process_time():
    import sys
    import time
    reader = fa.ListenerGitMetadata()
    assert reader._git(sys.executable, '.', ['-c', 'print("x" * 10000)'], time.monotonic() + 1) is None
    started = time.monotonic()
    assert reader._git(sys.executable, '.', ['-c', 'import time; time.sleep(10)'], started + 0.05) is None
    assert time.monotonic() - started < 1


def test_same_process_keeps_verified_launch_when_owner_pane_has_ended(tmp_path, monkeypatch):
    agent = fa.FabricAgent(Mock(), fa.AgentConfig(tailnet_ip='127.0.0.1', store_path=tmp_path / 'state', control_plane_url='http://test'))
    proof = {'kind': 'herdr_process_ancestry', 'listener_pid': 300, 'listener_start': 'birth'}
    agent._store['8080'] = {'label': 'app', 'source_port': 8080, 'ownership': proof}
    agent._ownership.classify = lambda *args, **kwargs: None
    agent._git_metadata.read = lambda *args, **kwargs: {}
    agent._report_service = Mock(return_value=True)
    monkeypatch.setattr(fa, '_listener_project', lambda *args, **kwargs: None)
    monkeypatch.setattr(fa, '_process_identity', lambda *args, **kwargs: {'parent_pid': 1, 'start': 'birth'})
    agent._report_services({8080: {'pid': 300}})
    assert agent._report_service.call_args.args[0]['ownership'] == proof
    agent._cp_last_report.clear()
    monkeypatch.setattr(fa, '_process_identity', lambda *args, **kwargs: {'parent_pid': 1, 'start': 'new-birth'})
    agent._report_services({8080: {'pid': 300}})
    assert agent._report_service.call_args.args[0]['ownership'] is None
