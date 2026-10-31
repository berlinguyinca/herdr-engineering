# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Plan 3, Task 7a: the collab-public artifact bridge reads (list / head / read / history), the ``agents.json``
feed and the audit trail.

A real ``CollabService`` with two agents: alice's agent A holds a write grant on X, bob's agent B a read grant on Y.
The mirror is hand-written the way the worker writes it (``grants.json`` with ``limits``, ``head.json``, blobs named
by sha256); the outbox is a temp dir. One fake clock drives both the service and the bridge."""
from __future__ import annotations

import hashlib
import os
import time

import pytest

from herdr_engineering.control_plane import collab
from herdr_engineering.control_plane.collab import (
    BULK_EVENTS,
    CollabError,
    CollabService,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.collab_artifacts import (
    AGENTS_FEED,
    AUDIT_EVERY,
    MAX_READ_BYTES,
    UNAVAILABLE,
    CollabArtifactBridge,
)
from herdr_engineering.control_plane.collab_bridge_files import (
    FEED_MAX_AGE,
    BridgeLimits,
    atomic_write_bytes,
    atomic_write_json,
    read_json_beneath,
)
from herdr_engineering.control_plane.collab_intents import AGENTS_FEED as WORKER_FEED
from herdr_engineering.control_plane.collab_intents import AGENTS_FEED_MAX_BYTES
from herdr_engineering.control_plane.collab_mirror import (
    ARTIFACTS_DIR,
    BLOBS_DIR,
    GRANTS_NAME,
    HEAD_NAME,
)
from tests.control_plane.test_collab import _digest
from tests.control_plane.test_collab_approval import call, who
from tests.control_plane.test_collab_approval import s as approval_svc  # noqa: F401
from tests.control_plane.test_collab_external import enroll
from tests.control_plane.test_collab_tasks import USERS, W, svc  # noqa: F401

X, Y = "art_x", "art_y"
TEXT = ("plain ascii, then é and € and 😀; " * 40).encode()
OLD_TEXT = b"the previous head\n"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_entry(name, data, *, text=True, mirrored=True, media_type="text/plain"):
    return {"name": name, "size_bytes": len(data), "sha256": sha(data), "media_type": media_type,
            "text": text, "mirrored": mirrored}


def version(vid, number, files, *, mirrored=True, parents=(), message="m"):
    return {"version_id": vid, "version_number": number, "parent_version_ids": list(parents),
            "created_via": "owner", "collaborator_agent_id": None, "author_label": "owner", "message": message,
            "created_at": "2026-10-30T12:00:00+00:00", "merge": len(parents) > 1, "mirrored": mirrored,
            "files": files}


class Mirror:
    """The worker's side of the bridge, written by hand."""

    def __init__(self, root, clock):
        self.root, self.clock = root, clock
        self.agents: dict = {}

    def grants(self, *, age=0.0, limits="default"):
        doc = {"generated_at": self.clock() - age, "agents": self.agents}
        if limits == "default":
            doc["limits"] = BridgeLimits().to_dict()
        elif limits is not None:
            doc["limits"] = limits
        atomic_write_json(self.root, GRANTS_NAME, doc)

    def grant(self, agent_id, artifact_id, permission, ttl=86400.0):
        self.agents.setdefault(agent_id, {})[artifact_id] = {"permission": permission,
                                                            "expires_at": self.clock() + ttl}

    def head(self, artifact_id, versions, *, title="t.txt", writable=True):
        doc = {"artifact_id": artifact_id, "title": title, "artifact_type": "file", "writable": writable,
               "writable_reason": None if writable else "report artifacts are read-only for collaborators",
               "head": {"version_id": versions[0]["version_id"], "version_number": versions[0]["version_number"]},
               "versions": versions}
        atomic_write_json(self.root / ARTIFACTS_DIR / artifact_id, HEAD_NAME, doc)

    def blob_path(self, artifact_id, data):
        return self.root / ARTIFACTS_DIR / artifact_id / BLOBS_DIR / sha(data)

    def blob(self, artifact_id, data, *, content=None):
        atomic_write_bytes(self.root / ARTIFACTS_DIR / artifact_id / BLOBS_DIR, sha(data),
                           data if content is None else content)


class World:
    def __init__(self, w, tmp_path):
        self.w, self.s, self.clock = w, w.s, w.s.clock
        self.a, self.a_info = w.agent("alice")
        self.b, self.b_info = w.agent("bob", actions=("read", "post"))
        self.mirror = Mirror(tmp_path / "mirror", self.clock)
        self.outbox = tmp_path / "outbox"
        self.outbox.mkdir()
        m = self.mirror
        m.grant(self.a.id, X, "write")
        m.grant(self.b.id, Y, "read")
        m.head(X, [version("v_x2", 2, [file_entry("a.txt", TEXT),
                                        file_entry("logo.png", PNG, text=False, mirrored=False,
                                                   media_type="image/png")], parents=("v_x1",)),
                   version("v_x1", 1, [file_entry("a.txt", OLD_TEXT, mirrored=False)], mirrored=False)],
               title="notes.txt")
        m.blob(X, TEXT)
        y_text = b"why\n"
        m.head(Y, [version("v_y1", 1, [file_entry("y.txt", y_text)])], title="y.txt", writable=False)
        m.blob(Y, y_text)
        m.grants()
        self.bridge = CollabArtifactBridge(self.s, m.root, self.outbox, clock=self.clock, feed_interval=3600.0)
        self.s.artifact_bridge = self.bridge

    def events(self, event_type):
        return [e for e in self.s._log if e["event_type"] == event_type]


@pytest.fixture
def world(svc, tmp_path):
    out = World(W(svc), tmp_path)
    yield out
    out.bridge.stop()
    assert not out.bridge.feed_thread_alive()


def status(fn, *a, **kw):
    with pytest.raises(CollabError) as e:
        fn(*a, **kw)
    return e.value.status, e.value.message


# ------------------------------------------------------------------------------ listing and isolation
def test_each_agent_lists_only_its_own_grants(world):
    br = world.bridge
    listed = br.list(world.a)["artifacts"]
    assert listed == [{"artifact_id": X, "title": "notes.txt", "permission": "write", "writable": True,
                       "head_version_id": "v_x2", "head_version_number": 2}]
    listed = br.list(world.b)["artifacts"]
    assert listed == [{"artifact_id": Y, "title": "y.txt", "permission": "read", "writable": False,
                       "head_version_id": "v_y1", "head_version_number": 1}]
    assert br.has_grants(world.a) and br.has_grants(world.b)
    assert br.has_write_grant(world.a) and not br.has_write_grant(world.b)


def test_a_granted_artifact_without_head_json_is_not_listed_and_reads_404(world):
    world.mirror.grant(world.a.id, "art_gone", "read")
    world.mirror.grants()
    assert [r["artifact_id"] for r in world.bridge.list(world.a)["artifacts"]] == [X]
    assert status(world.bridge.head, world.a, "art_gone") == (404, "not found")


def test_an_ungranted_artifact_is_the_same_404_as_an_unknown_one_and_is_audited(world):
    br, b = world.bridge, world.b
    calls = [
        lambda aid: br.head(b, aid),
        lambda aid: br.read(b, aid, "a.txt"),
        lambda aid: br.history(b, aid),
    ]
    for fn in calls:
        for aid in (X, "art_unknown", "../etc", "x" * 400):
            world.clock.t += 61                                  # past _audit_denied's per-minute throttle
            world.mirror.grants()                                # the worker's heartbeat keeps it fresh
            assert status(fn, aid) == (404, "not found")
    denied = [e["payload"] for e in world.events("audit_denied")]
    assert {(p["action"], p["reason"]) for p in denied} == {
        ("artifact.head", "no_grant"), ("artifact.read", "no_grant"), ("artifact.history", "no_grant")}
    assert {p["resource_id"] for p in denied if p["action"] == "artifact.read"} >= {X, "art_unknown"}
    assert all(len(p["resource_id"]) <= 128 for p in denied)
    assert all(p["actor_agent_id"] == b.id for p in denied)
    assert not world.events("artifact_tool_used")


def test_an_expired_or_unknown_permission_grant_is_no_grant(world):
    world.mirror.grant(world.b.id, X, "read", ttl=-1.0)
    world.mirror.agents[world.a.id][X]["permission"] = "admin"
    world.mirror.grants()
    for pr in (world.a, world.b):
        assert status(world.bridge.head, pr, X) == (404, "not found")
        assert X not in [r["artifact_id"] for r in world.bridge.list(pr)["artifacts"]]
    assert not world.bridge.has_write_grant(world.a)


def test_a_person_is_never_served(world):
    assert status(world.bridge.head, world.w.alice, X) == (404, "not found")
    assert not world.bridge.has_grants(world.w.alice)


@pytest.mark.parametrize("case", ["stale", "no_limits", "bad_limits", "missing", "no_agents"])
def test_a_stale_or_invalid_grants_json_makes_the_bridge_unavailable(world, case):
    m, br = world.mirror, world.bridge
    if case == "stale":
        m.grants(age=FEED_MAX_AGE + 1)
    elif case == "no_limits":
        m.grants(limits=None)
    elif case == "bad_limits":
        m.grants(limits={"commits_per_minute": 0})
    elif case == "missing":
        os.unlink(m.root / GRANTS_NAME)
    else:
        atomic_write_json(m.root, GRANTS_NAME, {"generated_at": world.clock(), "limits": BridgeLimits().to_dict()})
    assert not br.available()
    for pr in (world.a, world.b):
        assert not br.has_grants(pr) and not br.has_write_grant(pr)
    for fn in (lambda: br.list(world.a), lambda: br.head(world.a, X), lambda: br.read(world.a, X, "a.txt"),
               lambda: br.history(world.a, X), br.limits):
        assert status(fn) == (503, UNAVAILABLE)
    assert UNAVAILABLE == "collaboration bridge unavailable"


def test_a_fresh_grants_json_at_the_age_limit_is_available_and_carries_its_limits(world):
    world.mirror.grants(age=FEED_MAX_AGE, limits={"commits_per_minute": 7})
    assert world.bridge.available()
    assert world.bridge.limits() == BridgeLimits(commits_per_minute=7)


# ------------------------------------------------------------------------------ head / history
def test_head_returns_the_head_manifest(world):
    out = world.bridge.head(world.a, X)
    assert out["artifact_id"] == X and out["head_version_id"] == "v_x2" and out["head_version_number"] == 2
    assert out["permission"] == "write" and out["writable"] is True and out["title"] == "notes.txt"
    files = {f["name"]: f for f in out["files"]}
    assert files["a.txt"] == {"name": "a.txt", "size_bytes": len(TEXT), "sha256": sha(TEXT),
                              "media_type": "text/plain", "text": True, "mirrored": True}
    assert files["logo.png"]["text"] is False
    assert world.bridge.head(world.b, Y)["writable"] is False        # read grant on a read-only artifact


def test_history_lists_versions_newest_first_with_a_bounded_limit(world):
    out = world.bridge.history(world.a, X)
    assert [v["version_id"] for v in out["versions"]] == ["v_x2", "v_x1"]
    assert out["versions"][0]["parent_version_ids"] == ["v_x1"] and out["versions"][1]["mirrored"] is False
    assert out["truncated"] is False
    one = world.bridge.history(world.a, X, limit=1)
    assert [v["version_id"] for v in one["versions"]] == ["v_x2"] and one["truncated"] is True
    for bad in (0, 201, -1, True, "5", 1.5):
        assert status(world.bridge.history, world.a, X, limit=bad)[0] == 400


# ------------------------------------------------------------------------------ read
def test_read_returns_text_and_slices_never_split_a_character(world):
    whole = world.bridge.read(world.a, X, "a.txt", max_bytes=MAX_READ_BYTES)
    assert whole["text"].encode() == TEXT and whole["next_offset"] is None
    assert whole["version_id"] == "v_x2" and whole["size_bytes"] == len(TEXT) and whole["offset"] == 0
    for step in (1, 2, 3, 5, 7):
        offset, parts = 0, []
        while offset is not None:
            out = world.bridge.read(world.a, X, "a.txt", offset=offset, max_bytes=step)
            assert out["offset"] == offset
            raw = out["text"].encode()
            assert raw and (len(raw) <= step or len(raw) <= 4)    # a lone character may exceed a tiny max_bytes
            parts.append(raw)
            offset = out["next_offset"]
        assert b"".join(parts) == TEXT, step


def test_read_argument_bounds(world):
    br, a = world.bridge, world.a
    assert MAX_READ_BYTES == 1024 * 1024
    for bad in (0, -1, MAX_READ_BYTES + 1, True, "10"):
        assert status(br.read, a, X, "a.txt", max_bytes=bad)[0] == 400
    for bad in (-1, len(TEXT) + 1, True, 1.0):
        assert status(br.read, a, X, "a.txt", offset=bad)[0] == 400
    mid = TEXT.index("é".encode()) + 1                           # inside a two-byte character
    assert status(br.read, a, X, "a.txt", offset=mid)[0] == 400
    end = br.read(a, X, "a.txt", offset=len(TEXT))
    assert end["text"] == "" and end["next_offset"] is None
    for bad in (None, 5, "", "x" * 5000):
        assert status(br.read, a, X, bad)[0] == 400


def test_an_unmirrored_version_or_file_is_404_not_mirrored_and_binary_is_415(world):
    br, a = world.bridge, world.a
    assert status(br.read, a, X, "a.txt", version_id="v_x1") == (404, "version not mirrored")
    assert status(br.read, a, X, "logo.png") == (415, "binary files are not readable through collaboration")
    assert status(br.read, a, X, "nope.txt") == (404, "not found")
    assert status(br.read, a, X, "a.txt", version_id="v_unknown") == (404, "not found")


def test_a_blob_collected_after_the_head_read_is_404_after_one_retry(world):
    os.unlink(world.mirror.blob_path(X, TEXT))
    assert status(world.bridge.read, world.a, X, "a.txt") == (404, "version not mirrored")


def test_a_blob_whose_sha256_differs_from_the_manifest_is_502_and_returns_no_text(world, caplog):
    tampered = TEXT.replace(b"plain", b"PLAIN", 1)
    world.mirror.blob(X, TEXT, content=tampered)
    with pytest.raises(CollabError) as e:
        world.bridge.read(world.a, X, "a.txt")
    assert (e.value.status, e.value.message) == (502, "mirror integrity")
    assert "PLAIN" not in caplog.text and "plain" not in caplog.text
    world.mirror.blob(X, TEXT, content=TEXT[:-1])                 # a short blob is an integrity failure too
    assert status(world.bridge.read, world.a, X, "a.txt") == (502, "mirror integrity")


def test_slices_of_an_unchanged_blob_are_hashed_once(world):
    br, a = world.bridge, world.a
    br.read(a, X, "a.txt", max_bytes=10)
    br.read(a, X, "a.txt", offset=10, max_bytes=10)
    br.read(a, X, "a.txt", offset=20, max_bytes=10)
    assert br.stats["blob_hashes"] == 1


def test_replacing_the_blob_file_re_verifies_and_a_tampered_one_is_502(world):
    br, a = world.bridge, world.a
    path = world.mirror.blob_path(X, TEXT)
    br.read(a, X, "a.txt", max_bytes=10)
    before = os.stat(path)
    tampered = TEXT.replace(b"plain", b"PLAIN", 1)               # same size and mtime: only the inode tells
    world.mirror.blob(X, TEXT, content=tampered)                  # atomic_write_bytes: a new inode
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(path)
    assert after.st_ino != before.st_ino
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert status(br.read, a, X, "a.txt", max_bytes=10) == (502, "mirror integrity")
    assert br.stats["blob_hashes"] == 2
    world.mirror.blob(X, TEXT)
    assert br.read(a, X, "a.txt", max_bytes=10)["text"]
    assert br.stats["blob_hashes"] == 3


def test_an_in_place_overwrite_with_a_new_mtime_re_verifies(world):
    br, a = world.bridge, world.a
    path = world.mirror.blob_path(X, TEXT)
    br.read(a, X, "a.txt", max_bytes=10)
    ino = os.stat(path).st_ino
    with open(path, "r+b") as fh:                                 # test harness tampering, not bridge code
        fh.write(b"PLAIN")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert os.stat(path).st_ino == ino and os.stat(path).st_size == len(TEXT)
    assert status(br.read, a, X, "a.txt", max_bytes=10) == (502, "mirror integrity")


def test_the_verification_cache_is_bounded(world):
    br = world.bridge
    for i in range(br.VERIFY_CACHE_MAX + 50):
        br._remember_verified(("p", i, 1, 1))
    assert len(br._verified) == br.VERIFY_CACHE_MAX


# ------------------------------------------------------------------------------ audit
def test_reads_are_coalesced_into_one_event_per_600_seconds_with_a_calls_count(world):
    br, a = world.bridge, world.a
    start = world.clock.t
    for i in range(100):
        world.clock.t = start + i * 5                                # the 100th call lands at +495 s
        if i % 20 == 0:
            world.mirror.grants()                                    # the worker's heartbeat
        br.read(a, X, "a.txt", max_bytes=8)
    used = world.events("artifact_tool_used")
    assert len(used) == 1
    p = used[0]["payload"]
    assert p["calls"] == 1 and p["tool"] == "artifact_read" and p["resource_id"] == X
    assert p["actor_agent_id"] == a.id and p["outcome"] == "allowed"
    world.clock.t = start + AUDIT_EVERY
    world.mirror.grants()
    br.read(a, X, "a.txt", max_bytes=8)
    used = world.events("artifact_tool_used")
    assert len(used) == 2 and used[1]["payload"]["calls"] == 100
    assert TEXT[:8].decode() not in str(used)                       # never the text


def test_a_call_just_inside_the_window_stays_suppressed(world):
    br, a = world.bridge, world.a
    start = world.clock.t
    br.head(a, X)
    world.clock.t = start + AUDIT_EVERY - 1
    world.mirror.grants()
    br.head(a, X)
    assert len(world.events("artifact_tool_used")) == 1
    br.history(a, X)                                                  # another tool is another key
    br.list(a)
    assert {e["payload"]["tool"] for e in world.events("artifact_tool_used")} == {
        "artifact_head", "artifact_history", "artifacts_list"}


def test_a_full_log_refuses_the_read_rather_than_serving_it_unaudited(world):
    world.s.max_log_events = len(world.s._log)
    assert status(world.bridge.read, world.a, X, "a.txt")[0] == 503
    world.s.max_log_events = 200_000
    assert world.bridge.read(world.a, X, "a.txt")["text"]
    assert world.events("artifact_tool_used")[0]["payload"]["calls"] == 1


def test_the_new_events_are_bulk_and_wake_no_room(world):
    for t in ("artifact_tool_used", "artifact_commit_submitted", "artifact_upload_staged"):
        assert t in BULK_EVENTS
        assert world.s._log_limit(t) == world.s._log_limit("message")
    before = dict(world.s._room_seq)
    for t in ("artifact_tool_used", "artifact_commit_submitted", "artifact_upload_staged"):
        world.s._touch_rooms({"room_id": world.w.room, "agent_id": world.a.id}, 999, t)
    assert world.s._room_seq == before


def test_replaying_the_log_with_the_new_events_is_harmless_also_on_older_code(world, monkeypatch):
    br = world.bridge
    br.read(world.a, X, "a.txt")
    br.head(world.a, X)
    for t in ("artifact_commit_submitted", "artifact_upload_staged"):
        world.s._commit(t, world.s._actor(world.a, action="artifact.commit", outcome="allowed", resource_id=X,
                                          artifact_id=X, project_ids=[]))
    rows = list(world.s._log)

    def fresh():
        return CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=world.clock)
    again = fresh()
    assert again.hydrate_or_degrade(rows) and again.degraded is None
    assert set(again._agents) == set(world.s._agents) and again.seq == world.s.seq
    old = BULK_EVENTS - {"artifact_tool_used", "artifact_commit_submitted", "artifact_upload_staged"}
    monkeypatch.setattr(collab, "BULK_EVENTS", old)                   # the code before this task
    older = fresh()
    assert older.hydrate_or_degrade(rows) and older.degraded is None
    assert set(older._agents) == set(world.s._agents)


# ------------------------------------------------------------------------------ agents.json
def read_feed(outbox):
    return read_json_beneath(outbox, [AGENTS_FEED], max_bytes=AGENTS_FEED_MAX_BYTES)


def test_the_feed_lists_agents_with_the_external_join_id_and_hard_expiry(world):
    assert AGENTS_FEED == WORKER_FEED
    ext_pr, ext_out, inv = enroll(world.w)
    world.bridge.write_agents_feed()
    feed = read_feed(world.outbox)
    assert feed["generated_at"] == world.clock()
    agents = feed["agents"]
    row = agents[ext_pr.id]
    assert row == {"owner": "alice", "status": "active", "expires_at": ext_out["expires_at"], "external": True,
                   "join_id": inv["invite_id"], "hard_expiry": ext_out["expires_at"]}
    a = agents[world.a.id]
    assert a["owner"] == "alice" and a["status"] == "active" and a["external"] is False
    assert a["join_id"] is None and a["hard_expiry"] is None and a["expires_at"] > world.clock()
    blob = (world.outbox / AGENTS_FEED).read_text()
    assert world.a_info["credential"] not in blob and ext_out["credential"] not in blob and inv["code"] not in blob


def test_the_feed_is_rewritten_on_revoke(world):
    br = world.bridge
    ext_pr, _, _ = enroll(world.w)
    br.write_agents_feed()
    assert world.a.id in read_feed(world.outbox)["agents"]
    world.s.revoke_agent(world.w.alice, world.a.id)
    assert world.a.id not in read_feed(world.outbox)["agents"]
    world.s.revoke_external(world.w.alice, world.w.room, ext_pr.id)
    assert ext_pr.id not in read_feed(world.outbox)["agents"]
    assert world.b.id in read_feed(world.outbox)["agents"]


def test_the_feed_shows_a_disabled_owners_agents_as_disabled(approval_svc, tmp_path):
    s = approval_svc
    who(s, "bob")
    call(s, "POST", ["admin", "users", "bob", "approve"], "adm")
    info = s.connect_agent(who(s, "bob"), {"harness_type": "pi"})
    (tmp_path / "out").mkdir()
    br = CollabArtifactBridge(s, tmp_path / "mirror", tmp_path / "out", clock=s._now, feed_interval=3600.0)
    s.artifact_bridge = br
    br.write_agents_feed()
    assert read_feed(tmp_path / "out")["agents"][info["agent_id"]]["status"] == "active"
    call(s, "POST", ["admin", "users", "bob", "disable"], None, operator="op")
    assert read_feed(tmp_path / "out")["agents"][info["agent_id"]]["status"] == "disabled"
    call(s, "POST", ["admin", "users", "bob", "enable"], None, operator="op")
    assert read_feed(tmp_path / "out")["agents"][info["agent_id"]]["status"] == "active"


def test_the_feed_thread_writes_at_start_and_on_wake_and_stops_cleanly(world):
    br = world.bridge
    br.start()
    assert br.feed_thread_alive()
    first = read_feed(world.outbox)
    assert first["generated_at"] == world.clock()
    world.clock.t += 10
    br.agents_changed()                                                # what _commit does on agent_revoked
    deadline = time.monotonic() + 5
    while read_feed(world.outbox)["generated_at"] != world.clock() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert read_feed(world.outbox)["generated_at"] == world.clock()
    br.start()                                                         # idempotent: still one thread
    br.stop()
    assert not br.feed_thread_alive()


def test_the_feed_thread_rewrites_on_its_interval(svc, tmp_path):
    w = W(svc)
    (tmp_path / "out").mkdir()
    br = CollabArtifactBridge(svc, tmp_path / "mirror", tmp_path / "out", clock=svc.clock, feed_interval=0.02)
    try:
        br.start()
        svc.clock.t += 5
        deadline = time.monotonic() + 5
        while read_feed(tmp_path / "out")["generated_at"] != svc.clock() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert read_feed(tmp_path / "out")["generated_at"] == svc.clock()
    finally:
        br.stop()
    assert not br.feed_thread_alive()
    assert w.room


def test_a_failing_feed_write_never_raises_and_leaves_no_partial_file(world, caplog):
    blocked = world.outbox / "blocked"
    blocked.write_text("not a dir")
    br = CollabArtifactBridge(world.s, world.mirror.root, blocked, clock=world.clock, feed_interval=3600.0)
    br.write_agents_feed()                                             # logged, not raised
    assert "agents feed write failed" in caplog.text
    world.s.artifact_bridge = br
    world.s.revoke_agent(world.w.alice, world.a.id)                   # the revoke itself still succeeds
    assert world.s._agents[world.a.id]["status"] == "revoked"
    br.write_agents_feed()
    assert not [p for p in world.outbox.iterdir() if p.name.startswith(".tmp-")]
    br.stop()
