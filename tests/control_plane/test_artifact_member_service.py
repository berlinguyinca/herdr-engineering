# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Plan 5, Task 6: ``MemberService``, the transport-neutral member layer.  A real approval-mode ``CollabService``, a
real bridge on temp directories, the member view / grants written by hand in the worker's schema."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from herdr_engineering.control_plane.artifact_member_service import (
    MembersConfig,
    MemberService,
    MemberServiceError,
    member_actor,
)
from herdr_engineering.control_plane.artifact_members import member_owner_key
from herdr_engineering.control_plane.collab_bridge_files import (
    CHUNK_BYTES,
    BridgeLimits,
    atomic_write_json,
    upload_id_for,
)
from herdr_engineering.control_plane.collab_mirror import GRANTS_NAME
from tests.control_plane.test_collab_approval import s as approval_svc  # noqa: F401
from tests.control_plane.test_collab_member_feed import Net

ART = "art_alice1"
OTHER = "art_other1"
GHOST = "art_nothere"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class World(Net):
    def __init__(self, s, tmp_path, **cfg):
        super().__init__(s, tmp_path)
        self.cfg = MembersConfig(**{"result_wait_seconds": 0, **cfg})
        self.ms = MemberService(s, self.bridge, self.cfg)
        s.member_service = self.ms
        self.person("op")
        self.person("lab")
        self.person("mal")                       # signed in, not in the member group
        self.write_grants()

    def write_grants(self, **limits):
        doc = {"generated_at": self.s._now(), "agents": {}, "limits": BridgeLimits(**limits).to_dict()}
        atomic_write_json(self.bridge.mirror, GRANTS_NAME, doc)

    def view(self, name, artifacts=(), shared=(), principal=None, perms="read"):
        key = member_owner_key(f"member:{name}")
        arts = [{"artifact_id": a, "title": f"title {a}", "lifecycle": "active", "head_version_id": "v_1",
                 "head_version_number": 1, "bytes": 3,
                 "versions": [{"version_id": "v_1", "version_number": 1, "created_at": "2026-10-30T12:00:00+00:00",
                               "message": "first", "parent_version_ids": []}]} for a in artifacts]
        sh = [{"artifact_id": a, "title": "shared", "head_version_id": "v_9", "head_version_number": 9,
               "permissions": perms} for a in shared]
        doc = {"principal": principal or f"member:{name}", "updated": "2026-10-30T12:00:00+00:00", "usage": {},
               "artifacts": arts, "shared_with_me": sh, "links": []}
        atomic_write_json(self.bridge.mirror / "member-views", f"{key}.json", doc)

    def user(self, name):
        return member_actor(self.people[name])

    def staged(self, name):
        root = self.outbox / "staging" / member_owner_key(f"member:{name}")
        return sorted(p.name for p in root.iterdir()) if root.exists() else []

    def chunk(self, data, index, *, artifact=ART, name="f.bin", piece=None, digest=None):
        piece = data[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES] if piece is None else piece
        return {"artifact_id": artifact, "name": name, "size_bytes": len(data), "file_sha256": sha(data),
                "total": -(-len(data) // CHUNK_BYTES), "index": index,
                "data": base64.b64encode(piece).decode(), "sha256": digest or sha(piece)}


@pytest.fixture
def world(approval_svc, tmp_path):
    w = World(approval_svc, tmp_path)
    w.view("op", [ART])
    w.view("lab", [OTHER])
    yield w
    w.bridge.stop()


def refuse(fn, *a, **kw) -> MemberServiceError:
    with pytest.raises(MemberServiceError) as e:
        fn(*a, **kw)
    return e.value


DATA = bytes(range(256)) * 4096          # 1 MiB: two chunks


def test_only_group_members_may_use_the_service(world):
    outsider = member_actor(world.people["mal"])
    for fn, args in ((world.ms.mine, ()), (world.ms.create, ("t", "a.html", "web")),
                     (world.ms.upload_chunk, (world.chunk(DATA, 0),))):
        e = refuse(fn, outsider, *args)
        assert (e.status, e.message) == (403, "artifact tools are for group members")
    assert refuse(world.ms.mine, {"kind": "user", "id": "op"}).status == 403      # no principal: fail closed
    forged = {**world.user("op"), "id": "lab"}
    assert refuse(world.ms.mine, forged).status == 403


def test_chunk_stages_replays_and_conflicts_and_the_upload_id_is_channel_independent(world):
    agent, _ = world.agent("op")
    expect = upload_id_for(member_owner_key("member:op"), ART, "f.bin", sha(DATA), len(DATA))
    first = world.ms.upload_chunk(member_actor(agent), world.chunk(DATA, 0))
    assert first["upload_id"] == expect and first["replayed"] is False and first["total"] == 2
    replay = world.ms.upload_chunk(world.user("op"), world.chunk(DATA, 0))          # the web session, same bytes
    assert replay["upload_id"] == expect and replay["replayed"] is True
    assert world.staged("op") == [expect]
    bad = world.chunk(DATA, 0, piece=b"x" * CHUNK_BYTES)
    bad["sha256"] = sha(b"x" * CHUNK_BYTES)
    e = refuse(world.ms.upload_chunk, world.user("op"), bad)
    assert e.status == 409
    last = world.ms.upload_chunk(world.user("op"), world.chunk(DATA, 1))
    assert last["complete"] is True and last["received"] == 2


def test_malformed_chunks_are_400_and_stage_nothing(world):
    ok = world.chunk(DATA, 0)
    cases = [{**ok, "data": "%%%not base64"}, {**ok, "sha256": "0" * 64}, world.chunk(DATA, 0, piece=b"short"),
             {**ok, "size_bytes": world.cfg.max_upload_bytes + 1}, {**ok, "extra": 1}]
    for body in cases:
        assert refuse(world.ms.upload_chunk, world.user("op"), body).status == 400
    assert world.staged("op") == []


def test_oversize_files_are_refused_by_the_configured_limit(approval_svc, tmp_path):
    w = World(approval_svc, tmp_path, max_upload_bytes=CHUNK_BYTES)
    w.view("op", [ART])
    try:
        assert refuse(w.ms.upload_chunk, w.user("op"), w.chunk(DATA, 0)).status == 400
        assert w.staged("op") == []
    finally:
        w.bridge.stop()


def test_a_foreign_artifact_and_a_missing_one_are_indistinguishable(world):
    a, b = (refuse(world.ms.upload_chunk, world.user("op"), world.chunk(DATA, 0, artifact=x))
            for x in (OTHER, GHOST))
    assert (a.status, a.code, a.message) == (b.status, b.code, b.message) == (404, "not_found", "not found")
    x = refuse(world.ms.delete, world.user("op"), OTHER, True)
    y = refuse(world.ms.delete, world.user("op"), GHOST, True)
    assert (x.status, x.code, x.message) == (y.status, y.code, y.message)
    x = refuse(world.ms.publish, world.user("op"), OTHER, files=[{"name": "a", "upload_id": "u"}], message="m")
    y = refuse(world.ms.publish, world.user("op"), GHOST, files=[{"name": "a", "upload_id": "u"}], message="m")
    assert (x.status, x.code, x.message) == (y.status, y.code, y.message)
    assert world.staged("op") == [] and world.staged("lab") == []


def test_a_view_that_names_someone_else_is_treated_as_absent(world):
    world.view("op", [ART], principal="member:lab")
    assert refuse(world.ms.upload_chunk, world.user("op"), world.chunk(DATA, 0)).status == 404
    assert world.ms.mine(world.user("op"))["available"] is False


def test_refusals_are_audited(world):
    def denied():
        return [(e["payload"]["action"], e["payload"]["reason"]) for e in world.s._log if e["event_type"] == "audit_denied"]
    before = len(denied())
    refuse(world.ms.upload_chunk, world.user("op"), world.chunk(DATA, 0, artifact=OTHER))
    refuse(world.ms.mine, member_actor(world.people["mal"]))
    got = denied()[before:]
    assert ("artifact.upload_chunk", "not_owner") in got and ("artifact.mine", "not_in_group") in got


def test_null_member_limits_leave_the_bridge_limits_in_charge(approval_svc, tmp_path):
    """Effective limits are min(member config, bridge limits); a null member value drops its side of the min."""
    w = World(approval_svc, tmp_path, max_upload_bytes=None, staging_bytes_per_member=None)
    w.write_grants(max_file_bytes=CHUNK_BYTES, staging_bytes_per_agent=2 * CHUNK_BYTES)
    w.view("op", [ART])
    try:
        limits = w.ms._limits()
        assert limits.max_file_bytes == CHUNK_BYTES                       # the bridge value, not a member cap
        assert w.ms._staging_cap(limits) == 2 * CHUNK_BYTES
        assert refuse(w.ms.upload_chunk, w.user("op"), w.chunk(DATA, 0)).status == 400      # bridge cap still bites
        (tmp_path / "t").mkdir()
        tight = World(approval_svc, tmp_path / "t", max_upload_bytes=5, staging_bytes_per_member=7)
        try:
            tight.write_grants(max_file_bytes=CHUNK_BYTES, staging_bytes_per_agent=2 * CHUNK_BYTES)
            assert tight.ms._limits().max_file_bytes == 5 and tight.ms._staging_cap(tight.ms._limits()) == 7
        finally:
            tight.bridge.stop()
    finally:
        w.bridge.stop()


def test_per_member_staging_cap_is_429(approval_svc, tmp_path):
    w = World(approval_svc, tmp_path, staging_bytes_per_member=len(DATA))
    w.view("op", [ART])
    try:
        w.ms.upload_chunk(w.user("op"), w.chunk(DATA, 0))
        other = bytes(reversed(DATA))
        e = refuse(w.ms.upload_chunk, w.user("op"), w.chunk(other, 0, name="g.bin"))
        assert (e.status, e.code) == (429, "rate_limited")
        assert len(w.staged("op")) == 1
    finally:
        w.bridge.stop()


def test_whole_volume_staging_cap_is_429(world):
    world.write_grants(staging_bytes_total=len(DATA))
    world.view("lab", [OTHER])
    world.ms.upload_chunk(world.user("op"), world.chunk(DATA, 0))
    other = bytes(reversed(DATA))
    e = refuse(world.ms.upload_chunk, world.user("lab"), world.chunk(other, 0, artifact=OTHER))
    assert e.status == 429 and world.staged("lab") == []


def test_the_1201st_chunk_in_a_minute_is_429_and_the_window_slides(world):
    c = world.chunk(DATA, 0)
    for _ in range(1200):
        world.ms.upload_chunk(world.user("op"), c)
    e = refuse(world.ms.upload_chunk, world.user("op"), c)
    assert e.status == 429
    assert world.ms.upload_chunk(world.user("lab"), world.chunk(DATA, 0, artifact=OTHER))["replayed"] is False
    world.s._now.t += 61
    world.write_grants()
    assert world.ms.upload_chunk(world.user("op"), c)["replayed"] is True


def run_args(world, data, first=0, **kw):
    return dict(artifact_id=ART, name="big.bin", size_bytes=kw.pop("size", len(data)), file_sha256=sha(data),
                first_index=first, **kw)


def test_a_web_run_stages_each_chunk_and_a_bad_run_hash_stages_nothing(world):
    big = bytes(range(251)) * (CHUNK_BYTES * 32 // 251 + 1)
    big = big[:CHUNK_BYTES * 32]
    out = world.ms.upload_run(world.user("op"), data=big, run_sha256=sha(big), **run_args(world, big))
    assert out["chunks"] == 32 and out["complete"] is True
    other = bytes(reversed(big))
    e = refuse(world.ms.upload_run, world.user("lab"), data=other, run_sha256=sha(big),
               **{**run_args(world, other), "artifact_id": OTHER})
    assert e.status == 400 and world.staged("lab") == []
    e = refuse(world.ms.upload_run, world.user("op"), data=big + b"x", run_sha256=sha(big + b"x"),
               **run_args(world, big))
    assert e.status == 400                                                             # 33 chunks: too many per request


def test_a_bad_chunk_inside_a_run_stages_none_of_it(world):
    big = bytes(range(200)) * (CHUNK_BYTES * 3 // 200 + 1)
    run = big[:CHUNK_BYTES * 2]                               # claims 3 chunks of a 1-chunk-too-short file
    e = refuse(world.ms.upload_run, world.user("op"), data=run, run_sha256=sha(run),
               **run_args(world, big, size=CHUNK_BYTES * 2 - 5))
    assert e.status == 400 and world.staged("op") == []


def test_a_run_counts_every_chunk_toward_the_minute(world):
    data = bytes(CHUNK_BYTES * 32)
    for _ in range(37):
        world.ms.upload_run(world.user("op"), data=data, run_sha256=sha(data), **run_args(world, data))
    e = refuse(world.ms.upload_run, world.user("op"), data=data, run_sha256=sha(data), **run_args(world, data))
    assert e.status == 429


def test_status_missing_ranges_shrink_as_chunks_arrive(world):
    data = bytes(range(255)) * (CHUNK_BYTES * 5 // 255 + 1)
    data = data[:CHUNK_BYTES * 4 + 10]
    q = ("f.bin", len(data), sha(data))
    st = world.ms.upload_status(world.user("op"), ART, *q)
    assert st["missing_ranges"] == [[0, 4]] and st["complete"] is False and st["total"] == 5
    for i in (0, 3):
        world.ms.upload_chunk(world.user("op"), world.chunk(data, i))
    st = world.ms.upload_status(world.user("op"), ART, *q)
    assert st["missing_ranges"] == [[1, 2], [4, 4]]
    for i in (1, 2, 4):
        world.ms.upload_chunk(world.user("op"), world.chunk(data, i))
    st = world.ms.upload_status(world.user("op"), ART, *q)
    assert st["missing_ranges"] == [] and st["complete"] is True and st["verified"] is False
    assert refuse(world.ms.upload_status, world.user("op"), OTHER, *q).status == 404


def test_abort_removes_only_the_callers_own_upload(world):
    uid = world.ms.upload_chunk(world.user("op"), world.chunk(DATA, 0))["upload_id"]
    world.ms.upload_chunk(world.user("lab"), world.chunk(DATA, 0, artifact=OTHER))
    assert refuse(world.ms.upload_abort, world.user("op"), OTHER, uid).status == 404
    assert refuse(world.ms.upload_abort, world.user("op"), ART, "../x").status == 400
    assert world.ms.upload_abort(world.user("op"), ART, uid)["aborted"] is True
    assert world.staged("op") == []
    assert len(world.staged("lab")) == 1


def test_delete_needs_confirm_true(world):
    for bad in (False, None, "true", 1):
        e = refuse(world.ms.delete, world.user("op"), ART, bad)
        assert e.status == 400
    assert not (world.outbox / "intents").exists() or not list((world.outbox / "intents").glob("*"))
    out = world.ms.delete(world.user("op"), ART, True)
    assert out["status"] == "pending" and out["ticket"]


def test_write_intents_return_a_ticket_then_the_worker_result(world):
    t = world.ms.create(world.user("op"), "T", "a.html", "web")["ticket"]
    assert refuse(world.ms.result, world.user("lab"), t, 0).status == 404            # someone else's ticket
    assert world.ms.result(world.user("op"), t, 0) == {"ticket": t, "status": "pending"}
    atomic_write_json(world.bridge.mirror / "results", f"{t}.json",
                      {"ticket": t, "kind": "member.artifact_create", "actor": {"kind": "user", "id": "op"},
                       "artifact_id": ART, "result": "ok"})
    assert world.ms.result(world.user("op"), t, 0) == {"ticket": t, "status": "done", "result": "ok",
                                                       "artifact_id": ART}


def test_create_with_a_bad_payload_is_400_and_the_write_rate_limit_is_429(world):
    assert refuse(world.ms.create, world.user("op"), "T", "a.html", "nope").status == 400
    for i in range(4):
        world.ms.create(world.user("op"), "T", f"a{i}.html", "web")
    # the bridge's pending-intent cap (4 per actor) trips first: also a 429
    assert refuse(world.ms.create, world.user("op"), "T", "z.html", "web").status == 429


def test_publish_enforces_file_count_and_message_length(world):
    f = [{"name": "a.txt", "upload_id": "upl_" + "0" * 24}]
    assert refuse(world.ms.publish, world.user("op"), ART, files=[], message="m").status == 400
    too_many = f * (world.cfg.max_publish_files + 1)
    assert refuse(world.ms.publish, world.user("op"), ART, files=too_many, message="m").status == 400
    assert refuse(world.ms.publish, world.user("op"), ART, files=f, message="x" * 501).status == 400
    ok = world.ms.publish(world.user("op"), ART, base_version_id=None, files=f, message="m")
    assert ok["status"] == "pending"


def test_mine_returns_only_the_callers_view_and_history_hides_existence(world):
    world.view("op", [ART], shared=["art_sh1"])
    mine = world.ms.mine(world.user("op"))
    assert [a["artifact_id"] for a in mine["artifacts"]] == [ART]
    assert [a["artifact_id"] for a in mine["shared_with_me"]] == ["art_sh1"]
    assert [a["artifact_id"] for a in world.ms.mine(world.user("lab"))["artifacts"]] == [OTHER]
    h = world.ms.history(world.user("op"), ART)
    assert h["versions"][0]["message"] == "first"
    shared = world.ms.history(world.user("op"), "art_sh1")
    assert shared["truncated"] is True
    a = refuse(world.ms.history, world.user("op"), OTHER)
    b = refuse(world.ms.history, world.user("op"), GHOST)
    assert (a.status, a.code, a.message) == (b.status, b.code, b.message) == (404, "not_found", "not found")
    assert world.ms.head(world.user("op"), ART)["permission"] == "owner"
    assert str(refuse(world.ms.head, world.user("op"), OTHER)) == str(refuse(world.ms.head, world.user("op"), GHOST))


def test_user_and_agent_actors_of_one_person_get_identical_results(world):
    agent, _ = world.agent("op")
    actors = {"user": world.user("op"), "agent": member_actor(agent)}
    out = {}
    for kind, actor in actors.items():
        out[kind] = {
            "mine": world.ms.mine(actor),
            "history": world.ms.history(actor, ART),
            "bad": (lambda e: (e.status, e.code, e.message))(
                refuse(world.ms.upload_chunk, actor, {**world.chunk(DATA, 0), "sha256": "0" * 64})),
            "status": world.ms.upload_status(actor, ART, "f.bin", len(DATA), sha(DATA)),
            "foreign": (lambda e: (e.status, e.code, e.message))(
                refuse(world.ms.upload_chunk, actor, world.chunk(DATA, 0, artifact=OTHER))),
        }
    assert world.ms.upload_chunk(actors["agent"], world.chunk(DATA, 0))["upload_id"] == \
        world.ms.upload_chunk(actors["user"], world.chunk(DATA, 0))["upload_id"]
    assert out["user"] == out["agent"]


def test_a_deleted_artifact_takes_no_uploads_or_publishes(world):
    key = member_owner_key("member:op")
    path = world.bridge.mirror / "member-views" / f"{key}.json"
    doc = json.loads(path.read_text())
    doc["artifacts"][0]["lifecycle"] = "deleted"
    atomic_write_json(path.parent, path.name, doc)
    assert refuse(world.ms.upload_chunk, world.user("op"), world.chunk(DATA, 0)).status == 404
    f = [{"name": "a.txt", "upload_id": "upl_" + "0" * 24}]
    assert refuse(world.ms.publish, world.user("op"), ART, files=f, message="m").status == 404
    assert world.staged("op") == []


def ticket_of_newest_intent(world):
    paths = sorted((world.outbox / "intents").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
    return paths[-1].stem


def worker_answers(world, actor_id="op"):
    """The worker's side of a result: written the first time the service sleeps in its 5 s wait."""
    def sleep(_seconds):
        t = ticket_of_newest_intent(world)
        atomic_write_json(world.bridge.mirror / "results", f"{t}.json",
                          {"ticket": t, "kind": "member.artifact_create", "actor": {"kind": "user", "id": actor_id},
                           "artifact_id": ART, "result": "ok"})
    world.bridge.sleep = sleep


def test_a_result_ready_within_the_wait_comes_back_done(approval_svc, tmp_path):
    w = World(approval_svc, tmp_path, result_wait_seconds=5.0)
    w.view("op", [ART])
    try:
        worker_answers(w)
        out = w.ms.create(w.user("op"), "T", "a.html", "web")
        assert out["status"] == "done" and out["result"] == "ok" and out["artifact_id"] == ART
    finally:
        w.bridge.stop()


def test_a_result_that_never_comes_returns_the_ticket_after_the_wait(approval_svc, tmp_path):
    w = World(approval_svc, tmp_path, result_wait_seconds=5.0)
    w.view("op", [ART])
    try:
        clock = iter(range(0, 1000, 2))
        w.bridge.monotonic = lambda: next(clock)
        w.bridge.sleep = lambda _s: None
        out = w.ms.delete(w.user("op"), ART, True)
        assert out["status"] == "pending" and out["ticket"] == ticket_of_newest_intent(w)
    finally:
        w.bridge.stop()


def test_a_full_waiter_slot_never_loses_the_ticket(approval_svc, tmp_path):
    from herdr_engineering.control_plane import collab_waiters
    w = World(approval_svc, tmp_path, result_wait_seconds=5.0)
    w.view("op", [ART])
    try:
        with collab_waiters.waiter_slot("artifact_result:user:op") as a, \
                collab_waiters.waiter_slot("artifact_result:user:op") as b:
            assert a and b
            out = w.ms.create(w.user("op"), "T", "a.html", "web")      # the wait is refused (429) ...
        assert out["status"] == "pending" and out["ticket"] == ticket_of_newest_intent(w)   # ... the ticket is not
    finally:
        w.bridge.stop()


def test_operator_limits_can_only_be_tightened_by_the_member_defaults(world):
    world.write_grants(max_file_bytes=CHUNK_BYTES)                 # operator: 512 KiB per file; member default: 1 TiB
    assert refuse(world.ms.upload_chunk, world.user("op"), world.chunk(DATA, 0)).status == 400
    q = ("f.bin", len(DATA), sha(DATA))
    assert refuse(world.ms.upload_status, world.user("op"), ART, *q).status == 400
    assert world.staged("op") == []
    world.s._now.t += 61                                           # the refused attempts above also count as hits
    world.write_grants(chunks_per_minute=2)
    for _ in range(2):
        world.ms.upload_chunk(world.user("op"), world.chunk(DATA, 0))
    assert refuse(world.ms.upload_chunk, world.user("op"), world.chunk(DATA, 0)).status == 429
    world.write_grants(staging_bytes_per_agent=len(DATA) - 1)
    assert refuse(world.ms.upload_chunk, world.user("lab"),
                  world.chunk(DATA, 0, artifact=OTHER)).status == 429


def test_publish_is_capped_at_the_validators_file_limit_with_a_clear_400(world):
    files = [{"name": f"f{i}", "upload_id": "upl_" + "0" * 24} for i in range(101)]
    e = refuse(world.ms.publish, world.user("op"), ART, files=files, message="m")
    assert e.status == 400 and "1 to 100" in e.message
    assert world.ms.publish(world.user("op"), ART, files=files[:100], message="m")["status"] == "pending"


def intent_keys(world):
    return [json.loads(p.read_text()).get("idempotency_key") for p in sorted((world.outbox / "intents").glob("*.json"))]


def test_idempotency_keys_reach_the_intent_and_bad_ones_are_400(world):
    key = "retry-key-0001"
    world.ms.create(world.user("op"), "T", "a.html", "web", idempotency_key=key)
    world.ms.create(world.user("op"), "T", "a.html", "web", idempotency_key=key)      # the retry: same key, new ticket
    f = [{"name": "a.txt", "upload_id": "upl_" + "0" * 24}]
    world.ms.publish(world.user("op"), ART, files=f, message="m", idempotency_key="pub-key-0001")
    assert sorted(k for k in intent_keys(world)) == ["pub-key-0001", key, key]
    for bad in ("short", "x" * 129, "has space in it", 12345678):
        assert refuse(world.ms.create, world.user("op"), "T", "a.html", "web", idempotency_key=bad).status == 400
    assert len(intent_keys(world)) == 3


# ---------------------------------------------------------------------------------------------- Task 7: shares
def secret_file(world, ticket, principal, url):
    from herdr_engineering.control_plane.collab_bridge_files import link_new_beneath
    link_new_beneath(world.bridge.outbox, ["secrets"], ticket + ".json",
                     json.dumps({"ticket": ticket, "principal": principal, "url": url, "at": 1.0}).encode(),
                     mode=0o600, create_dirs=True, dir_mode=0o700)


def live_result(world, ticket, name="op"):
    atomic_write_json(world.bridge.mirror / "results", f"{ticket}.json",
                      {"ticket": ticket, "kind": "member.share_approve", "actor": {"kind": "user", "id": name},
                       "artifact_id": ART, "result": "live", "share_id": "share_" + "a" * 24, "audience": "public",
                       "secret_result": True})


def test_share_create_is_429_after_the_hourly_cap_and_the_window_slides(approval_svc, tmp_path):
    w = World(approval_svc, tmp_path, share_creates_per_hour=3, writes_per_minute=1000)
    w.view("op", [ART])
    try:
        for _ in range(3):
            assert w.ms.share_create(w.user("op"), ART, "v_1", "public")["status"] == "pending"
        assert refuse(w.ms.share_create, w.user("op"), ART, "v_1", "public").status == 429
        w.s._now.t += 1800
        w.write_grants()
        assert refuse(w.ms.share_create, w.user("op"), ART, "v_1", "public").status == 429    # still inside the hour
        w.s._now.t += 1801
        w.write_grants()
        assert w.ms.share_create(w.user("op"), ART, "v_1", "public")["status"] == "pending"
    finally:
        w.bridge.stop()


def test_a_share_only_grant_may_create_links_but_not_read_and_a_read_only_grant_may_not_share(world):
    """D3: ``share`` is the gate for links; it does not open the bytes, and ``read`` alone does not open links."""
    world.view("op", [ART], shared=["art_sh_only"], perms=["share"])
    assert world.ms.share_create(world.user("op"), "art_sh_only", "v_9", "public")["status"] == "pending"
    assert refuse(world.ms.history, world.user("op"), "art_sh_only").status == 404
    assert refuse(world.ms.head, world.user("op"), "art_sh_only").status == 404
    assert world.ms.can_read(world.user("op"), "art_sh_only") is False
    world.view("op", [ART], shared=["art_ro"], perms=["read"])
    assert refuse(world.ms.share_create, world.user("op"), "art_ro", "v_9", "public").status == 404
    assert world.ms.head(world.user("op"), "art_ro")["permission"] == ["read"]
    assert world.ms.can_read(world.user("op"), "art_ro") is True


def test_share_create_on_an_artifact_outside_the_view_is_the_same_404_as_a_ghost(world):
    a = refuse(world.ms.share_create, world.user("op"), OTHER, "v_1", "public")
    b = refuse(world.ms.share_create, world.user("op"), GHOST, "v_1", "public")
    assert (a.status, a.code, a.message) == (b.status, b.code, b.message) == (404, "not_found", a.message)
    assert intent_keys(world) == []


def test_share_approve_needs_confirm_true(world):
    for bad in (None, False, "true", 1):
        assert refuse(world.ms.share_approve, world.user("op"), ART, "share_" + "a" * 24, bad).status == 400
    assert intent_keys(world) == []


def test_the_secret_url_is_returned_exactly_once(world):
    r = world.ms.share_approve(world.user("op"), ART, "share_" + "a" * 24, True)
    t = r["ticket"]
    live_result(world, t)
    secret_file(world, t, "member:op", "https://s.example/s/TOKEN")
    first = world.ms.result(world.user("op"), t, 0)
    assert first["result"] == "live" and first["secret"]["url"] == "https://s.example/s/TOKEN"
    assert "TOKEN" not in json.dumps({k: v for k, v in first.items() if k != "secret"})
    second = world.ms.result(world.user("op"), t, 0)
    assert second["secret"] == {"status": "already_read"}
    assert "TOKEN" not in json.dumps(second)
    assert not (world.bridge.outbox / "secrets" / f"{t}.json").exists()


def test_a_secret_left_for_another_principal_is_not_handed_out(world):
    t = world.ms.share_approve(world.user("op"), ART, "share_" + "a" * 24, True)["ticket"]
    live_result(world, t)
    secret_file(world, t, "member:lab", "https://s.example/s/OTHER")
    got = world.ms.result(world.user("op"), t, 0)
    assert got["secret"] == {"status": "already_read"} and "OTHER" not in json.dumps(got)
    assert (world.bridge.outbox / "secrets" / f"{t}.json").exists()           # left for its owner


def test_share_list_returns_the_view_links_without_urls(world):
    key = member_owner_key("member:op")
    doc = json.loads((world.bridge.mirror / "member-views" / f"{key}.json").read_text())
    doc["links"] = [{"share_id": "share_" + "a" * 24, "artifact_id": ART, "audience": "public", "state": "live"},
                    {"share_id": "share_" + "b" * 24, "artifact_id": OTHER, "audience": "lab", "state": "live",
                     "mine": True}]
    atomic_write_json(world.bridge.mirror / "member-views", f"{key}.json", doc)
    assert len(world.ms.share_list(world.user("op"))["links"]) == 2
    assert [r["artifact_id"] for r in world.ms.share_list(world.user("op"), ART)["links"]] == [ART]
    assert refuse(world.ms.share_list, member_actor(world.people["mal"])).status == 403


def test_share_list_drops_links_the_caller_neither_created_nor_owns(world):
    key = member_owner_key("member:op")
    doc = json.loads((world.bridge.mirror / "member-views" / f"{key}.json").read_text())
    doc["links"] = [{"share_id": "share_" + "a" * 24, "artifact_id": ART, "mine": False},       # on an owned artifact
                    {"share_id": "share_" + "b" * 24, "artifact_id": OTHER, "mine": True},       # created by the caller
                    {"share_id": "share_" + "c" * 24, "artifact_id": OTHER, "mine": False}]      # neither
    atomic_write_json(world.bridge.mirror / "member-views", f"{key}.json", doc)
    got = [r["share_id"][6] for r in world.ms.share_list(world.user("op"))["links"]]
    assert got == ["a", "b"]


def test_library_defaults_to_empty_without_a_view(world):
    assert world.ms.library(world.user("op")) == {"items": [], "folders": []}
