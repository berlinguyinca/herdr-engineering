from pathlib import Path

from herdr_engineering.handoff_dependency import check_handoff_dependency


def test_handoff_dependency_accepts_all_four_skill_markers(tmp_path: Path):
    root = tmp_path / "skills"
    for harness in ("codex", "claude", "pi", "opencode"):
        for skill in ("herdr", "herdr-handoff", "herdr-artifact"):
            (root / harness / skill).mkdir(parents=True)
            (root / harness / skill / "SKILL.md").write_text(f"---\nname: {skill}\n---\n")
    result = check_handoff_dependency(skill_root=root, executable="/bin/true")
    assert result.status == "ok"
    assert result.missing_harnesses == []


def test_strict_dependency_fails_when_cli_or_skill_is_missing(tmp_path: Path):
    result = check_handoff_dependency(skill_root=tmp_path / "missing", executable=None)
    assert result.status == "fail"
    assert "herdr-handoff" in result.detail
