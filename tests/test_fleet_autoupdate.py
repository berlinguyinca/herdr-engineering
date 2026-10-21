"""Unattended updates refuse local work and serialize timer invocations."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / ".gitignore").write_text("ansible/inventory/\n")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "Initial"], check=True, capture_output=True)
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return root


@pytest.mark.parametrize("dirty", ["tracked", "untracked"])
def test_dirty_checkout_refuses_updates(checkout, dirty, capsys):
    from herdr_engineering import fleet_autoupdate

    (checkout / (".gitignore" if dirty == "tracked" else "notes.txt")).write_text("local work")
    with patch.object(fleet_autoupdate, "fleet_update") as update:
        assert fleet_autoupdate.main() == 1
    assert "dirty" in json.loads(capsys.readouterr().out)["error"]
    update.assert_not_called()


def test_non_main_checkout_refuses_updates(checkout, capsys):
    from herdr_engineering import fleet_autoupdate

    subprocess.run(["git", "-C", str(checkout), "checkout", "-b", "feature"], check=True, capture_output=True)
    with patch.object(fleet_autoupdate, "fleet_update") as update:
        assert fleet_autoupdate.main() == 1
    assert "main" in json.loads(capsys.readouterr().out)["error"]
    update.assert_not_called()


def test_success_allows_ignored_inventory_and_reports_result(checkout, capsys):
    from herdr_engineering import fleet_autoupdate

    inventory = checkout / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts.yml").write_text("private inventory")
    result = {"ok": True, "steps": {"fleet_sync": {"rc": 0}}}
    with patch.object(fleet_autoupdate, "fleet_update", return_value=result) as update:
        assert fleet_autoupdate.main() == 0
    update.assert_called_once_with()
    assert json.loads(capsys.readouterr().out) == result


def test_update_failure_has_nonzero_exit(checkout, capsys):
    from herdr_engineering import fleet_autoupdate

    result = {"ok": False, "steps": {"fleet_sync": {"rc": 2}}}
    with patch.object(fleet_autoupdate, "fleet_update", return_value=result):
        assert fleet_autoupdate.main() == 1
    assert json.loads(capsys.readouterr().out) == result


def test_overlapping_timer_is_rejected(checkout, tmp_path, capsys):
    from herdr_engineering import fleet_autoupdate

    lock_path = tmp_path / "state" / "herdr-engineering" / "fleet-auto-update.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(fleet_autoupdate, "fleet_update") as update:
            assert fleet_autoupdate.main() == 1
    assert "running" in json.loads(capsys.readouterr().out)["error"]
    update.assert_not_called()


def test_missing_repository_setting_fails_closed(monkeypatch, capsys):
    from herdr_engineering import fleet_autoupdate

    monkeypatch.delenv("HERDR_ENGINEERING_REPO", raising=False)
    with patch.object(fleet_autoupdate, "fleet_update") as update:
        assert fleet_autoupdate.main() == 1
    assert "HERDR_ENGINEERING_REPO" in json.loads(capsys.readouterr().out)["error"]
    update.assert_not_called()


def test_installer_renders_explicit_checkout_and_interpreter_without_enabling(tmp_path):
    source = Path(__file__).resolve().parents[1]
    root = tmp_path / 'checkout space %n $here "quote"'
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("fleet-auto-update.sh", "herdr-eng-fleet-update.service", "herdr-eng-fleet-update.timer"):
        shutil.copy(source / "scripts" / name, scripts / name)
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config")}
    subprocess.run(["bash", str(scripts / "fleet-auto-update.sh"), "--no-enable"], env=env, check=True)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    service = (unit_dir / "herdr-eng-fleet-update.service").read_text()
    assert 'Environment="HERDR_ENGINEERING_REPO=' in service
    assert '%%n $here \\"quote\\"' in service
    assert '%%n $$here \\"quote\\"/.venv/bin/python" -m herdr_engineering.fleet_autoupdate' in service
    assert 'Environment="PATH=' in service
    timer = (unit_dir / "herdr-eng-fleet-update.timer").read_text()
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=30m" in timer
    if shutil.which("systemd-analyze"):
        validation = subprocess.run(
            ["systemd-analyze", "--user", "verify", str(unit_dir / "herdr-eng-fleet-update.service"),
             str(unit_dir / "herdr-eng-fleet-update.timer")],
            capture_output=True, text=True,
        )
        # Environment-gated: `systemd-analyze --user` needs a user manager it
        # can initialize (bus/runtime dir). On hosts where that is unavailable
        # it fails with "Failed to initialize manager" / "Failed to lookup
        # RuntimeDirectory" even for valid units -- skip the verification
        # there; the rendered-unit assertions above still run.
        stderr = validation.stderr or ""
        if "Failed to initialize manager" in stderr or "Failed to lookup RuntimeDirectory" in stderr:
            pytest.skip("user systemd manager unavailable for systemd-analyze verify")
        assert validation.returncode == 0, stderr
