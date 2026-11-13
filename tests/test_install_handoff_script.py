"""`scripts/install-handoff.sh` run for real: the pinned herdr-handoff must actually arrive and update.

herdr-handoff is a required operational dependency, but it was installed from a revision hardcoded in
`scripts/install.sh` while `lock/upstreams.yaml` declared a different one, and its skill installer refused
to move an existing install forward.  Everything here runs the real script against real local git remotes
and real `sh` installers - no package index is touched, because the CLI install step is pointed at a
fixture interpreter that is a real python.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "install-handoff.sh"
LOCK = REPO / "lock" / "upstreams.yaml"


def pinned_revision() -> str:
    block = LOCK.read_text().split("herdr-handoff:", 1)[1]
    return re.search(r'revision:\s*"?([0-9a-f]{7,40})"?', block).group(1)


def run_script(*args, env=None, expect=0):
    result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                            env={**os.environ, **(env or {})}, check=False)
    assert result.returncode == expect, f"exit {result.returncode}\nSTDOUT\n{result.stdout}\nSTDERR\n{result.stderr}"
    return result


def _upstream(tmp_path: Path, revision_marker: str) -> tuple[Path, str]:
    """A real git repo that behaves like herdr-handoff: a skill tree plus a linker installer."""
    source = tmp_path / "upstream"
    for skill in ("herdr-handoff", "herdr"):
        (source / "skill" / skill).mkdir(parents=True)
        (source / "skill" / skill / "SKILL.md").write_text(f"---\nname: {skill}\n---\n{revision_marker}\n")
    (source / "pyproject.toml").write_text('[project]\nname = "herdr-handoff"\nversion = "0.0.0"\n'
                                           'requires-python = ">=3.9"\n')
    (source / "install").mkdir()
    (source / "install" / "install.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        'here="$(dirname -- "$0")/.."\n'
        'echo "linked $(cat "$here/skill/herdr-handoff/SKILL.md")"\n'
    )
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=t@example.com", "-c", "user.name=Test",
                    "commit", "-q", "-m", revision_marker], check=True)
    sha = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(bare)], check=True)
    return bare, sha


# --- the pin has exactly one source of truth -----------------------------------------------------------

def test_the_script_exists():
    assert SCRIPT.is_file(), "scripts/install-handoff.sh is what installs and updates the pinned dependency"


def test_the_plan_resolves_the_revision_declared_in_the_lock(tmp_path):
    # install.sh used to carry its own hardcoded default (3051d8c) while the lock declared 2bbae4d, so a
    # machine could be "installed" two revisions behind the declared one and nothing said so.
    result = run_script("--plan", env={"HERDR_HANDOFF_SKILL_ROOT": str(tmp_path)})
    planned = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
    assert planned["pin"] == pinned_revision()
    assert planned["cache"].endswith(f"herdr-handoff/{pinned_revision()}")
    assert planned["upstream"].endswith("herdr-handoff.git")


def test_an_explicit_override_still_wins(tmp_path):
    result = run_script("--plan", env={"HERDR_HANDOFF_REF": "deadbeef", "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path)})
    assert "pin: deadbeef" in result.stdout


# --- installing a pinned revision from a real remote ----------------------------------------------------

def test_installing_a_pin_clones_it_and_runs_the_upstream_skill_installer(tmp_path):
    bare, sha = _upstream(tmp_path, "revision A")
    skill_root = tmp_path / "harness-homes"
    cache = tmp_path / "cache"
    result = run_script("--skills-only", env={"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha,
                                              "HERDR_HANDOFF_CACHE": str(cache),
                                              "HERDR_HANDOFF_SKILL_ROOT": str(skill_root)})
    assert f"pin: {sha}" in result.stdout
    assert (cache / "skill" / "herdr-handoff" / "SKILL.md").read_text().strip().endswith("revision A")
    assert subprocess.run(["git", "-C", str(cache), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip() == sha
    assert "revision A" in result.stdout       # the upstream installer's own report


def test_re_running_the_same_revision_is_a_no_op(tmp_path):
    bare, sha = _upstream(tmp_path, "revision A")
    env = {"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha, "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
           "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes")}
    run_script("--skills-only", env=env)
    again = run_script("--skills-only", env=env)
    assert "already at" in again.stdout


def test_a_new_revision_replaces_the_installed_one(tmp_path):
    bare, sha_a = _upstream(tmp_path, "revision A")
    source = tmp_path / "upstream"
    (source / "skill" / "herdr-handoff" / "SKILL.md").write_text("---\nname: herdr-handoff\n---\n"
                                                                "revision B: takeover\n")
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=t@example.com", "-c", "user.name=Test",
                    "commit", "-q", "-m", "B"], check=True)
    sha_b = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True,
                           text=True).stdout.strip()
    subprocess.run(["git", "-C", str(bare), "fetch", "-q", str(source),
                    f"+refs/heads/*:refs/heads/*"], check=True)

    env = {"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha_b, "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
           "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes")}
    result = run_script("--skills-only", env=env)
    assert f"pin: {sha_b}" in result.stdout
    assert "takeover" in (tmp_path / "cache" / "skill" / "herdr-handoff" / "SKILL.md").read_text()


def test_an_unreachable_revision_fails_loudly(tmp_path):
    bare, _ = _upstream(tmp_path, "revision A")
    result = run_script("--skills-only", expect=1, env={"HERDR_HANDOFF_URL": str(bare),
                                                        "HERDR_HANDOFF_REF": "ffffffffffffffffffffffffffffffffffffffff",
                                                        "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
                                                        "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes")})
    assert "ffffffff" in result.stderr
