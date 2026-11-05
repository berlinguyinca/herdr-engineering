"""fleet_rollout ingest + GET /api/v1/fleet-rollouts*: the rollout event model (spec section 8).

No mocks: a real ControlPlaneServer on a loopback port, real HTTP requests, a real event sink.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ingest import INGEST_ENTITY_TYPES, validate_fleet_rollout
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_watch.plan import rollout_id as make_rollout_id

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
RID = make_rollout_id({"autospec": SHA_A})
RID2 = make_rollout_id({"autospec": SHA_B})
TS = "2026-11-04T10:00:00+00:00"
TS2 = "2026-11-04T11:00:00+00:00"


def ev(rid=RID, seq=0, **fields):
    return {"rollout_id": rid, "ts": TS, "seq": seq, **fields}


TRIGGER = {"repo": "autospec", "sha": SHA_A, "pr": 7, "pr_title": "Add thing", "merged_at": TS}
TARGETS = [{"repo": "autospec", "sha": SHA_A, "before": SHA_C}]


def observed(rid=RID, seq=0, **over):
    return ev(rid, seq, **{"trigger": TRIGGER, "targets": TARGETS, **over})


def host_finished(rid=RID, seq=5, host="beast", status="ok", **over):
    return ev(rid, seq, **{"host": host, "repo": "autospec", "status": status,
                           "before_sha": SHA_C, "after_sha": SHA_A, "started_at": TS, "finished_at": TS,
                           "duration_ms": 1200, "checks": [{"name": "parity", "ok": True}],
                           "log_tail": "installed\nok", **over})


def envelope(event_type, payload, rid=None):
    return {"source": "fleet-watch", "entity_type": "fleet_rollout",
            "entity_id": rid or payload["rollout_id"], "event_type": event_type, "payload": payload}


# --------------------------------------------------------------------------- unit: validator

def test_fleet_rollout_is_an_ingest_entity_type():
    assert "fleet_rollout" in INGEST_ENTITY_TYPES


def test_valid_events_pass_and_return_a_clean_copy():
    payload = observed()
    clean = validate_fleet_rollout(RID, "rollout.observed", payload)
    assert clean == payload and clean is not payload
    assert validate_fleet_rollout(RID, "host.finished", host_finished())["status"] == "ok"


def test_event_id_is_deterministic_rollout_and_seq():
    from herdr_engineering.control_plane.fleet_rollout import event_id
    assert event_id(RID, 3) == f"{RID}:3"


def _without(payload, key):
    return {k: v for k, v in payload.items() if k != key}


@pytest.mark.parametrize("event_type,payload", [
    ("rollout.nonsense", observed()),                                   # unknown event type
    ("rollout.observed", {**observed(), "env": {"HOME": "/home/x"}}),    # unknown field
    ("rollout.observed", {**observed(), "log_tail": "x"}),               # known field, wrong event type
    ("rollout.observed", _without(observed(), "trigger")),
    ("rollout.observed", {**observed(), "rollout_id": "f" * 16}),        # must equal entity_id
    ("rollout.observed", {**observed(), "seq": -1}),
    ("rollout.observed", {**observed(), "seq": True}),
    ("rollout.observed", {**observed(), "seq": "1"}),
    ("rollout.observed", {**observed(), "ts": "yesterday"}),
    ("rollout.observed", {**observed(), "ts": "2026-11-04T10:00:00+02:00"}),   # not UTC
    ("rollout.observed", {**observed(), "ts": "2026-11-04T10:00:00"}),         # naive
    ("rollout.observed", observed(trigger={**TRIGGER, "repo": "evil"})),
    ("rollout.observed", observed(trigger={**TRIGGER, "sha": "abc"})),
    ("rollout.observed", observed(trigger={**TRIGGER, "extra": 1})),
    ("rollout.observed", observed(trigger={**TRIGGER, "pr": -1})),
    ("rollout.observed", observed(trigger={**TRIGGER, "pr_title": "t" * 121})),
    ("rollout.observed", observed(trigger={**TRIGGER, "pr_title": "bad\x00title"})),
    ("rollout.observed", observed(targets=[{"repo": "autospec", "sha": SHA_A, "before": "short"}])),
    ("rollout.observed", observed(targets=[{"repo": "autospec", "sha": SHA_A, "before": None, "x": 1}])),
    ("rollout.observed", observed(targets=[TARGETS[0]] * 6)),
    ("host.finished", host_finished(status="exploded")),                # closed enum
    ("host.finished", host_finished(reason="because I said so")),
    ("host.finished", host_finished(host="Beast")),                      # inventory name charset
    ("host.finished", host_finished(host="a" * 41)),
    ("host.finished", host_finished(host="beast; rm -rf /")),
    ("host.finished", host_finished(duration_ms=-1)),
    ("host.finished", host_finished(after_sha="A" * 40)),                # uppercase hex
    ("host.finished", host_finished(checks=[{"name": "n" * 41, "ok": True}])),
    ("host.finished", host_finished(checks=[{"name": "n", "ok": "yes"}])),
    ("host.finished", host_finished(checks=[{"name": "n", "ok": True}] * 33)),
    ("host.finished", host_finished(counts={"dirty_paths": 1, "secret": 2})),
    ("host.finished", host_finished(counts={"dirty_paths": -1})),
    ("host.finished", host_finished(links={"pi_session_rollout": "x" * 100})),
    ("host.finished", host_finished(links={"other": "x"})),
    ("host.finished", _without(host_finished(), "host")),
    ("rollout.halted", ev(halted_at_host="bender; x", reason="health_failed")),
], ids=lambda v: v if isinstance(v, str) else "")
def test_malformed_events_are_rejected(event_type, payload):
    with pytest.raises(ValueError):
        validate_fleet_rollout(RID, event_type, payload)


def test_oversize_log_tail_is_rejected_not_truncated():
    with pytest.raises(ValueError, match="log_tail"):
        validate_fleet_rollout(RID, "host.finished", host_finished(log_tail="x" * 2049))
    with pytest.raises(ValueError, match="log_tail"):
        validate_fleet_rollout(RID, "host.finished", host_finished(log_tail="\n".join("l" for _ in range(41))))
    ok = validate_fleet_rollout(RID, "host.finished", host_finished(log_tail="\n".join("l" for _ in range(40))))
    assert ok["log_tail"].count("\n") == 39


@pytest.mark.parametrize("tail", [
    "Authorization: Bearer abc",
    "authorization: token xyz",
    "curl https://user:hunter2@github.com/x.git",
    "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "export TOKEN=abc",
    "token=abc",
    "PASSWORD=hunter2",
    "password: hunter2",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "key AKIAIOSFODNN7EXAMPLEAKIAIOSFODNN",
    "reading /home/wohlgemuth/.ssh/id_ed25519",
    "reading /Users/alice/Library/x",
    "bell \x07 char",
], ids=lambda t: t[:30])
def test_secret_and_home_path_log_tails_are_rejected_by_the_server(tail):
    with pytest.raises(ValueError, match="log_tail"):
        validate_fleet_rollout(RID, "host.finished", host_finished(log_tail=tail))


def test_pr_title_rejects_secret_markers():
    for bad in ("fix password=hunter2", "see https://u:p@h/x", "Authorization: Bearer x"):
        with pytest.raises(ValueError, match="pr_title"):
            validate_fleet_rollout(RID, "rollout.observed", observed(trigger={**TRIGGER, "pr_title": bad}))


def test_rollout_id_shape_is_enforced():
    with pytest.raises(ValueError, match="rollout_id"):
        validate_fleet_rollout("nothex", "rollout.observed", {**observed(), "rollout_id": "nothex"})


# --------------------------------------------------------------------------- integration: HTTP

@pytest.fixture()
def server(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, scheduler_admission=True)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"

    def post(body):
        req = urllib.request.Request(base + "/api/v1/ingest", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(path="/api/v1/fleet-rollouts", headers=None):
        req = urllib.request.Request(base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, (json.loads(e.read() or b"null") if e.code != 304 else None)

    def raw(path):
        with urllib.request.urlopen(base + path) as r:
            return r.status, dict(r.headers)

    yield post, get, repo, raw
    svr.shutdown()


def seed_rollout(post, rid=RID, sha=SHA_A, halted=False):
    trigger = {**TRIGGER, "sha": sha}
    targets = [{"repo": "autospec", "sha": sha, "before": SHA_C}]
    assert post(envelope("rollout.observed", ev(rid, 0, trigger=trigger, targets=targets)))[0] == 200
    assert post(envelope("rollout.planned", ev(rid, 1, trigger=trigger, targets=targets)))[0] == 200
    assert post(envelope("rollout.started", ev(rid, 2, targets=targets)))[0] == 200
    assert post(envelope("host.finished", host_finished(rid, 3, after_sha=sha)))[0] == 200
    if halted:
        bender = host_finished(rid, 4, "bender", "failed", reason="health_failed", after_sha=None)
        assert post(envelope("host.finished", bender))[0] == 200
        assert post(envelope("rollout.halted", ev(rid, 5, halted_at_host="bender", reason="health_failed")))[0] == 200
    else:
        assert post(envelope("rollout.succeeded", ev(rid, 4)))[0] == 200


def test_empty_list(server):
    _, get, *_ = server
    assert get() == (200, {"rollouts": []})


def test_ingest_then_list_summary_and_detail(server):
    post, get, *_ = server
    seed_rollout(post)
    status, body = get()
    assert status == 200
    [summary] = body["rollouts"]
    assert summary["rollout_id"] == RID and summary["status"] == "succeeded"
    assert summary["trigger"]["pr"] == 7 and summary["targets"][0]["sha"] == SHA_A
    assert summary["event_count"] == 5 and summary["updated_at"] == TS
    status, detail = get(f"/api/v1/fleet-rollouts/{RID}")
    assert status == 200 and detail["rollout_id"] == RID
    assert [e["seq"] for e in detail["events"]] == [0, 1, 2, 3, 4]
    [row] = detail["hosts"]
    assert (row["host"], row["repo"], row["status"], row["after_sha"]) == ("beast", "autospec", "ok", SHA_A)
    assert row["log_tail"] == "installed\nok"


def test_halted_rollout_reports_where_and_why(server):
    post, get, *_ = server
    seed_rollout(post, halted=True)
    [summary] = get()[1]["rollouts"]
    assert (summary["status"], summary["halted_at_host"], summary["reason"]) == ("halted", "bender", "health_failed")
    hosts = get(f"/api/v1/fleet-rollouts/{RID}")[1]["hosts"]
    assert [(h["host"], h["status"]) for h in hosts] == [("beast", "ok"), ("bender", "failed")]


def test_list_is_newest_first_limited_and_repo_filtered(server):
    post, get, *_ = server
    seed_rollout(post)
    later = observed(RID2, 0, ts=TS2, trigger={**TRIGGER, "repo": "pi-engineering", "sha": SHA_B},
                     targets=[{"repo": "pi-engineering", "sha": SHA_B, "before": None}])
    assert post(envelope("rollout.observed", later))[0] == 200
    assert [r["rollout_id"] for r in get()[1]["rollouts"]] == [RID2, RID]
    assert [r["rollout_id"] for r in get("/api/v1/fleet-rollouts?limit=1")[1]["rollouts"]] == [RID2]
    assert [r["rollout_id"] for r in get("/api/v1/fleet-rollouts?repo=autospec")[1]["rollouts"]] == [RID]
    assert get("/api/v1/fleet-rollouts?repo=nope")[1] == {"rollouts": []}
    assert get("/api/v1/fleet-rollouts?limit=abc")[0] == 200   # bad limit falls back to the default


def test_unknown_rollout_is_404(server):
    _, get, *_ = server
    assert get(f"/api/v1/fleet-rollouts/{'0' * 16}")[0] == 404
    assert get("/api/v1/fleet-rollouts/not-an-id")[0] == 404


def test_replaying_an_event_is_idempotent(server):
    post, get, repo, _ = server
    first = envelope("rollout.observed", observed())
    assert post(first)[0] == 200
    assert post(first)[0] == 200
    assert len(repo.stream("fleet_rollout", RID)) == 1
    assert get()[1]["rollouts"][0]["event_count"] == 1


def test_replay_with_different_content_for_a_seq_is_a_conflict_and_stores_nothing_new(server):
    post, _, repo, _ = server
    assert post(envelope("rollout.observed", observed()))[0] == 200
    changed = observed(targets=[{"repo": "autospec", "sha": SHA_B, "before": None}])
    assert post(envelope("rollout.observed", changed))[0] == 409
    assert len(repo.stream("fleet_rollout", RID)) == 1


def test_a_mismatched_entity_id_is_a_400(server):
    post, get, *_ = server
    assert post(envelope("rollout.observed", observed(), rid=RID2))[0] == 400
    assert get() == (200, {"rollouts": []})


def test_home_path_in_log_tail_is_a_400_and_stores_nothing(server):
    post, get, repo, _ = server
    status, body = post(envelope("host.finished", host_finished(log_tail="cd /home/wohlgemuth/work")))
    assert status == 400 and "log_tail" in json.dumps(body), (status, body)
    assert repo.stream("fleet_rollout", RID) == []


def test_a_token_in_log_tail_never_reaches_storage_even_through_the_http_door(server):
    # The ingest door's blanket redactor and the validator are independent layers; whichever fires, the token is gone.
    post, get, repo, _ = server
    status, _ = post(envelope("host.finished", host_finished(log_tail="GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789")))
    stored = json.dumps(repo.stream("fleet_rollout", RID))
    assert "ghp_" not in stored and "abcdefghijklmnopqrstuvwxyz" not in stored


def test_unknown_field_is_a_400_and_stores_nothing(server):
    post, _, repo, _ = server
    assert post(envelope("rollout.observed", {**observed(), "environment": {"A": "b"}}))[0] == 400
    assert repo.stream("fleet_rollout", RID) == []


def test_reads_support_etag(server):
    post, get, _, raw = server
    seed_rollout(post)
    _, headers = raw("/api/v1/fleet-rollouts")
    etag = headers.get("ETag") or headers.get("Etag")
    assert etag
    assert get("/api/v1/fleet-rollouts", headers={"If-None-Match": etag})[0] == 304


def test_versions_projection(server):
    post, get, *_ = server
    seed_rollout(post)
    pin = ev(RID, 5, targets=TARGETS)
    assert post(envelope("lock.pinned", pin))[0] == 200
    # a later rolled-back host row wins for that host x repo; a failed one never counts as a version
    assert post(envelope("rollout.observed", observed(
        RID2, 0, ts=TS2, trigger={**TRIGGER, "sha": SHA_B},
        targets=[{"repo": "autospec", "sha": SHA_B, "before": SHA_A}])))[0] == 200
    rb = host_finished(RID2, 1, "bender", "rolled_back", after_sha=SHA_C, finished_at=TS2)
    fail = host_finished(RID2, 2, "macbook-pro", "failed", reason="ssh_failed", after_sha=None)
    assert post(envelope("host.finished", rb))[0] == 200
    assert post(envelope("host.finished", fail))[0] == 200
    status, body = get("/api/v1/fleet-rollouts/versions")
    assert status == 200
    hosts = {h["host"]: h["repos"] for h in body["hosts"]}
    assert hosts["beast"]["autospec"] == {"sha": SHA_A, "since": TS, "rollout_id": RID, "status": "ok"}
    assert hosts["bender"]["autospec"]["status"] == "rolled_back" and hosts["bender"]["autospec"]["sha"] == SHA_C
    assert "macbook-pro" not in hosts
    assert body["main_heads"] == {"autospec": SHA_B}
    assert body["lock"] == {"repos": {"autospec": SHA_A}, "pinned_at": TS, "rollout_id": RID}


def test_versions_is_empty_without_events(server):
    _, get, *_ = server
    assert get("/api/v1/fleet-rollouts/versions") == (200, {"hosts": [], "main_heads": {}, "lock": None, "sources": {}})


def test_source_events_do_not_create_rollouts_but_show_in_versions(server):
    from herdr_engineering.control_plane.fleet_rollout import source_stream_id
    post, get, *_ = server
    sid = source_stream_id("autospec")
    payload = {"rollout_id": sid, "ts": TS, "seq": 0, "repo": "autospec", "reason": "rate_limited"}
    assert post(envelope("source.degraded", payload))[0] == 200
    assert get()[1] == {"rollouts": []}
    assert get("/api/v1/fleet-rollouts/versions")[1]["sources"] == {
        "autospec": {"state": "degraded", "reason": "rate_limited", "since": TS}}
    rec = {"rollout_id": sid, "ts": TS2, "seq": 1, "repo": "autospec"}
    assert post(envelope("source.recovered", rec))[0] == 200
    assert get("/api/v1/fleet-rollouts/versions")[1]["sources"]["autospec"]["state"] == "ok"


def test_events_survive_reload(server, acknowledged_sink):
    post, *_ = server
    seed_rollout(post)
    fresh = ControlPlaneRepo()
    for event in acknowledged_sink.events:
        fresh.record_event(event["entity_type"], event["entity_id"], event["event_type"], event["payload"])
    assert [r["rollout_id"] for r in fresh.list_fleet_rollouts()] == [RID]
    assert fresh.list_fleet_rollouts()[0]["status"] == "succeeded"


# --------------------------------------------------------------------------- review round 1: secret shapes everywhere

#: (text, fragment that must never be readable back)
SECRET_SHAPES = [
    ("https://user:pw@example.com/x", "user:pw"),
    ("/home/alice/.aws", "alice"),
    ("/root/.ssh/id_rsa", "id_rsa"),
    ("/root/.config", "/root"),
    ("/Users/bob", "bob"),
    ("Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("Bearer abcdef123456", "abcdef123456"),
    ("ssh -i ~/.ssh/id_ed25519 host", "id_ed25519"),
    ("/var/lib/x/.ssh/id_rsa", "id_rsa"),
    ("pwd=hunter2", "hunter2"),
    ("passwd=hunter2", "hunter2"),
    ("Cookie: session=abc12345", "abc12345"),
    ("machine x login u password p", "login u"),
    ("ghp_" + "a" * 36, "ghp_"),
    ("sk-abcdef123456", "sk-abc"),
    ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7"),
    ("ASIAIOSFODNN7EXAMPLE", "ASIAIOSFODNN7"),
    ("xoxb-123-456-abc", "xoxb"),
    ("eyJhbGciOi.eyJzdWIi.sig", "eyJhbGciOi"),
    ("tok " + "x" * 30, "xxxxxxxx"),
    ("-----BEGIN RSA PRIVATE KEY-----", "PRIVATE KEY"),
    ("BEGIN OPENSSH PRIVATE KEY", "PRIVATE KEY"),
    ("authorization: basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("[REDACTED] dXNlcjpwYXNz", "dXNlcjpwYXNz"),
]
SHAPE_IDS = [s[:24] for s, _ in SECRET_SHAPES]


@pytest.mark.parametrize("text,_needle", SECRET_SHAPES, ids=SHAPE_IDS)
def test_check_names_reject_secret_shapes(text, _needle):
    with pytest.raises(ValueError, match="check name"):
        validate_fleet_rollout(RID, "host.finished", host_finished(checks=[{"name": text, "ok": True}]))


@pytest.mark.parametrize("text,_needle", SECRET_SHAPES, ids=SHAPE_IDS)
def test_pr_titles_reject_secret_shapes(text, _needle):
    with pytest.raises(ValueError, match="pr_title"):
        validate_fleet_rollout(RID, "rollout.observed", observed(trigger={**TRIGGER, "pr_title": text}))


@pytest.mark.parametrize("text,_needle", SECRET_SHAPES, ids=SHAPE_IDS)
def test_log_tails_reject_secret_shapes(text, _needle):
    with pytest.raises(ValueError, match="log_tail"):
        validate_fleet_rollout(RID, "host.finished", host_finished(log_tail=f"before\n{text}\nafter"))


def test_ordinary_text_is_still_accepted():
    ok = host_finished(checks=[{"name": "installed revision matches", "ok": True}], log_tail="Basic checks ok\nunit active")
    validate_fleet_rollout(RID, "host.finished", ok)
    validate_fleet_rollout(RID, "rollout.observed", observed(trigger={**TRIGGER, "pr_title": "Fix authorization bug in pwd handling"}))


@pytest.mark.parametrize("field", ["log_tail", "pr_title", "check"])
def test_the_ingest_doors_redaction_marker_is_a_rejection_not_a_stored_value(field):
    marked = "[REDACTED] trailing"
    if field == "log_tail":
        call = ("host.finished", host_finished(log_tail=marked))
    elif field == "pr_title":
        call = ("rollout.observed", observed(trigger={**TRIGGER, "pr_title": marked}))
    else:
        call = ("host.finished", host_finished(checks=[{"name": marked, "ok": True}]))
    with pytest.raises(ValueError, match="redact"):
        validate_fleet_rollout(RID, *call)


@pytest.mark.parametrize("text,needle", SECRET_SHAPES, ids=SHAPE_IDS)
def test_nothing_credential_shaped_is_readable_back_through_http(server, text, needle):
    post, get, repo, _ = server
    status, _body = post(envelope("host.finished", host_finished(log_tail=text)))
    assert status == 400
    assert post(envelope("host.finished", host_finished(1, checks=[{"name": text[:40], "ok": True}]))
                if False else envelope("rollout.observed", observed(trigger={**TRIGGER, "pr_title": text})))[0] == 400
    for path in ("/api/v1/fleet-rollouts", f"/api/v1/fleet-rollouts/{RID}", "/api/v1/fleet-rollouts/versions"):
        body = json.dumps(get(path)[1])
        assert needle not in body
    assert needle not in json.dumps(repo.stream("fleet_rollout", RID))
