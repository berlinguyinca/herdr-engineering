"""Pure decision/record logic behind ansible/playbooks/fleet-update.yml."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parent.parent / "ansible" / "files" / "fleet-update-state.py"
_spec = importlib.util.spec_from_file_location("fleet_update_state", HELPER)
fus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fus)

NOW = 1_800_000_000.0  # fixed epoch for every test
DAY = 86400


def _iso(epoch):
    return fus.iso(epoch)


def _sessions(*rows, status=200):
    return {"status": status, "json": {"sessions": list(rows)}}


def _decide(state_file, hosts, sessions, *, force=False, max_days=7):
    return fus.decide({"state_file": str(state_file), "now": NOW, "force": force,
                       "max_defer_days": max_days, "sessions": sessions, "hosts": hosts})


def test_idle_host_with_readable_sessions_updates(tmp_path):
    out = _decide(tmp_path / "s.json", {"beast": {"status": 404, "json": {}}}, _sessions())
    assert out["beast"]["action"] == "update"
    assert out["beast"]["busy"] == "no"


def test_active_session_on_host_defers_and_records_first_deferral(tmp_path):
    rows = [{"session_id": "a", "host_id": "beast", "status": "active"},
            {"session_id": "b", "host_id": "fry", "status": "active"}]
    out = _decide(tmp_path / "s.json", {"beast": {"status": 200, "json": {}},
                                       "bender": {"status": 200, "json": {}}},
                  _sessions(*rows))
    assert out["beast"]["action"] == "defer"
    assert out["beast"]["reason"] == "agents running"
    assert out["beast"]["deferred_since"] == _iso(NOW)
    assert out["bender"]["action"] == "update"


def test_stopped_sessions_do_not_count(tmp_path):
    rows = [{"session_id": "a", "host_id": "beast", "status": "stopped"}]
    out = _decide(tmp_path / "s.json", {"beast": {"status": 200, "json": {}}}, _sessions(*rows))
    assert out["beast"]["action"] == "update"


def test_agents_running_on_host_record_is_busy(tmp_path):
    host = {"status": 200, "json": {"latest": {"agents_running": 2}}}
    out = _decide(tmp_path / "s.json", {"beast": host}, _sessions())
    assert out["beast"]["action"] == "defer"
    # busy even when the sessions read failed
    out = _decide(tmp_path / "s.json", {"beast": host}, _sessions(status=-1))
    assert out["beast"]["busy"] == "yes"


@pytest.mark.parametrize("sessions", [_sessions(status=-1), _sessions(status=401),
                                      {"status": 200, "json": {"oops": 1}}, {}])
def test_unreadable_sessions_mean_busy_unknown_and_defer(tmp_path, sessions):
    # A host record without agents_running is not a signal (the agent does not
    # report it today), so the sessions read must succeed.
    hosts = {"beast": {"status": 200, "json": {"latest": {"cpu": 3}}}}
    out = _decide(tmp_path / "s.json", hosts, sessions)
    assert out["beast"] == {**out["beast"], "action": "defer", "busy": "unknown",
                            "reason": "busy state unknown"}


def test_unreachable_control_plane_defers(tmp_path):
    out = _decide(tmp_path / "s.json", {"beast": {"status": -1}}, {"status": -1})
    assert out["beast"]["action"] == "defer"
    assert out["beast"]["reason"] == "busy state unknown"


def test_first_deferral_is_kept_across_runs(tmp_path):
    state = tmp_path / "s.json"
    first = NOW - 3 * DAY
    state.write_text(json.dumps({"beast": {"deferred_since": _iso(first),
                                           "last_updated": _iso(NOW - 20 * DAY)}}))
    rows = [{"host_id": "beast", "status": "active"}]
    out = _decide(state, {"beast": {"status": 200, "json": {}}}, _sessions(*rows))
    assert out["beast"]["deferred_since"] == _iso(first)
    assert out["beast"]["last_updated"] == _iso(NOW - 20 * DAY)


def test_max_deferral_still_never_forces(tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"beast": {"deferred_since": _iso(NOW - 9 * DAY)}}))
    rows = [{"host_id": "beast", "status": "active"}]
    out = _decide(state, {"beast": {"status": 200, "json": {}}}, _sessions(*rows))
    assert out["beast"]["action"] == "defer"
    assert "max 7 d reached" in out["beast"]["reason"]


def test_force_overrides_busy_and_unknown(tmp_path):
    rows = [{"host_id": "beast", "status": "active"}]
    out = _decide(tmp_path / "s.json", {"beast": {"status": 200, "json": {}}},
                  _sessions(*rows), force=True)
    assert out["beast"]["action"] == "update" and out["beast"]["forced"] is True
    out = _decide(tmp_path / "s.json", {"beast": {"status": -1}}, {"status": -1}, force=True)
    assert out["beast"]["action"] == "update"


def test_corrupt_state_file_is_treated_as_empty(tmp_path):
    state = tmp_path / "s.json"
    state.write_text("{not json")
    out = _decide(state, {"beast": {"status": 404}}, _sessions())
    assert out["beast"]["last_updated"] is None


# ---- record -----------------------------------------------------------------

def _record(state_file, hosts, *, dry_run=False):
    return fus.record({"state_file": str(state_file), "now": NOW, "dry_run": dry_run,
                       "hosts": hosts})


def _host(outcome, **kw):
    base = {"outcome": outcome, "reason": "", "maintenance": False,
            "deferred_since": None, "last_updated": None,
            "pins": {"herdr": "0.9.1", "pi": "", "autospec": None,
                     "pi_extensions": {"pi-engineering": "1.2.0", "build-gate.ts": None}}}
    return {**base, **kw}


def test_updated_sets_last_updated_and_clears_deferral(tmp_path):
    state = tmp_path / "sub" / "s.json"
    out = _record(state, {"beast": _host("updated", deferred_since=_iso(NOW - DAY))})
    body = out["beast"]
    assert body["entity_type"] == "host" and body["entity_id"] == "beast"
    assert body["event_type"] == "host.fleet_update"
    fu = body["payload"]["fleet_update"]
    assert fu == {"last_updated": _iso(NOW), "deferred_since": None, "result": "updated"}
    saved = json.loads(state.read_text())
    assert saved["beast"]["last_updated"] == _iso(NOW)
    assert saved["beast"]["deferred_since"] is None
    assert state.stat().st_mode & 0o777 == 0o600


def test_payload_never_carries_telemetry_and_always_maintenance(tmp_path):
    out = _record(tmp_path / "s.json", {"a": _host("updated"),
                                        "b": _host("deferred", reason="agents running",
                                                   maintenance=True)})
    for body in out.values():
        assert "telemetry" not in body["payload"]
        assert isinstance(body["payload"]["maintenance"], bool)
    assert out["a"]["payload"]["maintenance"] is False
    assert out["b"]["payload"]["maintenance"] is True


@pytest.mark.parametrize("value,expected", [("false", False), ("True", True), ("no", False),
                                            (None, False), (0, False), (1, True)])
def test_maintenance_is_coerced_to_bool(tmp_path, value, expected):
    out = _record(tmp_path / "s.json", {"a": _host("updated", maintenance=value)})
    assert out["a"]["payload"]["maintenance"] is expected


def test_pins_drop_unset_values(tmp_path):
    out = _record(tmp_path / "s.json", {"a": _host("updated")})
    assert out["a"]["payload"]["pins"] == {"herdr": "0.9.1",
                                           "pi_extensions": {"pi-engineering": "1.2.0"}}
    out = _record(tmp_path / "s.json", {"a": _host("updated", pins={"pi_extensions": {}})})
    assert out["a"]["payload"]["pins"] == {}


def test_deferred_keeps_previous_last_updated(tmp_path):
    prev = _iso(NOW - 5 * DAY)
    out = _record(tmp_path / "s.json", {"a": _host("deferred", reason="busy state unknown",
                                                   last_updated=prev,
                                                   deferred_since=_iso(NOW - DAY))})
    assert out["a"]["payload"]["fleet_update"] == {
        "last_updated": prev, "deferred_since": _iso(NOW - DAY),
        "result": "deferred: busy state unknown"}


def test_failed_keeps_previous_dates_and_short_reason(tmp_path):
    prev = _iso(NOW - 5 * DAY)
    out = _record(tmp_path / "s.json", {"a": _host("failed", reason="x" * 500,
                                                   last_updated=prev)})
    fu = out["a"]["payload"]["fleet_update"]
    assert fu["last_updated"] == prev
    assert fu["result"].startswith("failed: ") and len(fu["result"]) <= 128


def test_record_merges_other_hosts_and_dry_run_writes_nothing(tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"fry": {"last_updated": "keep"}}))
    _record(state, {"beast": _host("updated")}, dry_run=True)
    assert json.loads(state.read_text()) == {"fry": {"last_updated": "keep"}}
    _record(state, {"beast": _host("updated")})
    saved = json.loads(state.read_text())
    assert saved["fry"] == {"last_updated": "keep"} and "beast" in saved


def test_cli_reads_stdin_and_prints_json(tmp_path):
    inp = {"state_file": str(tmp_path / "s.json"), "now": NOW, "force": False,
           "max_defer_days": 7, "sessions": {"status": -1}, "hosts": {"beast": {"status": -1}}}
    res = subprocess.run([sys.executable, "-I", str(HELPER), "decide"], input=json.dumps(inp),
                         capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(res.stdout)["beast"]["reason"] == "busy state unknown"


# ---- end to end against the real ingest path ----------------------------------

def test_event_body_is_accepted_by_the_control_plane(tmp_path):
    from herdr_engineering.control_plane.ingest import IngestService
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    # the agent's own heartbeat first: its telemetry must survive our event
    svc.ingest({"source": "fabric-agent", "entity_type": "host", "entity_id": "beast",
                "event_type": "host.heartbeat",
                "payload": {"host_name": "beast", "telemetry": {"cpu": 12.5, "mem": 40}}})
    before = repo.get_host("beast")["latest"]
    on = _record(tmp_path / "s.json", {"beast": _host("updated", maintenance=True)})["beast"]
    svc.ingest(on)
    host = repo.get_host("beast")
    assert host["maintenance"] is True
    assert host["pins"] == {"herdr": "0.9.1", "pi_extensions": {"pi-engineering": "1.2.0"}}
    assert host["fleet_update"]["result"] == "updated"
    assert host["latest"]["cpu"] == before["cpu"]
    # maintenance false must reach the control plane too (it keeps old values otherwise)
    since = _iso(repo._now() - 9 * DAY)
    off = _record(tmp_path / "s.json", {"beast": _host("deferred", reason="agents running",
                                                       deferred_since=since)})["beast"]
    svc.ingest(off)
    host = repo.get_host("beast")
    assert host["maintenance"] is False
    assert host["fleet_update"]["deferred_since"] == since
    assert "updates_deferred" in {p["code"] for p in host.get("problems", [])}


def test_session_under_an_alias_name_still_counts_as_busy(tmp_path):
    rows = [{"host_id": "mac", "status": "active"}]
    out = _decide(tmp_path / "s.json",
                  {"Gerts-MBP.local": {"status": 404, "names": ["Gerts-MBP.local", "Gerts-MBP", "mac"]}},
                  _sessions(*rows))
    assert out["Gerts-MBP.local"]["action"] == "defer"
