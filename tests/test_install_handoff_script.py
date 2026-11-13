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

import pytest

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "install-handoff.sh"
LOCK = REPO / "lock" / "upstreams.yaml"


def pinned_revision() -> str:
    block = LOCK.read_text().split("herdr-handoff:", 1)[1]
    return re.search(r'revision:\s*"?([0-9a-f]{7,40})"?', block).group(1)


_TMP_HOME: list[str] = []


def tmp_path_home() -> str | None:
    """The home the script may write to - always a temp dir, never the operator's real one."""
    return _TMP_HOME[-1] if _TMP_HOME else None


def run_script(*args, env=None, expect=0):
    base = {"HOME": str(tmp_path_home()) if tmp_path_home() else os.environ["HOME"]}
    result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                            env={**os.environ, **base, **(env or {})}, check=False)
    assert result.returncode == expect, f"exit {result.returncode}\nSTDOUT\n{result.stdout}\nSTDERR\n{result.stderr}"
    return result


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # install-handoff.sh writes under $HOME by default; a test must never reach the operator's real
    # ~/.local/bin or skill directories.
    home = tmp_path / "home"
    home.mkdir()
    _TMP_HOME.append(str(home))
    monkeypatch.setenv("HOME", str(home))
    yield
    _TMP_HOME.pop()


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


def test_a_python_that_refuses_pip_falls_back_to_uv(tmp_path):
    # The macOS hosts this runs on carry a Homebrew python that is externally managed, and keep tools
    # under ~/.local/share/uv/tools; failing the whole install there would be a dead end when uv is the
    # working path.
    bare, sha = _upstream(tmp_path, "revision A")
    python = tmp_path / "python3"
    python.write_text('#!/bin/sh\nif [ "$2" = "pip" ]; then echo "error: externally-managed-environment" >&2; exit 1; fi\n'
                      'exit 0\n')
    python.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "uv %s\\n" "$*" >> "' + str(tmp_path / 'uv.log') + '"\n'
                  'printf "%s\\n" "$@"\n')
    uv.chmod(0o755)
    result = run_script(env={"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha,
                             "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
                             "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes"),
                             "HERDR_HANDOFF_PYTHON": str(python),
                             "PATH": f"{tmp_path}:/usr/bin:/bin",
                             "HERDR_HANDOFF_NO_PATH_LINK": "1"})
    log = (tmp_path / "uv.log").read_text()
    assert "tool install" in log and str(tmp_path / "cache") in log, (log, result.stdout, result.stderr)
    assert "externally-managed-environment" in result.stderr + result.stdout
    assert "herdr-handoff" in result.stdout


def test_no_pip_and_no_uv_stops_instead_of_losing_the_install(tmp_path):
    bare, sha = _upstream(tmp_path, "revision A")
    python = tmp_path / "python-no-pip"
    python.write_text('#!/bin/sh\nif [ "$2" = "pip" ]; then echo "no pip" >&2; exit 1; fi\nexit 0\n')
    python.chmod(0o755)
    result = run_script(env={"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha,
                             "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
                             "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes"),
                             "HERDR_HANDOFF_PYTHON": str(python),
                             "PATH": "/usr/bin:/bin",
                             "HERDR_HANDOFF_NO_PATH_LINK": "1"}, expect=1)
    assert "--skills-only" in result.stderr


def test_the_path_link_never_points_the_entrypoint_at_itself(tmp_path):
    # This ran once against the real ~/.local/bin: the uv tool's shim is already the PATH entry, and
    # `ln -sf` onto itself produced `herdr-handoff -> herdr-handoff`, leaving a working CLI answering
    # "Too many levels of symbolic links".
    home = tmp_path / "home"
    uv_bin = home / ".local" / "share" / "uv" / "tools" / "herdr-handoff" / "bin"
    uv_bin.mkdir(parents=True)
    shim = uv_bin / "herdr-handoff"
    shim.write_text("#!/bin/sh\necho the uv tool shim\n")
    shim.chmod(0o755)
    bare, sha = _upstream(tmp_path, "revision A")
    # The PATH directory *is* the directory the CLI already lives in.
    result = run_script("--skills-only", env={"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha,
                                              "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
                                              "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes"),
                                              "HERDR_LOCAL_BIN": str(uv_bin), "PATH": "/usr/bin:/bin"})
    assert shim.is_file() and not shim.is_symlink(), result.stdout + result.stderr
    assert shim.read_text().startswith("#!/bin/sh\necho the uv tool shim")
    assert "leaving PATH alone" in result.stderr, result.stdout + result.stderr
