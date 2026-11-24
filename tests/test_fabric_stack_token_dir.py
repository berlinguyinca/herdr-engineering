"""scripts/fabric-stack.sh up creates and checks the fabric token mount source before compose runs, so docker never
creates it root-owned 0755 (which a later `[ ! -e ]` guard would skip forever)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GNU = "/opt/homebrew/opt/coreutils/libexec/gnubin"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/fabric-stack.sh", tmp_path / "scripts/fabric-stack.sh")
    shutil.copytree(ROOT / "deploy/fabric-stack", tmp_path / "deploy/fabric-stack", ignore=shutil.ignore_patterns("generated"))
    (tmp_path / "deploy/control-plane").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(f'#!/bin/sh\necho "docker $*" >> "{tmp_path}/docker.log"\nexit 0\n')
    (bindir / "tailscale").write_text("#!/bin/sh\necho 100.64.0.9\n")
    for n in ("docker", "tailscale"):
        (bindir / n).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return tmp_path, bindir, home


def up(tree):
    tmp, bindir, home = tree
    return subprocess.run(["bash", str(tmp / "scripts/fabric-stack.sh"), "up"], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, HOME=str(home), PATH=f"{bindir}:{GNU}:{os.environ['PATH']}"))


def test_up_creates_the_token_dir_0700_before_compose(tree):
    tmp, _, home = tree
    d = home / ".config/herdr-engineering/fabric-gateway"
    r = up(tree)
    assert r.returncode == 0, r.stderr
    assert d.is_dir() and (d.stat().st_mode & 0o777) == 0o700
    assert "compose" in (tmp / "docker.log").read_text()


@pytest.mark.parametrize("bad", ["wide", "file", "symlink"])
def test_up_refuses_a_token_dir_that_is_not_a_private_real_directory(tree, bad):
    tmp, _, home = tree
    d = home / ".config/herdr-engineering/fabric-gateway"
    d.parent.mkdir(parents=True)
    if bad == "wide":
        d.mkdir(mode=0o755)
        d.chmod(0o755)
    elif bad == "file":
        d.write_text("x")
    else:
        (tmp / "elsewhere").mkdir()
        d.symlink_to(tmp / "elsewhere")
    r = up(tree)
    assert r.returncode != 0 and "fabric-gateway" in r.stderr
    log = tmp / "docker.log"
    assert not log.exists() or "compose" not in log.read_text()   # nothing was started
