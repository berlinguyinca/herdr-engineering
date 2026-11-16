"""Tests for the structural gates (scripts/ci/structural_gates.py).

The gate exists because specific failures reached `main` and nothing caught them, so these tests reproduce those failures rather
than restating the implementation. Each mutation test is the historical incident, not a synthetic one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import structural_gates as sg  # noqa: E402

LOCK = "lock/upstreams.yaml"
PIN = "herdr_engineering/memory_install.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A miniature checkout: the gate reads `ROOT` at call time, so pointing it here is enough."""
    (tmp_path / "lock").mkdir()
    (tmp_path / "herdr_engineering").mkdir()
    (tmp_path / "lock" / "upstreams.yaml").write_text(
        "version: 1\ndependencies:\n"
        "  herdr-memory:\n"
        "    source: \"berlinguyinca/herdr-memory\"\n"
        "    revision: \"abc1234abc1234\"\n"
        "    decision: ADOPT\n"
        "  herdr-browser:\n"
        "    source: \"someone/herdr-browser\"\n"
        "    revision: null\n"
        "    decision: ADAPT\n"
    )
    (tmp_path / PIN).write_text('PINNED_REVISION = "abc1234abc1234"\nX = 1\n')
    (tmp_path / "clean.json").write_text('{"ok": true}\n')
    monkeypatch.setattr(sg, "ROOT", tmp_path)
    return tmp_path


def files_of(root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_file()]


# ------------------------------------------------------------------ the real repository ---------------------------------------------

def test_the_shipped_repository_passes():
    """The gate is green on what is actually committed - a gate that is red on day one gets ignored."""
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "ci" / "structural_gates.py")],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STRUCTURAL GATES PASSED" in result.stdout


def test_every_tracked_data_file_is_actually_parsed():
    """A gate that silently skips the file class that broke would pass the incident it was written for."""
    tracked = sg.tracked_files()
    for suffix in sg.YAML_SUFFIXES + sg.JSON_SUFFIXES + sg.TOML_SUFFIXES:
        assert any(f.endswith(suffix) for f in tracked), f"no tracked {suffix} file was checked"
    assert LOCK in tracked
    assert not sg.PARSE_ALLOWLIST, "an exclusion must name the reason it stopped being true"


# ------------------------------------------------------------------ the incidents it exists for ---------------------------------------

def test_a_stray_ref_line_in_the_lock_fails_the_parse_check(repo):
    """`a4a1fe45` landed `lock/upstreams.yaml` with a bare ` origin/main` line: valid-looking text, invalid YAML.

    This is the regression that started it - it sat on `main` for hours because no pipeline runs on a push to `main`, and it
    reddened unrelated PRs. The parse check catches it because the file stopped being YAML, not because a line looked odd.
    """
    lock = repo / LOCK
    original = lock.read_text()
    lock.write_text(original.replace("    decision: ADOPT", " origin/main\n    decision: ADOPT", 1))
    failures = sg.check_parses(files_of(repo))
    assert any(LOCK in f and "does not parse" in f for f in failures), failures


def test_a_partial_pin_bump_is_named_explicitly(repo):
    """Bumping `PINNED_REVISION` without the lock (or the reverse) must say which side drifted."""
    (repo / PIN).write_text('PINNED_REVISION = "ffff0000ffff0000"\n')
    failures = sg.check_lock_and_pin_agree(files_of(repo))
    assert len(failures) == 1 and "pin drift" in failures[0], failures
    assert "ffff0000ffff0000" in failures[0] and "abc1234abc1234" in failures[0]


def test_an_unresolved_conflict_marker_is_found(repo):
    (repo / "clean.json").write_text('{\n<<<<<<< HEAD\n  "a": 1\n=======\n  "a": 2\n>>>>>>> other\n}\n')
    failures = sg.check_no_merge_artifacts(files_of(repo))
    assert failures and "clean.json" in failures[0] and "conflict marker" in failures[0]


def test_a_markdown_heading_underline_is_not_a_conflict_marker(repo):
    """`=======` under a heading is ordinary Markdown; matching it would make the gate cry wolf."""
    (repo / "notes.md").write_text("Title\n=======\n\nbody\n")
    assert sg.check_no_merge_artifacts(files_of(repo)) == []


# ------------------------------------------------------------------ lock structure invariants ---------------------------------------

def test_an_explicitly_unpinned_dependency_is_not_a_failure(repo):
    """`revision: null` is a decision, not an omission - several dependencies are tracked by source only."""
    assert sg.check_lock_and_pin_agree(files_of(repo)) == []


def test_a_dependency_without_source_is_reported(repo):
    lock = repo / LOCK
    lock.write_text(lock.read_text().replace('    source: "someone/herdr-browser"\n', ''))
    failures = sg.check_lock_and_pin_agree(files_of(repo))
    assert any("herdr-browser" in f and "source" in f for f in failures), failures


def test_a_malformed_revision_is_reported(repo):
    lock = repo / LOCK
    lock.write_text(lock.read_text().replace('revision: "abc1234abc1234"', 'revision: "not a commit!!"'))
    failures = sg.check_lock_and_pin_agree(files_of(repo))
    assert any("not a commit" in f for f in failures), failures


def test_a_broken_lock_reports_once_not_twice_per_check(repo):
    """When the lock will not parse, both checks must say so rather than raising out from under the run."""
    (repo / LOCK).write_text("dependencies: [\n  unbalanced\n")
    assert any("does not parse" in f for f in sg.check_parses(files_of(repo)))
    assert any("does not parse" in f for f in sg.check_lock_and_pin_agree(files_of(repo)))


def test_a_broken_json_file_is_reported_with_its_path(repo):
    (repo / "clean.json").write_text('{"trailing": 1,}\n')
    failures = sg.check_parses(files_of(repo))
    assert any("clean.json" in f for f in failures), failures


def test_main_reports_one_exit_code_for_all_failures(repo, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["structural_gates.py"])
    (repo / LOCK).write_text("dependencies: [\n  unbalanced\n")
    assert sg.main(files=files_of(repo)) == 1
    printed = capsys.readouterr().out
    assert printed.count("FAIL") >= 2, "every failing check must be reported, not just the first"


def test_a_missing_pyyaml_is_exit_2_not_a_traceback(repo, monkeypatch, capsys):
    """The gate runs before the project venv exists; a missing dependency is an operator problem, not a code failure."""
    monkeypatch.setitem(sys.modules, "yaml", None)  # `import yaml` now raises ImportError
    assert sg.main() == 2
    assert "pyyaml" in capsys.readouterr().err


def test_a_clean_repository_exits_zero(repo, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["structural_gates.py"])
    assert sg.main(files=files_of(repo)) == 0


def test_main_without_a_file_listing_asks_git():
    """The default path is what CI uses, so it must be exercised rather than only the injected one."""
    assert sg.tracked_files(), "git ls-files returned nothing inside the real repository"
