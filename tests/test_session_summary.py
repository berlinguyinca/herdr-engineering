"""Install, upgrade, compatibility and uninstall of the /summary skill (all four agents)."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering import session_summary as ss

SKILL = Path(ss.SOURCE / "SKILL.md")


def _by_action(results):
    out = {}
    for r in results:
        out.setdefault(r.action, []).append(r.target)
    return out


def _snapshot(root: Path):
    return {str(p.relative_to(root)): (os.readlink(p) if p.is_symlink() else p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_symlink() or p.is_file()}


def test_fresh_global_install_reaches_all_four_agents(tmp_path):
    results = ss.install(home=tmp_path)
    assert {r.action for r in results} == {"installed"}
    for agent_dir in (".claude/skills", ".codex/skills", ".pi/agent/skills", ".config/opencode/skills"):
        link = tmp_path / agent_dir / "summary"
        assert link.is_symlink() and (link / "SKILL.md").read_bytes() == SKILL.read_bytes()
    assert (tmp_path / ".pi/agent/prompts/summary.md").is_file()
    assert (tmp_path / ".config/opencode/commands/summary.md").is_file()
    ok, _ = ss.verify(home=tmp_path)
    assert ok


def test_codex_gets_a_skill_but_no_command_file(tmp_path):
    ss.install(home=tmp_path)
    assert not list((tmp_path / ".codex").rglob("summary*.md")) or all(
        p.name == "SKILL.md" for p in (tmp_path / ".codex").rglob("summary*.md"))
    assert not (tmp_path / ".codex/prompts").exists()


def test_reinstall_is_idempotent(tmp_path):
    ss.install(home=tmp_path)
    before = _snapshot(tmp_path)
    again = ss.install(home=tmp_path)
    assert {r.action for r in again} == {"unchanged"}
    assert _snapshot(tmp_path) == before


def test_upgrade_rewrites_only_unmodified_managed_files(tmp_path, monkeypatch):
    ss.install(home=tmp_path)
    src = tmp_path / "src"
    (src / "adapters").mkdir(parents=True)
    for rel in ("SKILL.md", "adapters/pi-summary.md", "adapters/opencode-summary.md"):
        (src / rel).write_bytes((ss.SOURCE / rel).read_bytes() + b"\nv2\n")
    monkeypatch.setattr(ss, "SOURCE", src)
    results = ss.install(home=tmp_path)
    assert {r.action for r in results if r.target.endswith(("SKILL.md", "summary.md"))} == {"upgraded"}
    assert (tmp_path / ".claude/skills/summary/SKILL.md").read_bytes().endswith(b"v2\n")


def test_customized_files_are_preserved_on_install_and_upgrade(tmp_path, monkeypatch):
    ss.install(home=tmp_path)
    prompt = tmp_path / ".pi/agent/prompts/summary.md"
    prompt.write_text("my own summary command\n")
    opencode = tmp_path / ".config/opencode/commands/summary.md"
    opencode.write_text("mine too\n")
    src = tmp_path / "src"
    (src / "adapters").mkdir(parents=True)
    for rel in ("SKILL.md", "adapters/pi-summary.md", "adapters/opencode-summary.md"):
        (src / rel).write_bytes((ss.SOURCE / rel).read_bytes() + b"x")
    monkeypatch.setattr(ss, "SOURCE", src)
    results = ss.install(home=tmp_path)
    assert prompt.read_text() == "my own summary command\n"
    assert opencode.read_text() == "mine too\n"
    assert {str(prompt), str(opencode)} <= set(_by_action(results)["preserved"])
    ok, report = ss.verify(home=tmp_path)
    assert "preserved" in {r.action for r in report}


def test_preexisting_user_skill_is_never_replaced(tmp_path):
    user = tmp_path / ".claude/skills/summary"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text("---\nname: summary\n---\nmy skill\n")
    results = ss.install(home=tmp_path)
    assert (user / "SKILL.md").read_text().endswith("my skill\n")
    assert not user.is_symlink()
    assert str(user) in _by_action(results)["preserved"]
    removed = ss.uninstall(home=tmp_path)
    assert (user / "SKILL.md").read_text().endswith("my skill\n")
    assert str(user) in _by_action(removed)["preserved"]


def test_foreign_symlink_survives_install_and_uninstall(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / ".codex/skills/summary"
    link.parent.mkdir(parents=True)
    link.symlink_to(elsewhere)
    ss.install(home=tmp_path)
    ss.uninstall(home=tmp_path)
    assert link.is_symlink() and os.readlink(link) == str(elsewhere)


def test_uninstall_removes_only_what_was_installed(tmp_path):
    keep = tmp_path / ".claude/skills/other/SKILL.md"
    keep.parent.mkdir(parents=True)
    keep.write_text("x")
    ss.install(home=tmp_path)
    ss.uninstall(home=tmp_path)
    assert keep.read_text() == "x"
    assert not any("summary" in str(p) for p in tmp_path.rglob("*") if p.is_file() or p.is_symlink())


def test_uninstall_keeps_modified_files(tmp_path):
    ss.install(home=tmp_path)
    prompt = tmp_path / ".pi/agent/prompts/summary.md"
    prompt.write_text("edited\n")
    results = ss.uninstall(home=tmp_path)
    assert prompt.read_text() == "edited\n"
    assert str(prompt) in _by_action(results)["preserved"]


def test_project_scope_uses_agents_dir_and_leaves_home_alone(tmp_path):
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir(); proj.mkdir()
    ss.install(scope="project", project=proj)
    assert (proj / ".agents/skills/summary/SKILL.md").is_file()
    assert (proj / ".claude/skills/summary/SKILL.md").is_file()  # relative link resolves
    assert not os.path.isabs(os.readlink(proj / ".claude/skills/summary"))
    assert (proj / ".pi/prompts/summary.md").is_file()
    assert (proj / ".opencode/commands/summary.md").is_file()
    ok, _ = ss.verify(scope="project", project=proj)
    assert ok
    ss.uninstall(scope="project", project=proj)
    assert not (proj / ".agents/skills/summary").exists()


def test_dry_run_writes_nothing(tmp_path):
    results = ss.install(home=tmp_path, dry_run=True)
    assert {r.action for r in results} == {"installed"}
    assert list(tmp_path.rglob("*")) == []


def test_opencode_singular_command_dir_is_reused(tmp_path):
    (tmp_path / ".config/opencode/command").mkdir(parents=True)
    ss.install(home=tmp_path)
    assert (tmp_path / ".config/opencode/command/summary.md").is_file()
    assert not (tmp_path / ".config/opencode/commands").exists()


def test_verify_reports_missing_and_stale(tmp_path):
    ok, results = ss.verify(home=tmp_path)
    assert not ok and results[0].action == "missing"


# --- compatibility of the canonical sources ---------------------------------

def test_skill_frontmatter_and_sections_match_the_contract():
    text = SKILL.read_text()
    front = text.split("---")[1]
    assert re.search(r"^name: summary$", front, re.M)
    assert re.search(r"^description: .{40,}", front, re.M)
    for n, title in enumerate(["Original objective", "Starting point", "Timeline of work", "Changes completed",
                               "Decisions and rationale", "Verification and results",
                               "Outstanding work and blockers", "Current worktree state",
                               "Prioritized next actions", "Resume point"], 1):
        assert f"{n}. **{title}**" in text
    assert "Evidence coverage" in text and "reported but unverified" in text


def test_skill_is_read_only_by_construction():
    text = SKILL.read_text()
    assert "git --no-optional-locks" in text
    assert "never modify files" in text
    bad = re.findall(r"`(git (?:add|commit|checkout|reset|stash|push)[^`]*)`", text)
    assert bad == []


def test_adapters_defer_to_the_single_skill_and_are_read_only():
    for name in ("pi-summary.md", "opencode-summary.md"):
        body = (ss.SOURCE / "adapters" / name).read_text()
        assert body.startswith("---\ndescription:")
        assert "`summary` skill" in body and "Do not modify" in body
        assert len(body) < 800  # no duplicate skill logic


def test_cli_install_verify_uninstall_round_trip(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(Path(ss.__file__).parents[1]))
    def run(*a):
        return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "summary", *a, "--home", str(tmp_path)],
                              capture_output=True, text=True, env=env)
    assert run("verify").returncode == 1
    assert run("install").returncode == 0
    assert run("verify").returncode == 0
    assert run("uninstall").returncode == 0
    assert run("verify").returncode == 1


def test_installed_wheel_carries_the_skill_and_installs(tmp_path):
    """A pip-installed (non-editable) herdr-eng must not depend on the repo checkout."""
    repo = Path(ss.__file__).resolve().parents[1]
    target = tmp_path / "site"
    import shutil
    py = next((c for c in (sys.executable, shutil.which("python3"), "/usr/bin/python3") if c and subprocess.run(
        [c, "-c", "import setuptools"], capture_output=True).returncode == 0), None)
    if py is None:
        pytest.skip("no interpreter with setuptools to build a wheel")
    build = subprocess.run(
        [py, "-m", "pip", "install", "--quiet", "--no-deps", "--no-build-isolation",
         "--no-cache-dir", "--target", str(target), str(repo)],
        capture_output=True, text=True, cwd=str(tmp_path))
    if build.returncode != 0:
        pytest.skip(f"cannot build a wheel offline here: {build.stderr[-200:]}")
    home = tmp_path / "home"
    home.mkdir()
    env = {"PYTHONPATH": str(target), "PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    def run(*a):
        return subprocess.run([py, "-I", "-c",
                               "import sys; sys.path.insert(0, %r); from herdr_engineering import cli; "
                               "raise SystemExit(cli.main(sys.argv[1:]))" % str(target), "summary", *a,
                               "--home", str(home)], capture_output=True, text=True, cwd=str(tmp_path), env=env)
    first = run("install")
    assert first.returncode == 0, first.stderr
    assert run("verify").returncode == 0
    assert (target / "herdr_engineering/skill_data/summary/SKILL.md").is_file()
    assert (target / "herdr_engineering/skill_data/summary/adapters/pi-summary.md").is_file()
    assert (home / ".claude/skills/summary/SKILL.md").read_bytes() == SKILL.read_bytes()
