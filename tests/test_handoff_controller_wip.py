"""WIP commit helper: real git repos with a real bare remote."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from herdr_engineering.handoff_controller.wip import WipReason, WipResult, _push, wip_commit


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path: Path, branch: str = "feat/x") -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "config", "user.name", "T")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "commit.gpgsign", "false")
    (work / "a.txt").write_text("one\n")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "main")
    if branch != "main":
        git(work, "checkout", "-q", "-b", branch)
    return work


def remote_heads(tmp_path: Path) -> str:
    return git(tmp_path / "remote.git", "branch", "--list")


def test_clean_tree_no_commit(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    head = git(work, "rev-parse", "HEAD")
    result = wip_commit(work, push_allowed=True)
    assert result == WipResult(committed=False, sha=None, pushed=False, unpushed=(), untracked=())
    assert git(work, "rev-parse", "HEAD") == head


def test_tracked_change_committed_and_pushed(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "a.txt").write_text("two\n")
    cwd = os.getcwd()
    result = wip_commit(work, push_allowed=True)
    assert os.getcwd() == cwd
    assert result.committed and result.pushed and result.unpushed == ()
    assert result.sha == git(work, "rev-parse", "--short", "HEAD")
    assert git(work, "log", "-1", "--format=%s") == "wip: auto-handoff checkpoint"
    assert "feat/x" in remote_heads(tmp_path)
    assert git(tmp_path / "remote.git", "rev-parse", "--short", "feat/x") == result.sha


def test_push_not_allowed_records_unpushed_sha(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=False)
    assert result.committed and not result.pushed
    assert result.unpushed == (result.sha,)
    assert result.reason is WipReason.PUSH_NOT_ALLOWED
    assert "feat/x" not in remote_heads(tmp_path)


def test_never_commits_on_main(tmp_path: Path) -> None:
    work = make_repo(tmp_path, branch="main")
    head = git(work, "rev-parse", "HEAD")
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert not result.committed and not result.pushed and result.sha is None
    assert git(work, "rev-parse", "HEAD") == head
    assert git(work, "status", "--porcelain") == "M a.txt"
    assert result.reason is WipReason.ON_MAIN


def test_detached_head_is_treated_like_main(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "-q", "--detach")
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert not result.committed and result.reason is WipReason.DETACHED


def test_untracked_files_not_committed(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "a.txt").write_text("two\n")
    (work / "new.txt").write_text("n\n")
    result = wip_commit(work, push_allowed=True)
    assert result.committed and result.untracked == ("new.txt",)
    assert "new.txt" not in git(work, "show", "--name-only", "--format=", "HEAD")


def test_untracked_only_makes_no_commit(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "new.txt").write_text("n\n")
    result = wip_commit(work, push_allowed=True)
    assert not result.committed and result.untracked == ("new.txt",)


def test_failed_push_is_reported_not_raised(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert result.committed and not result.pushed and result.unpushed == (result.sha,)
    assert result.reason is WipReason.PUSH_FAILED and result.error


def test_pushes_to_differently_named_upstream(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "push", "-q", "origin", "feat/x:remote-name")
    git(work, "branch", "--set-upstream-to=origin/remote-name")
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert result.pushed
    assert git(tmp_path / "remote.git", "rev-parse", "--short", "remote-name") == result.sha


def test_rejecting_hook_reports_not_committed(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert not result.committed and not result.pushed
    assert result.reason is WipReason.COMMIT_REJECTED and result.error


def test_clean_and_pushed_have_no_reason(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    assert wip_commit(work, push_allowed=True).reason is None
    (work / "a.txt").write_text("two\n")
    assert wip_commit(work, push_allowed=True).reason is None


def test_slow_hook_times_out_without_raising(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nsleep 5\n")
    hook.chmod(0o755)
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True, timeout_s=0.5)
    assert not result.committed and result.reason is WipReason.GIT_ERROR
    assert result.error and "commit" in result.error and "index.lock" in result.error


CALL = "import sys; from pathlib import Path; from herdr_engineering.handoff_controller.wip import wip_commit; print(wip_commit(Path(sys.argv[1]), push_allowed=True))"


def run_wip(work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", CALL, str(work)], cwd=Path(__file__).resolve().parents[1],
                          env={"PYTHONPATH": str(Path(__file__).resolve().parents[1]), **env}, capture_output=True,
                          text=True, timeout=60)


def test_missing_git_does_not_raise(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    done = run_wip(work, {"PATH": str(tmp_path / "empty")})
    assert done.returncode == 0, done.stderr
    assert "GIT_ERROR" in done.stdout or "git_error" in done.stdout


def test_git_failure_is_an_error_not_a_clean_tree(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    result = wip_commit(not_a_repo, push_allowed=True)
    assert result.reason is WipReason.GIT_ERROR and result.error and not result.committed and not result.pushed


def test_ambient_git_dir_is_ignored(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "a.txt").write_text("two\n")
    done = run_wip(work, {"PATH": os.environ["PATH"], "GIT_DIR": str(tmp_path / "bogus"), "GIT_WORK_TREE": str(tmp_path / "bogus")})
    assert done.returncode == 0 and "committed=True" in done.stdout, done.stdout + done.stderr


def test_non_ascii_untracked_name_is_not_quoted(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "h\u00e9llo \u4e16.txt").write_text("n\n")
    assert wip_commit(work, push_allowed=True).untracked == ("h\u00e9llo \u4e16.txt",)


def test_upstream_main_is_never_pushed(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "branch", "--set-upstream-to=origin/main")
    (work / "a.txt").write_text("two\n")
    before = git(tmp_path / "remote.git", "rev-parse", "main")
    result = wip_commit(work, push_allowed=True)
    assert result.committed and not result.pushed
    assert git(tmp_path / "remote.git", "rev-parse", "main") == before


def test_rejected_non_fast_forward_push_is_never_forced(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "push", "-q", "origin", "feat/x")
    git(work, "branch", "--set-upstream-to=origin/feat/x")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "feat/x", str(tmp_path / "remote.git"), str(other)], check=True)
    git(other, "config", "user.name", "T")
    git(other, "config", "user.email", "t@example.com")
    (other / "b.txt").write_text("b\n")
    git(other, "add", "b.txt")
    git(other, "commit", "-q", "-m", "remote-only")
    git(other, "push", "-q", "origin", "feat/x")
    remote_tip = git(tmp_path / "remote.git", "rev-parse", "feat/x")
    git(work, "reset", "-q", "--soft", "HEAD")  # local branch is behind: a different commit on top of the old tip
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert result.committed and not result.pushed and result.reason is WipReason.PUSH_FAILED
    assert git(tmp_path / "remote.git", "rev-parse", "feat/x") == remote_tip
    assert result.unpushed == (result.sha,)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    pidfile = tmp_path / "child.pid"
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nwait\n")
    hook.chmod(0o755)
    (work / "a.txt").write_text("two\n")
    wip_commit(work, push_allowed=False, timeout_s=0.5)
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(pid)


def test_commit_timeout_reports_head_truthfully(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    hook = work / ".git" / "hooks" / "post-commit"  # runs after HEAD moved
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nsleep 5\n")
    hook.chmod(0o755)
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=False, timeout_s=1.0)
    assert result.reason is WipReason.GIT_ERROR
    assert result.committed and result.sha == git(work, "rev-parse", "--short", "HEAD")


def test_stale_index_lock_is_named_and_never_deleted(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / ".git" / "index.lock").write_text("")
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=False)
    assert result.reason is WipReason.COMMIT_REJECTED and not result.committed
    assert (work / ".git" / "index.lock").exists()


def test_push_timeout_is_unknown_state(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    hook = tmp_path / "remote.git" / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nsleep 5\n")
    hook.chmod(0o755)
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True, timeout_s=1.0)
    assert result.committed and not result.pushed
    assert result.reason is WipReason.GIT_ERROR and result.error and "push" in result.error


def test_failed_config_lookup_never_pushes(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    with (work / ".git" / "config").open("a") as f:
        f.write("[branch \"feat/x\"\n  remote = \n")  # a corrupt section: every config read fails
    reason, error = _push(work, "feat/x", 10.0)
    assert reason is WipReason.GIT_ERROR and error
    assert "feat/x" not in remote_heads(tmp_path)
