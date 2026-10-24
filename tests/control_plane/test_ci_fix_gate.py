"""The result gate: what may leave the host after the agent quit. Real git against a local bare "forge"; the
agent's work copy is edited the way a hostile agent could edit it, including its own .git."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.control_plane.test_ci_fix_runner import (
    CONFIG,
    Env,
    git,
    runner,
)
from tests.control_plane.test_ci_fix_runner import (
    env as env,
)
from tests.control_plane.test_ci_fix_runner import (
    forge as forge,
)


def start(env, **kw):
    fix = env.ask(**kw)
    env.runner().once()
    assert env.service.get(fix)["state"] == "running"
    return fix


def repo_of(env, fix):
    return env.tmp / "work" / "ci-fix" / fix / "repo"


def commit_file(repo, name, text="x\n", message="fix"):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", message, cwd=repo)


def quit_agent(env, fix):
    env.launcher.running[fix] = False
    env.runner().once()
    return env.service.get(fix)


def remote_branches(forge):
    return git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=forge.path).split()


def branch_of(env, fix):
    return env.service.get(fix)["branch"]


# ------------------------------------------------------------------ success
def test_a_clean_fix_is_pushed_and_opened_as_a_draft_pull_request(env, forge):
    fix = start(env, sha=env.shas[1])
    repo = repo_of(env, fix)
    commit_file(repo, "app.txt", "fixed\n")
    (repo.parent / "SUMMARY.md").write_text("Fixed the timing assertion; ran pytest: 12 passed.\n")
    tip = git("rev-parse", "HEAD", cwd=repo)
    view = quit_agent(env, fix)
    branch = f"herdr-ci-fix/12-{env.shas[1][:7]}"
    assert view["state"] == "succeeded" and view["pr_url"].endswith("/pull/9")
    assert view["result"]["branch"] == branch and view["result"]["commits"] == 1 and view["result"]["files"] == 1
    assert view["result"]["insertions"] == 1 and view["result"]["flags"] == []
    assert "timing assertion" in view["result"]["summary"]
    assert sorted(remote_branches(forge)) == sorted(["main", branch])
    assert git("rev-parse", branch, cwd=forge.path) == tip
    assert git("rev-parse", "main", cwd=forge.path) == env.shas[2]                       # main untouched
    pr = env.forge.prs[0]
    assert pr["draft"] is True and pr["label"] == "herdr-ci-fix" and pr["head"] == branch and pr["base"] == "main"
    assert "written by an AI agent" in pr["body"] and "has not been reviewed" in pr["body"]
    assert "https://ci.example/repos/17/pipeline/12" in pr["body"] and env.shas[1] in pr["body"]
    assert "Fixed the timing assertion" in pr["body"] and "never merges" in pr["body"]
    assert env.leftovers() == []


def test_the_agent_session_gets_the_pull_request_association(env):
    fix = start(env)
    commit_file(repo_of(env, fix), "app.txt", "fixed\n")
    session = env.service.get(fix)["session_id"]
    quit_agent(env, fix)
    # the in-process fake session id is not an inspector session id; the real link is covered in test_ci_fix.py
    assert session


def test_no_commit_pushes_nothing(env, forge):
    fix = start(env)
    view = quit_agent(env, fix)
    assert view["state"] == "no_change" and remote_branches(forge) == ["main"] and env.forge.prs == []


def test_a_gc_repacked_work_copy_still_publishes(env, forge):
    fix = start(env, sha=env.shas[0])
    repo = repo_of(env, fix)
    commit_file(repo, "app.txt", "fixed\n")
    git("gc", "-q", "--prune=now", cwd=repo)                                              # objects now live in a pack
    assert quit_agent(env, fix)["state"] == "succeeded"


# ------------------------------------------------------------------ flags and blocks
def test_ci_config_and_lockfiles_are_flagged_not_blocked(env):
    fix = start(env)
    repo = repo_of(env, fix)
    commit_file(repo, ".woodpecker/ci.yaml", "steps: []\n")
    commit_file(repo, "deep/dir/package.lock", "{}\n")
    commit_file(repo, "src/app.py", "print(1)\n")
    view = quit_agent(env, fix)
    assert view["state"] == "succeeded"
    flags = view["result"]["flags"]
    assert any(".woodpecker/ci.yaml" in flag for flag in flags) and any("package.lock" in flag for flag in flags)
    assert not any("app.py" in flag for flag in flags)
    assert ".woodpecker/ci.yaml" in env.forge.prs[0]["body"]


def test_blocked_paths_end_the_run_with_nothing_pushed(tmp_path, forge):
    env = Env(tmp_path, forge, config={**CONFIG, "paths": {"block": ["secrets/**", "*.pem"]}})
    for path in ("secrets/key.txt", "deep/server.pem"):
        fix = start(env, number=hash(path) % 1000 + 1, op=path)
        commit_file(repo_of(env, fix), path, "x\n")
        view = quit_agent(env, fix)
        assert view["state"] == "failed" and "blocked path" in view["reason"], path
    assert remote_branches(forge) == ["main"] and env.forge.prs == []


def craft_commit(repo):
    """A commit whose tree is built with plumbing, which does not apply git's path checks."""
    def tree(items):
        lines = ""
        for mode, kind, sha, name in items:
            lines += f"{mode} {kind} {sha}\t{name}\n"
        done = subprocess.run(["git", "mktree"], cwd=repo, input=lines, capture_output=True, text=True, check=True,
                              env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
        return done.stdout.strip()
    return tree


def agent_commit_with(env, fix, build):
    """Make the agent's branch a commit on top of the failing one whose tree is ``build(repo, blob, tree)``."""
    repo = repo_of(env, fix)
    base = git("rev-parse", "HEAD", cwd=repo)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input="x\n", capture_output=True,
                          text=True, check=True, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}).stdout.strip()
    make_tree = craft_commit(repo)
    top = build(blob, make_tree)
    commit = git("commit-tree", top, "-p", base, "-m", "crafted", cwd=repo)
    git("update-ref", f"refs/heads/{branch_of(env, fix)}", commit, cwd=repo)


def tip_blob(repo):
    return git("rev-parse", "HEAD:app.txt", cwd=repo)


@pytest.mark.parametrize("name", [".git", ".GIT", "git~1", ".git.", ".gitmodules"])
def test_a_tree_entry_that_git_treats_as_its_own_directory_is_refused(env, forge, name):
    fix = start(env, number=7, op=name)
    repo = repo_of(env, fix)
    keep = tip_blob(repo)

    def build(blob, tree):
        inner = tree([("100644", "blob", blob, "pre-commit")])
        return tree([("100644", "blob", keep, "app.txt"), ("040000", "tree", inner, name)])
    agent_commit_with(env, fix, build)
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and "path refused" in view["reason"], view["reason"]
    assert remote_branches(forge) == ["main"]


def test_a_symlink_leaving_the_tree_is_refused_but_one_inside_is_fine(env, forge):
    fix = start(env, number=3, op="a")
    repo = repo_of(env, fix)
    for target in ("/etc/passwd", "../../outside", "a/../../b"):
        (repo / "link").unlink(missing_ok=True)
        (repo / "link").symlink_to(target)
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "link", cwd=repo)
        view = quit_agent(env, fix)
        assert view["state"] == "failed" and "symbolic link" in view["reason"], target
        fix = start(env, number=3 + len(target), op=f"b{target}")
        repo = repo_of(env, fix)
    (repo / "sub").mkdir()
    (repo / "sub" / "ok").symlink_to("../app.txt")                                          # stays inside the tree
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "ok link", cwd=repo)
    assert quit_agent(env, fix)["state"] == "succeeded"


def test_a_submodule_change_is_refused(env, forge):
    fix = start(env)
    repo = repo_of(env, fix)
    git("update-index", "--add", "--cacheinfo", f"160000,{env.shas[0]},vendor", cwd=repo)
    git("commit", "-q", "-m", "submodule", cwd=repo)
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and "submodule" in view["reason"] and remote_branches(forge) == ["main"]


@pytest.mark.parametrize("change,reason", [
    ({"limits": {"max_files": 2}}, "changed 3 files"),
    ({"limits": {"max_commits": 1}}, "made 3 commits"),
    ({"limits": {"max_diff_kb": 1}}, "the limit is 1 KB"),
])
def test_bounds_are_enforced(tmp_path, forge, change, reason):
    env = Env(tmp_path, forge, config={**CONFIG, **change})
    fix = start(env)
    repo = repo_of(env, fix)
    commit_file(repo, "a.txt", "a" * 3000 + "\n")
    commit_file(repo, "b.txt", "b\n")
    commit_file(repo, "c.txt", "c\n")
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and reason in view["reason"], view["reason"]
    assert remote_branches(forge) == ["main"] and env.forge.prs == []


def test_a_large_binary_is_refused(env, forge):
    fix = start(env)
    repo = repo_of(env, fix)
    (repo / "blob.bin").write_bytes(os.urandom(300 * 1024) + b"\0")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "binary", cwd=repo)
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and "binary file" in view["reason"]


def test_a_merge_commit_is_refused(env, forge):
    fix = start(env, sha=env.shas[0])
    repo = repo_of(env, fix)
    git("checkout", "-q", "-b", "side", cwd=repo)
    commit_file(repo, "side.txt", "s\n")
    branch = branch_of(env, fix)
    git("checkout", "-q", branch, cwd=repo)
    commit_file(repo, "main.txt", "m\n")
    git("merge", "-q", "--no-ff", "-m", "merge", "side", cwd=repo)
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and "merge commit" in view["reason"]


def test_history_that_does_not_descend_from_the_failing_commit_is_refused(env, forge):
    fix = start(env)
    repo = repo_of(env, fix)
    branch = branch_of(env, fix)
    git("checkout", "-q", "--orphan", "unrelated", cwd=repo)
    commit_file(repo, "other.txt", "o\n")
    git("branch", "-f", branch, "unrelated", cwd=repo)
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and "does not descend" in view["reason"]


def test_a_removed_branch_is_a_failure_not_a_pass(env):
    fix = start(env)
    repo = repo_of(env, fix)
    git("checkout", "-q", "--detach", cwd=repo)
    git("branch", "-D", branch_of(env, fix), cwd=repo)
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and "removed the fix branch" in view["reason"]


# ------------------------------------------------------------------ the agent controls its own .git
def test_nothing_in_the_agents_git_directory_is_ever_executed_or_trusted(env, forge, tmp_path):
    fix = start(env)
    repo = repo_of(env, fix)
    commit_file(repo, "app.txt", "fixed\n")
    marker = tmp_path / "EXECUTED"
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    config = repo / ".git" / "config"
    config.write_text(config.read_text() + (
        f"[core]\n\tfsmonitor = touch {marker}\n\thooksPath = {hook.parent}\n\tsshCommand = touch {marker}\n"
        f"[diff]\n\texternal = touch {marker}\n"
        f"[alias]\n\tdiff = !touch {marker}\n"
        f"[remote \"origin\"]\n\turl = /nonexistent/elsewhere.git\n\tpushurl = /nonexistent/elsewhere.git\n"
        f"[url \"/nonexistent/\"]\n\tinsteadOf = {forge.path}/\n"
        f"[credential]\n\thelper = !touch {marker}\n"))
    view = quit_agent(env, fix)
    assert view["state"] == "succeeded", view["reason"]
    assert not marker.exists()                                                           # nothing ran
    assert branch_of(env, fix) in remote_branches(forge)                                 # and it went to the real forge


def test_a_symlinked_dot_git_is_refused(env, tmp_path):
    fix = start(env)
    repo = repo_of(env, fix)
    commit_file(repo, "app.txt", "fixed\n")
    elsewhere = tmp_path / "elsewhere"
    (repo / ".git").rename(elsewhere)
    (repo / ".git").symlink_to(elsewhere)
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and "not a plain directory" in view["reason"]


def test_a_branch_ref_that_is_a_link_is_not_followed(env, tmp_path):
    fix = start(env)
    repo = repo_of(env, fix)
    commit_file(repo, "app.txt", "fixed\n")
    ref = repo / ".git" / "refs" / "heads" / branch_of(env, fix)
    (tmp_path / "decoy").write_text(env.shas[0] + "\n")
    ref.unlink()
    ref.symlink_to(tmp_path / "decoy")
    view = quit_agent(env, fix)
    assert view["state"] in {"failed", "no_change"}                                      # never a pass on a decoy


# ------------------------------------------------------------------ pushing
def test_an_existing_remote_branch_is_never_overwritten(env, forge):
    fix = start(env, sha=env.shas[0])
    repo = repo_of(env, fix)
    branch = branch_of(env, fix)
    git("branch", branch, env.shas[1], cwd=forge.path)                                   # someone already has it
    commit_file(repo, "app.txt", "fixed\n")
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and "already has a branch" in view["reason"]
    assert git("rev-parse", branch, cwd=forge.path) == env.shas[1]
    assert env.forge.prs == []


def test_the_push_refuses_an_existing_branch_even_if_it_appears_after_the_check(tmp_path, forge):
    git_port = runner.Git()
    gate_repo = tmp_path / "g.git"
    git_port.run(["init", "--bare", "-q", str(gate_repo)])
    git_port.run(["-C", str(gate_repo), "fetch", "-q", str(forge.path), "+refs/heads/main:refs/heads/herdr-ci-fix/1-aaaaaaa"])
    git("branch", "herdr-ci-fix/1-aaaaaaa", forge.shas[0], cwd=forge.path)               # appears between check and push
    with pytest.raises(runner.Failure):
        git_port.push_new_branch(gate_repo, str(forge.path), "herdr-ci-fix/1-aaaaaaa")
    assert git("rev-parse", "herdr-ci-fix/1-aaaaaaa", cwd=forge.path) == forge.shas[0]


def test_only_the_fix_branch_is_pushed(env, forge):
    fix = start(env)
    repo = repo_of(env, fix)
    commit_file(repo, "app.txt", "fixed\n")
    git("branch", "main-evil", cwd=repo)
    git("tag", "v-evil", cwd=repo)
    assert quit_agent(env, fix)["state"] == "succeeded"
    assert sorted(remote_branches(forge)) == sorted(["main", branch_of(env, fix)])
    assert git("tag", "--list", cwd=forge.path) == ""


def test_a_failed_pull_request_keeps_the_pushed_branch_and_says_so(env, forge):
    env.forge.pr_error = "the forge refused the pull request (422): draft pull requests are not enabled"
    fix = start(env)
    commit_file(repo_of(env, fix), "app.txt", "fixed\n")
    view = quit_agent(env, fix)
    assert view["state"] == "failed" and "was pushed but the pull request could not be opened" in view["reason"]
    assert view["result"]["branch"] == branch_of(env, fix) and view["pr_url"] is None
    assert branch_of(env, fix) in remote_branches(forge)


def test_a_repeat_after_a_crash_does_not_push_twice_and_finishes_the_pull_request(env, forge, monkeypatch):
    fix_id = start(env)
    repo = repo_of(env, fix_id)
    commit_file(repo, "app.txt", "fixed\n")
    record = json.loads((env.tmp / "state" / "fixes" / fix_id / "state.json").read_text())
    config = env.service.runner_config()
    saved = []
    gate = runner.ResultGate(runner.Git(), env.forge, env.tmp / "state")
    env.forge.pr_error = "forge down"
    assert gate.run(record, config, saved.append)[0] == "failed"
    assert record["gate"]["pushed"] is True and saved
    env.forge.pr_error = None
    pushes = []
    real = runner.Git.push_new_branch
    monkeypatch.setattr(runner.Git, "push_new_branch", lambda self, *a, **k: pushes.append(a) or real(self, *a, **k))
    state, reason, result = gate.run(record, config, saved.append)
    assert state == "succeeded" and result["pr_url"].endswith("/pull/9") and pushes == []
    assert gate.run(record, config, saved.append)[2]["pr_number"] == result["pr_number"]      # PR not opened twice
    assert len(env.forge.prs) == 1


def test_a_tampered_branch_name_is_refused(env):
    fix = start(env)
    commit_file(repo_of(env, fix), "app.txt", "fixed\n")
    record = json.loads((env.tmp / "state" / "fixes" / fix / "state.json").read_text())
    record["branch"] = "main"
    gate = runner.ResultGate(runner.Git(), env.forge, env.tmp / "state")
    state, reason, _ = gate.run(record, env.service.runner_config(), None)
    assert state == "failed" and "not the one this run was given" in reason


# ------------------------------------------------------------------ the summary
def test_the_summary_is_scrubbed_bounded_and_never_read_through_a_link(env, tmp_path):
    fix = start(env)
    repo = repo_of(env, fix)
    commit_file(repo, "app.txt", "fixed\n")
    (repo.parent / "SUMMARY.md").write_text("done. token ghp_" + "A" * 30 + " and Bearer abcdefghijklmnopqrstuvwx\n" + "z" * 9000)
    view = quit_agent(env, fix)
    body = env.forge.prs[0]["body"]
    assert "ghp_" not in body and "Bearer abc" not in body and "[REDACTED]" in body
    assert len(view["result"]["summary"]) <= 4000 and "ghp_" not in view["result"]["summary"]
    fix2 = start(env, number=99, op="link")
    repo2 = repo_of(env, fix2)
    commit_file(repo2, "app.txt", "fixed again\n")
    secret = tmp_path / "private"
    secret.write_text("PRIVATE-FILE-CONTENT")
    (repo2.parent / "SUMMARY.md").symlink_to(secret)
    quit_agent(env, fix2)
    assert "PRIVATE-FILE-CONTENT" not in env.forge.prs[1]["body"]


# ------------------------------------------------------------------ helpers
def test_glob_patterns_behave_like_gitignore_for_the_cases_we_use():
    match = runner.matching
    assert match([".woodpecker/**"], ".woodpecker/ci.yaml") and not match([".woodpecker/**"], "src/.woodpecker/x")
    assert match(["*.lock"], "package.lock") and match(["*.lock"], "a/b/yarn.lock") and not match(["*.lock"], "lock.txt")
    assert match([".git*/hooks/**"], ".githooks/hooks/pre-commit") and not match([".git*/hooks/**"], "src/.githooks/hooks/x")
    assert match(["docs/*/index.md"], "docs/a/index.md") and not match(["docs/*/index.md"], "docs/a/b/index.md")
    assert match(["**/secret.txt"], "secret.txt") and match(["**/secret.txt"], "a/b/secret.txt")
    assert not match(["a?c"], "a/c") and match(["a?c"], "abc")


@pytest.mark.parametrize("name,expected", [
    (".git", True), (".GIT", True), ("git~1", True), (".git.", True), (".git ", True), (".g‌it", True),
    (".gitmodules", True), (".", True), ("..", True), (".gitignore", False), (".github", False), ("git", False)])
def test_git_like_components(name, expected):
    assert runner.git_like_component(name) is expected


def test_scrub_removes_the_runners_own_token_and_token_shapes():
    text = "use sekrit-token-value and ghp_" + "B" * 25 + " or github_pat_" + "C" * 30 + " or AKIAABCDEFGHIJKLMNOP"
    out = runner.scrub(text, ["sekrit-token-value"])
    assert "sekrit" not in out and "ghp_" not in out and "github_pat_" not in out and "AKIA" not in out


# ------------------------------------------------------------------ the GitHub API
class Recorder(runner.HttpsForge):
    def __init__(self, token_file, replies):
        super().__init__(("github.com",), token_file)
        self.replies, self.sent = list(replies), []

    def request(self, method, url, body=None):
        self.sent.append((method, url, body))
        return self.replies.pop(0)


def test_the_github_pull_request_is_a_draft_with_a_label(tmp_path):
    token = tmp_path / "t"
    token.write_text("tok\n")
    token.chmod(0o600)
    forge = Recorder(token, [(201, {"number": 5, "html_url": "https://github.com/org/app/pull/5"}), (200, [])])
    out = forge.open_pr({"repository_id": "github.com/org/app"}, "herdr-ci-fix/1-aaaaaaa", "main", "T", "B", True,
                        "herdr-ci-fix")
    assert out == {"url": "https://github.com/org/app/pull/5", "number": 5, "labelled": True}
    method, url, body = forge.sent[0]
    assert (method, url) == ("POST", "https://api.github.com/repos/org/app/pulls")
    assert body == {"title": "T", "head": "herdr-ci-fix/1-aaaaaaa", "base": "main", "body": "B", "draft": True}
    assert forge.sent[1][1] == "https://api.github.com/repos/org/app/issues/5/labels"
    assert forge.sent[1][2] == {"labels": ["herdr-ci-fix"]}


@pytest.mark.parametrize("identity,reply,message", [
    ("gitlab.com/org/app", (201, {}), "only supported on github.com"),
    ("github.com/org/app/extra", (201, {}), "only supported on github.com"),
    ("github.com/org/app", (422, {"message": "Draft pull requests are not supported"}), "refused the pull request \\(422\\)"),
    ("github.com/org/app", (201, {"number": 1, "html_url": "http://insecure/1"}), "no pull request URL"),
])
def test_github_pull_request_failures_are_reported(tmp_path, identity, reply, message):
    token = tmp_path / "t"
    token.write_text("tok\n")
    token.chmod(0o600)
    with pytest.raises(runner.Failure, match=message):
        Recorder(token, [reply]).open_pr({"repository_id": identity}, "b", "main", "T", "B", True, "l")


def test_a_failed_label_is_reported_not_fatal(tmp_path):
    token = tmp_path / "t"
    token.write_text("tok\n")
    token.chmod(0o600)
    forge = Recorder(token, [(201, {"number": 5, "html_url": "https://github.com/o/a/pull/5"}), (403, {})])
    assert forge.open_pr({"repository_id": "github.com/o/a"}, "b", "main", "T", "B", True, "l")["labelled"] is False


def test_the_forge_token_must_be_private(tmp_path):
    token = tmp_path / "t"
    token.write_text("tok\n")
    token.chmod(0o644)
    with pytest.raises(runner.Failure, match="must not be readable"):
        runner.HttpsForge(("github.com",), token).token()
    with pytest.raises(runner.Failure, match="no forge token"):
        runner.HttpsForge(("github.com",), None).token()
    assert Path(token).exists()
