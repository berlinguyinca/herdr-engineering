# ruff: noqa: F811  (the public-listener fixture is imported by name)
"""Attention inbox (design 2026-10-24): derivation, lifecycle, storage, API, operator-only routes."""
import hashlib
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.attention_inbox import (
    AttentionInbox,
    attention_config,
    mission_health,
    status_outcome,
)
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.reviews import ReviewService
from tests.control_plane.test_collab_public import stack  # noqa: F401

NOW = 1_800_000_000
HOUR = 3600


def _iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class _Sink:
    def record(self, event):
        pass

    record_required = record


def _world(ci=None, **kw):
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=_Sink())
    view = {"status": "ok", "pipelines": {}} if ci is None else ci
    inbox = AttentionInbox(repo, ci_snapshot=lambda: view, config=kw.get("config"), now=lambda: clock[0])
    return repo, inbox, clock, view


def _by_kind(inbox, **kw):
    out = {}
    for item in inbox.snapshot(state="all", **kw)["items"]:
        out.setdefault(item["kind"], []).append(item)
    return out


def _run(number, state, repository="org/app", branch="main", at=NOW - 60):
    return {"run_number": number, "state": state, "repository": repository, "branch": branch,
            "updated_at": _iso(at - number)}


def test_a_quiet_fleet_has_no_items_and_is_not_partial():
    _, inbox, _, _ = _world()
    body = inbox.snapshot()
    assert body["items"] == [] and body["partial"] is False and body["unavailable"] == []
    assert body["counts"] == {"open": 0, "acknowledged": 0, "snoozed": 0, "resolved": 0, "critical": 0}


def test_failed_agent_service_host_and_review_each_make_their_item():
    repo, inbox, _, _ = _world()
    sid = repo.create_session(agent_role="coder")
    repo._sessions[sid]["status"] = "failed"
    repo.upsert_host("h1", "beast", "100.1.1.1")
    repo._hosts["h1"]["last_seen"] = _iso(NOW - 2 * HOUR)
    reviews = ReviewService(repo)
    digest = hashlib.sha256(b"plan").hexdigest()
    reviews.register({"review_id": "rev1", "plan": "plan", "instance_id": "i1", "title": "Plan X",
                      "review_url": "https://plannotator.example/r"})
    reviews.complete("rev1", {"instance_id": "i1", "plan_digest": digest,
                              "result": {"decision": "denied", "feedback": ""}})
    kinds = _by_kind(inbox)
    (agent,) = kinds["agent_failed"]
    assert agent["severity"] == "critical" and agent["href"] == f"#/herd/session/{sid}"
    assert agent["key"] == f"session:{sid}" and agent["state"] == "open"
    (host,) = kinds["host_stale"]
    assert host["severity"] == "warning" and host["href"] == "#/hosts/h1"
    (review,) = kinds["review_changes"]
    assert review["href"] == "#/reviews?tab=changes&search=rev1" and review["severity"] == "warning"


def test_only_herdr_scope_unavailable_services_count():
    repo, inbox, _, _ = _world()

    def svc(sid, scope, state="unavailable", status="unavailable", archived=False):
        return {"service_id": sid, "service_name": sid, "status": status, "updated_at": _iso(NOW),
                "catalog": {"scope": scope}, "lifecycle": {"state": state, "archived": archived}}

    repo.list_services = lambda include_archived=True: [
        svc("down", "herdr"), svc("tunnel", "unassigned"), svc("unit", "system"),
        svc("fine", "herdr", state="current", status="healthy"), svc("old", "herdr", state="archived", archived=True)]
    (item,) = _by_kind(inbox)["service_down"]
    assert item["key"] == "service:down" and item["severity"] == "critical" and item["href"] == "#/services/down"


def test_failed_runs_coalesce_per_repo_and_branch_with_a_count():
    runs = [_run(n, "failure") for n in range(1, 10)] + [_run(30, "success", branch="dev"), _run(31, "failure", branch="dev", at=NOW - 10)]
    repo, inbox, _, _ = _world(ci={"status": "ok", "pipelines": {"org/app": runs}})
    items = _by_kind(inbox)["ci_failed"]
    by_key = {i["key"]: i for i in items}
    assert by_key["ci:org/app:main"]["count"] == 9
    assert by_key["ci:org/app:dev"]["count"] == 1
    main = by_key["ci:org/app:main"]
    assert main["severity"] == "critical"
    assert main["href"] == "#/ci?tab=failed&repository=org%2Fapp&branch=main"
    assert "9 failed runs" in main["detail"]


def test_a_newer_green_run_clears_the_ci_item():
    runs = [_run(1, "failure"), _run(2, "failure"), {**_run(3, "success"), "updated_at": _iso(NOW - 5)}]
    _, inbox, _, _ = _world(ci={"status": "ok", "pipelines": {"org/app": runs}})
    assert "ci_failed" not in _by_kind(inbox)


def test_a_failed_mission_and_a_stalled_live_mission():
    repo, inbox, _, _ = _world()
    repo.upsert_mission("m-failed", title="Broken build", stage="failed")
    repo.upsert_mission("m-old", title="Ancient failure", stage="failed")
    repo._missions["m-old"]["updated_at"] = _iso(NOW - 30 * 24 * HOUR)
    IngestService(repo).ingest({
        "entity_type": "mission_activity", "entity_id": "pi:MSN-1", "event_type": "mission_activity.snapshot",
        "source": "pi", "payload": {
            "contract_version": 1, "host_id": "bender", "instance_id": "i1", "session_id": "s1",
            "observed_at": _iso(NOW), "snapshot_at": _iso(NOW), "mission_revision": 1, "connection": "connected",
            "mission": {"id": "MSN-1", "title": "Port parser", "status": "active"},
            "activity": {"health": "stalled", "waitingReason": "approval of the plan", "workerDetails": []}}})
    kinds = _by_kind(inbox)
    (failed,) = kinds["mission_failed"]
    assert failed["key"] == "mission:m-failed" and failed["severity"] == "critical"   # the month-old failure is out of window
    (stalled,) = kinds["mission_attention"]
    assert stalled["key"] == "mission:pi:MSN-1" and stalled["severity"] == "warning"
    assert stalled["href"] == "#/missions/pi%3AMSN-1" and "approval of the plan" in stalled["detail"]


def test_a_stale_live_report_is_not_attention():
    repo, inbox, clock, _ = _world()
    IngestService(repo).ingest({
        "entity_type": "mission_activity", "entity_id": "pi:MSN-2", "event_type": "mission_activity.snapshot",
        "source": "pi", "payload": {
            "contract_version": 1, "host_id": "bender", "instance_id": "i1", "session_id": "s1",
            "observed_at": _iso(NOW), "snapshot_at": _iso(NOW), "mission_revision": 1, "connection": "connected",
            "mission": {"id": "MSN-2", "title": "Quiet", "status": "active"},
            "activity": {"health": "blocked", "workerDetails": []}}})
    assert "mission_attention" in _by_kind(inbox)
    clock[0] += 200
    assert "mission_attention" not in _by_kind(inbox)   # stale telemetry is unknown, never an incident


def test_control_plane_health_and_fleet_drift():
    repo, inbox, _, _ = _world()
    inbox._health = lambda: {"status": "degraded"}
    repo.fleet_inventory = lambda: {"summary": {"drifted": 3}}
    kinds = _by_kind(inbox)
    assert kinds["control_plane"][0]["severity"] == "critical" and kinds["control_plane"][0]["href"] == "#/topology"
    assert kinds["fleet_drift"][0]["href"] == "#/hosts?drift=1"


def test_an_unreadable_source_is_reported_not_hidden():
    repo, inbox, _, _ = _world()

    def boom():
        raise RuntimeError("db gone")

    repo.list_hosts = boom
    body = inbox.snapshot()
    assert body["partial"] is True and body["unavailable"] == ["hosts", "fleet"]   # fleet drift reads hosts
    item = next(i for i in body["items"] if i["key"] == "source:hosts")
    assert item["kind"] == "source_unavailable" and "db gone" not in json.dumps(item)


def test_stale_or_broken_ci_is_partial_but_not_configured_is_not():
    _, inbox, _, view = _world(ci={"status": "ok", "pipelines": {}, "stale": True})
    assert inbox.snapshot()["unavailable"] == ["ci"]
    view.update(stale=False, status="auth_error")
    assert inbox.snapshot()["unavailable"] == ["ci"]
    view.update(status="not_configured", available=False)
    assert inbox.snapshot()["unavailable"] == []


def _one_failed_agent():
    repo, inbox, clock, view = _world()
    sid = repo.create_session()
    repo._sessions[sid]["status"] = "failed"
    (item,) = inbox.snapshot()["items"]
    return repo, inbox, clock, item


def test_acknowledge_keeps_the_item_listed_but_not_open():
    _, inbox, _, item = _one_failed_agent()
    assert inbox.acknowledge(item["key"], item["fingerprint"])["stored"] is True
    assert inbox.snapshot()["items"] == []
    (listed,) = inbox.snapshot(state="all")["items"]
    assert listed["state"] == "acknowledged"
    counts = inbox.snapshot()["counts"]
    assert (counts["open"], counts["acknowledged"], counts["critical"]) == (0, 1, 0)
    assert inbox.acknowledge(item["key"], item["fingerprint"]) == {"stored": False, "reason": "unchanged"}


def test_snooze_hides_until_expiry_then_reopens_and_bounds_hold():
    _, inbox, clock, item = _one_failed_agent()
    for bad in (59, 3599, 604801, "1h", True, None):
        with pytest.raises(ValueError):
            inbox.snooze(item["key"], item["fingerprint"], bad)
    assert inbox.snooze(item["key"], item["fingerprint"], 2 * HOUR)["stored"] is True
    assert inbox.snapshot()["items"] == []
    (listed,) = inbox.snapshot(state="all")["items"]
    assert listed["state"] == "snoozed" and listed["snoozed_until"].startswith("2027-01-15T10:")
    clock[0] += 2 * HOUR + 1
    assert [i["state"] for i in inbox.snapshot()["items"]] == ["open"]


def test_a_changed_fingerprint_reopens_an_acknowledged_item():
    repo, inbox, _, item = _one_failed_agent()
    inbox.acknowledge(item["key"], item["fingerprint"])
    inbox._previous.clear()
    repo.upsert_host("h", "beast", "1.1.1.1")
    repo._hosts["h"]["status"] = "offline"
    other = [i for i in inbox.snapshot()["items"] if i["kind"] == "host_stale"][0]
    assert other["state"] == "open"
    inbox.acknowledge(other["key"], other["fingerprint"])
    repo._hosts["h"]["status"] = "healthy"
    repo._hosts["h"]["last_seen"] = _iso(NOW - 3 * HOUR)       # a different condition on the same key: stale, not down
    again = [i for i in inbox.snapshot()["items"] if i["kind"] == "host_stale"]
    assert again and again[0]["key"] == other["key"] and again[0]["fingerprint"] != other["fingerprint"]
    assert again[0]["state"] == "open"


def test_clear_undoes_an_ack():
    _, inbox, _, item = _one_failed_agent()
    inbox.acknowledge(item["key"], item["fingerprint"])
    assert inbox.clear(item["key"], item["fingerprint"])["stored"] is True
    assert [i["state"] for i in inbox.snapshot()["items"]] == ["open"]
    assert inbox.clear(item["key"], item["fingerprint"])["stored"] is False


def test_a_pair_the_server_does_not_derive_is_not_stored():
    repo, inbox, _, item = _one_failed_agent()
    before = len(repo.attention_events())
    assert inbox.acknowledge("session:nope", "abc")["stored"] is False
    assert inbox.acknowledge(item["key"], "not-the-fingerprint")["stored"] is False
    assert len(repo.attention_events()) == before
    for key, fp in (("", "a"), ("k" * 257, "a"), ("a b", "a"), ("k", ""), ("k", "x" * 65), ("k", "a b"), (None, "a")):
        with pytest.raises(ValueError):
            inbox.acknowledge(key, fp)


def test_writes_are_rate_capped():
    _, inbox, _, item = _one_failed_agent()
    for _ in range(30):
        inbox.snooze(item["key"], item["fingerprint"], 2 * HOUR)
    with pytest.raises(PermissionError):
        inbox.snooze(item["key"], item["fingerprint"], 2 * HOUR)


def test_a_cleared_condition_moves_to_resolved_and_the_ring_is_bounded_and_ages_out():
    repo, inbox, clock, item = _one_failed_agent()
    repo._sessions[item["key"].split(":", 1)[1]]["status"] = "active"
    body = inbox.snapshot(state="all")
    assert body["items"] == [] and body["counts"]["resolved"] == 1
    assert body["resolved"][0]["key"] == item["key"] and body["resolved"][0]["state"] == "resolved"
    clock[0] += 25 * HOUR
    assert inbox.snapshot(state="all")["resolved"] == []
    small = AttentionInbox(repo, ci_snapshot=lambda: {"status": "ok"}, now=lambda: clock[0],
                           config={"resolved": {"keep_hours": 24, "max_items": 2}})
    for n in range(4):
        sid = repo.create_session()
        repo._sessions[sid]["status"] = "failed"
        small.snapshot()
        repo._sessions[sid]["status"] = "active"
        small.snapshot()
    assert len(small.snapshot(state="all")["resolved"]) == 2


def test_a_source_outage_does_not_resolve_its_items():
    repo, inbox, _, item = _one_failed_agent()
    real = repo.list_sessions

    def boom(*a, **k):
        raise RuntimeError("x")

    repo.list_sessions = boom
    body = inbox.snapshot(state="all")
    assert body["counts"]["resolved"] == 0 and body["partial"] is True
    repo.list_sessions = real
    assert [i["key"] for i in inbox.snapshot()["items"]] == [item["key"]]


def test_item_caps_total_and_per_kind():
    repo, inbox, _, _ = _world()
    for _ in range(60):
        sid = repo.create_session()
        repo._sessions[sid]["status"] = "failed"
    body = inbox.snapshot()
    assert len(body["items"]) == 50 and body["truncated"] is True
    assert body["counts"]["open"] == 60


def test_ack_events_replay_after_a_restart():
    repo, inbox, clock, item = _one_failed_agent()
    inbox.acknowledge(item["key"], item["fingerprint"])
    twin = ControlPlaneRepo(now=lambda: clock[0])
    twin.import_state(repo.export_state())
    sid = item["key"].split(":", 1)[1]
    assert sid in twin._sessions
    revived = AttentionInbox(twin, ci_snapshot=lambda: {"status": "ok"}, now=lambda: clock[0])
    (listed,) = revived.snapshot(state="all")["items"]
    assert listed["state"] == "acknowledged"
    assert ("attention", "operator") not in {(t, i) for t, i, _ in twin._event_order}   # never in the feed


def test_filters_and_bad_parameters():
    repo, inbox, _, _ = _world(ci={"status": "ok", "pipelines": {"o/r": [_run(1, "failure", repository="o/r")]}})
    sid = repo.create_session()
    repo._sessions[sid]["status"] = "failed"
    assert {i["kind"] for i in inbox.snapshot(kind="ci_failed")["items"]} == {"ci_failed"}
    assert {i["severity"] for i in inbox.snapshot(severity="critical")["items"]} == {"critical"}
    for bad in ({"state": "x"}, {"kind": "x"}, {"severity": "x"}):
        with pytest.raises(ValueError):
            inbox.snapshot(**bad)


def test_status_and_health_ports_match_the_browser_rules():
    assert status_outcome("failed") == "failed" and status_outcome("Crashed") == "failed"
    assert status_outcome("active", "complete") == "finished" and status_outcome("paused_review") == "current"
    assert status_outcome(None, None) == "unknown" and status_outcome("weird") == "unknown"
    assert mission_health({"health": "healthy"}, {"status": "FAILED"}) == "failed"
    assert mission_health({}, {"status": "PAUSED_REVIEW"}) == "paused: review"
    assert mission_health({"health": "active", "workerDetails": [{"state": "blocked"}]}, {}) == "blocked"
    assert mission_health({"workerDetails": [{"state": "blocked", "endedAt": "x"}]}, {}) == "unknown"


def test_config_defaults_and_validation():
    cfg = attention_config(None)
    assert cfg["enabled"] and cfg["snooze"] == {"min_seconds": 3600, "max_seconds": 604800}
    assert cfg["notify"]["quiet_hours"] == {"start": 22, "end": 7} and cfg["resolved"]["max_items"] == 100
    assert attention_config({"enabled": False})["enabled"] is False
    for bad in ({"enabled": "yes"}, {"bogus": 1}, {"snooze": {"min_seconds": 0}},
                {"snooze": {"min_seconds": 100, "max_seconds": 50}}, {"snooze": {"nope": 1}},
                {"thresholds": {"llm_window": "30d"}}, {"notify": {"min_severity": "loud"}},
                {"notify": {"quiet_hours": {"start": 24, "end": 7}}}, {"resolved": {"max_items": 0}}, []):
        with pytest.raises(ValueError):
            attention_config(bad)


# ------------------------------------------------------------------ HTTP
def _call(svr, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{svr.port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _serve(repo, **kw):
    svr = ControlPlaneServer(repo, host="127.0.0.1", port=0, **kw)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr


def test_routes_on_the_operator_listener():
    repo = ControlPlaneRepo()
    sid = repo.create_session()
    repo._sessions[sid]["status"] = "failed"
    svr = _serve(repo, allow_open=True)
    svr.attention._ci_snapshot = lambda: {"status": "ok", "pipelines": {}}
    try:
        st, body = _call(svr, "GET", "/api/v1/attention")
        assert st == 200 and body["counts"]["open"] == 1 and body["items"][0]["kind"] == "agent_failed"
        item = body["items"][0]
        pair = {"key": item["key"], "fingerprint": item["fingerprint"]}
        assert _call(svr, "POST", "/api/v1/attention/ack", pair)[1]["stored"] is True
        assert _call(svr, "GET", "/api/v1/attention")[1]["items"] == []
        assert _call(svr, "GET", "/api/v1/attention?state=all")[1]["items"][0]["state"] == "acknowledged"
        assert _call(svr, "POST", "/api/v1/attention/clear", pair)[1]["stored"] is True
        assert _call(svr, "POST", "/api/v1/attention/snooze", {**pair, "seconds": 7200})[1]["stored"] is True
        assert _call(svr, "POST", "/api/v1/attention/snooze", {**pair, "seconds": 5})[0] == 400
        assert _call(svr, "POST", "/api/v1/attention/ack", {"key": "", "fingerprint": ""})[0] == 400
        assert _call(svr, "GET", "/api/v1/attention?kind=bogus")[0] == 400
        assert _call(svr, "GET", "/api/v1/attention?state=bogus")[0] == 400
        assert _call(svr, "POST", "/api/v1/attention/bogus", pair)[0] == 404
        (event,) = [e for e in repo.attention_events() if e["event_type"] == "attention_ack"]
        assert event["payload"]["actor"].startswith("operator:")
    finally:
        svr.shutdown()


def test_posts_need_the_operator_role_and_same_origin():
    repo = ControlPlaneRepo()
    svr = _serve(repo, api_keys={"op": "operator", "view": "viewer"})
    try:
        pair = {"key": "k", "fingerprint": "f"}
        assert _call(svr, "GET", "/api/v1/attention", headers={"X-API-Key": "view"})[0] == 200
        assert _call(svr, "POST", "/api/v1/attention/ack", pair, {"X-API-Key": "view"})[0] == 403
        assert _call(svr, "POST", "/api/v1/attention/ack", pair)[0] == 401
        status, _ = _call(svr, "POST", "/api/v1/attention/ack", pair,
                          {"X-API-Key": "op", "Origin": "https://evil.example"})
        assert status == 403
        assert _call(svr, "POST", "/api/v1/attention/ack", pair, {"X-API-Key": "op"})[0] == 200
    finally:
        svr.shutdown()


def test_disabled_attention_answers_404():
    svr = _serve(ControlPlaneRepo(), allow_open=True, attention={"enabled": False})
    try:
        assert _call(svr, "GET", "/api/v1/attention")[0] == 404
        assert _call(svr, "POST", "/api/v1/attention/ack", {"key": "k", "fingerprint": "f"})[0] == 404
    finally:
        svr.shutdown()


@pytest.mark.parametrize("method,path", [("GET", "/api/v1/attention"), ("GET", "/api/v1/attention?state=all"),
                                         ("POST", "/api/v1/attention/ack"), ("POST", "/api/v1/attention/snooze"),
                                         ("POST", "/api/v1/attention/clear"), ("GET", "/attention-view.js"),
                                         ("GET", "/attention")])
def test_attention_does_not_exist_on_the_public_listener(stack, method, path):
    status, _ = stack.pub.call(method, path, {"key": "k", "fingerprint": "f"} if method == "POST" else None)
    assert status == 404
