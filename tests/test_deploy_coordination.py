"""Real processes and kernel locks; Docker/health stay in disposable fixtures."""
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import time
from contextlib import contextmanager

import pytest

from tests.test_deploy_scripts import (
    DEPLOY,
    ROOT,
    _layout_commit,
    _seed_previous,
    candidate_ref,
    deploy,
    env,
)

# Re-export the existing disposable deployment fixtures for pytest.
__all__ = ["candidate_ref", "env"]

# Complete candidate CLI imports and Git worktree cleanup can exceed 30s on the
# shared integration host. Keep the real process checks bounded without changing
# the separate 10s contention checks or any state/ownership assertions.
DEPLOY_TIMEOUT = 90


def state(e):
    return e["home"] / ".local/state/herdr-engineering/deploys"


def command(e, script=DEPLOY):
    return ["bash", str(script), "--no-fetch", "--ref", e["ref"],
            "--fleet-config", str(e["central"])]


def wait_for(path, proc):
    deadline = time.monotonic() + 60
    while not path.exists():
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(f"deploy exited {proc.returncode}: {out}\n{err}")
        assert time.monotonic() < deadline, f"never reached {path}"
        time.sleep(0.02)


def barrier(e, executable, condition, label=""):
    """Pause the real deploy at an external command, without production hooks."""
    path = e["tmp"] / "bin" / executable
    original = path.with_suffix(".original")
    path.rename(original)
    entered, release = e["tmp"] / f"{label}entered", e["tmp"] / f"{label}release"
    path.write_text(f'''#!/usr/bin/env bash
if [ "${{COORDINATED_FIRST:-}}" = 1 ] && {condition}; then
  touch '{entered}'
  while [ ! -e '{release}' ]; do sleep 0.02; done
fi
exec '{original}' "$@"
''')
    path.chmod(0o755)
    return entered, release


@contextmanager
def first(e, prefix=()):
    proc = subprocess.Popen([*prefix, *command(e)], cwd=ROOT,
                            env=dict(e["env"], COORDINATED_FIRST="1"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        yield proc
    finally:
        (e["tmp"] / "release").touch()
        if proc.poll() is None:
            try:
                proc.communicate(timeout=DEPLOY_TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()


def second_entrypoint(e):
    # A genuinely separate checkout entry point, sharing only HOME and git storage.
    target = e["tmp"] / "other-checkout" / "deploy" / "control-plane"
    target.mkdir(parents=True)
    shutil.copy(DEPLOY, target / "deploy.sh")
    for name in ("deploy-state.py", "deploy-lock.sh"):
        source = DEPLOY.parent / name
        if source.exists():
            shutil.copy(source, target / name)
    gitdir = subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"],
                                    cwd=ROOT, text=True).strip()
    (target.parents[1] / ".git").write_text(f"gitdir: {gitdir}\n")
    return target / "deploy.sh"


@pytest.mark.parametrize("phase", ["build", "verify", "rollback"])
def test_two_entrypoints_contend_before_any_docker_or_host_mutation(env, phase):
    if phase == "build":
        entered, release = barrier(env, "docker", '[ "$1" = build ]')
    elif phase == "verify":
        entered, release = barrier(env, "curl", "true")
    else:
        _seed_previous(env, _layout_commit())
        env["env"]["STUB_UNHEALTHY_TAGS"] = env["ref"][:12]
        entered, release = barrier(env, "docker", '[[ "$*" == *rollback-images.json* ]]')
    other = second_entrypoint(env)
    with first(env) as proc:
        wait_for(entered, proc)
        before = env["log"].read_bytes()
        result = subprocess.run(command(env, other), env=env["env"], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 75, result.stdout + result.stderr
        assert env["log"].read_bytes() == before
        metadata = json.loads((state(env) / "rollout-owner.json").read_text())
        assert metadata["phase"] == phase
        assert metadata["owner"] in result.stderr and phase in result.stderr
        assert stat.S_IMODE((state(env) / "rollout-owner.json").stat().st_mode) == 0o600
        assert all(s not in result.stdout + result.stderr + json.dumps(metadata)
                   for s in env["secrets"])
        with (state(env) / "rollout.lock").open("a") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release.touch()
        out, err = proc.communicate(timeout=DEPLOY_TIMEOUT)
        assert proc.returncode == (1 if phase == "rollback" else 0), out + err
    assert not (state(env) / "rollout-owner.json").exists()
    assert (state(env) / "rollout.lock").exists()


@pytest.mark.parametrize("lock_name", ["rollout.lock", "deploy.lock"])
def test_outer_flock_wrapper_is_adopted(env, lock_name):
    state(env).mkdir(parents=True)
    lock = state(env) / lock_name
    lock.touch()
    inode = lock.stat().st_ino
    entered, release = barrier(env, "docker", '[ "$1" = build ]')
    with first(env, ["flock", "-n", str(lock)]) as proc:
        wait_for(entered, proc)
        result = deploy(env)
        assert result.returncode == 75, result.stderr
        release.touch()
        out, err = proc.communicate(timeout=DEPLOY_TIMEOUT)
        assert proc.returncode == 0, out + err
    assert lock.stat().st_ino == inode


def test_dry_run_concurrent_does_not_touch_owner_or_host(env):
    entered, release = barrier(env, "docker", '[ "$1" = build ]')
    with first(env) as proc:
        wait_for(entered, proc)
        owner = state(env) / "rollout-owner.json"
        before = owner.read_bytes()
        result = deploy(env, "--dry-run")
        assert result.returncode == 0, result.stderr
        assert owner.read_bytes() == before
        assert not (env["home"] / ".config").exists()
        release.touch()


def test_stale_owner_is_not_a_lock_and_failure_releases_inode(env):
    state(env).mkdir(parents=True)
    owner = state(env) / "rollout-owner.json"
    owner.write_text(json.dumps({"owner": "stale", "phase": "build"}))
    lock = state(env) / "rollout.lock"
    lock.touch()
    inode = lock.stat().st_ino
    env["env"]["FAIL_BUILD"] = "1"
    result = deploy(env)
    assert result.returncode == 1, result.stderr
    assert not owner.exists()
    assert lock.stat().st_ino == inode
    env["env"].pop("FAIL_BUILD")
    assert deploy(env).returncode == 0


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("process_group", [False, True])
def test_interrupt_rolls_back_under_lock_before_release(env, signum, process_group):
    _, previous = _seed_previous(env, _layout_commit())
    entered, release = barrier(env, "curl", "true")
    with first(env) as proc:
        wait_for(entered, proc)
        if process_group:
            os.killpg(proc.pid, signum)
        else:
            proc.send_signal(signum)
        assert deploy(env).returncode == 75
        release.touch()
        out, err = proc.communicate(timeout=DEPLOY_TIMEOUT)
        assert proc.returncode == 128 + signum, out + err
        assert "and healthy" in err
    metadata = json.loads(next(state(env).glob("attempt-*/metadata.json")).read_text())
    assert metadata["outcome"] == "rolled_back"
    assert (env["tmp"] / "running-tag").read_text().strip() == previous
    assert not (state(env) / "rollout-owner.json").exists()
    with (state(env) / "rollout.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("inherited", [False, True])
def test_metadata_and_unrelated_inherited_fd_cannot_bypass_busy_lock(env, inherited):
    state(env).mkdir(parents=True)
    owner = state(env) / "rollout-owner.json"
    owner.write_text(json.dumps({"owner": "SECRET\nforged", "phase": "SECRET\x1b[2J",
                                "pid": "SECRET"}))
    env["env"].update(DEPLOY_LOCK_OWNER="forged", DEPLOY_LOCK_FD="3",
                      HERDR_DEPLOY_LOCK_HELD="1")
    with (state(env) / "rollout.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (env["tmp"] / "unrelated.lock").open("a") as wrong:
            fcntl.flock(wrong, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = owner.read_bytes()
            result = subprocess.run(command(env), cwd=ROOT, env=env["env"],
                                    pass_fds=(wrong.fileno(),) if inherited else (),
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 75
            assert "SECRET" not in result.stderr and "unknown" in result.stderr
            assert owner.read_bytes() == before
            assert not env["log"].exists()


def test_unlocked_matching_fd_is_acquired_not_blindly_trusted(env):
    state(env).mkdir(parents=True)
    lock = state(env) / "rollout.lock"
    entered, release = barrier(env, "docker", '[ "$1" = build ]')
    with lock.open("a") as handle:
        proc = subprocess.Popen(command(env), cwd=ROOT,
                                env=dict(env["env"], COORDINATED_FIRST="1"),
                                pass_fds=(handle.fileno(),), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(entered, proc)
            assert deploy(env).returncode == 75
            release.touch()
            out, err = proc.communicate(timeout=DEPLOY_TIMEOUT)
            assert proc.returncode == 0, out + err
            # Closing our inherited copy must not unlock the outer owner's FD.
            assert deploy(env).returncode == 75
        finally:
            release.touch()
            proc.communicate(timeout=DEPLOY_TIMEOUT)
    assert deploy(env).returncode == 0


def test_wrong_owner_cannot_clear_or_change_metadata(env):
    helper = DEPLOY.parent / "deploy-state.py"
    state(env).mkdir(parents=True)
    owner = state(env) / "rollout-owner.json"
    for action, extra in (("lock-start", ["123"]), ("lock-phase", ["build"])):
        subprocess.run(["python3", str(helper), action, str(state(env)), "a" * 32, *extra],
                       check=True)
    before = owner.read_bytes()
    for action, extra in (("lock-phase", ["cleanup"]), ("lock-clear", [])):
        subprocess.run(["python3", str(helper), action, str(state(env)), "b" * 32, *extra],
                       check=True)
        assert owner.read_bytes() == before


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interrupt_before_host_mutation_does_not_roll_back(env, signum):
    entered, release = barrier(env, "docker", '[ "$1" = build ]')
    with first(env) as proc:
        wait_for(entered, proc)
        proc.send_signal(signum)
        release.touch()
        out, err = proc.communicate(timeout=DEPLOY_TIMEOUT)
        assert proc.returncode == 128 + signum, out + err
        assert "rolling back" not in err
        assert not list(state(env).glob("attempt-*"))
        assert not (env["home"] / ".config").exists()
    assert not (state(env) / "rollout-owner.json").exists()


def test_interrupt_recovery_keeps_lock_and_ignores_repeated_termination(env):
    _seed_previous(env, _layout_commit())
    verifying, continue_verify = barrier(env, "curl", "true", "verify-")
    rolling_back, continue_rollback = barrier(
        env, "docker", '[[ "$*" == *rollback-images.json* ]]', "rollback-")
    with first(env) as proc:
        try:
            wait_for(verifying, proc)
            proc.send_signal(signal.SIGTERM)
            continue_verify.touch()
            wait_for(rolling_back, proc)
            proc.send_signal(signal.SIGTERM)
            before = env["log"].read_bytes()
            result = deploy(env)
            assert result.returncode == 75 and "phase=rollback" in result.stderr
            assert env["log"].read_bytes() == before
            continue_rollback.touch()
            out, err = proc.communicate(timeout=DEPLOY_TIMEOUT)
            assert proc.returncode == 143 and "and healthy" in err, out + err
        finally:
            continue_verify.touch()
            continue_rollback.touch()
    assert not (state(env) / "rollout-owner.json").exists()


def test_standard_input_fd_is_not_adopted(env):
    state(env).mkdir(parents=True)
    with (state(env) / "rollout.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(command(env), cwd=ROOT, env=env["env"], stdin=handle,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 75
        assert not env["log"].exists()


def test_canonical_symlink_resolves_helpers_and_repository(env):
    launcher = env["home"] / ".local/bin/herdr-deploy"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(DEPLOY)
    result = subprocess.run([str(launcher), *command(env)[2:]], cwd=env["home"],
                            env=env["env"], capture_output=True, text=True, timeout=DEPLOY_TIMEOUT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (state(env) / f'{env["ref"][:12]}.json').exists()


def test_dry_run_never_fetches_or_creates_git_worktree_or_state(env):
    real_git = shutil.which("git")
    wrapper = env["tmp"] / "bin/git"
    forbidden = env["tmp"] / "git-mutation"
    wrapper.write_text(f'''#!/usr/bin/env bash
case " $* " in
  *" fetch "*|*" worktree "*) touch '{forbidden}'; exit 97 ;;
esac
exec '{real_git}' "$@"
''')
    wrapper.chmod(0o755)
    args = command(env)
    args.remove("--no-fetch")
    result = subprocess.run([*args, "--dry-run"], cwd=ROOT, env=env["env"],
                            capture_output=True, text=True, timeout=DEPLOY_TIMEOUT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not forbidden.exists()
    assert not (env["home"] / ".local").exists()
    assert not (env["home"] / ".config").exists()


@pytest.fixture
def tool_routes(env):
    """Actual new tooling committed only in a disposable clone, older caller HEAD."""
    from tests.test_deploy_install import INSTALLER, git

    repo = env["tmp"] / "tool-source"
    subprocess.run(["git", "clone", "-q", "--shared", str(ROOT), str(repo)], check=True)
    for name in ("deploy.sh", "deploy-lock.sh", "deploy-state.py"):
        shutil.copy(DEPLOY.parent / name, repo / "deploy/control-plane" / name)
    git(repo, "add", "deploy/control-plane")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-qm", "disposable deployment tool")
    tool_commit = git(repo, "rev-parse", "HEAD")
    caller = env["tmp"] / "caller"
    git(repo, "worktree", "add", "--detach", str(caller), env["ref"])
    legacy = caller / "deploy/control-plane/deploy.sh"
    subprocess.run(["python3", str(INSTALLER), "--repo", str(repo), "--ref", tool_commit,
                    "--home", str(env["home"]), "--legacy", str(legacy)], check=True,
                   capture_output=True)
    return repo, caller, env["home"] / ".local/bin/herdr-deploy", legacy, tool_commit


@pytest.mark.parametrize("route", ["canonical", "legacy"])
@pytest.mark.parametrize("ref", ["HEAD", "HEAD^{commit}", "HEAD~0", "HEAD@{0}", "@", "@^0", "@{0}"])
def test_installed_routes_resolve_head_in_proven_caller_worktree(env, tool_routes, route, ref):
    _, caller, launcher, legacy, tool_commit = tool_routes
    entry = launcher if route == "canonical" else legacy
    result = subprocess.run([str(entry), "--no-fetch", "--ref", ref,
                             "--fleet-config", str(env["central"])],
                            cwd=caller / "deploy/control-plane", env=env["env"], capture_output=True, text=True, timeout=DEPLOY_TIMEOUT)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = state(env) / f'{env["ref"][:12]}.json'
    assert receipt.exists(), "caller HEAD was silently replaced by canonical tool HEAD"
    assert json.loads(receipt.read_text())["commit"] == env["ref"]
    assert not (state(env) / f"{tool_commit[:12]}.json").exists()


@pytest.mark.parametrize("outside_repo", [False, True])
@pytest.mark.parametrize("ref", ["HEAD", "HEAD~0", "HEAD@{0}", "@", "@^0", "@{0}"])
def test_installed_head_refuses_unproven_caller_but_full_sha_is_unambiguous(env, tool_routes, outside_repo, ref):
    _, _, launcher, _, _ = tool_routes
    # ROOT has identical commit objects but a DIFFERENT common repository.
    cwd = env["home"] if outside_repo else ROOT
    result = subprocess.run([str(launcher), "--no-fetch", "--ref", ref,
                             "--fleet-config", str(env["central"])],
                            cwd=cwd, env=env["env"], capture_output=True, text=True, timeout=DEPLOY_TIMEOUT)
    assert result.returncode != 0
    assert "full commit SHA" in result.stderr and "published ref" in result.stderr
    assert not env["log"].exists()
    result = subprocess.run([str(launcher), *command(env)[2:]], cwd=cwd, env=env["env"],
                            capture_output=True, text=True, timeout=DEPLOY_TIMEOUT)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("relation", ["ancestor", "same", "stale", "divergent"])
@pytest.mark.parametrize("override", [False, True])
def test_forward_requires_verified_deployed_commit_ancestry(env, tool_routes, relation, override):
    from tests.test_deploy_install import git

    repo, _, launcher, _, tool_commit = tool_routes
    if relation == "ancestor":
        previous = git(repo, "rev-parse", env["ref"] + "^")
    elif relation == "same":
        previous = env["ref"]
    elif relation == "stale":
        previous = tool_commit  # candidate HEAD is an ancestor of the deployed tool commit
    else:
        # Synthetic sibling exists only in the disposable repository; no shared refs change.
        previous = subprocess.check_output(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c",
             "user.email=test@example.invalid", "commit-tree", env["ref"] + "^{tree}",
             "-p", env["ref"] + "^", "-m", "disposable divergent previous"], text=True).strip()
    tag = previous[:12]
    state(env).mkdir(parents=True, exist_ok=True)
    (state(env) / f"{tag}.json").write_text(json.dumps(
        {"commit": previous, "image": f"herdr-engineering:{tag}"}))
    env["env"].update(STUB_RUNNING_IMAGE=f"herdr-engineering:{tag}",
                      STUB_PREVIOUS_TAG=tag, STUB_PREVIOUS_COMMIT=previous)
    if relation == "same":
        # Re-deploying the identical image means both current and target use ID b.
        env["env"]["STUB_PREVIOUS_TAG_ID"] = "sha256:" + "b" * 64
        (env["tmp"] / "running-images.json").write_text(json.dumps({
            name: "sha256:" + "b" * 64
            for name in ("herdr-cp-web", "herdr-cp-worker", "herdr-llm-proxy")
        }))
    # Deliberate divergence is explicitly authorized by this CLI flag only.
    args = [str(launcher), *command(env)[2:], *(["--allow-divergent"] if override else [])]
    result = subprocess.run(args, cwd=ROOT, env=env["env"], capture_output=True,
                            text=True, timeout=DEPLOY_TIMEOUT)
    if relation in ("ancestor", "same") or override:
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads((state(env) / f'{env["ref"][:12]}.json').read_text())
        assert receipt["previous_commit"] == previous
        assert receipt["commit"] == env["ref"]
    else:
        assert result.returncode != 0, "stale/divergent candidate reached a deployment"
        assert "--allow-divergent" in result.stderr and "does not descend" in result.stderr
        assert "docker build" not in env["log"].read_text()
        assert " up " not in env["log"].read_text()
        assert not (env["home"] / ".config").exists()
        assert not list(state(env).glob("attempt-*"))
    assert not (state(env) / "rollout-owner.json").exists()


def test_explicit_divergent_forward_failure_uses_protected_automatic_rollback(env, tool_routes):
    _, _, launcher, _, previous = tool_routes
    tag = previous[:12]
    state(env).mkdir(parents=True, exist_ok=True)
    (state(env) / f"{tag}.json").write_text(json.dumps(
        {"commit": previous, "image": f"herdr-engineering:{tag}"}))
    env["env"].update(STUB_RUNNING_IMAGE=f"herdr-engineering:{tag}",
                      STUB_PREVIOUS_TAG=tag, STUB_PREVIOUS_COMMIT=previous,
                      STUB_UNHEALTHY_TAGS=env["ref"][:12])
    result = subprocess.run([str(launcher), *command(env)[2:], "--allow-divergent"],
                            cwd=ROOT, env=env["env"], capture_output=True, text=True, timeout=DEPLOY_TIMEOUT)
    assert result.returncode == 1 and "and healthy" in result.stderr, result.stdout + result.stderr
    metadata = json.loads(next(state(env).glob("attempt-*/metadata.json")).read_text())
    assert metadata["outcome"] == "rolled_back"
    assert metadata["previous"]["commit"] == previous
    assert metadata["previous"]["images"]["control-plane-web"] == "sha256:" + "a" * 64
    assert (env["tmp"] / "running-tag").read_text().strip() == tag
    assert not (state(env) / f'{env["ref"][:12]}.json').exists()
    assert not (state(env) / "rollout-owner.json").exists()


def test_forward_refuses_unavailable_verified_history_before_build(env, tool_routes):
    _, _, launcher, _, _ = tool_routes
    previous = "f" * 40  # Verified image identity, but no ancestry object locally.
    tag = previous[:12]
    state(env).mkdir(parents=True, exist_ok=True)
    (state(env) / f"{tag}.json").write_text(json.dumps(
        {"commit": previous, "image": f"herdr-engineering:{tag}"}))
    env["env"].update(STUB_RUNNING_IMAGE=f"herdr-engineering:{tag}",
                      STUB_PREVIOUS_TAG=tag, STUB_PREVIOUS_COMMIT=previous)
    result = subprocess.run([str(launcher), *command(env)[2:]], cwd=ROOT,
                            env=env["env"], capture_output=True, text=True, timeout=DEPLOY_TIMEOUT)
    assert result.returncode != 0
    assert "does not descend" in result.stderr
    assert "--allow-divergent" in result.stderr
    assert "docker build" not in env["log"].read_text()
    assert " up " not in env["log"].read_text()
    assert not list(state(env).glob("attempt-*"))
    assert not (state(env) / "rollout-owner.json").exists()


@pytest.mark.parametrize("fail", [False, True])
def test_target_image_checkpoint_precedes_host_and_service_changes(env, fail):
    _seed_previous(env, _layout_commit())
    docker = env["tmp"] / "bin/docker"
    original = docker.with_suffix(".prewarm-original")
    docker.rename(original)
    docker.write_text(f'''#!/usr/bin/env bash
if [ "$1" = run ]; then
  for path in "$HOME/.config/herdr-engineering/github-app" "$HOME/.config/herdr-engineering/apps" "$HOME/.local/share/herdr-engineering/pipelines"; do
    [ ! -e "$path" ] || exit 98
  done
  echo 'prewarm-before-host-changes' >> '{env["log"]}'
  if [ "${{STUB_PREWARM_FAIL:-}}" = 1 ]; then
    echo 'PREWARM-SECRET-MUST-NOT-LEAK' >&2
    echo 'PREWARM-SECRET-MUST-NOT-LEAK'
    exit 1
  fi
  echo '{{"ok": true, "rows_scanned": 7}}'
fi
exec '{original}' "$@"
''')
    docker.chmod(0o755)
    if fail:
        env["env"]["STUB_PREWARM_FAIL"] = "1"
    entered, release = barrier(env, "docker", '[ "$1" = run ]', "prewarm-")
    with first(env) as proc:
        try:
            wait_for(entered, proc)
            owner = json.loads((state(env) / "rollout-owner.json").read_text())
            assert owner["phase"] == "checkpoint-prewarm"
            assert proc.poll() is None
            assert " up " not in env["log"].read_text()
        finally:
            release.touch()
        out, err = proc.communicate(timeout=DEPLOY_TIMEOUT)
        result = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    log = env["log"].read_text()
    assert "prewarm-before-host-changes" in log
    assert "PREWARM-SECRET" not in result.stdout + result.stderr
    metadata_path = next(state(env).glob("attempt-*/metadata.json"))
    metadata = json.loads(metadata_path.read_text())
    prewarm_log = metadata_path.parent / "checkpoint-prewarm.log"
    assert stat.S_IMODE(prewarm_log.stat().st_mode) == 0o600
    assert str(prewarm_log) in result.stdout + result.stderr
    if fail:
        assert "PREWARM-SECRET-MUST-NOT-LEAK" in prewarm_log.read_text()
        assert result.returncode == 1, result.stdout + result.stderr
        assert metadata["outcome"] == "prewarm_failed"
        assert " up " not in log
        assert not (env["home"] / ".config").exists()
        assert not (state(env) / f'{env["ref"][:12]}.json').exists()
    else:
        assert json.loads(prewarm_log.read_text())["rows_scanned"] == 7
        assert result.returncode == 0, result.stdout + result.stderr
        runs = [line for line in log.splitlines() if line.startswith("docker run ")]
        assert len(runs) == 1
        run = runs[0]
        assert "--network container:herdr-cp-web" in run
        assert "--entrypoint herdr-eng" in run
        assert "sha256:" + "b" * 64 in run
        assert "control-plane checkpoint --fleet-config /etc/herdr/fleet/fleet-config.yaml" in run
        assert "--migrations /app/control-plane/migrations" in run
        assert f"src={metadata_path.parent}/candidate,dst=/etc/herdr/fleet,readonly" in run
        assert log.index("docker run ") < log.index(" up ")
    assert not (state(env) / "rollout-owner.json").exists()


def test_initially_absent_web_skips_prewarm_network_and_is_recreated(env):
    _seed_previous(env, _layout_commit())
    missing = env["tmp"] / "missing"
    missing.write_text("herdr-cp-web")
    result = deploy(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "projection checkpoint prewarm skipped" in result.stdout
    assert "docker run " not in env["log"].read_text()
    assert missing.read_text().split() == []
    metadata = json.loads(next(state(env).glob("attempt-*/metadata.json")).read_text())
    assert metadata["outcome"] == "succeeded"
    assert not (state(env) / "rollout-owner.json").exists()
