# ruff: noqa: F811
"""Round 1 review fixes of the size cap that remain: huge targets, bounded and cached discovery, git
semantics, config validation. Real cargo builds, real files and processes; the only constants overridden are
the module's own limits."""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_maintenance as maintenance
from tests.test_build_cleanup_size_cap import (  # noqa: F401  (env is a fixture)
    HUGE,
    SMALL,
    env,
    project,
    sweep,
)


def _old(path: Path, hours=48) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def _fill(target: Path, count: int) -> Path:
    """``count`` small files in ``deps``: far more entries than a lowered limit, old like the rest."""
    deps = target / "debug/deps"
    deps.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (deps / f"f{index}.o").write_bytes(b"x")
        _old(deps / f"f{index}.o")
    _old(deps)
    return deps


# --- H1: a target past the entry limit is still capped, and an undecidable one is reported -------

def test_a_target_past_the_entry_limit_is_still_capped(env, monkeypatch):
    root, state = env
    target = project(root)
    _fill(target, 300)
    monkeypatch.setattr(cleanup, "_MAX_ENTRIES", 150)
    with pytest.raises(OSError, match="limit"):
        cleanup._inspect(target)
    result = sweep(root, state, max_target_bytes=8192)
    assert result["removed"] == [str(target)], result
    assert result["reasons"][str(target)] == "size_cap"


def test_an_undecidable_target_is_a_warning_never_a_silent_ok(env, monkeypatch):
    root, state = env
    target = project(root)
    _fill(target, 300)
    monkeypatch.setattr(cleanup, "_MAX_ENTRIES", 150)
    result = sweep(root, state, max_target_bytes=HUGE)
    assert result["status"] == "warning", result
    assert any(str(target) in warning and "limit" in warning for warning in result["warnings"])
    assert (target / "debug").exists()


def test_a_fresh_file_in_a_huge_target_is_recent_activity(env, monkeypatch):
    root, state = env
    target = project(root)
    deps = _fill(target, 300)
    (deps / "just-built.o").write_bytes(b"y")  # now: creating it also moved the mtime of deps
    monkeypatch.setattr(cleanup, "_MAX_ENTRIES", 150)
    result = sweep(root, state, max_target_bytes=8192)
    assert result["removed_count"] == 0 and (target / "debug").exists()
    assert {"path": str(target), "reason": "recent activity"} in result["skipped"]


# --- M3 / M4: bounded and cached discovery ---------------------------------------------------

def test_the_discovery_limit_keeps_what_was_found(tmp_path, monkeypatch):
    target = project(tmp_path, "p")
    for index in range(10):
        (target.parent / f"d{index}").mkdir()
    monkeypatch.setattr(cleanup, "_MAX_ENTRIES", 3)
    notes: list[str] = []
    found = cleanup._discover(target.parent, notes=notes)
    assert found == [target]
    assert notes and "limit" in notes[0]


def test_home_discovery_skips_library_and_other_roots_inside_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    mine = project(home / "src", "a")
    library = project(home / "Library", "x")
    nested = project(home / "work", "b")
    found = cleanup._discover(home, exclude=frozenset({home / "work"}))
    assert mine in found and library not in found and nested not in found


def _home_with_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home, project(home / "src", "app")


def test_cached_discovery_does_not_walk_again_until_the_refresh_interval(tmp_path, monkeypatch):
    home = tmp_path / "root"
    home.mkdir()
    first = project(home / "src", "app")
    state = tmp_path / "state"
    cleanup.run_once([home], state, max_target_bytes=HUGE, discovery="cached", **{"high_water": 100, "low_water": 0.0})
    cache = state / "build-cleanup-targets.json"
    assert str(first) in cache.read_text()
    second = project(home / "src", "newer")
    result = cleanup.run_once([home], state, max_target_bytes=SMALL, discovery="cached",
                              high_water=100, low_water=0.0)
    assert result["removed"] == [str(first)] and (second / "debug").exists()  # the new one is not known yet
    data = json.loads(cache.read_text())
    for entry in data.values():
        entry["at"] = time.time() - 7 * 3600
    cache.write_text(json.dumps(data))
    result = cleanup.run_once([home], state, max_target_bytes=SMALL, discovery="cached",
                              high_water=100, low_water=0.0)
    assert result["removed"] == [str(second)]


def test_the_disk_guard_relief_never_walks_the_home_directory(tmp_path, monkeypatch):
    from herdr_engineering.disk_guard import relieve
    home, target = _home_with_project(tmp_path, monkeypatch)
    state = tmp_path / "state"
    cfg = {"roots": [str(home)], "max_target_gb": 50, "min_age_hours": 24}
    relieve(cfg, state, 1)
    assert (target / "debug").exists()  # no cache yet: the home root is not walked before a build
    assert not (state / "build-cleanup-targets.json").exists()


def test_default_roots_cover_work_and_ideaprojects_without_duplicates(tmp_path, monkeypatch):
    from herdr_engineering.config import DEFAULTS
    home = tmp_path / "home"
    for name in ("work", "IdeaProjects", ".cache"):
        (home / name).mkdir(parents=True)
    (home / "alias").symlink_to(home / "work")
    monkeypatch.setenv("HOME", str(home))
    assert DEFAULTS["build_cleanup"]["roots"] == ["~", "~/work", "~/IdeaProjects", "~/.cache"]
    roots = maintenance.effective_roots({"roots": ["~", "~/work", "~/alias", "~/IdeaProjects", "~/.cache"]})
    assert roots[:4] == [home, home / "work", home / "IdeaProjects", home / ".cache"]
    assert home / "alias" not in roots


# --- L1: git ---------------------------------------------------------------------------------

def test_a_project_outside_git_inside_a_home_work_tree_is_not_cleaned(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)  # dotfiles kept as ``git init ~``
    monkeypatch.setenv("HOME", str(home))
    target = project(home / "src", "app")
    subprocess.run(["rm", "-rf", str(target.parent / ".git")], check=True)
    assert not cleanup._untracked(target)


def test_inherited_git_variables_do_not_hide_tracked_output(env, monkeypatch, tmp_path):
    root, _ = env
    target = project(root)
    marker = next(target.rglob("invoked.timestamp"))
    subprocess.run(["git", "-C", str(target.parent), "add", "-f", str(marker)], check=True)
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert not cleanup._untracked(target)


# --- L4: configuration -----------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "50GB", float("nan"), float("inf"), -1, True, [1]])
def test_a_bad_max_target_gb_falls_back_to_the_default_with_a_warning(bad, caplog):
    with caplog.at_level("WARNING"):
        kwargs = maintenance.policy_kwargs({"max_target_gb": bad})
    assert kwargs["max_target_bytes"] == 50 * 1024**3
    assert any("max_target_gb" in message for message in caplog.messages)


def test_a_valid_max_target_gb_is_taken_as_is():
    assert maintenance.policy_kwargs({"max_target_gb": 0})["max_target_bytes"] is None
    assert maintenance.policy_kwargs({"max_target_gb": 1.5})["max_target_bytes"] == int(1.5 * 1024**3)


def test_a_numeric_string_max_target_gb_is_accepted():
    """YAML reads ``1e-06`` as a string; a string that is a finite number >= 0 is a number all the same."""
    assert maintenance.policy_kwargs({"max_target_gb": "1e-06"})["max_target_bytes"] == int(1e-06 * 1024**3)
    assert maintenance.policy_kwargs({"max_target_gb": "0"})["max_target_bytes"] is None
