"""Unattended updates report refused preconditions as skips and serialize invocations."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """An unattended-update test must never record into the developer's real state dir."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


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
def test_dirty_checkout_is_a_reported_skip_not_a_failure(checkout, dirty, capsys):
    """Local work is never touched, and a refused update must not look like a broken timer."""
    from herdr_engineering import fleet_autoupdate

    (checkout / (".gitignore" if dirty == "tracked" else "notes.txt")).write_text("local work")
    with patch.object(fleet_autoupdate, "fleet_update") as update:
        assert fleet_autoupdate.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["skipped"] is True
    assert "dirty" in report["reason"]
    assert report["detail"]["dirty_paths"] == 1
    assert report["detail"]["tracked_changes"] == (1 if dirty == "tracked" else 0)
    assert report["detail"]["examples"]
    assert "clean checkout" in report["detail"]["hint"]
    update.assert_not_called()
    recorded = fleet_autoupdate.read_state()
    assert recorded["outcome"] == "skipped" and recorded["skipped"] is True
    assert "dirty" in recorded["reason"]


def test_non_main_checkout_is_a_reported_skip(checkout, capsys):
    from herdr_engineering import fleet_autoupdate

    subprocess.run(["git", "-C", str(checkout), "checkout", "-b", "feature"], check=True, capture_output=True)
    with patch.object(fleet_autoupdate, "fleet_update") as update:
        assert fleet_autoupdate.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["skipped"] is True
    assert "main branch" in report["reason"] and report["detail"]["branch"] == "feature"
    update.assert_not_called()
    assert fleet_autoupdate.read_state()["outcome"] == "skipped"


def test_success_allows_ignored_inventory_and_records_the_revision(checkout, capsys):
    from herdr_engineering import fleet_autoupdate

    inventory = checkout / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts.yml").write_text("private inventory")
    result = {"ok": True, "steps": {"fleet_sync": {"rc": 0}}}
    with patch.object(fleet_autoupdate, "fleet_update", return_value=result) as update:
        assert fleet_autoupdate.main() == 0
    update.assert_called_once_with()
    assert json.loads(capsys.readouterr().out) == result
    recorded = fleet_autoupdate.read_state()
    assert recorded["outcome"] == "updated" and recorded["ok"] is True
    assert recorded["last_run"] and recorded["revision"] is None


def test_update_failure_has_nonzero_exit_and_is_recorded(checkout, capsys):
    from herdr_engineering import fleet_autoupdate

    result = {"ok": False, "steps": {"fleet_sync": {"rc": 2}}, "error": "fleet sync failed"}
    with patch.object(fleet_autoupdate, "fleet_update", return_value=result):
        assert fleet_autoupdate.main() == 1
    assert json.loads(capsys.readouterr().out) == result
    recorded = fleet_autoupdate.read_state()
    assert recorded["outcome"] == "failed" and recorded["ok"] is False
    assert recorded["reason"] == "fleet sync failed"


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


def test_run_update_refuses_the_sentinel_form_python_m_actually_produces(checkout, monkeypatch,
                                                                        capsys):
    """`python -m` leaves `sys.path[0] == ""`; that sentinel is the working directory.

    This is the shape bender's unit produced (`WorkingDirectory=<checkout>` + `-m`). The first version
    of `shadowed()` returned False for an empty entry, so the real invocation went past the guard -
    caught by running the new code in a clean main checkout on bender, not by the unit tests.
    """
    from herdr_engineering import fleet_autoupdate

    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(checkout))
    monkeypatch.setattr(fleet_autoupdate, "package_root", lambda: checkout)
    monkeypatch.setattr(fleet_autoupdate, "ENTRY_PATH_ZERO", "")
    monkeypatch.chdir(checkout)
    with patch.object(fleet_autoupdate, "fleet_update") as update:
        assert fleet_autoupdate.main() == 1
    assert "checkout it would update" in json.loads(capsys.readouterr().out)["error"]
    update.assert_not_called()


def test_entry_captures_the_path_before_fleet_rewrites_it():
    """`herdr_engineering.fleet` inserts `<checkout>/scripts` at `sys.path[0]` when it imports.

    A guard reading `sys.path[0]` at call time therefore never sees the cwd sentinel - the mistake
    that let bender's `python -m` invocation through (#584). `_entry` is imported before `.fleet`, so
    its value is what the interpreter started with. Proven in a fresh interpreter: in-process the
    order depends on which test imported the package first.
    """
    from pathlib import Path

    from herdr_engineering import fleet_autoupdate

    checkout = Path(fleet_autoupdate.__file__).resolve().parent.parent
    probe = subprocess.run(
        [sys.executable, "-c",
         "import herdr_engineering._entry as e, herdr_engineering.fleet, sys;"
         "print(e.PATH_ZERO or '<cwd-sentinel>'); print(sys.path[0])"],
        cwd=str(checkout), capture_output=True, text=True, check=True)
    captured, after = probe.stdout.split()
    assert captured != after
    assert after.endswith("scripts")


def test_every_run_records_which_code_executed_it(checkout, capsys, monkeypatch):
    """The state file says which code performed the update, and whether that was the checkout."""
    from herdr_engineering import fleet_autoupdate

    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(checkout))
    with patch.object(fleet_autoupdate, "fleet_update", return_value={"ok": True}):
        assert fleet_autoupdate.main() == 0
    capsys.readouterr()

    runner = fleet_autoupdate.read_state()["detail"]
    assert runner["runner_module"] == str(fleet_autoupdate.package_root())
    assert runner["runner_is_checkout"] is False


def test_a_skip_records_the_runner_too(checkout, capsys, monkeypatch):
    from herdr_engineering import fleet_autoupdate

    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(checkout))
    subprocess.run(["git", "-C", str(checkout), "checkout", "-b", "feature"],
                   check=True, capture_output=True)
    with patch.object(fleet_autoupdate, "fleet_update") as update:
        assert fleet_autoupdate.main() == 0
    capsys.readouterr()
    update.assert_not_called()

    detail = fleet_autoupdate.read_state()["detail"]
    assert detail["branch"] == "feature"
    assert detail["runner_is_checkout"] is False


def test_editable_controller_runs_and_says_so(checkout, capsys, monkeypatch):
    """An editable install of the checkout is the other route to running its own code (bender).

    Not refused - for a clean checkout the fast-forward updates the code the next run uses - but it
    must be visible in the record, because it is why a dirty checkout stays permanently self-blocking.
    """
    from herdr_engineering import fleet_autoupdate

    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(checkout))
    monkeypatch.setattr(fleet_autoupdate, "package_root", lambda: checkout)
    with patch.object(fleet_autoupdate, "fleet_update", return_value={"ok": True}) as update:
        assert fleet_autoupdate.main() == 0
    capsys.readouterr()
    update.assert_called_once_with()

    runner = fleet_autoupdate.read_state()["detail"]
    assert runner["runner_is_checkout"] is True
    assert runner["runner_module"] == str(checkout)


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
    assert "OnCalendar=*-*-* 11:59:00" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=0" in timer
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


def test_service_working_directory_stays_outside_the_checkout():
    """Issue #584: `python -m` puts the working directory first on sys.path.

    The repository is flat-layout, so `WorkingDirectory=<checkout>` makes the timer import the
    checkout instead of the installed build - and then the updater a host needs is the code the
    checkout is too far behind (or too dirty) to receive.
    """
    source = Path(__file__).resolve().parents[1]
    text = (source / "scripts/herdr-eng-fleet-update.service").read_text()
    assert "WorkingDirectory=%h" in text
    # Nothing may re-inject the checkout as the working directory.
    assert "__REPO__" not in text
    assert "WorkingDirectory=__" not in text


def test_shadowing_is_only_cwd_shadowing():
    from herdr_engineering.fleet_autoupdate import shadowed

    target = Path("/srv/controller")
    assert shadowed(target, "/srv/controller", target) is True
    # An editable install of the same checkout reached through the console script keeps its own
    # interpreter directory first on sys.path; that is the operator's choice, not shadowing.
    assert shadowed(target, "/home/operator/.local/bin", target) is False
    # Running the installed build from inside the checkout is also fine.
    assert shadowed(Path("/opt/herdr-engineering"), "/srv/controller", target) is False
    # A sentinel sys.path[0] means the current directory; under pytest that is not /srv/controller.
    # (Its cwd-dependence is pinned in the next test - the first predicate excused the sentinel
    # altogether, and the sentinel is the shape `python -m` actually produces.)
    assert shadowed(target, "", target) is False


def test_the_sentinel_sys_path_entry_is_the_working_directory(tmp_path, monkeypatch):
    """`python -m` puts the sentinel `""` at `sys.path[0]`, and that sentinel means the cwd.

    Read back on bender with the new code in a clean main checkout: `-m herdr_engineering.fleet_autoupdate`
    ran with `sys.path[0] == ''` and the package inside the checkout, and the first version of
    `shadowed()` answered "not shadowing" - so the guard existed while the production shape walked past
    it. The predicate must resolve the sentinel against the process working directory.
    """
    from herdr_engineering.fleet_autoupdate import shadowed

    target = (tmp_path / "controller").resolve()
    target.mkdir()

    monkeypatch.chdir(target)
    assert shadowed(target, "", target) is True                    # -m from inside the checkout
    monkeypatch.chdir(tmp_path)
    assert shadowed(target, "", target) is False                   # -m from a neutral directory
    assert shadowed(target, str(target), target) is True           # cwd spelled absolutely
    assert shadowed(target, str(tmp_path / "bin"), target) is False  # console script


def test_run_update_refuses_to_update_the_checkout_it_imported(checkout, monkeypatch, tmp_path,
                                                               capsys):
    from herdr_engineering import fleet_autoupdate

    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(checkout))
    monkeypatch.setattr(fleet_autoupdate, "package_root", lambda: checkout)
    monkeypatch.setattr(fleet_autoupdate, "ENTRY_PATH_ZERO", str(checkout))
    with patch.object(fleet_autoupdate, "fleet_update") as update:
        assert fleet_autoupdate.main() == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert "checkout it would update" in error
    assert "herdr-eng fleet update" in error
    update.assert_not_called()
    # Refused before any work: no lock file, no state recorded as a fleet change.
    assert not (tmp_path / "state" / "herdr-engineering" / "fleet-auto-update.lock").exists()
