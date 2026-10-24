# ruff: noqa: F811  (pytest fixtures are imported by name)
"""T16 security, round 3 (independent review of 376e012): idempotent safety events, exact outstanding accounting,
repair that cannot lose acknowledged events, honest dry runs, forgery detection by timestamp, auth before 503,
and tests for the mutants the review found alive."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from herdr_engineering import cli
from herdr_engineering.control_plane.collab import (
    CollabError,
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.collab_repair import LogInUseError, classify, repair_file
from tests.control_plane.test_collab import _digest
from tests.control_plane.test_collab_approval import call, s, who  # noqa: F401
from tests.control_plane.test_collab_external import enroll
from tests.control_plane.test_collab_links import link, w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, W, code, svc  # noqa: F401
from tests.control_plane.test_graph_membership import fab, put  # noqa: F401
from tests.control_plane.test_t16_security import FOREIGN, STAMP, _file_log


# ======================================================================== MAJOR A: safety events are idempotent
def test_three_thousand_repeat_revokes_leave_room_to_remove_a_member(w):
    s = w.s
    s.max_log_events = 300
    _, info = w.agent("bob")
    before = len(s._log)
    s.revoke_agent(w.bob, info["agent_id"])
    after_first = len(s._log)
    assert after_first == before + 1
    for _ in range(3000):
        s.revoke_agent(w.bob, info["agent_id"])
    assert len(s._log) == after_first                           # a repeat commits nothing
    assert s.stats["log_full"] == 0
    s.remove_member(w.alice, w.room, "carol")                   # and the owner's revocation still goes through


def _each_safety_emitter(w):
    """(name, first, second): calling `second` after `first` must not commit another event."""
    s = w.s
    _, info = w.agent("bob")
    ext, _, inv = enroll(w, label="one")
    ext2, _, _ = enroll(w, label="two")
    tok = s.create_join_token(w.alice, {"harness_type": "x", "name_prefix": "f", "count": 2})
    inv2 = s.create_external_invite(w.alice, w.room, {"label": "open"})
    s.link_mission(w.alice, w.room, {"mission_id": "m1", "host": "bender"})
    ws = link(w)
    gid = w.agent("bob", name="granted")[1]["agent_id"]
    return [
        ("revoke_agent", lambda: s.revoke_agent(w.bob, info["agent_id"])),
        ("external_leave", lambda: s.external_leave(ext)),
        ("revoke_external", lambda: s.revoke_external(w.alice, w.room, ext2.id)),
        ("revoke_join_token", lambda: s.revoke_join_token(w.alice, tok["join_id"])),
        ("revoke_external_invite", lambda: s.revoke_external_invite(w.alice, w.room, inv2["invite_id"])),
        ("remove_member", lambda: s.remove_member(w.alice, w.room, "carol")),
        ("remove_agent_from_room", lambda: s.remove_agent_from_room(w.alice, w.room, gid)),
        ("unlink_mission", lambda: s.unlink_mission(w.alice, w.room, "m1")),
        ("unlink_workspace", lambda: s.unlink_workspace(w.alice, w.room, ws["link_id"])),
    ], inv


def test_no_safety_event_emitter_can_be_repeated_for_free(w):
    calls, _ = _each_safety_emitter(w)
    for name, fn in calls:
        w.s.clock.t += 61                                      # past the per-action throttles
        try:
            fn()
        except CollabError:
            pytest.fail(f"{name}: the first call must succeed")
        n = len(w.s._log)
        for _ in range(3):
            try:
                fn()
            except CollabError as exc:
                assert exc.status in (404, 409), (name, exc.status)
            assert len(w.s._log) == n, f"{name} committed a second event"


def test_repeating_the_same_user_decision_is_not_a_new_event(s):
    who(s, "bob")

    def decisions():
        return [e for e in s._log if e["event_type"] == "user_decided" and e["payload"]["user_id"] == "bob"]
    assert call(s, "POST", ["admin", "users", "bob", "deny"], None, operator="op")[0] == 200
    assert len(decisions()) == 1
    for _ in range(5):
        call(s, "POST", ["admin", "users", "bob", "deny"], None, operator="op")
    assert len(decisions()) == 1
    call(s, "POST", ["admin", "users", "bob", "kick"], None, operator="op")
    assert len(decisions()) == 2
    for _ in range(5):
        assert call(s, "POST", ["admin", "users", "bob", "kick"], None, operator="op")[0] == 200
    assert len(decisions()) == 2


# ======================================================================== outstanding accounting == state
def test_outstanding_revocations_always_equal_what_exists_in_state(w):
    s = w.s
    s.guest_ttl = 3600.0

    def now():
        return dict(s._recount())
    assert s._outstanding() == sum(now().values())
    base = now()

    s.add_member(w.alice, w.room, {"user_id": "mallory", "role": "viewer"})
    assert now()["member"] == base["member"] + 1
    s.remove_member(w.alice, w.room, "mallory")
    assert now() == base                                       # settled: no phantom

    agent_pr, info = w.agent("bob", actions=("read", "post"))
    after_grant = now()
    assert after_grant["agent"] == base["agent"] + 1 and after_grant["grant"] == base["grant"] + 1
    s.grant_agent(w.bob, w.room, {"agent_id": info["agent_id"], "actions": ["read"]})      # a re-grant of the same agent
    assert now()["grant"] == after_grant["grant"]              # no second reservation for the same (room, agent)
    s.remove_agent_from_room(w.alice, w.room, info["agent_id"])
    assert now()["grant"] == base["grant"]
    s.revoke_agent(w.bob, info["agent_id"])
    s.revoke_agent(w.bob, info["agent_id"])
    assert now()["agent"] == base["agent"]

    # a join token: created, then used up, then no longer outstanding
    tok = s.create_join_token(w.alice, {"harness_type": "x", "name_prefix": "f", "count": 1})
    assert now()["join"] == base["join"] + 1
    s.redeem_join_token({"token": tok["token"]})
    assert now()["join"] == base["join"]                       # used up: nothing left to revoke
    assert now()["agent"] == base["agent"] + 1                 # the agent it made is
    tok2 = s.create_join_token(w.alice, {"harness_type": "x", "name_prefix": "g", "count": 1})
    s.revoke_join_token(w.alice, tok2["join_id"])
    assert now()["join"] == base["join"]

    s.link_mission(w.alice, w.room, {"mission_id": "mm", "host": "bender"})
    assert now()["mission"] == base["mission"] + 1
    s.unlink_mission(w.alice, w.room, "mm")
    assert now()["mission"] == base["mission"]
    assert s._outstanding() == sum(now().values())


def test_a_lapsed_guest_stops_being_outstanding(w):
    s = w.s
    base = s._recount()["member"]
    s.add_member(w.alice, w.room, {"user_id": "mallory", "role": "guest"})
    assert s._recount()["member"] == base + 1
    full = s._outstanding()                                    # fills the cache
    s.clock.t += s.guest_ttl + 100
    assert s._recount()["member"] == base - 1                  # mallory AND the room's other guest (dave) have lapsed
    assert s._outstanding() == full - 2                        # the cache follows the clock with no commit in between


# ======================================================================== mutants from the review
@pytest.mark.parametrize("cap", [300, 301, 302, 303])
def test_a_creation_is_admitted_only_if_its_own_revocation_still_fits(w, cap):
    """Without the +1 for the creation itself the final state overshoots the ordinary limit by one slot."""
    s = w.s
    s.max_log_events = cap
    pid = s.create_project(w.alice, {"name": "many"})["project_id"]
    rooms = [s.create_room(w.alice, pid, {"name": f"m{i}"})["room_id"] for i in range(8)]
    n, end = 0, None
    try:
        while True:
            s.link_mission(w.alice, rooms[n % 8], {"mission_id": f"q{n}", "host": "bender"})
            n += 1
            if n % 40 == 0:
                s.clock.t += 3700
    except CollabError as exc:
        end = exc
    assert end is not None and end.status == 503                     # it stopped because the log is full, not a throttle
    ordinary = cap - max(1, cap // 20)
    assert ordinary - 2 <= len(s._log) + s._outstanding() <= ordinary


def test_the_503_bodies_are_generic_and_pinned(w):
    s = w.s
    s.max_log_events = len(s._log) + 1
    with pytest.raises(CollabError) as full:
        s.add_member(w.alice, w.room, {"user_id": "mallory", "role": "viewer"})
    assert (full.value.status, full.value.message) == (503, "collaboration log is full; ask the operator")
    s.degraded = "durable collab log failed its integrity check (KeyError: 'secret-detail')"
    with pytest.raises(CollabError) as off:
        s.remove_member(w.alice, w.room, "carol")
    assert (off.value.status, off.value.message) == (503, "collaboration is disabled; ask the operator")


def test_first_ts_is_bounded_and_an_evicted_event_id_is_treated_as_pre_join(fab, monkeypatch):
    from herdr_engineering.control_plane import graph as graph_mod
    g = fab.repo.graph
    monkeypatch.setattr(graph_mod, "FIRST_TS_MAX", 3)
    g._first_ts.clear()
    for i in range(5):                                          # drive the bound directly through its own code path
        g._first_ts[f"ev{i}"] = (1000.0, fab.room)
        if len(g._first_ts) > graph_mod.FIRST_TS_MAX:
            g._first_ts.pop(next(iter(g._first_ts)))
    assert len(g._first_ts) == 3 and "ev0" not in g._first_ts
    # an event whose admission time was forgotten is stripped (fail closed), a remembered post-join one is not
    assert g.strip_before({"event_id": "ev0"}, {fab.room: 10.0}) == {"event_id": None}
    assert g.strip_before({"event_id": "ev4"}, {fab.room: 10.0}) == {"event_id": "ev4"}


def test_graph_apply_evicts_the_oldest_admission_time_beyond_the_bound(fab, monkeypatch):
    from herdr_engineering.control_plane import graph as graph_mod
    g = fab.repo.graph
    monkeypatch.setattr(graph_mod, "FIRST_TS_MAX", 3)
    g._first_ts.clear()
    f = fab.feed
    for i in range(6):
        put(f, f"task:evict{i}", "room", label=f"l{i}", room_id=fab.room)
    assert len(g._first_ts) <= 3


# ======================================================================== the CLI contract
def _cli(*argv):
    return subprocess.run([sys.executable, "-c", "import sys; from herdr_engineering.cli import main; sys.exit(main(sys.argv[1:]))",
                           "control-plane", "collab-repair", *argv],
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": os.getcwd()})


def test_the_cli_is_a_dry_run_unless_apply_and_confirmation_are_both_given(tmp_path):
    path = _file_log(tmp_path, FOREIGN)
    before = path.read_bytes()
    out = _cli("--log-file", str(path))
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["mode"] == "dry-run" and report["quarantine"] and path.read_bytes() == before
    refused = _cli("--log-file", str(path), "--apply")
    assert refused.returncode != 0 and "--confirm-quarantine" in (refused.stdout + refused.stderr)
    assert path.read_bytes() == before and not (tmp_path / "collab.jsonl.quarantine.jsonl").exists()
    parser_default = cli._build_parser().parse_args(["control-plane", "collab-repair", "--log-file", str(path)])
    assert parser_default.apply is False and parser_default.include_gap_tail is False
    assert parser_default.confirm_quarantine is False


def test_the_cli_leaves_the_gap_tail_alone_unless_asked(tmp_path):
    path = _file_log(tmp_path)
    rows = [json.loads(line) for line in path.read_bytes().split(b"\n") if line.strip()]
    gone = rows.pop(3)                                           # a hole in the log
    path.write_bytes(b"".join((json.dumps(r) + "\n").encode() for r in rows))
    out = json.loads(_cli("--log-file", str(path)).stdout)
    assert out["quarantine"] == [] and out["gap_tail"] and gone["sequence"] not in {q["sequence"] for q in out["gap_tail"]}
    with_flag = json.loads(_cli("--log-file", str(path), "--include-gap-tail").stdout)
    assert {q["reason"] for q in with_flag["quarantine"]} == {"after_gap"}


# ======================================================================== MAJOR C: no rewrite under a live writer
def test_repair_refuses_while_a_live_event_log_holds_the_file(tmp_path):
    path = _file_log(tmp_path, FOREIGN)
    live = FileEventLog(str(path))                              # a running collab-only server
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=live)
    before = path.read_bytes()
    with pytest.raises(LogInUseError, match="running collaboration server"):
        repair_file(str(path), apply=True, confirmed=True)
    assert path.read_bytes() == before and not (tmp_path / "collab.jsonl.quarantine.jsonl").exists()
    live.record_required({"event_id": "late", "sequence": 9999, "x": 1})        # the writer is still intact
    assert b'"late"' in path.read_bytes() and svc is not None
    os.close(live._fd)                                          # the server stops ...
    assert repair_file(str(path), apply=True, confirmed=True)["quarantine_file"]    # ... and the repair may proceed


def test_repair_refuses_when_another_process_has_the_file_open_even_without_the_lock(tmp_path):
    """A server started before FileEventLog took the lock holds a plain append descriptor."""
    path = _file_log(tmp_path, FOREIGN)
    holder = subprocess.Popen([sys.executable, "-c", "import sys,time; f=open(sys.argv[1],'ab'); print('up',flush=True); "
                               "time.sleep(60)", str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "up"
        with pytest.raises(LogInUseError, match=r"process\(es\)"):
            repair_file(str(path), apply=True, confirmed=True)
    finally:
        holder.kill()
        holder.wait()
    assert repair_file(str(path), apply=True, confirmed=True)["quarantine"]


def test_a_server_cannot_start_writing_while_the_repair_holds_the_log(tmp_path):
    import fcntl
    path = _file_log(tmp_path)
    fd = os.open(str(path), os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)                              # what repair_file holds while it rewrites
    try:
        with pytest.raises(OSError, match="being repaired"):
            FileEventLog(str(path))
    finally:
        os.close(fd)
    os.close(FileEventLog(str(path))._fd)                       # and it starts normally afterwards


def test_events_written_after_a_repair_land_in_the_repaired_file(tmp_path):
    path = _file_log(tmp_path, FOREIGN)
    out = repair_file(str(path), apply=True, confirmed=True)
    log = FileEventLog(str(path))
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=log)
    assert svc.hydrate_or_degrade(log.load()) and not svc.degraded
    n = len(svc._log)
    svc.create_project(svc.authenticate("Bearer " + USERS["alice"], None), {"name": "after"})
    assert len([line for line in path.read_bytes().split(b"\n") if line.strip()]) == n + 1 and out["backup"]


# ======================================================================== minor 1: the dry run says what it cannot fix
def test_the_default_dry_run_names_the_gap_tail_instead_of_reporting_nothing(tmp_path):
    path = _file_log(tmp_path)
    rows = [json.loads(line) for line in path.read_bytes().split(b"\n") if line.strip()]
    forged = {**rows[0], "event_id": "forged0", "sequence": 0}                  # a perfect copy of the shape, at 0
    path.write_bytes(b"".join((json.dumps(r) + "\n").encode() for r in [forged, *rows]))
    out = repair_file(str(path))
    assert out["quarantine"] == [] and out["contiguous"] is False
    assert [g["event_id"] for g in out["gap_tail"]] == ["forged0"] and "--include-gap-tail" in out["note_gap"]
    fixed = repair_file(str(path), apply=True, confirmed=True, cut_tail=True)
    assert [q["event_id"] for q in fixed["quarantine"]] == ["forged0"]
    assert repair_file(str(path))["contiguous"] is True


def test_a_clean_log_reports_contiguous_and_no_gap_note(tmp_path):
    out = repair_file(str(_file_log(tmp_path)))
    assert out["contiguous"] is True and "gap_tail" not in out and out["quarantine"] == []


# ======================================================================== minor 2: forged rows do not share one instant
def _row(**kw):
    base = {"entity_type": "collab", "entity_id": "log", "sequence": 1, "event_type": "x", "event_id": "e",
            "source_timestamp": STAMP, "ingest_timestamp": STAMP,
            "payload": {"org_id": "d", "correlation_id": "c", "created_at": STAMP}}
    return {**base, **kw}


@pytest.mark.parametrize("override", [
    {"ingest_timestamp": "2026-01-01T00:00:01+00:00"},
    {"source_timestamp": "2026-01-01T00:00:00.5+00:00"},
    {"payload": {"org_id": "d", "correlation_id": "c", "created_at": "2026-01-01T00:00:09+00:00"}},
    {"source_timestamp": None},
    {"ingest_timestamp": "not a time"},
])
def test_a_row_whose_three_timestamps_disagree_is_not_a_real_collab_row(override):
    keep, bad = classify([_row(**override)])
    assert keep == [] and [why for _, why in bad] == ["timestamp_mismatch"]


def test_equal_instants_in_any_spelling_are_accepted():
    from datetime import UTC, datetime
    dt = datetime(2026, 1, 1, tzinfo=UTC)
    for row in (_row(), _row(source_timestamp=dt, ingest_timestamp=dt),
                _row(source_timestamp="2026-01-01T00:00:00Z", ingest_timestamp="2026-01-01 00:00:00+00:00")):
        keep, bad = classify([row])
        assert len(keep) == 1 and not bad


def test_every_row_the_service_writes_passes_the_timestamp_test(tmp_path):
    """The invariant the check relies on, for every event type a busy room produces."""
    path = _file_log(tmp_path)
    rows = [json.loads(line) for line in path.read_bytes().split(b"\n") if line.strip()]
    assert len(rows) > 5
    keep, bad = classify(rows)
    assert len(keep) == len(rows) and not bad


# ======================================================================== minor 4: auth before 503
def test_an_unauthenticated_caller_cannot_tell_that_collaboration_is_degraded():
    c = CollabService(StaticTokenAuthenticator({"alice": _digest("tok-alice")}))
    c.hydrate_or_degrade([{"entity_type": "collab", "entity_id": "log", "sequence": 9, "event_type": "x", "payload": {}}])
    assert c.degraded
    for header in ("", "Bearer", "Bearer wrong", "Agent nonsense", "Basic abc"):
        assert code(c.authenticate, header, None) == 401, header
    assert code(c.authenticate, "Bearer tok-alice", None) == 503          # only a valid token learns it


def test_a_healthy_service_answers_the_same_401_for_the_same_bad_tokens():
    c = CollabService(StaticTokenAuthenticator({"alice": _digest("tok-alice")}))
    for header in ("", "Bearer", "Bearer wrong", "Agent nonsense"):
        assert code(c.authenticate, header, None) == 401


# ======================================================================== minor 5: strip_before on a shared mission
def test_a_mission_event_written_from_another_room_is_shown_to_the_reader_after_their_join(fab):
    g = fab.repo.graph
    g._first_ts["m-ev-after"] = (500.0, "room-r1-the-reader-is-not-in")
    g._first_ts["m-ev-before"] = (50.0, "room-r1-the-reader-is-not-in")
    joins = {fab.room2: 100.0}                                   # the reader is only in R2, joined at 100
    out = g.strip_before([{"event_id": "m-ev-after"}, {"event_id": "m-ev-before"}], joins)
    assert out == [{"event_id": "m-ev-after"}, {"event_id": None}]      # fail closed only for what predates the join
    assert g.strip_before({"event_id": "m-ev-after"}, {}) == {"event_id": None}      # no join at all: nothing is shown


# ======================================================================== Pi H6: a restart can resume from the 409
def test_a_sequence_conflict_names_the_callers_own_last_sequence_and_nobody_elses():
    from tests.control_plane.test_t16_security import _http, _server, v2
    a, b, op = "ing-a-0123456789abcdef", "ing-b-0123456789abcdef", "op-key-0123456789abcdef"
    srv = _server({a: "ingest", b: "ingest", op: "operator"})
    try:
        for n in range(3):
            assert _http(srv, "POST", "/api/v1/ingest", a,
                         body=v2("host", "h1", "host.registered", {"host_name": "h1"}, n, source="pi"))[0] == 200
        for n in range(7):                                       # another key, same source NAME, a different domain
            assert _http(srv, "POST", "/api/v1/ingest", b,
                         body=v2("host", "h2", "host.registered", {"host_name": "h2"}, n, source="pi", event_id=f"b-{n}"))[0] == 200
        # A restarted emitter starts over at sequence 1 with fresh event ids
        status, body = _http(srv, "POST", "/api/v1/ingest", a,
                             body=v2("host", "h1", "host.registered", {"host_name": "h1"}, 1, source="pi", event_id="again-1"))
        assert status == 409 and body["reason"] == "sequence_conflict" and body["last_sequence"] == 2
        status, body = _http(srv, "POST", "/api/v1/ingest", b,
                             body=v2("host", "h2", "host.registered", {"host_name": "h2"}, 1, source="pi", event_id="again-b"))
        assert status == 409 and body["last_sequence"] == 6      # its own, not the other key's 2
        assert "b-" not in json.dumps(_http(srv, "POST", "/api/v1/ingest", a, body=v2(
            "host", "h1", "host.registered", {"host_name": "h1"}, 0, source="pi", event_id="again-0"))[1])
        # resuming at last_sequence + 1 works
        assert _http(srv, "POST", "/api/v1/ingest", a, body=v2(
            "host", "h1", "host.registered", {"host_name": "h1"}, 3, source="pi", event_id="resume-3"))[0] == 200
        # other 409s keep the plain reason and carry no sequence
        status, body = _http(srv, "POST", "/api/v1/ingest", a, body=v2(
            "host", "h1", "host.registered", {"host_name": "CHANGED"}, 0, source="pi"))
        assert status == 409 and body["reason"] == "conflict" and "last_sequence" not in body
    finally:
        srv.shutdown()


def test_the_fallback_source_listing_still_works_for_a_sources_own_key():
    from tests.control_plane.test_t16_security import _http, _server, v2
    a, b = "ing-a-0123456789abcdef", "ing-b-0123456789abcdef"
    srv = _server({a: "ingest", b: "ingest"})
    try:
        _http(srv, "POST", "/api/v1/ingest", a, body=v2("host", "h1", "host.registered", {"host_name": "h1"}, 4, source="pi"))
        _http(srv, "POST", "/api/v1/ingest", b, body=v2("host", "h2", "host.registered", {"host_name": "h2"}, 9, source="other"))
        status, body = _http(srv, "GET", "/api/v1/ingest/sources", a)
        assert status == 200 and [(s["source"], s["last_sequence"]) for s in body["sources"]] == [("pi", 4)]
    finally:
        srv.shutdown()
