"""Deployment event store and its host-authenticated ingest route (deploy-4 Task 1).

Events come from the inferweave host updaters (``deploy/pipelines/deploy_event.py``):
``{id, ts, host, component, step, outcome, from, to, evidence}``, POSTed to
``/api/deployments/events`` with ``Authorization: Bearer <host token>``.
"""
import hashlib
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest
import yaml

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.deployments_monitor import (
    MAX_EVIDENCE,
    MAX_REQUEST_BYTES,
    DeploymentsMonitor,
    redact_evidence,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigStore

T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC).timestamp()
HOST_TOKEN = "placeholder-host-hive"
OTHER_TOKEN = "placeholder-host-beast"
REV_A = "a" * 40
REV_B = "b" * 40


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _ts(offset=0):
    return datetime.fromtimestamp(T0 + offset, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_IDS = iter(range(1, 10**9))


def _event(component="hive-gateway", step="cycle", outcome="ok", offset=0,
           frm=REV_A, to=REV_B, evidence="", **extra):
    ev = {"id": f"{next(_IDS):032x}", "ts": _ts(offset), "host": "login2.hive",
          "component": component, "step": f"deployment.{component}.{step}",
          "outcome": outcome, "from": frm, "to": to, "evidence": evidence}
    ev.update(extra)
    return ev


# ------------------------------------------------------------------ store

def test_ingest_is_idempotent_on_event_id():
    mon = DeploymentsMonitor(now=Clock())
    ev = _event(outcome="failed", evidence="boom")
    mon.ingest(ev)
    mon.ingest(dict(ev))
    assert len(mon.snapshot()["components"]["hive-gateway"]["timeline"]) == 1


def test_ingest_is_idempotent_on_host_ts_component_step_outcome_without_id():
    mon = DeploymentsMonitor(now=Clock())
    ev = _event()
    del ev["id"]
    mon.ingest(ev)
    mon.ingest(dict(ev))
    assert len(mon.snapshot()["components"]["hive-gateway"]["timeline"]) == 1


def test_started_and_ok_in_the_same_second_are_both_kept():
    # ts has one-second resolution and started/ok share the step string, so the
    # dedupe key must not collapse them (or the terminal outcome would be lost).
    mon = DeploymentsMonitor(now=Clock())
    started = _event(outcome="started")
    ok = _event(outcome="ok")
    del started["id"], ok["id"]
    mon.ingest(started)
    mon.ingest(ok)
    row = mon.snapshot()["components"]["hive-gateway"]
    assert [e["outcome"] for e in row["timeline"]] == ["started", "ok"]
    assert row["health"] == "healthy"


def test_snapshot_reflects_ok():
    clock = Clock(T0)
    mon = DeploymentsMonitor(now=clock, hold_s=600)
    mon.ingest(_event(outcome="started", offset=0))
    clock.t = T0 + 60
    mon.ingest(_event(outcome="ok", offset=60))
    clock.t = T0 + 120
    row = mon.snapshot()["components"]["hive-gateway"]
    assert row["running"] == REV_B
    assert row["target"] == REV_B
    assert row["step"] == "deployment.hive-gateway.cycle"
    assert row["health"] == "healthy"
    assert row["last_result"]["outcome"] == "ok"
    assert row["hold_remaining_s"] == 540  # ok arrived at +60, now +120
    assert row["clock_skew_s"] == 0
    clock.t = T0 + 10_000
    assert mon.snapshot()["components"]["hive-gateway"]["hold_remaining_s"] == 0


def test_snapshot_reflects_failed():
    mon = DeploymentsMonitor(now=Clock(T0 + 120))
    mon.ingest(_event(outcome="ok", offset=0, frm="", to=REV_A))
    mon.ingest(_event(outcome="started", offset=30))
    mon.ingest(_event(outcome="failed", offset=60, evidence="probe timed out"))
    row = mon.snapshot()["components"]["hive-gateway"]
    assert row["running"] == REV_A        # the failed roll never became running
    assert row["target"] == REV_B
    assert row["health"] == "unhealthy"
    assert row["last_result"]["outcome"] == "failed"
    assert row["hold_remaining_s"] is None
    assert row["timeline"][-1]["evidence"] == "probe timed out"


def test_rollback_restores_running_to_previous():
    mon = DeploymentsMonitor(now=Clock(T0 + 120))
    mon.ingest(_event(component="hive-nodes", step="install", outcome="ok", offset=0, frm="", to=REV_A))
    mon.ingest(_event(component="hive-nodes", step="install", outcome="ok", offset=10, frm=REV_A, to=REV_B))
    mon.ingest(_event(component="hive-nodes", step="rollback", outcome="rolled-back", offset=20,
                      frm=REV_B, to=REV_A))
    row = mon.snapshot()["components"]["hive-nodes"]
    assert row["running"] == REV_A
    assert row["health"] == "unhealthy"
    assert row["last_result"]["outcome"] == "rolled-back"


def test_order_is_arrival_order_not_host_time():
    # Host clocks are not trusted. A late event counts from its arrival; for an old
    # "started" that is the conservative reading (updating: the stage does not advance).
    mon = DeploymentsMonitor(now=Clock(T0 + 120))
    mon.ingest(_event(outcome="ok", offset=60))
    mon.ingest(_event(outcome="started", offset=0))
    row = mon.snapshot()["components"]["hive-gateway"]
    assert row["health"] == "updating"
    assert [e["outcome"] for e in row["timeline"]] == ["ok", "started"]


def test_host_clock_ahead_is_accepted_shown_and_cannot_freeze_a_component():
    # The emitter cannot replay a refused event, so a skewed one is kept, not refused.
    clock = Clock(T0)
    mon = DeploymentsMonitor(now=clock, hold_s=600)
    mon.ingest(_event(outcome="failed", offset=3600))   # host clock one hour ahead
    row = mon.snapshot()["components"]["hive-gateway"]
    assert row["health"] == "unhealthy"
    assert row["clock_skew_s"] == 3600
    assert row["timeline"][-1]["host_ts"] == _ts(3600)
    assert row["timeline"][-1]["received_at"] == _ts(0)
    clock.t = T0 + 30
    mon.ingest(_event(outcome="ok", offset=30))          # a correct host, later
    row = mon.snapshot()["components"]["hive-gateway"]
    assert row["health"] == "healthy"
    assert row["hold_remaining_s"] == 600
    assert row["clock_skew_s"] == 0


def test_host_clock_behind_still_gets_the_full_hold():
    mon = DeploymentsMonitor(now=Clock(T0), hold_s=600)
    mon.ingest(_event(outcome="ok", offset=-900))       # host clock 15 min behind
    row = mon.snapshot()["components"]["hive-gateway"]
    assert row["hold_remaining_s"] == 600
    assert row["clock_skew_s"] == -900


def test_a_replayed_or_late_ok_never_shortens_the_hold():
    clock = Clock(T0)
    mon = DeploymentsMonitor(now=clock, hold_s=600)
    ok = _event(outcome="ok", offset=0)
    mon.ingest(ok)
    clock.t = T0 + 300
    mon.ingest(dict(ok))                                 # exact replay: a no-op
    assert mon.snapshot()["components"]["hive-gateway"]["hold_remaining_s"] == 300
    mon.ingest(_event(outcome="ok", offset=-3000))      # a late, older ok: restarts the hold
    assert mon.snapshot()["components"]["hive-gateway"]["hold_remaining_s"] == 600
    clock.t = T0 + 10_000
    assert mon.snapshot()["components"]["hive-gateway"]["hold_remaining_s"] == 0


def test_distinct_ids_in_the_same_second_are_both_kept():
    mon = DeploymentsMonitor(now=Clock())
    mon.ingest(_event(step="install", outcome="failed", evidence="first failure"))
    mon.ingest(_event(step="install", outcome="failed", evidence="second failure"))
    assert len(mon.snapshot()["components"]["hive-gateway"]["timeline"]) == 2


def test_running_falls_back_to_the_most_recent_from():
    clock = Clock(T0)
    mon = DeploymentsMonitor(now=clock)
    mon.ingest(_event(outcome="started", frm=REV_A, to=REV_B))
    clock.t += 1
    mon.ingest(_event(outcome="failed", frm="", to=REV_B))     # no from on the later event
    assert mon.snapshot()["components"]["hive-gateway"]["running"] == REV_A


def test_skipped_keeps_previous_health():
    mon = DeploymentsMonitor(now=Clock(T0 + 120))
    mon.ingest(_event(outcome="ok", offset=0))
    mon.ingest(_event(outcome="skipped", offset=30))
    row = mon.snapshot()["components"]["hive-gateway"]
    assert row["health"] == "healthy"
    assert row["last_result"]["outcome"] == "skipped"


def test_unknown_component_row_is_absent_and_empty_snapshot_is_valid():
    assert DeploymentsMonitor(now=Clock()).snapshot() == {"components": {}}


@pytest.mark.parametrize("mutate, message", [
    (lambda e: e.pop("component"), "component"),
    (lambda e: e.update(outcome="exploded"), "outcome"),
    (lambda e: e.update(ts="yesterday"), "ts"),
    (lambda e: e.update(step="deployment.other.cycle"), "step"),
    (lambda e: e.update(component="Bad Name!"), "component"),
    (lambda e: e.update(evidence=7), "evidence"),
    (lambda e: e.update(to="x" * 500), "to"),
    (lambda e: e.update(id="not hex!"), "id"),
    (lambda e: e.update(host=""), "host"),
    (lambda e: e.update(surprise="field"), "surprise"),
])
def test_malformed_events_are_rejected_with_a_clear_error(mutate, message):
    mon = DeploymentsMonitor(now=Clock())
    ev = _event()
    mutate(ev)
    with pytest.raises(ValueError, match=message):
        mon.ingest(ev)
    assert mon.snapshot() == {"components": {}}


def test_non_dict_event_is_rejected():
    with pytest.raises(ValueError, match="object"):
        DeploymentsMonitor(now=Clock()).ingest(["not", "an", "event"])


def test_timeline_is_bounded_per_component():
    mon = DeploymentsMonitor(now=Clock(T0 + 10_000), timeline_limit=5)
    mon.ingest(_event(outcome="ok", offset=0, frm="", to=REV_A))
    for i in range(1, 40):
        mon.ingest(_event(outcome="failed", offset=i))
    row = mon.snapshot()["components"]["hive-gateway"]
    assert len(row["timeline"]) == 5
    assert row["timeline"][-1]["host_ts"] == _ts(39)
    # state outlives the bounded timeline
    assert row["running"] == REV_A


def test_component_count_is_bounded():
    mon = DeploymentsMonitor(now=Clock(), max_components=3)
    for i in range(3):
        mon.ingest(_event(component=f"c{i}"))
    with pytest.raises(ValueError, match="components"):
        mon.ingest(_event(component="c9"))
    # an existing component still ingests
    mon.ingest(_event(component="c0", offset=5))
    assert set(mon.snapshot()["components"]) == {"c0", "c1", "c2"}


def test_dedupe_memory_is_bounded():
    mon = DeploymentsMonitor(now=Clock(), seen_limit=10, timeline_limit=5)
    for i in range(50):
        mon.ingest(_event(offset=i))
    assert len(mon._seen) <= 10


def test_evidence_is_capped_keeping_the_tail():
    mon = DeploymentsMonitor(now=Clock())
    evidence = "early line\n" + ("x" * 80 + "\n") * 400 + "FINAL ERROR"
    mon.ingest(_event(outcome="failed", evidence=evidence))
    stored = mon.snapshot()["components"]["hive-gateway"]["timeline"][-1]["evidence"]
    assert len(stored) <= MAX_EVIDENCE
    assert stored.endswith("FINAL ERROR")
    assert stored.startswith("[earlier output truncated]")


@pytest.mark.parametrize("secret_text, secret", [
    ("Authorization: Bearer abc.def-123", "abc.def-123"),
    ("curl -H 'Bearer s3cr3tvalue'", "s3cr3tvalue"),
    ("GET /x?token=tk123 HTTP/1.1", "tk123"),
    ("api key=k9 rest", "k9"),
    ("db_key=k9", "k9"),
    ("password: hunter2", "hunter2"),
    ("fetch https://user:pw1234@example.org/x", "pw1234"),
    ("fetch https://tokenonly@example.org/x", "tokenonly"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----", "AAAA"),
    ("-----BEGIN RSA PRIVATE KEY-----\nBBBBunterminated", "BBBBunterminated"),
    ("Cookie: session=c00kieval", "c00kieval"),
    ("Set-Cookie: sid=s3tc00kie; Path=/", "s3tc00kie"),
    ("X-Api-Key: xak123", "xak123"),
    ("export IW_GATEWAY_KEK=kekvalue1", "kekvalue1"),
    ("export DB_KEY=dbk3y", "dbk3y"),
    ("curl -u admin:pw9 https://h/x", "pw9"),
    ("curl --user admin:pw8 https://h/x", "pw8"),
    ("sshpass -p sshpw1 ssh host", "sshpw1"),
    ("mysql -uroot -pMyPw1 db", "MyPw1"),
    ("inferweave gateway --admin-secret adm1n --port 1", "adm1n"),
    ("run --api-token=tok42 now", "tok42"),
    ("docker login -p dockpw1 reg", "dockpw1"),
    ("redis-cli -a redpw1 ping", "redpw1"),
    ("echo dockpw2 | docker login --password-stdin", "dockpw2"),
    ("https://b/o?X-Amz-Signature=sig123abc", "sig123abc"),
    ("token ghp_" + "A" * 30, "ghp_" + "A" * 30),
])
def test_redact_evidence_strips_secrets(secret_text, secret):
    out = redact_evidence(secret_text)
    assert secret not in out
    assert "[REDACTED]" in out


def test_evidence_is_redacted_a_second_time_before_storing():
    # The host redacts first; herdr does not trust that and redacts again.
    mon = DeploymentsMonitor(now=Clock())
    mon.ingest(_event(outcome="failed",
                      evidence="probe: https://op:hunter22@zapp/v1 failed\nAuthorization: Bearer XYZ.secret"))
    stored = mon.snapshot()["components"]["hive-gateway"]["timeline"][-1]["evidence"]
    assert "hunter22" not in stored and "XYZ.secret" not in stored
    assert "probe:" in stored and "failed" in stored


def test_events_survive_a_new_repo_over_the_same_sink(acknowledged_sink):
    clock = Clock(T0 + 120)
    repo = ControlPlaneRepo(sink=acknowledged_sink, now=clock)
    repo.deployments.ingest(_event(outcome="started", offset=0), reported_by="hive")
    ok = _event(outcome="ok", offset=60)
    repo.deployments.ingest(ok, reported_by="hive")
    repo.deployments.ingest(dict(ok), reported_by="hive")   # duplicate: not persisted twice
    persisted = [e for e in acknowledged_sink.events if e["entity_type"] == "deployment"]
    assert len(persisted) == 2
    before = repo.deployments.snapshot()

    class Source:
        def missions(self):
            return []

        def sessions(self):
            return []

        def services(self):
            return []

        def hosts(self):
            return []

        def events(self):
            return list(acknowledged_sink.events)

    fresh = ControlPlaneRepo(now=clock)
    load_into(fresh, Source())
    after = fresh.deployments.snapshot()
    assert after == before
    # the replayed id is still known: a late duplicate stays idempotent
    fresh.deployments.ingest(dict(ok))
    assert fresh.deployments.snapshot() == before
    # private stream: deployment events never sit in the shared event feed
    assert not any(k[0] == "deployment" for k in fresh._events)


# ------------------------------------------------------------------ route

def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


@pytest.fixture()
def dep_server(tmp_path, acknowledged_sink):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "sections": {},
        "hosts": {"hive": {"roles": ["node"], "token_sha256": _sha(HOST_TOKEN)},
                  "beast": {"roles": ["ci"], "token_sha256": _sha(OTHER_TOKEN)}},
    }))
    os.chmod(path, 0o600)
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, api_keys={"opkey": "operator", "viewkey": "viewer"},
                             fleet_config=FleetConfigStore(path))
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield svr.port, repo
    svr.shutdown()


def _post(port, path, *, body=None, raw=None, headers=None, content_type="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = dict(headers or {})
    if content_type:
        h["Content-Type"] = content_type
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _bearer(token=HOST_TOKEN):
    return {"Authorization": "Bearer " + token}


def test_route_ingests_with_host_bearer_token(dep_server):
    port, repo = dep_server
    status, body = _post(port, "/api/deployments/events", body=_event(), headers=_bearer())
    assert status == 200 and body == {"ok": True}
    row = repo.deployments.snapshot()["components"]["hive-gateway"]
    assert row["timeline"][-1]["reported_by"] == "hive"


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer nope"},
    {"Authorization": "Basic " + HOST_TOKEN},
    {"Authorization": "Bearer"},
    {"X-API-Key": "opkey"},           # an operator key is not a host credential
])
def test_route_requires_a_host_token(dep_server, headers):
    port, repo = dep_server
    status, body = _post(port, "/api/deployments/events", body=_event(), headers=headers)
    assert status == 401 and body == {"error": "unauthorized"}
    assert repo.deployments.snapshot() == {"components": {}}


def test_host_token_grants_nothing_else(dep_server):
    port, _ = dep_server
    for path in ("/api/v1/ingest", "/api/v1/handoffs"):
        assert _post(port, path, body={}, headers=_bearer())[0] == 401
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/hosts", headers=_bearer())
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 401


def test_route_rejects_malformed_events(dep_server):
    port, repo = dep_server
    bad = _event(outcome="exploded")
    status, body = _post(port, "/api/deployments/events", body=bad, headers=_bearer())
    assert status == 400 and "outcome" in body["error"]
    status, body = _post(port, "/api/deployments/events", raw=b"{not json", headers=_bearer())
    assert status == 400
    status, _ = _post(port, "/api/deployments/events", body=_event(), headers=_bearer(),
                      content_type="text/plain")
    assert status == 415
    assert repo.deployments.snapshot() == {"components": {}}


def test_route_rejects_deeply_nested_json(dep_server):
    port, repo = dep_server
    depth = 50_000
    raw = b"[" * depth + b"]" * depth
    assert len(raw) < MAX_REQUEST_BYTES
    status, body = _post(port, "/api/deployments/events", raw=raw, headers=_bearer())
    assert status == 400 and "nested" in body["error"]
    assert repo.deployments.snapshot() == {"components": {}}


def test_route_rejects_oversize_bodies_before_reading_them(dep_server):
    # Headers only, no body: a 413 proves the size is decided before any read
    # (and avoids a reset racing the response on an unread body).
    port, repo = dep_server
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall((f"POST /api/deployments/events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      f"Authorization: Bearer {HOST_TOKEN}\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {MAX_REQUEST_BYTES + 1}\r\n\r\n").encode())
        reply = sock.recv(4096).decode()
    assert reply.startswith("HTTP/1.1 413")
    assert repo.deployments.snapshot() == {"components": {}}


def test_route_accepts_worst_case_escaped_evidence(dep_server):
    # The emitter caps evidence at 8192 characters and json.dumps escapes astral
    # characters to 12 bytes each; that event must still fit.
    port, repo = dep_server
    ev = _event(outcome="failed", evidence="\U0001F600" * MAX_EVIDENCE)
    assert len(json.dumps(ev)) > 64 * 1024
    assert _post(port, "/api/deployments/events", body=ev, headers=_bearer())[0] == 200
    assert repo.deployments.snapshot()["components"]["hive-gateway"]["timeline"][-1]["evidence"]


def test_route_is_idempotent_over_http(dep_server):
    port, repo = dep_server
    ev = _event(outcome="failed")
    for _ in range(3):
        assert _post(port, "/api/deployments/events", body=ev, headers=_bearer())[0] == 200
    assert len(repo.deployments.snapshot()["components"]["hive-gateway"]["timeline"]) == 1


def test_route_redacts_evidence_again(dep_server):
    port, repo = dep_server
    ev = _event(outcome="failed", evidence="git clone https://x:ghtok999@github.com/o/r\npassword=hunter2")
    assert _post(port, "/api/deployments/events", body=ev, headers=_bearer())[0] == 200
    stored = repo.deployments.snapshot()["components"]["hive-gateway"]["timeline"][-1]["evidence"]
    assert "ghtok999" not in stored and "hunter2" not in stored
