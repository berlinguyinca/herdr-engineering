from __future__ import annotations

from herdr_engineering.control_plane.collab_skill_discovery import discover_skills


def test_discover_skills_reads_valid_skill_manifests_and_ignores_invalid_entries(tmp_path):
    root = tmp_path / "skills"
    (root / "review").mkdir(parents=True)
    (root / "review" / "SKILL.md").write_text("---\nname: Review\ndescription: review\n---\n")
    (root / "bad-name").mkdir()
    (root / "bad-name" / "SKILL.md").write_text("---\nname: bad\n---\n")
    (root / ".private").mkdir()
    (root / ".private" / "SKILL.md").write_text("---\nname: private\ndescription: private\n---\n")
    (root / "review.md").write_text("not a directory skill")

    assert discover_skills([root]) == ["skill:review"]


def test_discover_skills_deduplicates_roots_and_caps_results(tmp_path):
    root = tmp_path / "skills"
    for name in ("one", "two", "three"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n")

    assert discover_skills([root, root], max_skills=2) == ["skill:one", "skill:three"]


def test_discover_skills_supports_nested_manifests_and_flat_markdown_skills(tmp_path):
    root = tmp_path / "skills"
    nested = root / "teams" / "release"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\ndescription: release\n---\n")
    (root / "review.md").write_text("---\ndescription: review\n---\n")

    assert discover_skills([root]) == ["skill:release", "skill:review"]
