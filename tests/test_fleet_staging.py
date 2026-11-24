"""Managed releases preserve work and require real installation provenance."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'ansible/files/stage-release.py'
    spec = importlib.util.spec_from_file_location('stage_release', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'upstream'
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True)
    for key, value in [('user.name', 'Test'), ('user.email', 'test@example.test')]:
        subprocess.run(['git', '-C', str(repo), 'config', key, value], check=True)
    (repo / 'package.json').write_text(json.dumps({'name': 'pi-engineering-runtime'}))
    (repo / 'package-lock.json').write_text('{}')
    (repo / '.gitignore').write_text('node_modules/\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'initial'], check=True, capture_output=True)
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    return repo, sha


def install_ok(cmd, **kwargs):
    if cmd[0] == 'npm':
        (Path(kwargs['cwd']) / 'node_modules').mkdir(exist_ok=True)
        return ''
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def test_staging_pins_revision_preserves_settings_and_developer_work(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    settings = home / '.pi/agent/settings.json'
    settings.parent.mkdir(parents=True)
    developer = home / 'IdeaProjects/pi-engineering'
    developer.mkdir(parents=True)
    (developer / 'package.json').write_text('{"name":"pi-engineering-runtime"}')
    (developer / 'notes').write_text('uncommitted work')
    settings.write_text(json.dumps({'theme': 'dark', 'packages': [
        'npm:unrelated', {'source': str(developer), 'extensions': ['!hidden.ts']}]}))
    (repo / 'new-file').write_text('upstream moved')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'next'], check=True, capture_output=True)
    result = stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert result['revision'] == sha
    release = Path(result['sourceRoot'])
    assert not (release / 'new-file').exists()
    assert (developer / 'notes').read_text() == 'uncommitted work'
    assert json.loads(settings.read_text()) == {'theme': 'dark', 'packages': [
        'npm:unrelated', {'source': str(release), 'extensions': ['!hidden.ts']}]}
    assert stage.describe_pi(home, runner=install_ok) == result
    assert stage.stage_pi(home, sha, repository=str(repo), runner=install_ok) == result


def test_install_failure_does_not_register_or_attest(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    def fail(cmd, **kwargs):
        if cmd[0] == 'npm':
            raise RuntimeError('dependency install failed')
        return install_ok(cmd, **kwargs)
    with pytest.raises(RuntimeError, match='dependency install failed'):
        stage.stage_pi(home, sha, repository=str(repo), runner=fail)
    assert not (home / '.pi/agent/settings.json').exists()
    assert stage.describe_pi(home, runner=install_ok) is None


def test_dirty_managed_release_is_preserved_and_refused(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    result = stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    changed = Path(result['sourceRoot']) / 'package.json'
    changed.write_text('local work')
    with pytest.raises(RuntimeError, match='dirty'):
        stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert changed.read_text() == 'local work'
    assert stage.describe_pi(home, runner=install_ok) is None


def test_invalid_settings_are_not_overwritten(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    settings = home / '.pi/agent/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text('{not json')
    with pytest.raises(ValueError):
        stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert settings.read_text() == '{not json'


def test_moving_ref_rejected_without_commands(tmp_path):
    stage = module()
    with pytest.raises(ValueError, match='commit'):
        stage.stage_pi(tmp_path, 'main', runner=lambda *a, **kw: pytest.fail('must not run'))


def test_pi_settings_lock_is_respected(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    settings = home / '.pi/agent/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme":"dark"}')
    settings.with_name('settings.json.lock').mkdir()
    with pytest.raises(RuntimeError, match='settings.*locked'):
        stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert settings.read_text() == '{"theme":"dark"}'
    assert stage.describe_pi(home, runner=install_ok) is None


def test_pi_concurrent_settings_edit_is_preserved(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    settings = home / '.pi/agent/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme":"dark"}')
    def editing(cmd, **kwargs):
        if cmd[0] == 'npm':
            settings.write_text('{"theme":"light"}')
        return install_ok(cmd, **kwargs)
    with pytest.raises(RuntimeError, match='settings changed'):
        stage.stage_pi(home, sha, repository=str(repo), runner=editing)
    assert settings.read_text() == '{"theme":"light"}'
    assert stage.describe_pi(home, runner=install_ok) is None


def test_herdr_install_uses_managed_exact_source(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    developer = home / 'herdr-engineering-repo'
    developer.mkdir(parents=True)
    (developer / 'work').write_text('preserve me')
    calls = []
    def installer(cmd, **kwargs):
        calls.append(cmd)
        if '-c' in cmd:
            return json.dumps({'url': (home / '.local/share/herdr-engineering/herdr/releases' / sha).as_uri(), 'dir_info': {}})
        if '-m' in cmd and ('venv' in cmd or 'pip' in cmd):
            return ''
        return install_ok(cmd, **kwargs)
    result = stage.stage_herdr(home, sha, repository=str(repo), runner=installer)
    assert result['revision'] == sha
    assert result['sourceRoot'].endswith('/herdr/releases/' + sha)
    assert (developer / 'work').read_text() == 'preserve me'
    install = next(c for c in calls if 'pip' in c)
    assert install[-1] == result['sourceRoot']
    assert stage.describe(home, 'herdr', runner=installer) == result


def test_staging_lock_refuses_concurrent_install(repository, tmp_path):
    import fcntl
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    root = home / '.local/share/herdr-engineering/pi'
    root.mkdir(parents=True)
    with (root / 'stage.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert stage.describe_pi(home, runner=install_ok) is None


def test_herdr_attestation_refuses_replaced_installed_distribution(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    installed_origin = {}
    def installer(cmd, **kwargs):
        if '-m' in cmd and 'pip' in cmd:
            installed_origin.update({'url': Path(cmd[-1]).as_uri(), 'dir_info': {}})
            return ''
        if '-m' in cmd and 'venv' in cmd:
            return ''
        if '-c' in cmd:
            return json.dumps(installed_origin)
        return install_ok(cmd, **kwargs)
    result = stage.stage_herdr(home, sha, repository=str(repo), runner=installer)
    assert stage.describe(home, 'herdr', runner=installer) == result
    installed_origin.update({'url': 'https://github.com/berlinguyinca/herdr-engineering.git',
                             'vcs_info': {'vcs': 'git', 'commit_id': 'f' * 40}})
    assert stage.describe(home, 'herdr', runner=installer) is None


def test_macos_companion_receives_pinned_release_path():
    import yaml
    root = Path(__file__).resolve().parents[1]
    tasks = yaml.safe_load((root / 'ansible/tasks/fabric-agent.yml').read_text())[0]['block']
    command = next(task['ansible.builtin.command'] for task in tasks
                   if task.get('register') == 'fabric_agent_result')
    # This is a deployment configuration contract. Rendering belongs to Ansible;
    # the Python test suite must run with only the declared project dependencies.
    argv = command['argv']
    assert isinstance(argv, str)
    expression = ' '.join(argv.split())
    assert "herdr_engineering_checkout ~ '/scripts/'" in expression
    assert "('fabric-agent-macos.sh' if ansible_facts['system'] == 'Darwin' else 'fabric-agent.sh')" in expression
    assert expression.endswith(
        "+ (['--repo', herdr_engineering_checkout] if ansible_facts['system'] == 'Darwin' else []) }}")
    checkout = next(task['ansible.builtin.set_fact']['herdr_engineering_checkout']
                    for task in tasks
                    if 'herdr_engineering_checkout' in task.get('ansible.builtin.set_fact', {}))
    assert checkout == "{{ fleet_home }}/.local/share/herdr-engineering/herdr/releases/{{ herdr_engineering_revision }}"


def test_distribution_probe_reads_actual_target_interpreter_metadata(tmp_path):
    import sys
    stage = module()
    home = tmp_path / 'home'
    venv = home / '.venvs/herdr-engineering'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
    python = venv / 'bin/python'
    site = Path(subprocess.check_output([str(python), '-I', '-c',
        "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    distribution = site / 'herdr_engineering-0.1.0.dist-info'
    distribution.mkdir()
    (distribution / 'METADATA').write_text('Metadata-Version: 2.1\nName: herdr-engineering\nVersion: 0.1.0\n')
    release = home / '.local/share/herdr-engineering/herdr/releases' / ('a' * 40)
    metadata = distribution / 'direct_url.json'
    metadata.write_text(json.dumps({'url': release.as_uri(), 'dir_info': {}}))
    stage.verify_herdr_distribution(home, release, stage.run)
    metadata.write_text(json.dumps({'url': (tmp_path / 'different-source').as_uri(), 'dir_info': {}}))
    with pytest.raises(RuntimeError, match='distribution does not match'):
        stage.verify_herdr_distribution(home, release, stage.run)
    metadata.unlink()
    with pytest.raises(RuntimeError, match='distribution does not match'):
        stage.verify_herdr_distribution(home, release, stage.run)


def test_already_staged_release_is_reused_without_change(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    (home / '.pi/agent').mkdir(parents=True)
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd[0])
        return install_ok(cmd, **kwargs)

    first, changed = stage.stage_or_reuse(home, sha, 'pi', str(repo), runner)
    assert changed is True
    calls.clear()
    again, changed = stage.stage_or_reuse(home, sha, 'pi', str(repo), runner)
    assert changed is False and again == first
    assert 'npm' not in calls  # nothing reinstalled


# --- #881: receipts prove the artifact they describe --------------------------


def test_receipt_records_deterministic_artifact_digest(repository, tmp_path):
    stage = module()
    repo, sha = repository
    one = stage.stage_pi(tmp_path / 'home-a', sha, repository=str(repo), runner=install_ok)
    two = stage.stage_pi(tmp_path / 'home-b', sha, repository=str(repo), runner=install_ok)
    # The digest proves the staged content, not just the recorded commit.
    assert one['artifactDigest'].startswith('sha256:')
    # Two independent stagings of one commit must agree: the digest is the tracked
    # tree, free of install-specific noise (node_modules, build output, .git).
    assert one['artifactDigest'] == two['artifactDigest']
    release = Path(one['sourceRoot'])
    assert one['artifactDigest'] == stage.compute_digest(release, install_ok)
    # A receipt also carries an (initially empty) patch ledger.
    assert one['patches'] == []


def test_artifact_digest_covers_only_tracked_content(repository, tmp_path):
    # The artifact is the git-tracked tree: install/build output that git does not
    # track (node_modules, a setuptools build/ tree, __pycache__) is not the
    # artifact and must not move the digest, or every re-stage would false-drift.
    stage = module()
    repo, sha = repository
    result = stage.stage_pi(tmp_path / 'home', sha, repository=str(repo), runner=install_ok)
    release = Path(result['sourceRoot'])
    baseline = stage.compute_digest(release, install_ok)
    (release / 'node_modules' / 'injected.js').write_text('side effect')
    (release / 'build').mkdir(exist_ok=True)
    (release / 'build' / 'lib.py').write_text('compiled output')
    (release / '__pycache__').mkdir(exist_ok=True)
    (release / '__pycache__' / 'm.pyc').write_text('bytecode')
    assert stage.compute_digest(release, install_ok) == baseline


def test_artifact_digest_changes_when_a_tracked_file_is_edited(repository, tmp_path):
    stage = module()
    repo, sha = repository
    result = stage.stage_pi(tmp_path / 'home', sha, repository=str(repo), runner=install_ok)
    release = Path(result['sourceRoot'])
    baseline = stage.compute_digest(release, install_ok)
    (release / 'package.json').write_text('{"name":"pi-engineering-runtime","edited":true}')
    assert stage.compute_digest(release, install_ok) != baseline


def test_verify_artifact_reports_drift_when_digest_disagrees_and_no_patches(repository, tmp_path):
    # The exact 599c06d0 incident shape: a tracked file changed, patch ledger empty.
    stage = module()
    repo, sha = repository
    result = stage.stage_pi(tmp_path / 'home', sha, repository=str(repo), runner=install_ok)
    release = Path(result['sourceRoot'])
    receipt = json.loads((release.parents[1] / 'receipts' / (sha + '.json')).read_text())
    assert stage.verify_artifact(release, receipt, install_ok) == 'verified'
    (release / 'package.json').write_text('{"name":"pi-engineering-runtime","hotpatch":true}')
    assert stage.verify_artifact(release, receipt, install_ok) == 'drift'


def test_verify_artifact_reports_unknown_for_pre_digest_receipt(repository, tmp_path):
    # Receipts written before #881 have no artifactDigest: unknown, never verified.
    stage = module()
    repo, sha = repository
    result = stage.stage_pi(tmp_path / 'home', sha, repository=str(repo), runner=install_ok)
    release = Path(result['sourceRoot'])
    assert stage.verify_artifact(release, {'revision': sha, 'sourceRoot': str(release)}, install_ok) == 'unknown'


def test_verify_artifact_rejects_a_broken_patch_chain(repository, tmp_path):
    # A ledger only explains drift if it chains from the recorded digest; a patch
    # whose beforeDigest does not continue the chain is not an explanation.
    stage = module()
    repo, sha = repository
    result = stage.stage_pi(tmp_path / 'home', sha, repository=str(repo), runner=install_ok)
    release = Path(result['sourceRoot'])
    (release / 'package.json').write_text('{"name":"pi-engineering-runtime","edited":true}')
    current = stage.compute_digest(release, install_ok)
    forged = {'revision': sha, 'sourceRoot': str(release), 'artifactDigest': result['artifactDigest'],
              'patches': [{'path': 'package.json', 'beforeDigest': 'sha256:unrelated',
                           'afterDigest': current, 'appliedAt': 'x', 'actor': 'x', 'reason': 'x'}]}
    assert stage.verify_artifact(release, forged, install_ok) == 'drift'


def test_record_patch_appends_ledger_entry_and_keeps_verification_honest(repository, tmp_path):
    stage = module()
    repo, sha = repository
    result = stage.stage_pi(tmp_path / 'home', sha, repository=str(repo), runner=install_ok)
    release = Path(result['sourceRoot'])
    root = release.parents[1]
    target = release / 'package.json'
    before = stage.compute_digest(release, install_ok)
    target.write_text('{"name":"pi-engineering-runtime","hotpatch":683}')
    after = stage.compute_digest(release, install_ok)
    entry = stage.record_patch(root, sha, path='package.json', before=before, after=after,
                               actor='operator', reason='pi#683 hotpatch')
    assert entry['path'] == 'package.json'
    assert entry['beforeDigest'] == before and entry['afterDigest'] == after
    assert entry['actor'] == 'operator' and entry['reason'] == 'pi#683 hotpatch'
    assert 'appliedAt' in entry
    receipt = json.loads((root / 'receipts' / (sha + '.json')).read_text())
    assert receipt['patches'] == [entry]
    # A recorded patch chain that explains the current content verifies.
    assert stage.verify_artifact(release, receipt, install_ok) == 'verified'
    # A further undocumented edit drifts again.
    target.write_text('{"name":"pi-engineering-runtime","hotpatch":683,"sneaky":true}')
    assert stage.verify_artifact(release, receipt, install_ok) == 'drift'


def test_restaging_is_stable_when_untracked_build_output_appears(tmp_path):
    # The herdr false-drift guard: a pip/setuptools in-tree build writes build/ and
    # *.egg-info/ into the release on every install. The real repo gitignores them,
    # so they are neither `git status` dirty nor in the tracked digest -- a converge
    # must report no change, not drift.
    stage = module()
    repo = tmp_path / 'upstream-build'
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True)
    for key, value in [('user.name', 'Test'), ('user.email', 'test@example.test')]:
        subprocess.run(['git', '-C', str(repo), 'config', key, value], check=True)
    (repo / 'package.json').write_text(json.dumps({'name': 'pi-engineering-runtime'}))
    (repo / 'package-lock.json').write_text('{}')
    (repo / '.gitignore').write_text('node_modules/\nbuild/\n*.egg-info/\n__pycache__/\n')
    subprocess.run(['git', '-C', str(repo), 'add', 'package.json', 'package-lock.json', '.gitignore'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'initial'], check=True, capture_output=True)
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    home = tmp_path / 'home'
    (home / '.pi/agent').mkdir(parents=True)
    first, changed = stage.stage_or_reuse(home, sha, 'pi', str(repo), install_ok)
    assert changed is True
    release = Path(first['sourceRoot'])
    (release / 'build').mkdir(exist_ok=True)
    (release / 'build' / 'out.py').write_text('setuptools build output')
    (release / 'pkg.egg-info').mkdir(exist_ok=True)
    (release / 'pkg.egg-info' / 'PKG-INFO').write_text('Metadata')
    # git status must stay clean (the outputs are gitignored), and the digest unchanged.
    assert subprocess.check_output(['git', '-C', str(release), 'status', '--porcelain'], text=True) == ''
    again, changed = stage.stage_or_reuse(home, sha, 'pi', str(repo), install_ok)
    assert changed is False and again == first
    assert stage.describe_pi(home, runner=install_ok) == first


def test_stage_release_and_host_apply_digest_contracts_agree(repository, tmp_path):
    # Cross-unit parity: a receipt written by stage-release.py must be accepted by
    # host_apply's independent copy of the digest contract, and both must see the
    # same drift. If the two deployment units diverge, this fails.
    from herdr_engineering.fleet_watch import host_apply
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    result = stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    release = Path(result['sourceRoot'])
    receipt = json.loads((release.parents[1] / 'receipts' / (sha + '.json')).read_text())
    assert receipt['artifactDigest'] == host_apply._artifact_digest(release)
    assert host_apply.receipt_status(release, receipt) == 'verified'
    (release / 'package.json').write_text('{"name":"pi-engineering-runtime","edited":true}')
    assert host_apply.receipt_status(release, receipt) == 'drift'


def _commit(repo, message):
    subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', message], check=True, capture_output=True)
    return subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()


def test_digest_handles_tracked_symlink_to_directory(repository, tmp_path):
    # skills/handoff is a tracked symlink to a directory; read_bytes() on it raised
    # IsADirectoryError and aborted staging of every release that contained one.
    stage = module()
    repo, _ = repository
    (repo / 'shared').mkdir()
    (repo / 'shared' / 'SKILL.md').write_text('one')
    (repo / 'skills').mkdir()
    (repo / 'skills' / 'handoff').symlink_to('../shared')
    sha = _commit(repo, 'dir symlink')
    result = stage.stage_pi(tmp_path / 'home', sha, repository=str(repo), runner=install_ok)
    release = Path(result['sourceRoot'])
    assert (release / 'skills' / 'handoff').is_dir()
    assert result['artifactDigest'] == stage.compute_digest(release, install_ok)


def test_digest_covers_symlink_target_and_survives_dangling_links(repository, tmp_path):
    stage = module()
    repo, _ = repository
    (repo / 'a').mkdir()
    (repo / 'b').mkdir()
    (repo / 'link').symlink_to('a')
    (repo / 'dangling').symlink_to('missing-target')
    sha = _commit(repo, 'links')
    result = stage.stage_pi(tmp_path / 'home', sha, repository=str(repo), runner=install_ok)
    release = Path(result['sourceRoot'])
    baseline = stage.compute_digest(release, install_ok)
    (release / 'link').unlink()
    (release / 'link').symlink_to('b')
    # Retargeting a tracked link is an in-place edit of the artifact: it must drift.
    assert stage.compute_digest(release, install_ok) != baseline


def test_dirty_release_is_a_typed_error_naming_the_files(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    result = stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    (Path(result['sourceRoot']) / 'package.json').write_text('local work')
    with pytest.raises(stage.DirtyRelease) as raised:
        stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert raised.value.files == ['package.json']
    assert isinstance(raised.value, RuntimeError)


def test_dirty_release_exits_with_a_marker_the_controller_can_parse(
        repository, tmp_path, monkeypatch, capsys):
    from herdr_engineering.fleet import parse_stage_error

    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    result = stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    (Path(result['sourceRoot']) / 'package.json').write_text('local work')
    monkeypatch.setitem(stage.REPOSITORIES, 'pi', str(repo))
    monkeypatch.setattr(stage, 'run', install_ok)
    monkeypatch.setattr('sys.argv', ['stage-release.py', '--kind', 'pi', '--home', str(home),
                                     '--revision', sha])
    assert stage.main() == 3
    err = capsys.readouterr().err
    # Ansible JSON-escapes quotes in captured stderr; the marker must survive that.
    escaped = json.dumps({'stderr': err})
    parsed = parse_stage_error(escaped, '')
    assert parsed['code'] == 'managed_release_dirty'
    assert parsed['files'] == ['package.json']
    assert 'local work preserved' in parsed['error']
    assert parse_stage_error('PLAY RECAP unreachable=0', '') is None
