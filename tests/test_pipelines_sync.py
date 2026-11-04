"""deploy/control-plane/pipelines-sync.sh against a local bare repository as origin
(nothing is fetched from the network)."""
import fcntl
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "deploy" / "control-plane" / "pipelines-sync.sh"
VALIDATOR = ROOT / "herdr_engineering" / "control_plane" / "deployment_definitions.py"


def _definition(component, order=0, host=None):
    return {
        "component": component, "stage": 1, "order": order, "host": host or component.split("-")[0],
        "updater": "deploy/hive/iw-node-pull-deploy",
        "follows": {"repo": "InferWeave/inferweave", "tag": "node-ci-latest", "asset": "inferweave",
                    "branch": "main", "ci_context": "ci/woodpecker/push/rust"},
        "restart": "gateway-cycle", "gpu": False,
        "health": {"probe": "served-request", "timeout_s": 600, "hold_s": 600},
        "rollback": {"strategy": "previous-version"},
    }


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")).stdout


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(origin, "config", "uploadpack.allowFilter", "true")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@example.test")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-q", "-b", "main")
    home = tmp_path / "home"
    home.mkdir()
    out = home / ".local/share/herdr-engineering/pipelines"
    env = dict(os.environ, HOME=str(home), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")

    def commit(files, remove=()):
        pipes = work / "deploy" / "pipelines"
        pipes.mkdir(parents=True, exist_ok=True)
        for name in remove:
            (pipes / name).unlink()
        for name, body in files.items():
            p = pipes / name
            if isinstance(body, tuple) and body[0] == "symlink":
                p.symlink_to(body[1])
            elif isinstance(body, tuple) and body[0] == "dir":
                p.mkdir()
                (p / "nested.yaml").write_text(yaml.safe_dump(_definition("nested-x")))
            else:
                p.write_text(body if isinstance(body, str) else yaml.safe_dump(body))
        (work / "README").write_text("unrelated\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "update", "--allow-empty")
        _git(work, "push", "-q", "origin", "HEAD:main")
        return _git(work, "rev-parse", "HEAD").strip()

    def sync(*extra):
        return subprocess.run(
            ["bash", str(SYNC), "once", "--remote", f"file://{origin}", "--dir", str(out),
             "--clone", str(home / "clone.git"), "--validator", str(VALIDATOR), *extra],
            env=env, capture_output=True, text=True, timeout=120)

    return {"commit": commit, "sync": sync, "out": out, "home": home}


def _names(d):
    return sorted(p.name for p in d.iterdir())


def test_a_valid_set_is_installed_and_only_yaml_files_of_deploy_pipelines(repo):
    sha = repo["commit"]({"hive-gateway.yaml": _definition("hive-gateway"),
                          "farm-gateway.yaml": _definition("farm-gateway"),
                          "validate.py": "print('not a definition')\n",
                          "notes.md": "x\n",
                          "linked.yaml": ("symlink", "hive-gateway.yaml"),
                          "sub": ("dir",)})
    r = repo["sync"]()
    assert r.returncode == 0, r.stderr
    out = repo["out"]
    assert _names(out) == ["farm-gateway.yaml", "hive-gateway.yaml"]
    assert (out.stat().st_mode & 0o777) == 0o755
    assert all((p.stat().st_mode & 0o777) == 0o644 and not p.is_symlink() for p in out.iterdir())
    assert yaml.safe_load((out / "hive-gateway.yaml").read_text())["component"] == "hive-gateway"
    assert (out.parent / "pipelines.commit").read_text().strip() == sha
    assert not list(out.parent.glob(".pipelines-staging.*"))       # staging removed
    # herdr's loader accepts exactly what was installed
    from herdr_engineering.control_plane.deployment_definitions import load_definitions
    defs, errs = load_definitions(str(out))
    assert errs == [] and sorted(defs) == ["farm-gateway", "hive-gateway"]


def test_a_newer_set_replaces_changed_and_removes_stale_files_keeping_unchanged_inodes(repo):
    repo["commit"]({"hive-gateway.yaml": _definition("hive-gateway"),
                    "farm-gateway.yaml": _definition("farm-gateway")})
    assert repo["sync"]().returncode == 0
    out = repo["out"]
    dir_inode = out.stat().st_ino
    hive_inode = (out / "hive-gateway.yaml").stat().st_ino
    repo["commit"]({"whiteale-gateway.yaml": _definition("whiteale-gateway")}, remove=["farm-gateway.yaml"])
    r = repo["sync"]()
    assert r.returncode == 0, r.stderr
    assert "1 changed, 1 removed" in r.stdout
    assert _names(out) == ["hive-gateway.yaml", "whiteale-gateway.yaml"]
    assert out.stat().st_ino == dir_inode                  # the bind-mounted dir itself is never replaced
    assert (out / "hive-gateway.yaml").stat().st_ino == hive_inode


@pytest.mark.parametrize("bad", [
    {"farm-gateway.yaml": "component: [unterminated\n"},                       # unparseable
    {"farm-gateway.yaml": {**_definition("farm-gateway"), "restart": "reboot"}},  # invalid value
    {"farm-gateway.yaml": "x: " + "a" * 70000 + "\n"},                         # over the size cap
])
def test_an_invalid_set_is_rejected_and_the_previous_good_set_kept(repo, bad):
    good = repo["commit"]({"hive-gateway.yaml": _definition("hive-gateway")})
    assert repo["sync"]().returncode == 0
    out = repo["out"]
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    repo["commit"](bad)
    r = repo["sync"]()
    assert r.returncode != 0
    assert "keeping the previous definitions" in r.stderr
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert (out.parent / "pipelines.commit").read_text().strip() == good
    assert not list(out.parent.glob(".pipelines-staging.*"))


def test_an_empty_set_is_rejected(repo):
    repo["commit"]({"hive-gateway.yaml": _definition("hive-gateway")})
    assert repo["sync"]().returncode == 0
    repo["commit"]({}, remove=["hive-gateway.yaml"])
    r = repo["sync"]()
    assert r.returncode != 0 and "no *.yaml" in r.stderr
    assert _names(repo["out"]) == ["hive-gateway.yaml"]


def test_an_unreachable_origin_keeps_the_previous_set(repo):
    repo["commit"]({"hive-gateway.yaml": _definition("hive-gateway")})
    assert repo["sync"]().returncode == 0
    r = repo["sync"]("--remote", f"file://{repo['home']}/missing.git")
    assert r.returncode != 0 and "keeping the previous definitions" in r.stderr
    assert _names(repo["out"]) == ["hive-gateway.yaml"]


def test_a_running_sync_holds_the_lock_and_a_second_run_does_nothing(repo):
    repo["commit"]({"hive-gateway.yaml": _definition("hive-gateway")})
    out = repo["out"]
    out.parent.mkdir(parents=True)
    with open(f"{out}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = repo["sync"]()
    assert r.returncode == 0 and "another pipelines-sync is running" in r.stderr
    assert not out.exists() or _names(out) == []
    assert repo["sync"]().returncode == 0                 # lock released: the next run works
    assert _names(out) == ["hive-gateway.yaml"]


@pytest.mark.parametrize("fault", ["symlink_dir", "foreign_entry"])
def test_an_unexpected_target_is_refused_and_left_alone(repo, fault, tmp_path):
    repo["commit"]({"hive-gateway.yaml": _definition("hive-gateway")})
    out = repo["out"]
    out.parent.mkdir(parents=True)
    if fault == "symlink_dir":
        real = tmp_path / "real"
        real.mkdir()
        out.symlink_to(real)
    else:
        out.mkdir()
        (out / "keep-me.txt").write_text("operator file\n")
    r = repo["sync"]()
    assert r.returncode != 0
    if fault == "symlink_dir":
        assert "must be a real directory" in r.stderr and list((tmp_path / "real").iterdir()) == []
    else:
        assert "not regular *.yaml" in r.stderr and _names(out) == ["keep-me.txt"]


def test_install_writes_stable_copies_and_a_five_minute_timer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    (bindir / "systemctl").write_text(f"#!/usr/bin/env bash\necho \"$*\" >> {log}\n")
    (bindir / "systemctl").chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
    r = subprocess.run(["bash", str(SYNC), "install"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    share = home / ".local/share/herdr-engineering"
    assert (share / "pipelines-sync.sh").read_bytes() == SYNC.read_bytes()
    assert (share / "pipelines-validator/deployment_definitions.py").read_bytes() == VALIDATOR.read_bytes()
    units = home / ".config/systemd/user"
    service = (units / "herdr-pipelines-sync.service").read_text()
    timer = (units / "herdr-pipelines-sync.timer").read_text()
    assert '--remote "git@github.com:InferWeave/inferweave.git"' in service
    assert f'--dir "{share}/pipelines"' in service and "Environment" not in service
    assert "OnUnitActiveSec=5min" in timer
    assert "enable --now herdr-pipelines-sync.timer" in log.read_text()
    assert not (share / "pipelines").exists()             # install never fetches


def test_script_is_shellcheck_clean():
    assert subprocess.run(["bash", "-n", str(SYNC)]).returncode == 0
    if subprocess.run(["which", "shellcheck"], capture_output=True).returncode == 0:
        r = subprocess.run(["shellcheck", str(SYNC)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
