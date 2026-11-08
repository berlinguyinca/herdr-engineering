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
    (repo / "install.sh").write_text(
        '#!/bin/sh\ncat AGENTS.md > "$HOME/installed.txt"\necho run >> "$HOME/runs.txt"\n'
        'mkdir -p "$HOME/.claude"\nln -sfn "$PWD/AGENTS.md" "$HOME/.claude/AGENTS.md"\n'
        'grep -qs agent-rules "$HOME/.claude/CLAUDE.md" || echo "@$PWD/AGENTS.md" >> "$HOME/.claude/CLAUDE.md"\n')
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
    assert out.stdout.strip().splitlines()[-1] == f"rules={v1[:7]} changed=true install_changed=true"


def test_second_run_is_idempotent_and_tag_pins_work(tmp_path, rules_repo):
    repo, v1, v2 = rules_repo
    home = tmp_path / "home"
    home.mkdir()
    assert run(home, repo, "v1").returncode == 0
    again = run(home, repo, "v1")
    assert again.stdout.strip().splitlines()[-1] == f"rules={v1[:7]} changed=false install_changed=false"
    moved = run(home, repo, v2)
    assert moved.stdout.strip().splitlines()[-1] == f"rules={v2[:7]} changed=true install_changed=false"
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


def test_install_changed_tracks_what_the_installer_did(tmp_path, rules_repo):
    repo, v1, _ = rules_repo
    home = tmp_path / "home"
    home.mkdir()
    first = run(home, repo, v1).stdout.strip().splitlines()[-1]
    assert first.endswith("install_changed=true")
    assert (home / ".claude" / "AGENTS.md").is_symlink()
    assert run(home, repo, v1).stdout.strip().splitlines()[-1].endswith("install_changed=false")
    (home / ".claude" / "AGENTS.md").unlink()                      # a removed link is repaired by install.sh
    assert run(home, repo, v1).stdout.strip().splitlines()[-1].endswith("install_changed=true")


def test_a_branch_name_is_not_a_pin(tmp_path, rules_repo):
    repo, _, _ = rules_repo
    home = tmp_path / "home"
    home.mkdir()
    bad = run(home, repo, "main")
    assert bad.returncode != 0 and "branch" in bad.stderr and "tag or a commit" in bad.stderr
    assert not (home / "installed.txt").exists()


def test_abbreviated_commit_is_a_valid_pin(tmp_path, rules_repo):
    repo, v1, _ = rules_repo
    home = tmp_path / "home"
    home.mkdir()
    out = run(home, repo, v1[:8])
    assert out.returncode == 0, out.stderr
    assert (home / "installed.txt").read_text() == "v1\n"


def test_tag_wins_over_a_same_named_branch(tmp_path, rules_repo):
    repo, v1, v2 = rules_repo
    git(repo, "branch", "rel", v2)
    git(repo, "tag", "rel", v1)
    home = tmp_path / "home"
    home.mkdir()
    out = run(home, repo, "rel")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1].startswith(f"rules={v1[:7]} ")
    assert (home / "installed.txt").read_text() == "v1\n"


def test_ssh_is_non_interactive_and_accepts_new_host_keys():
    env = {k: v for k, v in os.environ.items() if k != "GIT_SSH_COMMAND"}
    out = subprocess.run(["bash", str(SCRIPT), "--print-ssh-command"], env=env, capture_output=True, text=True)
    assert "BatchMode=yes" in out.stdout and "StrictHostKeyChecking=accept-new" in out.stdout


def test_operator_ssh_command_wins():
    out = subprocess.run(["bash", str(SCRIPT), "--print-ssh-command"], capture_output=True, text=True,
                         env={**os.environ, "GIT_SSH_COMMAND": "ssh -i /k"})
    assert out.stdout.strip() == "ssh -i /k"


REFS = ["v1", "v1.2.3", "release/1.x", "8a92a224a0fb3eb661cc5c79e035442a856a8c0e", "a", "x.lock", "x/", "x.", "x/.y",
        "a..b", "a//b", "a@{1}", "-x", ".x", "/x", "a b", "a\nb", "", "a" * 128, "a" * 129, "tag.lock/x", "x.lock.y"]


@pytest.mark.parametrize("ref", REFS)
def test_shell_and_python_ref_rules_agree(ref):
    from herdr_engineering.control_plane.agent_rules_config import parse_agent_rules
    try:
        parse_agent_rules({"repo_url": "https://github.com/a/b.git", "ref": ref})
        py_ok = True
    except ValueError:
        py_ok = False
    sh_ok = subprocess.run(["bash", str(SCRIPT), "--validate-ref", ref]).returncode == 0
    assert py_ok == sh_ok, ref


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
    assert "ignore_errors" not in text and "install_changed=true" in text
    assert "agent-rules-sync.sh" in text and "agent_rules_repo_url" in text and "agent_rules_ref" in text


@pytest.mark.parametrize("playbook", ["fleet-sync.yml", "site.yml"])
def test_playbooks_converge_rules_before_the_fingerprint(playbook):
    tasks = yaml.safe_load((ROOT / "ansible/playbooks" / playbook).read_text())[0]["tasks"]
    includes = [t.get("ansible.builtin.include_tasks") for t in tasks]
    assert "../tasks/agent-rules.yml" in includes
    if "../tasks/fleet-fingerprint.yml" in includes:
        assert includes.index("../tasks/agent-rules.yml") < includes.index("../tasks/fleet-fingerprint.yml")


def test_a_failed_pin_is_recorded_and_does_not_abort_the_play():
    outer = yaml.safe_load(TASKS.read_text())[0]
    (rescue,) = [t for t in outer["rescue"] if "ansible.builtin.set_fact" in t]
    assert "agent_rules_failure" in rescue["ansible.builtin.set_fact"]
    assert "ignore_errors" not in str(outer["rescue"]) and "failed_when" not in str(outer["rescue"])
    text = FINGERPRINT.read_text()
    assert "fp_rules_value" in text and "FAILED:" in text and "agent_rules_failure" in text


def test_fingerprint_captures_rules_revision_and_playbooks_report_it():
    assert "fp_rules" in FINGERPRINT.read_text()
    for name in ("fleet-sync.yml", "fleet-status.yml"):
        assert "| rules={{ fp_rules_value" in (ROOT / "ansible/playbooks" / name).read_text()
