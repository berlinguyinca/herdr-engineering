"""Installer runs only against explicitly disposable repositories and HOME paths."""
import hashlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy/control-plane/install-deploy-tool.py"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def installation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    tool = repo / "deploy/control-plane/deploy.sh"
    tool.parent.mkdir(parents=True)
    tool.write_text('#!/usr/bin/env bash\nprintf "canonical:%s\\n" "$@"\n')
    tool.chmod(0o755)
    for name in ("deploy-lock.sh", "deploy-state.py"):
        (tool.parent / name).write_text("# clean tool companion\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    ref = git(repo, "rev-parse", "HEAD")
    home = tmp_path / "home"
    home.mkdir()
    legacy = tmp_path / "legacy dirty" / "deploy.sh"
    legacy.parent.mkdir()
    body = b'#!/usr/bin/env bash\n# keep local edits\necho LEGACY-BODY-MUST-NOT-RUN\n'
    legacy.write_bytes(body)
    legacy.chmod(0o750)
    return repo, ref, home, legacy, body


def install(data, *extra):
    repo, ref, home, legacy, _ = data
    return subprocess.run(["python3", str(INSTALLER), "--repo", str(repo), "--ref", ref,
                           "--home", str(home), "--legacy", str(legacy), *extra],
                          text=True, capture_output=True, timeout=20)


def test_installer_routes_canonical_and_preserves_body_backup_and_arguments(installation):
    repo, ref, home, legacy, body = installation
    result = install(installation)
    assert result.returncode == 0, result.stderr
    launcher = home / ".local/bin/herdr-deploy"
    canonical = home / f".local/share/herdr-engineering/deploy-tools/{ref}/deploy/control-plane/deploy.sh"
    assert launcher.resolve() == canonical
    assert git(canonical.parents[2], "rev-parse", "HEAD") == ref
    assert git(canonical.parents[2], "status", "--porcelain") == ""
    assert legacy.read_bytes().endswith(body)
    backups = list(legacy.parent.glob("deploy.sh.pre-herdr-routing-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == body
    assert legacy.stat().st_mode & 0o777 == 0o750
    for entrypoint in (launcher, legacy):
        result = subprocess.run([str(entrypoint), "arg with spaces", "'quoted'"],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "canonical:arg with spaces\ncanonical:'quoted'\n"
    before = legacy.read_bytes()
    assert install(installation).returncode == 0
    assert legacy.read_bytes() == before
    assert list(legacy.parent.glob("deploy.sh.pre-herdr-routing-*.bak")) == backups
    assert hashlib.sha256(canonical.read_bytes()).digest() == hashlib.sha256(
        (repo / "deploy/control-plane/deploy.sh").read_bytes()).digest()
    launcher.unlink()
    result = subprocess.run([str(legacy)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 69
    assert "LEGACY-BODY" not in result.stdout


def test_installer_refuses_to_shim_canonical_or_dirty_existing_tool(installation):
    _, ref, home, legacy, body = installation
    canonical = home / f".local/share/herdr-engineering/deploy-tools/{ref}/deploy/control-plane/deploy.sh"
    result = install(installation, "--legacy", str(canonical))
    assert result.returncode != 0
    assert legacy.read_bytes() == body
    assert not (home / ".local/bin/herdr-deploy").exists()
    assert install(installation).returncode == 0
    canonical.write_text("#!/bin/bash\necho modified\n")
    result = install(installation)
    assert result.returncode != 0
    assert "modified" in canonical.read_text()


def test_installer_refuses_missing_legacy_before_any_installation(installation):
    _, _, home, legacy, body = installation
    result = install(installation, "--legacy", str(legacy.parent / "missing"))
    assert result.returncode != 0
    assert legacy.read_bytes() == body
    assert not (home / ".local").exists()


def test_installer_does_not_run_during_rollout(installation):
    import fcntl

    _, _, home, legacy, body = installation
    state = home / ".local/state/herdr-engineering/deploys"
    state.mkdir(parents=True)
    with (state / "rollout.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = install(installation)
    assert result.returncode == 75
    assert legacy.read_bytes() == body
    assert not (home / ".local/bin/herdr-deploy").exists()
