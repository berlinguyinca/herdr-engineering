"""The doctor's control-plane backup check: real files in a temporary HOME, a fixed clock, nothing else is read."""
import os

import pytest

from herdr_engineering import control_plane_backup_status as cb
from herdr_engineering.config import DEFAULTS
from herdr_engineering.doctor import run_doctor

NOW = 1_800_000_000.0
HOUR = 3600


def dump(home, name, age_seconds):
    d = home / cb.BACKUP_DIR_REL
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("x")
    os.utime(f, (NOW - age_seconds, NOW - age_seconds))
    return f


@pytest.fixture
def host(tmp_path):
    """A host that runs the control plane: the deploy state directory exists."""
    (tmp_path / cb.DEPLOY_STATE_REL).mkdir(parents=True)
    return tmp_path


def one(home):
    (check,) = cb.check_control_plane_backup(home, now=lambda: NOW)
    return check


def test_a_host_that_does_not_run_the_control_plane_is_not_applicable(tmp_path):
    check = one(tmp_path)
    assert check["name"] == "control_plane_backup" and check["status"] == "ok"
    assert check["detail"].startswith("not applicable") and check["data"] == {"applicable": False}


def test_a_control_plane_host_with_no_backup_warns(host):
    check = one(host)
    assert check["status"] == "warn" and "no control-plane backup" in check["detail"] and "memory-backup.sh" in check["detail"]


def test_a_fresh_dump_and_object_archive_is_ok_and_reports_the_age(host):
    dump(host, "controlplane-20261101T000000.000000Z.dump", 3 * HOUR)
    dump(host, "controlplane-objects-20261101T000000.000000Z.tar", 3 * HOUR)
    check = one(host)
    assert check["status"] == "ok" and check["data"]["age_seconds"] == 3 * HOUR and "3 h old" in check["detail"]


def test_the_newest_dump_counts_and_pre_deploy_dumps_do_not(host):
    dump(host, "controlplane-20261101T000000.000000Z.dump", 90 * HOUR)
    dump(host, "predeploy-controlplane-20261102T000000.000000Z.dump", 1 * HOUR)
    dump(host, "controlplane-objects-20261101T000000.000000Z.tar", 90 * HOUR)
    check = one(host)
    assert check["data"]["age_seconds"] == 90 * HOUR and check["status"] == "warn"


@pytest.mark.parametrize("hours,status", [(35, "ok"), (37, "warn"), (24 * 8, "fail")])
def test_age_thresholds(host, hours, status):
    dump(host, "controlplane-20261101T000000.000000Z.dump", hours * HOUR)
    dump(host, "controlplane-objects-20261101T000000.000000Z.tar", hours * HOUR)
    assert one(host)["status"] == status


def test_a_fresh_dump_without_a_fresh_object_archive_is_a_partial_backup_and_warns(host):
    dump(host, "controlplane-20261101T000000.000000Z.dump", 2 * HOUR)
    check = one(host)
    assert check["status"] == "warn" and "object store" in check["detail"]
    dump(host, "controlplane-objects-20261030T000000.000000Z.tar", 100 * HOUR)
    assert one(host)["status"] == "warn"


def test_an_installed_timer_or_a_backup_directory_also_marks_the_host(tmp_path):
    unit = tmp_path / cb.TIMER_UNIT_REL
    unit.parent.mkdir(parents=True)
    unit.write_text("[Timer]")
    assert one(tmp_path)["status"] == "warn"


def test_an_unreadable_backup_directory_is_unknown_not_ok(host):
    d = host / cb.BACKUP_DIR_REL
    d.mkdir(parents=True)
    d.chmod(0)
    try:
        if os.access(d, os.R_OK):
            pytest.skip("running as a user that ignores directory permissions")
        assert one(host)["status"] == "unknown"
    finally:
        d.chmod(0o700)


def test_doctor_reports_the_check_without_crashing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    checks = {c["name"]: c for c in run_doctor(config=dict(DEFAULTS)).to_dict()["checks"]}
    assert checks["control_plane_backup"]["detail"].startswith("not applicable")
