"""The /herdr::join skill: source validity, four-harness install without overwriting, no auto-join hook."""
import os
import subprocess
import sys
from pathlib import Path

from herdr_engineering import session_summary as ss

SKILL = ss.JOIN_SOURCE / "SKILL.md"
ROOT = Path(ss.__file__).resolve().parents[1]
LINKS = (".claude/skills", ".codex/skills", ".pi/agent/skills", ".config/opencode/skills")
CLI = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"


def test_skill_source_is_valid_symlinked_and_packaged():
    head, body = SKILL.read_text().split("\n---", 1)
    assert "\nname: herdr-join\n" in head + "\n" and "description:" in head
    assert (ROOT / "skills/herdr-join").resolve() == ss.JOIN_SOURCE.resolve()
    assert "skill_data/herdr-join/*.md" in (ROOT / "pyproject.toml").read_text()
    for needle in ("herdr-eng rooms join-fabric NAME --json", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID",
                   "--no-workspace", "--control-plane", "room_access", "agent id", "/herdr::join"):
        assert needle in body, needle
    assert "`coordinate` and `grant_agent` are owner-only" in body
    assert "auto_join_on_session_start" in body and "off by default" in body


def test_documented_command_exists_in_the_cli():
    from herdr_engineering import rooms_client
    assert "join-fabric" in rooms_client.ACTIONS


def test_fresh_install_reaches_all_four_harnesses_with_a_namespaced_command(tmp_path):
    results = ss.install(home=tmp_path, skill="herdr-join")
    assert {r.action for r in results} == {"installed"}
    for d in LINKS:
        link = tmp_path / d / "herdr-join"
        assert link.is_symlink() and (link / "SKILL.md").read_bytes() == SKILL.read_bytes()
    assert (tmp_path / ".config/opencode/commands/herdr-join.md").is_file()
    # no top-level alias, and no Pi/Codex prompt that could shadow a built-in
    assert not (tmp_path / ".config/opencode/commands/join.md").exists()
    assert not (tmp_path / ".pi/agent/prompts").exists() and not (tmp_path / ".codex/prompts").exists()
    assert ss.verify(home=tmp_path, skill="herdr-join")[0]


def test_default_install_includes_herdr_join_and_is_idempotent(tmp_path):
    assert "herdr-join" in ss.SKILLS
    for skill in ss.SKILLS:
        ss.install(home=tmp_path, skill=skill)
    assert {r.action for r in ss.install(home=tmp_path, skill="herdr-join")} == {"unchanged"}


def test_preexisting_skills_and_commands_are_reported_never_overwritten(tmp_path):
    own = tmp_path / ".claude/skills/herdr-join"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("my own join\n")
    foreign_link = tmp_path / ".codex/skills/herdr-join"
    foreign_link.parent.mkdir(parents=True)
    foreign_link.symlink_to(tmp_path)
    cmd = tmp_path / ".config/opencode/commands/herdr-join.md"
    cmd.parent.mkdir(parents=True)
    cmd.write_text("my own command\n")
    got = {r.target: r.action for r in ss.install(home=tmp_path, skill="herdr-join")}
    assert got[str(own)] == "preserved" and got[str(foreign_link)] == "preserved" and got[str(cmd)] == "preserved"
    assert (own / "SKILL.md").read_text() == "my own join\n" and not own.is_symlink()
    assert foreign_link.resolve() == tmp_path.resolve() and cmd.read_text() == "my own command\n"
    assert (tmp_path / ".pi/agent/skills/herdr-join").is_symlink()          # the free harnesses still get it
    removed = {r.target: r.action for r in ss.uninstall(home=tmp_path, skill="herdr-join")}
    assert removed[str(own)] == "preserved" and cmd.read_text() == "my own command\n" and (own / "SKILL.md").exists()
    assert not (tmp_path / ".pi/agent/skills/herdr-join").exists()          # only our own link is removed


def test_cli_install_all_includes_herdr_join(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    p = subprocess.run([sys.executable, "-c", CLI, "summary", "install", "--home", str(tmp_path), "--json"],
                       env=env, capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0, p.stderr
    assert (tmp_path / ".pi/agent/skills/herdr-join/SKILL.md").is_file()
    v = subprocess.run([sys.executable, "-c", CLI, "summary", "verify", "--home", str(tmp_path), "--skill", "herdr-join"],
                       env=env, capture_output=True, text=True, cwd=ROOT)
    assert v.returncode == 0, v.stdout


def test_no_session_start_hook_was_added():
    # the skill text may DOCUMENT the opt-in key; the command adapters (which run it) must never wire a hook
    for path in (ROOT / "herdr_engineering/skill_data/herdr-join/adapters").rglob("*"):
        if path.is_file():
            text = path.read_text().lower()
            assert "sessionstart" not in text and "session_start" not in text
