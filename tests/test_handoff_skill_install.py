"""The /handoff skill rides the /summary installer: same ownership contract, no Pi prompt."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering import session_summary as ss

SKILL = ss.HANDOFF_SOURCE / "SKILL.md"
ROOT = Path(ss.__file__).resolve().parents[1]


def test_handoff_skill_source_is_valid_and_symlinked():
    head, body = SKILL.read_text().split("\n---", 1)
    assert "\nname: handoff\n" in head + "\n" and "description:" in head
    assert (ROOT / "skills/handoff").resolve() == ss.HANDOFF_SOURCE.resolve()
    for command in ("herdr-eng handoff harness", "herdr-eng summary history --json", "herdr-handoff create",
                    "herdr-handoff launch", "--from-json -", "manual_launch_required"):
        assert command in body
    assert "/herdr::toClaude" in body and "Pi's built-in `/handoff`" in body


def test_fresh_install_reaches_all_four_agents_without_a_pi_prompt(tmp_path):
    results = ss.install(home=tmp_path, skill="handoff")
    assert {r.action for r in results} == {"installed"}
    for agent_dir in (".claude/skills", ".codex/skills", ".pi/agent/skills", ".config/opencode/skills"):
        link = tmp_path / agent_dir / "handoff"
        assert link.is_symlink() and (link / "SKILL.md").read_bytes() == SKILL.read_bytes()
    assert (tmp_path / ".config/opencode/commands/handoff.md").is_file()
    assert not (tmp_path / ".pi/agent/prompts").exists()          # Pi's own /handoff owns that name
    assert not (tmp_path / ".codex/prompts").exists()
    assert ss.verify(home=tmp_path, skill="handoff")[0]


def test_summary_and_handoff_keep_separate_manifests(tmp_path):
    ss.install(home=tmp_path)
    ss.install(home=tmp_path, skill="handoff")
    base = tmp_path / ".local/share/herdr-engineering/skills"
    assert (base / ".summary-manifest.json").is_file() and (base / ".handoff-manifest.json").is_file()
    ss.uninstall(home=tmp_path, skill="handoff")
    assert ss.verify(home=tmp_path)[0] and not ss.verify(home=tmp_path, skill="handoff")[0]


def test_reinstall_is_idempotent(tmp_path):
    ss.install(home=tmp_path, skill="handoff")
    assert {r.action for r in ss.install(home=tmp_path, skill="handoff")} == {"unchanged"}


def test_customized_files_are_preserved_and_survive_uninstall(tmp_path):
    ss.install(home=tmp_path, skill="handoff")
    command = tmp_path / ".config/opencode/commands/handoff.md"
    command.write_text("my own command\n")
    again = {r.target: r.action for r in ss.install(home=tmp_path, skill="handoff")}
    assert again[str(command)] == "preserved" and command.read_text() == "my own command\n"
    removed = {r.target: r.action for r in ss.uninstall(home=tmp_path, skill="handoff")}
    assert removed[str(command)] == "preserved" and command.read_text() == "my own command\n"


def test_a_foreign_user_skill_named_handoff_is_left_alone(tmp_path):
    mine = tmp_path / ".claude/skills/handoff"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: handoff\n---\nmine\n")
    results = ss.install(home=tmp_path, skill="handoff")
    assert any(r.action == "preserved" and r.target == str(mine) for r in results)
    assert (mine / "SKILL.md").read_text().endswith("mine\n")


def test_unknown_skill_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ss.install(home=tmp_path, skill="nope")


def test_upgrade_rewrites_only_unmodified_managed_files(tmp_path, monkeypatch):
    ss.install(home=tmp_path, skill="handoff")
    src = tmp_path / "src"
    (src / "adapters").mkdir(parents=True)
    for rel in ("SKILL.md", "adapters/opencode-handoff.md"):
        (src / rel).write_bytes((ss.HANDOFF_SOURCE / rel).read_bytes() + b"\nv2\n")
    monkeypatch.setattr(ss, "HANDOFF_SOURCE", src)
    assert not ss.verify(home=tmp_path, skill="handoff")[0] or any(
        r.action == "stale" for r in ss.verify(home=tmp_path, skill="handoff")[1])
    upgraded = {r.action for r in ss.install(home=tmp_path, skill="handoff")}
    assert "upgraded" in upgraded


def test_cli_installs_both_skills_by_default_and_one_on_request(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    def run(*a):
        return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "summary", *a, "--home", str(tmp_path)],
                              capture_output=True, text=True, env=env)
    assert run("install", "--skill", "handoff").returncode == 0
    assert (tmp_path / ".claude/skills/handoff").is_symlink() and not (tmp_path / ".claude/skills/summary").exists()
    assert run("verify", "--skill", "summary").returncode == 1
    assert run("install").returncode == 0
    assert run("verify").returncode == 0
    assert run("uninstall").returncode == 0 and run("verify").returncode == 1
