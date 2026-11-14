"""`scripts/install-handoff.sh` run for real: the pinned herdr-handoff must actually arrive and update.

herdr-handoff is a required operational dependency, but its revision was declared twice - a literal in
`scripts/install.sh` and the entry in `lock/upstreams.yaml` - with nothing enforcing agreement, its skill
installer refused to move an existing install forward, and `herdr-eng doctor` only proved files existed, so
a host could sit on a superseded revision and still pass.  Everything here runs the real script against real
local git remotes and real `sh` installers; the CLI install step is pointed at fixture interpreters that are
real `sh` programs, because a test must not install a CLI into the environment it runs in.
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
        'here="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"\n'
        'if [ -n "${HERDR_HANDOFF_SKILL_ROOT:-}" ]; then root="$HERDR_HANDOFF_SKILL_ROOT";\n'
        '  codex_dir="$root/codex/skills"; claude_dir="$root/claude/skills"\n'
        '  pi_dir="$root/pi/skills"; opencode_dir="$root/opencode/skills"\n'
        'else\n'
        '  codex_dir="$HOME/.codex/skills"; claude_dir="$HOME/.claude/skills"\n'
        '  pi_dir="$HOME/.pi/agent/skills"; opencode_dir="$HOME/.config/opencode/skills"\n'
        'fi\n'
        'for dir in "$codex_dir" "$claude_dir" "$pi_dir" "$opencode_dir"; do\n'
        '  for skill in herdr herdr-handoff; do\n'
        '    mkdir -p "$dir"\n'
        '    if [ -L "$dir/$skill" ] && [ "$(readlink "$dir/$skill")" = "$here/skill/$skill" ]; then continue; fi\n'
        '    if [ -e "$dir/$skill" ] || [ -L "$dir/$skill" ]; then\n'
        '      echo "collision: leaving existing $dir/$skill unchanged" >&2; continue\n'
        '    fi\n'
        '    ln -s "$here/skill/$skill" "$dir/$skill"\n'
        '    echo "installed $dir/$skill"\n'
        '  done\n'
        'done\n'
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
    # Two places used to declare the same revision - a literal in scripts/install.sh and the entry in
    # lock/upstreams.yaml - with nothing enforcing that they agreed, so bumping the pin in the lock changed
    # nothing about what install.sh installed.  Only the lock is read now.
    result = run_script("--plan", env={"HERDR_HANDOFF_SKILL_ROOT": str(tmp_path)})
    planned = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
    assert planned["pin"] == pinned_revision()
    assert planned["cache"].endswith(f"herdr-handoff/{pinned_revision()}")
    assert planned["upstream"].endswith("herdr-handoff.git")
    # --plan names what it would touch before touching it, including where the skills and the entrypoint
    # would go; a dry run that hid that was not reviewable.
    assert "path_link: " in result.stdout
    assert str(tmp_path) in result.stdout          # the skill root override, listed per directory


def test_no_script_carries_a_literal_handoff_revision():
    # The defect this replaced was structural: install.sh declared `HERDR_HANDOFF_REF:-c425453` while
    # lock/upstreams.yaml declared the same revision by hand, so bumping the pin in the lock changed the
    # reported status of every host and none of what they installed.
    install = (REPO / "scripts" / "install.sh").read_text()
    assert not re.search(r"HERDR_HANDOFF_REF[=:][\"']?[0-9a-f]{7,40}", install), install
    assert "scripts/install-handoff.sh" in install, "install.sh must delegate the handoff install"
    # Step 7 links the same entrypoint, and `ln -sf` there would destroy a hand-written file the same way
    # step 3's script now refuses to.
    assert "does not own" in install, install


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
    assert "revision A" in (cache / "skill" / "herdr-handoff" / "SKILL.md").read_text()
    # The point of running the upstream linker instead of reimplementing it is that the skills really land
    # in all four harnesses, as links into the pinned tree, so a later revision moves them.
    for harness in ("codex", "claude", "pi", "opencode"):
        link = skill_root / harness / "skills" / "herdr-handoff"
        assert link.is_symlink(), (str(link), result.stdout, result.stderr)
        assert os.readlink(str(link)) == str(cache / "skill" / "herdr-handoff")


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
                    "+refs/heads/*:refs/heads/*"], check=True)

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
    # This ran once against the real ~/.local/bin: the uv tool's shim was already the PATH entry, and
    # `ln -sf` onto itself produced `herdr-handoff -> herdr-handoff`, leaving a working CLI answering
    # "Too many levels of symbolic links".
    uv_bin = tmp_path / "home" / ".local" / "share" / "uv" / "tools" / "herdr-handoff" / "bin"
    uv_bin.mkdir(parents=True)
    shim = uv_bin / "herdr-handoff"
    shim.write_text("#!/bin/sh\necho 0.0.0\n")        # the version the fake upstream declares
    shim.chmod(0o755)
    python = _fake_python(tmp_path, "exit 0\n")        # pip "succeeds"; the entrypoint is already in place
    bare, sha = _upstream(tmp_path, "revision A")
    result = run_script(env={"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha,
                             "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
                             "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes"),
                             "HERDR_HANDOFF_PYTHON": str(python),
                             "HERDR_LOCAL_BIN": str(uv_bin), "PATH": "/usr/bin:/bin"})
    assert shim.is_file() and not shim.is_symlink(), result.stdout + result.stderr
    assert shim.read_text() == "#!/bin/sh\necho 0.0.0\n", result.stdout + result.stderr
    assert "already reports 0.0.0" in result.stdout, result.stdout + result.stderr
    assert f"attested: {uv_bin / 'herdr-handoff'} reports 0.0.0" in result.stdout, result.stdout


def _fake_python(tmp_path, body):
    """A fixture interpreter: a real `sh` program that behaves like pip when asked to install."""
    python = tmp_path / "python3"
    python.write_text(f"#!/bin/sh\n{body}")
    python.chmod(0o755)
    return python


def _pip_that_lands_next_to_itself():
    # pip installs into <interpreter dir>/herdr-handoff; the fixture does the same so the entrypoint the
    # script looks for really exists.
    return ('if [ "$3" = "install" ]; then entry="$(dirname "$0")/herdr-handoff"; '
            'printf \'#!/bin/sh\\necho 0.0.0\\n\' > "$entry"; chmod 755 "$entry"; fi\nexit 0\n')


def test_the_install_refuses_to_overwrite_a_path_entry_it_does_not_own(tmp_path):
    # `ln -sf $candidate $destination` replaced whatever sat at ~/.local/bin/herdr-handoff - a wrapper
    # written by hand, or a link to someone's own checkout - with no message and no backup.  A regular file
    # there is not proof it came from this script, so it is now moved aside only when told to.
    link_dir = tmp_path / "home" / ".local" / "bin"
    link_dir.mkdir(parents=True)
    hand_written = link_dir / "herdr-handoff"
    hand_written.write_text("#!/bin/sh\necho my wrapper\n")
    hand_written.chmod(0o755)
    python = _fake_python(tmp_path, _pip_that_lands_next_to_itself())
    bare, sha = _upstream(tmp_path, "revision A")
    env = {"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha,
           "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
           "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes"),
           "HERDR_HANDOFF_PYTHON": str(python), "HERDR_LOCAL_BIN": str(link_dir),
           "PATH": f"{tmp_path}:/usr/bin:/bin"}
    result = run_script(env=env, expect=2)
    assert hand_written.read_text() == "#!/bin/sh\necho my wrapper\n", result.stdout + result.stderr
    assert "collision" in result.stderr and "--migrate-copies" in result.stderr

    # The caller can authorize it, and what was there is kept under a name saying which revision replaced it.
    run_script("--migrate-copies", env=env)
    backups = list(link_dir.glob(f"herdr-handoff.pre-{sha}*.bak"))
    assert backups and backups[0].read_text() == "#!/bin/sh\necho my wrapper\n", list(link_dir.iterdir())
    assert (link_dir / "herdr-handoff").is_symlink()


def test_the_install_attests_that_the_cli_on_path_is_the_pinned_revision(tmp_path):
    # Without this the script could report success while the entrypoint on PATH stayed the previous
    # revision - which is how both `herdr-eng doctor` and the fleet report went on crediting takeover
    # behaviour to a CLI that does not have it.
    uv_bin = tmp_path / "home" / ".local" / "share" / "uv" / "tools" / "herdr-handoff" / "bin"
    uv_bin.mkdir(parents=True)
    stale = uv_bin / "herdr-handoff"
    stale.write_text("#!/bin/sh\necho 0.2.0\n")        # an older CLI, already on PATH
    stale.chmod(0o755)
    python = _fake_python(tmp_path, "exit 0\n")         # installs nothing
    bare, sha = _upstream(tmp_path, "revision A")       # whose pyproject declares 0.0.0
    result = run_script(env={"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha,
                             "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
                             "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes"),
                             "HERDR_HANDOFF_PYTHON": str(python), "HERDR_LOCAL_BIN": str(uv_bin),
                             "PATH": "/usr/bin:/bin"}, expect=1)
    assert "0.2.0" in result.stderr and "0.0.0" in result.stderr, result.stdout + result.stderr


def test_a_bare_interpreter_name_is_resolved_to_an_absolute_path(tmp_path):
    # `dirname python3` is `.`, so the entrypoint candidate became `./herdr-handoff`, was never found, and
    # the script exited 0 having linked nothing.
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "python3").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "python3").chmod(0o755)
    result = run_script("--plan", env={"PATH": f"{fake_bin}:/usr/bin:/bin",
                                       "HERDR_HANDOFF_PYTHON": "python3",
                                       "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path)})
    assert f"python: {fake_bin / 'python3'}" in result.stdout, result.stdout


def test_a_branch_head_ref_is_refetched_rather_than_reused(tmp_path):
    # The cache used to trust a ref that already resolved locally, and even the re-fetch preferred a stale
    # local branch over FETCH_HEAD, so a branch-head override installed the commit fetched the first time,
    # forever.
    bare, _ = _upstream(tmp_path, "revision A")
    branch = subprocess.run(["git", "-C", str(bare), "symbolic-ref", "--short", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    env = {"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": branch,
           "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
           "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes"),
           "HERDR_HANDOFF_NO_PATH_LINK": "1"}
    run_script("--skills-only", env=env)
    first = _head(tmp_path / "cache")
    source = tmp_path / "upstream"
    (source / "skill" / "herdr-handoff" / "SKILL.md").write_text("---\nname: herdr-handoff\n---\nrevision C\n")
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=t@example.com", "-c", "user.name=Test",
                    "commit", "-q", "-m", "C"], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-q", str(bare), f"HEAD:refs/heads/{branch}"], check=True)
    moved = subprocess.run(["git", "-C", str(bare), "rev-parse", branch], capture_output=True, text=True,
                           check=True).stdout.strip()

    again = run_script("--skills-only", env=env)
    assert _head(tmp_path / "cache") == moved, (first, moved, again.stdout, again.stderr)
    assert first != moved


def _head(path: Path) -> str:
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


# --- the PATH entry is judged by what it reports, not by what it is -------------------------------------

def _env(tmp_path, bare, sha, **extra):
    base = {"HERDR_HANDOFF_URL": str(bare), "HERDR_HANDOFF_REF": sha,
            "HERDR_HANDOFF_CACHE": str(tmp_path / "cache"),
            "HERDR_HANDOFF_SKILL_ROOT": str(tmp_path / "harness-homes"),
            "PATH": f"{tmp_path}:/usr/bin:/bin"}
    return {**base, **extra}


def test_a_cli_pip_installed_into_the_path_directory_is_attested_not_replaced(tmp_path):
    # A pip user install lands a regular file exactly where this script wants its symlink (~/.local/bin on
    # Linux, every run).  Judging the PATH entry by its type refused it - exit 2 - and --migrate-copies,
    # which that error message itself recommended, then moved the freshly installed pinned CLI away and
    # left nothing on PATH.  What is on PATH is now judged by the version it reports.
    link_dir = tmp_path / "home" / ".local" / "bin"
    link_dir.mkdir(parents=True)
    python = _fake_python(tmp_path, 'if [ "$3" = "install" ]; then printf \'#!/bin/sh\\necho 0.0.0\\n\' > "'
                                    + str(link_dir) + '/herdr-handoff"; chmod 755 "' + str(link_dir)
                                    + '/herdr-handoff"; fi\nexit 0\n')
    bare, sha = _upstream(tmp_path, "revision A")
    result = run_script(env=_env(tmp_path, bare, sha, HERDR_HANDOFF_PYTHON=str(python),
                                 HERDR_LOCAL_BIN=str(link_dir)))
    entry = link_dir / "herdr-handoff"
    assert entry.is_file() and not entry.is_symlink(), (result.stdout, result.stderr)
    assert "already reports 0.0.0" in result.stdout, result.stdout
    assert f"attested: {entry} reports 0.0.0 (pinned revision {sha})" in result.stdout
    assert list(link_dir.iterdir()) == [entry], list(link_dir.iterdir())   # nothing moved aside


def test_an_interpreter_behind_a_symlinked_directory_still_links_the_entrypoint(tmp_path):
    # Comparing the entrypoint candidates against the destination with only the parent directory resolved
    # looked fine in tests (pytest's tmp_path is already resolved) and linked nothing on a fresh host,
    # because /tmp and /var are symlinks on macOS: pip's entrypoint was real, the path built from
    # $HERDR_HANDOFF_PYTHON did not resolve to it, and the run ended with "no entrypoint to link".
    real = tmp_path / "real-env"
    real.mkdir()
    python = _fake_python(real, _pip_that_lands_next_to_itself())
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    bare, sha = _upstream(tmp_path, "revision A")
    link_dir = tmp_path / "bin"
    result = run_script(env=_env(tmp_path, bare, sha, HERDR_HANDOFF_PYTHON=str(alias / python.name),
                                 HERDR_LOCAL_BIN=str(link_dir)))
    entry = link_dir / "herdr-handoff"
    assert entry.is_symlink(), (result.stdout, result.stderr)
    assert os.path.realpath(os.readlink(str(entry))) == os.path.realpath(str(real / "herdr-handoff"))
    assert "attested" in result.stdout, result.stdout


def test_a_path_entry_that_is_a_link_to_a_directory_is_never_written_into(tmp_path):
    # `ln -sf` follows a destination that is a symlink to a directory, so the new link was created *inside*
    # the pinned checkout and the PATH entry never changed; the attestation then failed on a file nobody
    # intended to put there.
    bare, sha = _upstream(tmp_path, "revision A")
    cache = tmp_path / "cache"
    python = _fake_python(tmp_path, "exit 0\n")
    env = _env(tmp_path, bare, sha, HERDR_HANDOFF_PYTHON=str(python), HERDR_HANDOFF_NO_PATH_LINK="1")
    run_script("--skills-only", env=env)                     # populate the cache first; clone needs it absent
    inside = cache / "somewhere"
    inside.mkdir()
    link_dir = tmp_path / "home" / ".local" / "bin"
    link_dir.mkdir(parents=True)
    (link_dir / "herdr-handoff").symlink_to(inside, target_is_directory=True)
    before = sorted(p.name for p in inside.iterdir())
    result = run_script(env=_env(tmp_path, bare, sha, HERDR_HANDOFF_PYTHON=str(python),
                                 HERDR_LOCAL_BIN=str(link_dir)), expect=1)
    assert sorted(p.name for p in inside.iterdir()) == before, (result.stdout, result.stderr)
    assert "MISMATCH" in result.stderr or "not executable" in result.stderr, result.stderr
    assert (link_dir / "herdr-handoff").is_symlink()
    assert os.path.realpath(os.readlink(str(link_dir / "herdr-handoff"))) == str(inside)


def test_a_broken_relative_symlink_on_path_is_reported_not_a_raw_cd_error(tmp_path):
    # Resolving a path must not stop the script with a shell's own complaint; the operator needs to be told
    # what sits on PATH and what to do about it.
    link_dir = tmp_path / "home" / ".local" / "bin"
    link_dir.mkdir(parents=True)
    (link_dir / "herdr-handoff").symlink_to("../gone/herdr-handoff")
    python = _fake_python(tmp_path, "exit 0\n")
    bare, sha = _upstream(tmp_path, "revision A")
    result = run_script(env=_env(tmp_path, bare, sha, HERDR_HANDOFF_PYTHON=str(python),
                                 HERDR_LOCAL_BIN=str(link_dir)), expect=2)
    assert "collision" in result.stderr, result.stderr
    assert "cd:" not in result.stderr, result.stderr


def test_a_pinned_tree_with_no_static_version_stops_the_install(tmp_path):
    # The attestation compares the entrypoint against the version the pinned tree declares.  If that cannot
    # be read there is nothing to compare against, and exiting 0 there is the same hole this whole step
    # closes: a stale CLI on PATH, reported as installed.
    bare, sha = _upstream(tmp_path, "revision A")
    source = tmp_path / "upstream"
    (source / "pyproject.toml").write_text('[project]\nname = "herdr-handoff"\ndynamic = ["version"]\n'
                                           'requires-python = ">=3.9"\n')
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.email=t@example.com", "-c", "user.name=Test",
                    "commit", "-q", "-m", "no static version"], check=True)
    subprocess.run(["git", "-C", str(bare), "fetch", "-q", str(source),
                    "+refs/heads/*:refs/heads/*"], check=True)
    new_sha = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD"], capture_output=True,
                             text=True).stdout.strip()
    python = _fake_python(tmp_path, "exit 0\n")
    result = run_script(env=_env(tmp_path, bare, new_sha, HERDR_HANDOFF_PYTHON=str(python),
                                 HERDR_LOCAL_BIN=str(tmp_path / "bin")), expect=1)
    assert "pyproject.toml" in result.stderr and "attest" in result.stderr, result.stderr


def test_the_skill_root_override_uses_the_layout_the_upstream_installer_writes(tmp_path):
    # HERDR_HANDOFF_SKILL_ROOT means the parent of the per-harness directories upstream; reading it as the
    # skills directory itself made --plan name a directory nothing was ever written to and made
    # --migrate-copies scan the wrong place.
    root = tmp_path / "harness-homes"
    result = run_script("--plan", env={"HERDR_HANDOFF_SKILL_ROOT": str(root)})
    for harness in ("codex", "claude", "pi", "opencode"):
        assert f"{root}/{harness}/skills" in result.stdout, result.stdout


def test_migrate_copies_refuses_to_nest_a_backup_inside_an_existing_one(tmp_path):
    # `mv dir existing_dir` puts the copy *inside* the old backup and reports success.  Two revisions of
    # hand-written skill or wrapper text are not something to merge silently.
    link_dir = tmp_path / "home" / ".local" / "bin"
    link_dir.mkdir(parents=True)
    hand_written = link_dir / "herdr-handoff"
    hand_written.write_text("#!/bin/sh\necho my wrapper\n")
    hand_written.chmod(0o755)
    bare, sha = _upstream(tmp_path, "revision A")
    (link_dir / f"herdr-handoff.pre-{sha}.bak").write_text("#!/bin/sh\necho an older backup\n")
    python = _fake_python(tmp_path, _pip_that_lands_next_to_itself())
    result = run_script("--migrate-copies", env=_env(tmp_path, bare, sha, HERDR_HANDOFF_PYTHON=str(python),
                                                     HERDR_LOCAL_BIN=str(link_dir)), expect=1)
    assert "already exists" in result.stderr, result.stderr
    assert hand_written.read_text() == "#!/bin/sh\necho my wrapper\n"
    assert (link_dir / f"herdr-handoff.pre-{sha}.bak").read_text() == "#!/bin/sh\necho an older backup\n"


def test_installing_from_a_local_repo_says_which_revision_it_used(tmp_path):
    # scripts/install.sh used to install herdr-handoff from a sibling checkout when one was present.  The
    # override still does that, but a local checkout is not the pin, so the script must name what it
    # actually installed rather than report "installed" against a revision it never looked at.
    bare, sha = _upstream(tmp_path, "revision A")
    local = tmp_path / "upstream"          # the working checkout behind the bare remote
    python = _fake_python(tmp_path, _pip_that_lands_next_to_itself())
    result = run_script("--skills-only", env=_env(tmp_path, bare, sha, HERDR_HANDOFF_REPO=str(local),
                                                  HERDR_HANDOFF_PYTHON=str(python),
                                                  HERDR_HANDOFF_NO_PATH_LINK="1"))
    assert f"resolved: {sha}" in result.stdout, result.stdout
    assert "warning" not in result.stderr, result.stderr

    # ... and when that checkout is on something else, the run says so instead of printing the pin.
    (local / "skill" / "herdr-handoff" / "SKILL.md").write_text("---\nname: herdr-handoff\n---\nlocal work\n")
    subprocess.run(["git", "-C", str(local), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(local), "-c", "user.email=t@example.com", "-c", "user.name=Test",
                    "commit", "-q", "-m", "local work"], check=True)
    local_sha = subprocess.run(["git", "-C", str(local), "rev-parse", "HEAD"], capture_output=True,
                               text=True).stdout.strip()
    result = run_script("--skills-only", env=_env(tmp_path, bare, sha, HERDR_HANDOFF_REPO=str(local),
                                                  HERDR_HANDOFF_PYTHON=str(python),
                                                  HERDR_HANDOFF_NO_PATH_LINK="1"))
    assert f"resolved: {local_sha}" in result.stdout, result.stdout
    assert "is not the pinned revision" in result.stderr, result.stderr
