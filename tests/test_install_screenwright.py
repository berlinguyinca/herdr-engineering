"""scripts/install-screenwright.sh against a real local git repo standing in for screenwright (no mocks, no network)."""
import os
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-screenwright.sh"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
ENSURE = """#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME/.agents/skills"
ln -sfn "$(cd -P "$(dirname "$0")/.." && pwd)" "$HOME/.agents/skills/screenwright-review"
"""


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit_file(work: Path, name: str, text: str) -> None:
    (work / name).write_text(text)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", f"change {name}")
    git(work, "push", "-q", "origin", "main")


class Upstream:
    """A bare repo plus a working clone to push new commits from."""

    def __init__(self, tmp: Path):
        self.bare = tmp / "screenwright.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        self.work = tmp / "work"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(self.work)], check=True, capture_output=True)
        git(self.work, "checkout", "-q", "-b", "main")
        skill = self.work / "skills" / "screenwright-review" / "scripts"
        skill.mkdir(parents=True)
        (self.work / "skills" / "screenwright-review" / "SKILL.md").write_text("---\nname: screenwright-review\n---\n")
        ensure = skill / "ensure-install.sh"
        ensure.write_text(ENSURE)
        ensure.chmod(0o755)
        commit_file(self.work, "README.md", "one\n")

    def push(self, name: str, text: str) -> None:
        commit_file(self.work, name, text)

    @property
    def url(self) -> str:
        return f"file://{self.bare}"


def run(tmp: Path, up: Upstream, **extra):
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "HERDR_SCREENWRIGHT_URL": up.url,
           "HERDR_SCREENWRIGHT_NPM": "0", **GIT_ENV, **extra}
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    return r, home / ".local/share/herdr-engineering/screenwright"


def final(r) -> dict:
    line = r.stdout.strip().splitlines()[-1]
    m = re.fullmatch(r"screenwright=(\S+) changed=(true|false)", line)
    assert m, r.stdout + r.stderr
    return {"sha": m.group(1), "changed": m.group(2) == "true"}


def test_first_run_clones_and_links_the_skill(tmp_path):
    up = Upstream(tmp_path)
    r, dest = run(tmp_path, up)
    assert r.returncode == 0, r.stderr
    assert git(dest, "rev-parse", "HEAD") == git(up.work, "rev-parse", "HEAD")
    out = final(r)
    assert out["changed"] is True and git(dest, "rev-parse", "HEAD").startswith(out["sha"])
    link = tmp_path / "home/.agents/skills/screenwright-review"
    assert link.is_symlink() and link.resolve() == (dest / "skills/screenwright-review").resolve()


def test_new_upstream_commit_fast_forwards_and_reports_changed(tmp_path):
    up = Upstream(tmp_path)
    run(tmp_path, up)
    up.push("two.txt", "2\n")
    r, dest = run(tmp_path, up)
    assert r.returncode == 0, r.stderr
    assert (dest / "two.txt").exists()
    assert final(r)["changed"] is True
    assert git(dest, "rev-parse", "HEAD") == git(up.work, "rev-parse", "HEAD")


def test_unchanged_rerun_reports_changed_false(tmp_path):
    up = Upstream(tmp_path)
    run(tmp_path, up)
    r, _ = run(tmp_path, up)
    assert r.returncode == 0, r.stderr
    assert final(r)["changed"] is False


def test_dirty_checkout_is_skipped_untouched(tmp_path):
    up = Upstream(tmp_path)
    _, dest = run(tmp_path, up)
    (dest / "README.md").write_text("local edit\n")
    up.push("two.txt", "2\n")
    r, _ = run(tmp_path, up)
    assert r.returncode == 0, r.stderr
    assert "dirty" in r.stderr
    assert (dest / "README.md").read_text() == "local edit\n"
    assert not (dest / "two.txt").exists()
    assert final(r)["changed"] is False


def test_non_main_checkout_is_skipped_untouched(tmp_path):
    up = Upstream(tmp_path)
    _, dest = run(tmp_path, up)
    git(dest, "checkout", "-q", "-b", "feature")
    up.push("two.txt", "2\n")
    r, _ = run(tmp_path, up)
    assert r.returncode == 0, r.stderr
    assert "feature" in r.stderr
    assert git(dest, "branch", "--show-current") == "feature"
    assert not (dest / "two.txt").exists()
    assert final(r)["changed"] is False


def test_opt_out_env_skips_everything(tmp_path):
    up = Upstream(tmp_path)
    r, dest = run(tmp_path, up, HERDR_INSTALL_SCREENWRIGHT="0")
    assert r.returncode == 0, r.stderr
    assert not dest.exists()


def test_unreachable_upstream_warns_and_exits_nonzero_without_creating_checkout(tmp_path):
    up = Upstream(tmp_path)
    r, dest = run(tmp_path, up, HERDR_SCREENWRIGHT_URL=f"file://{tmp_path}/missing.git")
    assert r.returncode != 0
    assert not (dest / ".git").exists()


def test_pin_ref_checks_out_that_commit_detached(tmp_path):
    up = Upstream(tmp_path)
    first = git(up.work, "rev-parse", "HEAD")
    up.push("two.txt", "2\n")
    r, dest = run(tmp_path, up, HERDR_SCREENWRIGHT_REF=first)
    assert r.returncode == 0, r.stderr
    assert git(dest, "rev-parse", "HEAD") == first
