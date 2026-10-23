"""Server-side host problems: a pure function plus its exposure on host reads."""
import datetime as dt

import pytest

from herdr_engineering.control_plane.host_problems import Thresholds, compute_problems
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1_800_000_000.0


def iso(ts):
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat()


def host(**over):
    h = {"host_id": "h", "last_seen": iso(NOW - 30),
         "latest": {"at": iso(NOW - 30), "cpu": 10, "mem": 40, "disk": 50}}
    h.update(over)
    return h


def codes(h, history=(), now=NOW):
    return [p["code"] for p in compute_problems(h, list(history), now)]


def mem_history(pct, span=300, step=30):
    return [{"ts": NOW - t, "mem": pct} for t in range(span, -1, -step)]


def test_healthy_host_has_no_problems():
    assert compute_problems(host(), [], NOW) == []


def test_never_reported_only():
    h = {"host_id": "h"}
    assert compute_problems(h, [], NOW) == [
        {"code": "never_reported", "severity": "critical",
         "message": "Host has never reported"}]


def test_never_reported_not_suppressed_by_maintenance():
    assert codes({"host_id": "h", "maintenance": True}) == ["never_reported"]


@pytest.mark.parametrize("age_min,fires", [(14, False), (16, True)])
def test_stale_threshold(age_min, fires):
    at = iso(NOW - age_min * 60)
    h = host(last_seen=at, latest={"at": at, "cpu": 1})
    assert ("stale" in codes(h)) is fires


def test_stale_message_and_severity():
    at = iso(NOW - 22 * 60)
    p = compute_problems(host(last_seen=at, latest={"at": at}), [], NOW)[0]
    assert p == {"code": "stale", "severity": "critical", "message": "No report for 22 min"}


def test_restored_latest_that_is_old_is_stale_not_fresh():
    at = iso(NOW - 3 * 3600)
    assert "stale" in codes(host(last_seen=at, latest={"at": at, "cpu": 1}))


@pytest.mark.parametrize("latest", [None, "junk", []])
def test_registered_without_telemetry_is_never_reported(latest):
    h = {"host_id": "h", "last_seen": iso(NOW - 5)}
    if latest is not None:
        h["latest"] = latest
    assert codes(h) == ["never_reported"]


def test_latest_without_any_parseable_time_is_stale():
    h = {"host_id": "h", "last_seen": "garbage", "latest": {"at": "nope", "cpu": 1}}
    assert compute_problems(h, [], NOW) == [
        {"code": "stale", "severity": "critical", "message": "No valid report time"}]
    h["maintenance"] = True
    assert codes(h) == []


def test_mem_history_ignores_non_dict_entries():
    assert codes(host(), [None, "x", 3] + mem_history(95)) == ["mem_pressure"]


def test_maintenance_suppresses_stale_only():
    at = iso(NOW - 3600)
    h = host(last_seen=at, latest={"at": at, "disk": 97}, maintenance=True)
    assert codes(h) == ["disk_critical"]


@pytest.mark.parametrize("disk,expected", [
    (84, []), (85, ["disk_warn"]), (94, ["disk_warn"]), (95, ["disk_critical"])])
def test_disk_thresholds_exclusive(disk, expected):
    h = host()
    h["latest"]["disk"] = disk
    assert codes(h) == expected


def test_disk_uses_worst_mount_and_names_it():
    h = host()
    h["latest"]["mounts"] = [{"path": "/", "pct": 50}, {"path": "/data", "pct": 91}]
    p = compute_problems(h, [], NOW)
    assert p == [{"code": "disk_warn", "severity": "warning",
                  "message": "Disk 91% full on /data"}]


def test_disk_fallback_message_names_root():
    h = host()
    h["latest"]["disk"] = 91
    assert compute_problems(h, [], NOW)[0]["message"] == "Disk 91% full on /"


def test_mem_pressure_sustained_fires():
    assert codes(host(), mem_history(95)) == ["mem_pressure"]


def test_mem_single_spike_does_not_fire():
    hist = mem_history(50)
    hist[-1]["mem"] = 99
    assert codes(host(), hist) == []


def test_mem_short_history_does_not_fire():
    assert codes(host(), mem_history(95, span=120)) == []


def test_mem_old_low_sample_outside_window_ignored():
    hist = [{"ts": NOW - 900, "mem": 10}] + mem_history(95)
    assert codes(host(), hist) == ["mem_pressure"]


def test_mem_low_sample_inside_window_blocks():
    hist = mem_history(95)
    hist[3]["mem"] = 80
    assert codes(host(), hist) == []


def test_mem_pressure_message_includes_swap():
    h = host()
    h["latest"]["swap"] = 60
    msg = compute_problems(h, mem_history(95), NOW)[0]["message"]
    assert "swap" in msg and "60%" in msg


def test_gpu_hot():
    h = host()
    h["latest"]["gpus"] = [{"temp_c": 70}, {"temp_c": 86}]
    p = compute_problems(h, [], NOW)
    assert [x["code"] for x in p] == ["gpu_hot"] and "86" in p[0]["message"]
    h["latest"]["gpus"] = [{"temp_c": 85}]
    assert codes(h) == []


def test_updates_signals():
    u = {"manager": "apt", "outdated": 4, "security": 3, "packages": [],
         "reboot_required": True, "failed_units": ["a.service", "b.service"]}
    p = compute_problems(host(updates=u), [], NOW)
    by = {x["code"]: x for x in p}
    assert set(by) == {"failed_units", "reboot_required", "security_updates"}
    assert by["security_updates"]["message"] == "3 security updates pending"
    assert all(x["severity"] == "warning" for x in p)


def test_updates_unknown_when_outdated_none():
    assert codes(host(updates={"manager": "x", "outdated": None})) == ["updates_unknown"]


def test_no_updates_received_no_problem():
    assert codes(host()) == []


def test_clean_updates_no_problem():
    u = {"manager": "apt", "outdated": 0, "security": 0, "reboot_required": False,
         "failed_units": []}
    assert codes(host(updates=u)) == []


def test_version_drift_and_per_extension():
    h = host(versions={"herdr": "1.0", "pi": "2.0", "autospec": "3.0",
                       "pi_extensions": {"a": "1", "b": "2"}},
             pins={"herdr": "1.0", "pi": "2.1", "pi_extensions": {"a": "1", "b": "9"}})
    p = compute_problems(h, [], NOW)
    assert [x["code"] for x in p] == ["version_drift"]
    assert "pi" in p[0]["message"] and "b" in p[0]["message"]
    assert "herdr" not in p[0]["message"] and "autospec" not in p[0]["message"]


def test_no_drift_without_pins_or_when_matching():
    v = {"herdr": "1.0", "pi_extensions": {"a": "1"}}
    assert codes(host(versions=v)) == []
    assert codes(host(versions=v, pins={"herdr": "1.0", "pi_extensions": {"a": "1"}})) == []


def test_updates_deferred_and_old():
    h = host(fleet_update={"last_updated": iso(NOW - 15 * 86400),
                           "deferred_since": iso(NOW - 8 * 86400), "result": "ok"})
    assert codes(h) == ["updates_deferred", "updates_old"]
    h = host(fleet_update={"last_updated": iso(NOW - 13 * 86400),
                           "deferred_since": iso(NOW - 6 * 86400)})
    assert codes(h) == []


def test_unparseable_timestamps_do_not_raise():
    h = host(fleet_update={"last_updated": "garbage", "deferred_since": None})
    assert codes(h) == []


def test_sorted_critical_first_then_code():
    h = host(maintenance=False)
    h["latest"]["disk"] = 97
    h["latest"]["gpus"] = [{"temp_c": 90}]
    h["updates"] = {"outdated": 1, "security": 1, "reboot_required": True}
    assert codes(h) == ["disk_critical", "gpu_hot", "reboot_required", "security_updates"]


def test_thresholds_are_configurable():
    h = host()
    h["latest"]["disk"] = 60
    p = compute_problems(h, [], NOW, Thresholds(disk_warn=50))
    assert [x["code"] for x in p] == ["disk_warn"]


def test_does_not_mutate_inputs():
    h = host(updates={"outdated": 1, "security": 1})
    import copy
    before = copy.deepcopy(h)
    compute_problems(h, mem_history(95), NOW)
    assert h == before


# ---- repo exposure -------------------------------------------------------

def test_repo_reads_expose_problems_without_mutating_store():
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    hid = repo.register_host("bender", "100.64.0.9")
    repo.record_host_metrics(hid, {"cpu": 5, "mem": 30, "disk": 96})
    got = repo.get_host(hid)
    assert got["problem_count"] == 1
    assert got["problems"][0]["code"] == "disk_critical"
    listed = repo.list_hosts()[0]
    assert listed["problem_count"] == 1 and listed["problems"] == got["problems"]
    assert "problems" not in repo._hosts[hid]
    clock[0] += 1800
    assert "stale" in [p["code"] for p in repo.get_host(hid)["problems"]]


def test_repo_unknown_host_still_none():
    assert ControlPlaneRepo().get_host("nope") is None


def test_repo_healthy_host_empty_list():
    repo = ControlPlaneRepo(now=lambda: NOW)
    hid = repo.register_host("ok", "100.64.0.1")
    repo.record_host_metrics(hid, {"cpu": 5, "mem": 30, "disk": 10})
    h = repo.get_host(hid)
    assert h["problems"] == [] and h["problem_count"] == 0


def test_repo_registered_but_silent_host_is_never_reported():
    repo = ControlPlaneRepo(now=lambda: NOW)
    hid = repo.register_host("beast", "100.64.0.5")
    assert [p["code"] for p in repo.get_host(hid)["problems"]] == ["never_reported"]
    listed = repo.list_hosts()[0]
    assert listed["problem_count"] == 1 and listed["problems"][0]["code"] == "never_reported"


def test_repo_mem_pressure_through_real_store_clock():
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    hid = repo.register_host("bender", "100.64.0.9")
    for _ in range(12):  # 30 s apart, 5.5 min
        repo.record_host_metrics(hid, {"cpu": 5, "mem": 95, "disk": 10})
        clock[0] += 30
    clock[0] -= 30
    assert "mem_pressure" in [p["code"] for p in repo.get_host(hid)["problems"]]
