"""scripts/agent-rules-sync.sh and ansible/tasks/agent-rules.yml: real local git repos, no mocks."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agent-rules-sync.sh"
TASKS = ROOT / "ansible" / "tasks" / "agent-rules.yml"
FINGERPRINT = ROOT / "ansible" / "tasks" / "fleet-fingerprint.yml"


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def rules_repo(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "AGENTS.md").write_text("v1\n")
    # install.sh stands in for the real installer: it records which AGENTS.md it saw and how often it ran.
    (repo / "install.sh").write_text('#!/bin/sh\ncat AGENTS.md > "$HOME/installed.txt"\necho run >> "$HOME/runs.txt"\n')
    os.chmod(repo / "install.sh", 0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v1")
    git(repo, "tag", "v1")
    v1 = git(repo, "rev-parse", "HEAD")
    (repo / "AGENTS.md").write_text("v2\n")
    git(repo, "commit", "-qam", "v2")
    return repo, v1, git(repo, "rev-parse", "HEAD")


def run(home, repo, ref):
    return subprocess.run(["bash", str(SCRIPT), str(repo), ref, str(home / ".agent-rules")],
                          env={**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null"},
                          capture_output=True, text=True)


def test_installs_pinned_ref_and_reports_short_sha(tmp_path, rules_repo):
    repo, v1, _ = rules_repo
    home = tmp_path / "home"
    home.mkdir()
    out = run(home, repo, v1)
    assert out.returncode == 0, out.stderr
    assert (home / "installed.txt").read_text() == "v1\n"
    assert out.stdout.strip().splitlines()[-1] == f"rules={v1[:7]} changed=true"


def test_second_run_is_idempotent_and_tag_pins_work(tmp_path, rules_repo):
    repo, v1, v2 = rules_repo
    home = tmp_path / "home"
    home.mkdir()
    assert run(home, repo, "v1").returncode == 0
    again = run(home, repo, "v1")
    assert again.stdout.strip().splitlines()[-1] == f"rules={v1[:7]} changed=false"
    moved = run(home, repo, v2)
    assert moved.stdout.strip().splitlines()[-1] == f"rules={v2[:7]} changed=true"
    assert (home / "installed.txt").read_text() == "v2\n"


def test_picks_up_commits_published_after_the_first_clone(tmp_path, rules_repo):
    repo, _, v2 = rules_repo
    home = tmp_path / "home"
    home.mkdir()
    assert run(home, repo, v2).returncode == 0
    (repo / "AGENTS.md").write_text("v3\n")
    git(repo, "commit", "-qam", "v3")
    v3 = git(repo, "rev-parse", "HEAD")
    out = run(home, repo, v3)
    assert out.returncode == 0, out.stderr
    assert (home / "installed.txt").read_text() == "v3\n"


def test_missing_ref_fails_and_leaves_checkout_alone(tmp_path, rules_repo):
    repo, v1, _ = rules_repo
    home = tmp_path / "home"
    home.mkdir()
    assert run(home, repo, v1).returncode == 0
    bad = run(home, repo, "no-such-ref")
    assert bad.returncode != 0 and "no-such-ref" in bad.stderr
    assert git(home / ".agent-rules", "rev-parse", "HEAD") == v1
    assert (home / "runs.txt").read_text().count("run") == 1


@pytest.mark.parametrize("ref", ["-x", "a..b", ""])
def test_unsafe_ref_is_refused_before_any_git_call(tmp_path, rules_repo, ref):
    home = tmp_path / "home"
    home.mkdir()
    out = run(home, rules_repo[0], ref)
    assert out.returncode != 0 and not (home / ".agent-rules").exists()


def test_failing_installer_fails_the_run(tmp_path, rules_repo):
    repo, _, _ = rules_repo
    (repo / "install.sh").write_text("#!/bin/sh\nexit 3\n")
    git(repo, "commit", "-qam", "broken")
    home = tmp_path / "home"
    home.mkdir()
    assert run(home, repo, git(repo, "rev-parse", "HEAD")).returncode != 0


def test_task_file_is_wired_to_the_script_and_skips_without_a_pin():
    tasks = yaml.safe_load(TASKS.read_text())[0]
    assert tasks["become"] is False and tasks["when"] == "agent_rules_ref is defined"
    text = TASKS.read_text()
    assert "agent-rules-sync.sh" in text and "agent_rules_repo_url" in text and "agent_rules_ref" in text


@pytest.mark.parametrize("playbook", ["fleet-sync.yml", "site.yml"])
def test_playbooks_converge_rules_before_the_fingerprint(playbook):
    tasks = yaml.safe_load((ROOT / "ansible/playbooks" / playbook).read_text())[0]["tasks"]
    includes = [t.get("ansible.builtin.include_tasks") for t in tasks]
    assert "../tasks/agent-rules.yml" in includes
    if "../tasks/fleet-fingerprint.yml" in includes:
        assert includes.index("../tasks/agent-rules.yml") < includes.index("../tasks/fleet-fingerprint.yml")


def test_fingerprint_captures_rules_revision_and_playbooks_report_it():
    assert "fp_rules" in FINGERPRINT.read_text()
    for name in ("fleet-sync.yml", "fleet-status.yml"):
        assert "rules={{ fp_rules" in (ROOT / "ansible/playbooks" / name).read_text()
