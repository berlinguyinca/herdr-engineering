"""Worktree reaper and docker hygiene against real git repos (bare origin) and, when a daemon is
reachable, real docker. PR state comes from local git ancestry (``pr_source='git'``): the same
production code path an offline host uses, with no fake ``gh``."""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from herdr_engineering.disk_hygiene import Policy, find_repos, load_policy, reap_worktrees, run
from herdr_engineering.docker_hygiene import (
    RUN_ARGS,
    docker_available,
    parse_docker_time,
    remove_ephemeral_container,
    run_args,
    sweep,
)

from .test_cleanup_command import Env, git

DAY = 86400


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def policy(**kw: object) -> Policy:
    base = {"pr_source": "git", "min_idle_hours": 0, "ttl_days": 7}
    return Policy(**{**base, **kw})  # type: ignore[arg-type]


def reap(env: Env, *, dry_run: bool = False, ahead_days: float = 0, **kw: object) -> dict:
    out = reap_worktrees(policy(**kw), dry_run=dry_run, extra_repos=[env.repo],
                         extra_roots=[str(env.roots[0])], now=time.time() + ahead_days * DAY)
    return {Path(w["path"]).name: w for r in out["repos"] for w in r["worktrees"]}


def merge_into_main(env: Env, wt: Path) -> None:
    """Add a commit to the branch, merge it into origin/main via a clone, advance origin/main."""
    env.commit(wt, f"merged-{time.time_ns()}.txt")
    git(wt, "push", "-q", "origin", wt.name)
    other = env.base / f"merger-{time.time_ns()}"
    subprocess.run(["git", "clone", "-q", str(env.origin), str(other)], check=True, capture_output=True)
    git(other, "fetch", "-q", "origin", wt.name)
    git(other, "merge", "-q", "--no-ff", "-m", "merge", "FETCH_HEAD")
    git(other, "push", "-q", "origin", "HEAD:main")
    git(env.repo, "fetch", "-q", "origin")


def test_merged_clean_worktree_is_reaped_with_its_target(env: Env) -> None:
    wt = env.worktree("done")
    merge_into_main(env, wt)
    (wt / "target" / "debug").mkdir(parents=True)  # build output inside the tree
    (wt / "target" / "debug" / "big.o").write_text("o" * 1000)
    (wt / ".gitignore").write_text("target\n")
    git(wt, "add", ".gitignore")
    git(wt, "commit", "-qm", "ignore")
    git(wt, "push", "-q", "origin", "done")
    merge_into_main(env, wt)
    rows = reap(env)
    assert rows["done"]["action"] == "removed" and rows["done"]["reason"] == "pr_merged"
    assert not wt.exists()


def test_dry_run_reports_but_keeps(env: Env) -> None:
    wt = env.worktree("done")
    merge_into_main(env, wt)
    rows = reap(env, dry_run=True)
    assert rows["done"]["action"] == "would_remove"
    assert wt.is_dir()


def test_merged_but_dirty_kept(env: Env) -> None:
    wt = env.worktree("done")
    merge_into_main(env, wt)
    (wt / "wip.txt").write_text("not saved anywhere")
    rows = reap(env)
    assert rows["done"] == {**rows["done"], "action": "kept", "reason": "dirty"}
    assert (wt / "wip.txt").exists()


def test_merged_but_unpushed_follow_up_commit_kept(env: Env) -> None:
    wt = env.worktree("done")
    merge_into_main(env, wt)
    env.commit(wt, "later.txt")  # new local work after the merge
    rows = reap(env)
    assert rows["done"]["action"] == "kept"
    # ancestry says merged only while the tip is inside main; new work means not merged
    assert wt.is_dir()


def test_unmerged_young_orphan_kept_until_ttl(env: Env) -> None:
    wt = env.worktree("orphan")
    assert reap(env)["orphan"]["reason"] == "orphan_within_ttl"
    assert wt.is_dir()


def test_orphan_older_than_ttl_removed_when_clean_and_pushed(env: Env) -> None:
    wt = env.worktree("orphan")
    env.commit(wt)
    git(wt, "push", "-q", "origin", "orphan")
    rows = reap(env, ahead_days=8)
    assert rows["orphan"] == {**rows["orphan"], "action": "removed", "reason": "orphan_ttl"}
    assert not wt.exists()


def test_orphan_older_than_ttl_with_unpushed_work_kept(env: Env) -> None:
    wt = env.worktree("orphan")
    env.commit(wt)  # never pushed
    rows = reap(env, ahead_days=30)
    assert rows["orphan"]["action"] == "kept" and rows["orphan"]["reason"] == "unpushed_commits"
    assert wt.is_dir()


def test_recent_activity_protects_even_merged(env: Env) -> None:
    wt = env.worktree("done")
    merge_into_main(env, wt)
    rows = reap(env, min_idle_hours=2)
    assert rows["done"]["reason"] == "recent_activity" and wt.is_dir()


def test_main_checkout_never_listed_or_removed(env: Env) -> None:
    env.worktree("done")
    reap(env, ahead_days=30)
    assert env.repo.is_dir()


def test_worktree_outside_roots_kept(env: Env, tmp_path: Path) -> None:
    far = env.base / "far-away"
    git(env.repo, "worktree", "add", "-q", "-b", "far", str(far), "origin/main")
    git(far, "push", "-q", "origin", "far")
    rows = reap(env, ahead_days=30)
    assert rows["far-away"]["reason"] == "outside_roots" and far.is_dir()


def test_unknown_pr_state_fails_closed(env: Env) -> None:
    """gh source in a repo with no GitHub remote cannot say: nothing is removed."""
    wt = env.worktree("done")
    merge_into_main(env, wt)
    rows = reap(env, ahead_days=30, pr_source="gh")
    assert rows["done"]["action"] == "kept" and wt.is_dir()


def test_stale_registration_is_reported_not_pruned(env: Env) -> None:
    wt = env.worktree("gone")
    import shutil
    shutil.rmtree(wt)
    rows = reap(env)
    assert rows["gone"]["action"] == "kept" and rows["gone"]["reason"] == "missing_directory"
    assert "gone" in git(env.repo, "worktree", "list")


def test_find_repos_discovers_main_checkouts_only(env: Env) -> None:
    env.worktree("wt1")
    assert find_repos(env.roots) == [env.repo]


def test_opt_out_by_config_and_env(monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    assert load_policy({}).enabled
    assert not load_policy({"enabled": False}).enabled
    monkeypatch.setenv("HERDR_DISK_HYGIENE", "0")
    assert not load_policy({}).enabled
    assert run(load_policy({}))["status"] == "disabled"


def test_timer_opt_out_is_read_from_user_config_not_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A launchd/systemd job has no shell env and a different cwd: only the user file counts."""
    cfg = tmp_path / "home" / ".config" / "herdr-engineering" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("disk_hygiene:\n  enabled: false\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERDR_DISK_HYGIENE", raising=False)
    monkeypatch.delenv("HERDR_ENGINEERING_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert not load_policy().enabled
    assert run(load_policy())["status"] == "disabled"


def test_repo_local_config_cannot_choose_reaper_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "herdr-engineering.yaml").write_text(f"disk_hygiene:\n  roots: ['{tmp_path}']\n  worktrees: {{enabled: false}}\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert load_policy().roots == () and load_policy().worktrees


def test_users_own_worktrees_in_ideaprojects_are_never_reaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    env = Env(tmp_path, root=home / "IdeaProjects")
    wt = env.worktree("mine")
    merge_into_main(env, wt)
    out = reap_worktrees(policy(), extra_repos=[env.repo], now=time.time() + 30 * DAY)
    row = out["repos"][0]["worktrees"][0]
    assert row["action"] == "kept" and row["reason"] == "outside_roots"
    assert wt.is_dir()


def test_agent_worktree_in_agent_root_is_discovered_and_reaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The repo is in the user's ~/IdeaProjects (not a root); only its agent worktree is."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    env = Env(tmp_path, root=home / "IdeaProjects")
    agent_root = home / ".local" / "share" / "worktrees"
    agent_root.mkdir(parents=True)
    wt = agent_root / "done"
    git(env.repo, "worktree", "add", "-q", "-b", "done", str(wt), "origin/main")
    git(wt, "push", "-q", "origin", "done")
    merge_into_main(env, wt)
    out = reap_worktrees(policy(), now=time.time())  # no extra_repos: discovery through the worktree
    rows = [w for r in out["repos"] for w in r["worktrees"]]
    assert [(w["action"], w["reason"]) for w in rows] == [("removed", "pr_merged")]
    assert env.repo.is_dir()


def test_defaults_are_on() -> None:
    p = load_policy({})
    assert p.enabled and p.worktrees and p.docker and p.ttl_days == 7 and p.builder_keep_storage == "5GB"


# --- docker -----------------------------------------------------------------------------------

def test_parse_docker_time() -> None:
    assert parse_docker_time("0001-01-01T00:00:00Z") is None
    assert parse_docker_time("") is None
    assert parse_docker_time("2026-11-18T10:00:00.123456789Z") == parse_docker_time("2026-11-18T10:00:00Z")
    assert parse_docker_time("2026-11-18T10:00:00Z") == parse_docker_time("2026-11-18T11:00:00+01:00")


def test_default_prune_steps_are_label_filtered_only() -> None:
    from herdr_engineering.docker_hygiene import prune_steps
    steps = prune_steps()
    assert [name for name, _ in steps] == ["volumes"]
    assert "label=herdr.ephemeral=1" in steps[0][1]
    assert [n for n, _ in prune_steps(builder_prune=True, image_prune=True)] == ["volumes", "builder", "images"]
    p = load_policy({})
    assert not p.builder_prune and not p.image_prune
    assert load_policy({"docker": {"builder_prune": True}}).builder_prune


def test_run_args_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERDR_DOCKER_RUN_LABELS", raising=False)
    assert run_args() == list(RUN_ARGS) == ["--rm", "--label", "herdr.ephemeral=1"]
    monkeypatch.setenv("HERDR_DOCKER_RUN_LABELS", "--rm --label herdr.ephemeral=1 --label x=y")
    assert run_args()[-1] == "x=y"


needs_docker = pytest.mark.skipif(not docker_available(), reason="no reachable docker daemon")


def _image() -> str:
    for image in ("busybox", "alpine"):
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
            return image
    pytest.skip("no local busybox/alpine image (tests never pull)")


def _create(label: bool) -> str:
    name = f"herdr-test-{uuid.uuid4().hex[:8]}"
    args = ["docker", "create", "--name", name, *(["--label", "herdr.ephemeral=1"] if label else []),
            _image(), "true"]
    subprocess.run(args, check=True, capture_output=True)
    return name


def _exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0


@needs_docker
def test_sweep_dry_run_default_plans_no_unlabelled_prune() -> None:
    report = sweep(dry_run=True)
    assert [s["step"] for s in report["steps"]] == ["volumes"]


@needs_docker
def test_sweep_removes_old_labelled_only() -> None:
    mine, other = _create(True), _create(False)
    try:
        report = sweep(min_age_seconds=3600, now=time.time() + 2 * 3600)
        assert any(c["name"] == mine for c in report["containers"])
        assert not _exists(mine)
        assert _exists(other)  # unlabelled is never touched
    finally:
        for n in (mine, other):
            subprocess.run(["docker", "rm", "-f", n], capture_output=True)


@needs_docker
def test_sweep_keeps_young_labelled() -> None:
    mine = _create(True)
    try:
        sweep(min_age_seconds=3600)
        assert _exists(mine)
    finally:
        subprocess.run(["docker", "rm", "-f", mine], capture_output=True)


@needs_docker
def test_cleanup_docker_scope_refuses_unlabelled_and_removes_labelled() -> None:
    from herdr_engineering.cleanup_command import Refusal
    labelled, plain = _create(True), _create(False)
    try:
        with pytest.raises(Refusal, match="label"):
            remove_ephemeral_container(plain, dry_run=False)
        remove_ephemeral_container(labelled, dry_run=False)
        assert not _exists(labelled) and _exists(plain)
    finally:
        for n in (labelled, plain):
            subprocess.run(["docker", "rm", "-f", n], capture_output=True)


def test_sweep_without_docker_reports_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    assert sweep()["status"] == "docker_unavailable"
    assert os.environ["PATH"] == "/nonexistent"


def test_installer_dry_run_renders_both_jobs_on_this_platform(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "install-disk-hygiene.sh"
    out = subprocess.run(["bash", str(script), "install", "--dry-run"], capture_output=True, text=True,
                         check=True, env={**os.environ, "HERDR_ENG": "/bin/echo", "HOME": str(tmp_path)}).stdout
    assert "disk-hygiene-worktrees" in out and "disk-hygiene-docker" in out
    assert "--only" in out and not any(tmp_path.iterdir())  # dry run writes nothing
    if os.uname().sysname == "Darwin":
        import plistlib
        plist = out.split("== ", 2)[1].split("\n", 1)[1]
        assert plistlib.loads(plist.encode())["StartInterval"] == 6 * 3600
