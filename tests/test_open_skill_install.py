"""The portable installer never replaces another harness's skill."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-open-skill.py"
TARGETS = {
    "claude": ".claude/skills/herdr-open",
    "codex": ".codex/skills/herdr-open",
    "pi": ".pi/agent/skills/herdr-open",
    "opencode": ".config/opencode/skills/herdr-open",
}


def run_install(home, source, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--home", str(home), "--source", str(source), *args],
        capture_output=True, text=True, check=False,
    )


def make_source(tmp_path):
    source = tmp_path / "canonical skill with spaces"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: herdr-open\ndescription: Test\n---\n")
    return source


def test_links_one_canonical_skill_into_all_four_harnesses(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "operator home"
    result = run_install(home, source)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)["harnesses"]
    assert set(rows) == set(TARGETS)
    for harness, relative in TARGETS.items():
        target = home / relative
        assert target.is_symlink()
        assert target.resolve() == source.resolve()
        assert rows[harness]["status"] == "installed"

    # Updating the canonical skill reaches every harness without copying drift.
    (source / "SKILL.md").write_text("Updated skill\n")
    for relative in TARGETS.values():
        assert (home / relative / "SKILL.md").read_text() == "Updated skill\n"


def test_repeat_install_is_unchanged(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    assert run_install(home, source).returncode == 0
    result = run_install(home, source)
    assert result.returncode == 0
    assert {r["status"] for r in json.loads(result.stdout)["harnesses"].values()} == {
        "unchanged"
    }


def test_collision_preserves_existing_skill_and_installs_nothing(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    existing = home / TARGETS["pi"]
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("User-owned skill\n")
    result = run_install(home, source)
    assert result.returncode == 1
    assert json.loads(result.stdout)["harnesses"]["pi"]["status"] == "conflict"
    assert (existing / "SKILL.md").read_text() == "User-owned skill\n"
    assert not (home / TARGETS["claude"]).exists()
    assert not (home / TARGETS["codex"]).exists()


def test_dangling_symlink_is_a_collision(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    existing = home / TARGETS["codex"]
    existing.parent.mkdir(parents=True)
    existing.symlink_to(tmp_path / "missing-other-skill")
    result = run_install(home, source)
    assert result.returncode == 1
    assert existing.is_symlink()
    assert existing.readlink() == tmp_path / "missing-other-skill"


def test_dry_run_does_not_create_home(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    result = run_install(home, source, "--dry-run")
    assert result.returncode == 0
    assert not home.exists()
    assert {r["status"] for r in json.loads(result.stdout)["harnesses"].values()} == {
        "would-install"
    }


def test_selected_harness_only(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    result = run_install(home, source, "--harness", "pi")
    assert result.returncode == 0
    assert set(json.loads(result.stdout)["harnesses"]) == {"pi"}
    assert not (home / ".claude").exists()


def test_missing_source_is_an_error_without_creating_directories(tmp_path):
    home = tmp_path / "home"
    result = run_install(home, tmp_path / "absent")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]
    assert not home.exists()


def test_activation_preserves_user_rules_and_is_idempotent(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    paths = [".claude/CLAUDE.md", ".codex/AGENTS.md", ".pi/agent/AGENTS.md",
             ".config/opencode/AGENTS.md"]
    for relative in paths:
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# My rules\n\nKeep existing rules.\n")
    result = run_install(home, source, "--activate")
    assert result.returncode == 0, result.stderr
    initial = [(home / path).read_text() for path in paths]
    assert all(content.startswith("# My rules\n\nKeep existing rules.\n") for content in initial)
    assert all(content.count("<!-- HERDR:OPEN:START -->") == 1 for content in initial)
    assert run_install(home, source, "--activate").returncode == 0
    assert initial == [(home / path).read_text() for path in paths]


def test_activation_dry_run_writes_nothing(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    result = run_install(home, source, "--activate", "--dry-run")
    assert result.returncode == 0
    assert not home.exists()


def test_activation_respects_effective_pi_and_codex_override_files(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    for relative in [".codex/AGENTS.override.md", ".pi/agent/AGENTS.override.md"]:
        path = home / relative
        path.parent.mkdir(parents=True)
        path.write_text("Override rules\n")
    result = run_install(home, source, "--activate", "--harness", "pi", "--harness", "codex")
    assert result.returncode == 0
    for relative in [".codex", ".pi/agent"]:
        assert "HERDR:OPEN:START" in (home / relative / "AGENTS.override.md").read_text()
        assert not (home / relative / "AGENTS.md").exists()


def test_malformed_activation_block_prevents_any_installs(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    instructions = home / ".claude/CLAUDE.md"
    instructions.parent.mkdir(parents=True)
    original = "User rules\n<!-- HERDR:OPEN:START -->\nbroken"
    instructions.write_text(original)
    result = run_install(home, source, "--activate")
    assert result.returncode == 1
    assert instructions.read_text() == original
    assert not (home / TARGETS["codex"]).exists()


def test_new_opencode_instructions_preserve_claude_fallback(tmp_path):
    source = make_source(tmp_path)
    home = tmp_path / "home"
    claude = home / ".claude/CLAUDE.md"
    claude.parent.mkdir(parents=True)
    claude.write_text("My global rules\n")
    result = run_install(home, source, "--activate", "--harness", "opencode")
    assert result.returncode == 0
    opencode = (home / ".config/opencode/AGENTS.md").read_text()
    assert str(claude) in opencode
    assert "Read" in opencode


def test_bundled_launcher_works_from_unrelated_cwd(tmp_path):
    launcher = SCRIPT.parent.parent / "skills/herdr-open/scripts/open-client.py"
    result = subprocess.run([sys.executable, str(launcher), "--help"], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "discover" in result.stdout
    assert "show" in result.stdout
