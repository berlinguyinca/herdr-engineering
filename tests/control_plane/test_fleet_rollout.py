"""fleet_rollout ingest + GET /api/v1/fleet-rollouts*: the rollout event model (spec section 8).

No mocks: a real ControlPlaneServer on a loopback port, real HTTP requests, a real event sink.
"""
import json
import random
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


LOG_REF = {"host": "beast", "path_id": "updater", "sha256": "ab" * 32, "bytes": 1234, "lines": 12}


def ev(rid=RID, seq=0, **fields):
    return {"rollout_id": rid, "ts": TS, "seq": seq, **fields}


TRIGGER = {"repo": "autospec", "sha": SHA_A, "pr": 7, "merged_at": TS}
TARGETS = [{"repo": "autospec", "sha": SHA_A, "before": SHA_C}]


def observed(rid=RID, seq=0, **over):
    return ev(rid, seq, **{"trigger": TRIGGER, "targets": TARGETS, **over})


def host_finished(rid=RID, seq=5, host="beast", status="ok", **over):
    return ev(rid, seq, **{"host": host, "repo": "autospec", "status": status,
                           "before_sha": SHA_C, "after_sha": SHA_A, "started_at": TS, "finished_at": TS,
                           "duration_ms": 1200,
                           "checks": [{"name": "parity_ok", "status": "ok", "duration_ms": 40}],
                           **({} if "log" in over else {"log": {**LOG_REF, "host": host}}), **over})


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
    ("host.finished", host_finished(links={"other": "x"})),
    ("host.finished", _without(host_finished(), "host")),
    ("rollout.halted", ev(halted_at_host="bender; x", reason="health_failed")),
], ids=lambda v: v if isinstance(v, str) else "")
def test_malformed_events_are_rejected(event_type, payload):
    with pytest.raises(ValueError):
        validate_fleet_rollout(RID, event_type, payload)


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


@pytest.mark.parametrize("host", ["glpat-abcdefghij1234567890", "hf_abcdefghij", "sk_live_abcdef", "deadbeefdeadbeef",
                                  "evilhost", "beast2"])
def test_hosts_are_a_closed_enum_of_the_fleet(host):
    with pytest.raises(ValueError, match="host"):
        validate_fleet_rollout(RID, "host.finished", host_finished(host=host))
    with pytest.raises(ValueError, match="halted_at_host"):
        validate_fleet_rollout(RID, "rollout.halted", ev(halted_at_host=host, reason="health_failed"))


@pytest.mark.parametrize("link", ["glpat-abcdefghij1234567890", "hf_abcdefghij", "sk_live_abcdef", "ghp_" + "a" * 36,
                                  "deadbeefdead", "x" * 33, "AKIAIOSFODNN7EXAMPLE", "../../etc/passwd"])
def test_links_must_be_a_rollout_id_not_free_text(link):
    with pytest.raises(ValueError, match="pi_session_rollout"):
        validate_fleet_rollout(RID, "host.finished", host_finished(links={"pi_session_rollout": link}))
    ok = validate_fleet_rollout(RID, "host.finished", host_finished(links={"pi_session_rollout": "a1" * 16}))
    assert ok["links"] == {"pi_session_rollout": "a1" * 16}
    validate_fleet_rollout(RID, "host.finished", host_finished(links={"pi_session_rollout": None}))


# --------------------------------------------------------------------------- decision 2026-11-05: no free text at all


from herdr_engineering.control_plane import fleet_rollout as fr  # noqa: E402


def make_secret_corpus(seed: int = 20261105, per_shape: int = 14) -> list[str]:
    """Synthesize secret-SHAPED strings at test time (nothing credential-like is committed): per format a prefix plus
    random characters, plus structural shapes (key=value lines, headers, DSNs, flags, JSON, PEM headers, passphrases)."""
    import base64
    import string
    rng = random.Random(seed)
    alnum, lower, hexd = string.ascii_letters + string.digits, string.ascii_lowercase + string.digits, "0123456789abcdef"

    def rand(chars, n):
        return "".join(rng.choice(chars) for _ in range(n))

    def b64(n):
        return base64.b64encode(rng.randbytes(n)).decode()

    makers = [
        lambda: "gh" + rng.choice("pousr") + "_" + rand(alnum, 36),
        lambda: "github_pat_" + rand(alnum + "_", 40),
        lambda: rng.choice(["AKI", "ASI"]) + "A" + rand(string.ascii_uppercase + string.digits, 16),
        lambda: "sk-" + rand(alnum, rng.randint(8, 40)),
        lambda: "sk_" + rng.choice(["live", "test"]) + "_" + rand(alnum, 24),
        lambda: "xox" + rng.choice("abprs") + "-" + rand(string.digits, 10) + "-" + rand(alnum, 24),
        lambda: "eyJ" + rand(alnum, 12) + "." + "eyJ" + rand(alnum, 12) + "." + rand(alnum + "_-", 20),
        lambda: "-----BEGIN " + rng.choice(["RSA ", "OPENSSH ", "EC ", ""]) + "PRIVATE KEY-----",
        lambda: "BEGIN PRIVATE KEY " + rand(alnum, 10),
        lambda: rng.choice(["postgres", "mysql", "mongodb", "redis", "amqp", "smtp", "ftp"]) + "://" + rand(lower, 6)
        + ":" + rand(alnum, 10) + "@" + rand(lower, 6) + ".example/db",
        lambda: "https://" + rand(lower, 6) + ":" + rand(alnum, 12) + "@git.example/o/r.git",
        lambda: "Authorization: " + rng.choice(["Basic ", "Bearer ", "token "]) + b64(rng.randint(9, 24)),
        lambda: rng.choice(["Basic ", "Bearer "]) + b64(rng.randint(9, 24)),
        lambda: rng.choice(["Cookie", "Set-Cookie", "X-Api-Key"]) + ": " + rand(alnum, 12),
        lambda: rand(hexd, rng.randint(16, 64)),
        lambda: rand(alnum, rng.randint(20, 48)),
        lambda: b64(rng.randint(12, 33)),
        lambda: rng.choice(["password", "passwd", "pwd", "secret", "token", "api_key", "apikey", "auth", "sig", "credential"])
        + rng.choice(["=", ": ", " = ", '": "']) + rand(alnum, rng.randint(6, 18)),
        lambda: rng.choice(["DB_PASS", "MY_APP_PW", "STRIPE_KEY", "ANSIBLE_BECOME_PASS", "SECRET_TOKEN"]) + "=" + rand(alnum, 10),
        lambda: '{"' + rng.choice(["password", "auth", "token", "key", "pw"]) + '":"' + rand(alnum, 10) + '"}',
        lambda: '{"auths":{"registry.example":{"auth":"' + b64(12) + '"}}}',
        lambda: rng.choice(["--password ", "--token ", "-p ", "-u user:", "--api-key "]) + rand(alnum, 10),
        lambda: rng.choice(["mysql -uroot -p", "docker login -u u -p ", "curl -u admin:", "sshpass -p "]) + rand(alnum, 10),
        lambda: "https://svc.example/x?" + rng.choice(["sig", "key", "token", "code", "auth"]) + "=" + rand(alnum, 14),
        lambda: "localhost:5432:db:user:" + rand(alnum, 10),
        lambda: "machine " + rand(lower, 5) + " login " + rand(lower, 4) + " password " + rand(alnum, 8),
        lambda: "ssh -i ~/.ssh/id_" + rng.choice(["rsa", "ed25519", "ecdsa"]) + " " + rand(lower, 5),
        lambda: "/home/" + rand(lower, 6) + "/." + rng.choice(["aws", "ssh", "config/gh", "netrc"]),
        lambda: "/Users/" + rand(lower, 6) + "/Library",
        lambda: " ".join(rng.choice(["correct", "horse", "battery", "staple", "mango", "orbit", "lamp", "river"]) for _ in range(4)),
        lambda: "the password is " + rand(alnum, 8),
        lambda: "[REDACTED] " + b64(12),
        lambda: "ya29." + rand(alnum + "_-", 24),
        lambda: "SG." + rand(alnum, 16) + "." + rand(alnum, 16),
        lambda: "https://hooks.slack.com/services/T" + rand(string.digits, 8) + "/B" + rand(string.digits, 8) + "/" + rand(alnum, 12),
        lambda: rng.choice(["glpat-", "hf_", "npm_"]) + rand(alnum, 24),
        lambda: "token " + rand(string.ascii_lowercase, 30),
        lambda: "x" * rng.randint(24, 60),
        lambda: "line one\n" + rng.choice(["password", "token"]) + "\n" + rand(alnum, 12),
        lambda: "\x1b[31m" + rand(alnum, 20) + "\x1b[0m",
        lambda: "ok" + "\x00" + rand(alnum, 6),
    ]
    out = {make() for make in makers for _ in range(per_shape)}
    return sorted(out)


CORPUS = make_secret_corpus()
SOURCE_ID = fr.source_stream_id("autospec")


def _legitimate(value: str) -> bool:
    """Strings that are valid for SOME structured field (enum, host, hex id/sha, UTC timestamp): not secrets."""
    import re
    from datetime import UTC, datetime
    enums = set(fr.STATUSES) | set(fr.REASONS) | set(fr.HOST_ORDER) | set(fr.WATCHED_REPOS) | set(fr.CHECK_NAMES) \
        | set(fr.CHECK_STATUSES) | set(fr.LOG_KINDS) | set(fr.COUNT_KEYS)
    if value in enums or re.fullmatch(r"[0-9a-f]{16,64}", value):
        return True
    try:
        return datetime.fromisoformat(value).utcoffset() == UTC.utcoffset(None)
    except ValueError:
        return False


CORPUS = [c for c in CORPUS if not _legitimate(c)]
LOG = LOG_REF
EXAMPLES = {
    "rollout.observed": observed(),
    "rollout.planned": ev(trigger=TRIGGER, targets=TARGETS),
    "rollout.started": ev(targets=TARGETS),
    "rollout.resumed": ev(reason="operator"),
    "host.started": ev(host="beast", repo="autospec", status="precheck"),
    "host.step": ev(host="beast", repo="autospec", status="applying", checks=[{"name": "parity_ok", "status": "ok"}], log=LOG),
    "host.finished": host_finished(links={"pi_session_rollout": "a1" * 16}, counts={"dirty_paths": 0}),
    "rollout.succeeded": ev(),
    "rollout.halted": ev(halted_at_host="bender", reason="health_failed"),
    "rollout.superseded": ev(reason="operator"),
    "rollout.abandoned": ev(reason="operator"),
    "lock.pinned": ev(targets=TARGETS),
    "lock.refused": ev(targets=TARGETS, reason="not_on_main"),
    "source.refused": ev(SOURCE_ID, repo="autospec", reason="not_on_main"),
    "source.degraded": ev(SOURCE_ID, repo="autospec", reason="rate_limited"),
    "source.recovered": ev(SOURCE_ID, repo="autospec"),
}


def _paths(node, path=()):
    """Every leaf (and every dict/list slot) of a payload as a path of keys/indexes."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _paths(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _paths(value, (*path, index))
    else:
        yield path


def _set(node, path, value):
    import copy
    node = copy.deepcopy(node)
    cursor = node
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = value
    return node


def _stream_of(payload):
    return payload["rollout_id"]


def test_every_example_event_is_valid():
    assert set(EXAMPLES) == fr.EVENT_TYPES
    for event_type, payload in EXAMPLES.items():
        validate_fleet_rollout(_stream_of(payload), event_type, payload)


def test_no_field_of_any_event_accepts_an_arbitrary_string():
    """Secret-free by construction: replace every leaf of every event with every corpus string -> always refused."""
    assert len(CORPUS) > 400
    checked = 0
    for event_type, payload in EXAMPLES.items():
        for path in _paths(payload):
            for text in CORPUS:
                mutated = _set(payload, path, text)
                with pytest.raises(ValueError):
                    validate_fleet_rollout(_stream_of(payload), event_type, mutated)
                checked += 1
    assert checked > 20000


def test_no_object_accepts_an_arbitrary_key():
    for event_type, payload in EXAMPLES.items():
        for text in CORPUS[:60]:
            with pytest.raises(ValueError):
                validate_fleet_rollout(_stream_of(payload), event_type, {**payload, text: "x"})
        for path in _paths(payload):
            if len(path) > 1 and isinstance(path[-1], str):
                parent = path[:-1]
                node = payload
                for step in parent:
                    node = node[step]
                if isinstance(node, dict):
                    mutated = _set(payload, (*parent, "free_text"), "anything") if parent else payload
                    with pytest.raises(ValueError):
                        validate_fleet_rollout(_stream_of(payload), event_type, mutated)


@pytest.mark.parametrize("old", ["log_tail", "pr_title"])
def test_the_removed_free_text_fields_are_unknown_fields(old):
    with pytest.raises(ValueError, match="unknown"):
        validate_fleet_rollout(RID, "host.finished", host_finished(**{old: "installed ok"}))
    with pytest.raises(ValueError, match="unknown"):
        validate_fleet_rollout(RID, "rollout.observed", observed(trigger={**TRIGGER, old: "x"}))


def test_checks_are_a_closed_enum_with_status_and_numeric_duration():
    ok = host_finished(checks=[{"name": n, "status": "failed" if i % 2 else "ok", "duration_ms": i}
                               for i, n in enumerate(fr.CHECK_NAMES)])
    assert validate_fleet_rollout(RID, "host.finished", ok)["checks"][0] == {"name": fr.CHECK_NAMES[0], "status": "ok", "duration_ms": 0}
    validate_fleet_rollout(RID, "host.finished", host_finished(checks=[{"name": "parity_ok", "status": "skipped"}]))
    for bad in ({"name": "parity", "status": "ok"}, {"name": "parity_ok", "status": "maybe"},
                {"name": "parity_ok", "ok": True}, {"name": "parity_ok", "status": "ok", "duration_ms": -1},
                {"name": "parity_ok", "status": "ok", "duration_ms": "5"}, {"name": "parity_ok", "status": "ok", "x": 1},
                {"status": "ok"}):
        with pytest.raises(ValueError):
            validate_fleet_rollout(RID, "host.finished", host_finished(checks=[bad]))


def test_a_log_reference_is_host_kind_hash_and_sizes_only():
    assert validate_fleet_rollout(RID, "host.finished", host_finished())["log"] == LOG_REF
    for bad in ({**LOG_REF, "path_id": "/etc/passwd"}, {**LOG_REF, "path_id": "other"}, {**LOG_REF, "sha256": "ab" * 31},
                {**LOG_REF, "sha256": "AB" * 32}, {**LOG_REF, "bytes": -1}, {**LOG_REF, "lines": "3"},
                {**LOG_REF, "host": "evil"}, {**LOG_REF, "host": "bender"},        # must equal the event's host
                {**LOG_REF, "content": "installed"}, {k: v for k, v in LOG_REF.items() if k != "sha256"}, "text", None):
        with pytest.raises(ValueError):
            validate_fleet_rollout(RID, "host.finished", host_finished(log=bad))


def test_random_sha256_survives_the_ingest_door_and_reads_back(server):
    import hashlib
    post, get, *_ = server
    ref = {**LOG_REF, "sha256": hashlib.sha256(b"some log").hexdigest()}
    assert post(envelope("rollout.observed", observed()))[0] == 200
    assert post(envelope("host.finished", host_finished(log=ref)))[0] == 200
    [row] = get(f"/api/v1/fleet-rollouts/{RID}")[1]["hosts"]
    assert row["log"] == ref


def test_corpus_strings_in_any_field_are_a_400_or_never_stored_through_http(server):
    post, get, repo, _ = server
    assert post(envelope("rollout.observed", observed()))[0] == 200
    targets = [(t, p) for t, p in EXAMPLES.items() if t in ("host.finished", "rollout.observed", "host.step")]
    leaves = [(t, p, path) for t, p in targets for path in _paths(p)]
    stored = 0
    for index, text in enumerate(CORPUS):
        event_type, payload, path = leaves[index % len(leaves)]
        mutated = _set(payload, path, text)
        mutated["seq"] = 1000 + index if path != ("seq",) else mutated["seq"]
        status, _ = post(envelope(event_type, mutated, rid=RID))
        assert status == 400, (event_type, path, text)
        stored += status == 200
    assert stored == 0
    blob = json.dumps([e["payload"] for e in repo.stream("fleet_rollout", RID)]) \
        + json.dumps(get(f"/api/v1/fleet-rollouts/{RID}")[1]["hosts"])
    vocabulary = json.dumps(EXAMPLES) + json.dumps(sorted(fr.EVENT_FIELDS))      # field names and values legitimately stored
    for text in CORPUS:
        assert text in vocabulary or text not in blob, text
