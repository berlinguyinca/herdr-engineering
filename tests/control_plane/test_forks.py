# ruff: noqa: F811, E701  (pytest fixtures are imported by name)
"""T13: alternative implementation forks, comparison and promotion (docs/contracts/lineage-events.md, "Forks (T13)").

Cast as in test_workbench: alice owner, bob contributor, carol viewer, dave guest, mallory in no room. Lineage is
written by the ingest key WRITER (the fork's emitter); evidence the operator trusts is written by OPERATOR."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.collab_workbench import (
    DEFAULT_ACTIONS,
    STEER_ACTIONS,
    normalize_steering,
)
from herdr_engineering.control_plane.forks import normalize_forks, promotion_check
from tests.control_plane.test_collab_links import link, w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc, user  # noqa: F401
from tests.control_plane.test_graph import OPERATOR, WRITER
from tests.control_plane.test_graph_membership import fab, put  # noqa: F401
from tests.control_plane.test_lineage_views import edge, node

H0, H1, H2, B0 = "a" * 40, "b" * 40, "d" * 40, "c" * 40
ALL = {"enabled": True, "actions": list(STEER_ACTIONS),
       "forks": {"enabled": True, "evidence_writers": ["key:op"], "max_forks_per_origin": 3,
                 "max_open_forks_per_room": 4}}
_n = [0]


def k():
    _n[0] += 1
    return f"f{_n[0]}"


def err(fn, *a, **kw):
    with pytest.raises(CollabError) as exc:
        fn(*a, **kw)
    return exc.value.status, getattr(exc.value, "extra", {}).get("code"), exc.value


@pytest.fixture
def fk(fab):
    fab.s.steering = normalize_steering(ALL)
    node(fab.feed, "attempt:a1", state="done", attrs={"head_sha": H0}, room_id=fab.room)
    return fab


def cfg(f):
    return f.s.steer_cfg()["forks"]


def mkfork(f, n, head=H1, state="running", origin="attempt:a1", **attrs):
    aid = f"approach:f{n}"
    node(f.feed, aid, state=state, label=f"fork {n}", room_id=f.room,
         attrs={"head_sha": head, "base_sha": B0, "branch": f"pi-fork/f{n}", "repo": "github.com/o/r",
                "files_changed": 2, "insertions": 10, "deletions": 1, "touched_paths": ["src/a.py", "docs/x.md"], **attrs})
    edge(f.feed, "fork", origin, aid, room_id=f.room)
    return aid


def validate(f, aid, head=H1, n=1, who=OPERATOR, **attrs):
    ci = f"ci:{aid.split(':')[1]}-{n}"
    node(f.feed, ci, state="done", room_id=f.room, principal=who)
    edge(f.feed, "validated", ci, aid, {"commit": head, **attrs}, room_id=f.room, principal=who)


def review(f, aid, verdict="lgtm", head=H1, n=1, who=OPERATOR):
    rv = f"review:{aid.split(':')[1]}-{n}"
    node(f.feed, rv, state="done", room_id=f.room, principal=who)
    edge(f.feed, "reviewed", rv, aid, {"commit": head, "verdict": verdict}, room_id=f.room, principal=who)


def tgt(f, who, nid):
    return f.s.steering_target(who, f.room, nid)


def propose(f, who, action, nid, **kw):
    t = tgt(f, who, nid)
    body = {"action": action, "node_id": nid, "expected_fence": t["fence"], "idempotency_key": k(), **kw}
    return f.s.propose_steering(who, f.room, body)[1]["steering"]


def confirm(f, who, st):
    t = tgt(f, who, st["target"]["node_id"])
    return f.s.confirm_steering(who, f.room, st["steering_id"],
                                {"digest": st["digest"], "expected_fence": t["fence"]})["steering"]


def pending(f):
    return f.s.steering_pending("key:writer")["steering"]


def compare(f, who, nid):
    return f.s.fork_comparison(who, f.room, {"node_id": nid})


# ----------------------------------------------------------------------------- config and defaults
def test_fork_and_promote_are_never_in_the_default_actions_and_need_their_own_switch(fab):
    assert "fork" not in DEFAULT_ACTIONS and "promote" not in DEFAULT_ACTIONS
    assert normalize_steering({"enabled": True})["actions"] == list(DEFAULT_ACTIONS)
    node(fab.feed, "attempt:a1", state="done", attrs={"head_sha": H0}, room_id=fab.room)
    fab.s.steering = normalize_steering({"enabled": True})
    t = tgt(fab, fab.bob, "attempt:a1")
    assert "fork" not in t["actions"] and "fork" not in t
    assert err(fab.s.propose_steering, fab.bob, fab.room,
               {"action": "fork", "node_id": "attempt:a1", "expected_fence": t["fence"], "idempotency_key": "x",
                "base_sha": H0})[0] == 400
    fab.s.steering = normalize_steering({"enabled": True, "actions": ["fork"]})        # named but forks switched off
    t = tgt(fab, fab.bob, "attempt:a1")
    st, c, _ = err(fab.s.propose_steering, fab.bob, fab.room,
                   {"action": "fork", "node_id": "attempt:a1", "expected_fence": t["fence"], "idempotency_key": "x",
                    "base_sha": H0})
    assert (st, c) == (403, "forks_disabled")
    assert err(compare, fab, fab.alice, "attempt:a1")[:2] == (403, "forks_disabled")


def test_forks_config_validation():
    for bad in ({"enabled": "yes"}, {"nope": 1}, {"evidence_writers": "key:x"}, {"evidence_writers": [1]},
                {"max_forks_per_origin": 0}, {"max_forks_per_origin": 99}, {"min_reviews": -1}, "x"):
        with pytest.raises(ValueError):
            normalize_forks(bad)
        with pytest.raises(ValueError):
            normalize_steering({"forks": bad})
    d = normalize_forks(None)
    assert d["enabled"] is False and d["require_validation"] is True and d["evidence_writers"] == []


def test_hot_config_failure_closes_forks(fk):
    fk.s.steering_source = lambda: (_ for _ in ()).throw(OSError("unreadable"))
    assert not cfg(fk)["enabled"]


# ----------------------------------------------------------------------------- fork requests
def test_fork_from_a_finished_node_two_step_then_one_typed_delivery(fk):
    t = tgt(fk, fk.bob, "attempt:a1")
    assert "fork" in t["actions"] and "pause" not in t["actions"]          # terminal: fork yes, steering no
    assert t["fork"]["base_candidates"] == {H0: "origin.head_sha"} and t["fork"]["existing_forks"] == 0
    st = propose(fk, fk.bob, "fork", "attempt:a1", base_sha=H0, label="try‮ it​ with a queue")
    assert st["params"] == {"base_sha": H0, "label": "try it with a queue", "origin": "attempt:a1"}
    assert pending(fk) == []                                               # nothing before confirmation
    confirm(fk, fk.alice, st)
    got = fk.s.steering_pending("key:writer")
    item = got["steering"][0]
    assert item["action"] == "fork" and item["params"]["base_sha"] == H0 and item["trust"] == "untrusted"
    assert "instruction" in item and item["instruction"] is None
    assert "branch" not in item["params"] and "path" not in item["params"]   # the receiver derives both itself
    assert fk.s.steering_ack("key:writer", st["steering_id"], {"outcome": "applied"})["status"] == "acked"
    assert pending(fk) == []


def test_fork_base_must_be_a_full_commit_the_target_shows(fk):
    t = tgt(fk, fk.bob, "attempt:a1")

    def go(**kw):
        return err(fk.s.propose_steering, fk.bob, fk.room,
                   {"action": "fork", "node_id": "attempt:a1", "expected_fence": t["fence"],
                    "idempotency_key": k(), **kw})
    assert go()[0] == 400                                                  # missing base
    for bad in ("HEAD", "main", "abc123", H0.upper(), "../../etc", H0 + "x", "--upload-pack=x", H0[:-1]):
        assert go(base_sha=bad)[0] == 400
    st, c, _ = go(base_sha=H2)                                             # well formed but never shown
    assert (st, c) == (409, "unknown_base")
    assert go(base_sha=H0, expected_head=H0)[0] == 400                     # promote-only field
    assert go(base_sha=H0, branch="x")[0] == 400                           # no branch/path field exists
    assert go(base_sha=H0, path="/tmp/x")[0] == 400
    # other actions refuse the fork-only fields
    assert err(fk.s.propose_steering, fk.bob, fk.room,
               {"action": "pause", "node_id": "attempt:a1", "expected_fence": t["fence"], "idempotency_key": k(),
                "base_sha": H0})[0] == 400


def test_a_checkpoint_commit_produced_by_the_origin_is_a_valid_base(fk):
    node(fk.feed, "artifact:cp1", state="done", attrs={"kind": "checkpoint", "commit": H2}, room_id=fk.room)
    edge(fk.feed, "produced", "attempt:a1", "artifact:cp1", {"commit": H2}, room_id=fk.room)
    assert H2 in tgt(fk, fk.bob, "attempt:a1")["fork"]["base_candidates"]
    assert propose(fk, fk.bob, "fork", "attempt:a1", base_sha=H2)["params"]["base_sha"] == H2


def test_who_may_fork_and_what_they_cannot_see(fk):
    t = tgt(fk, fk.bob, "attempt:a1")
    body = {"action": "fork", "node_id": "attempt:a1", "expected_fence": t["fence"], "idempotency_key": k(),
            "base_sha": H0}
    assert code(fk.s.propose_steering, fk.carol, fk.room, body) == 403      # viewer
    assert code(fk.s.propose_steering, fk.dave, fk.room, dict(body, idempotency_key=k())) in (403, 404)
    assert code(fk.s.propose_steering, fk.mallory, fk.room, dict(body, idempotency_key=k())) == 404
    agent, _ = fk.agent("alice")
    assert code(fk.s.propose_steering, agent, fk.room, dict(body, idempotency_key=k())) in (403, 404)
    # a node only another room can see is the same 404 as a missing one
    node(fk.feed, "attempt:other", state="done", attrs={"head_sha": H0}, room_id="room-elsewhere")
    assert err(fk.s.steering_target, fk.bob, fk.room, "attempt:other")[0] == 404
    assert err(fk.s.steering_target, fk.bob, fk.room, "attempt:nope")[0] == 404
    assert err(compare, fk, fk.bob, "attempt:other")[0] == 404


def test_self_confirmation_and_replay_are_refused_like_any_steering(fk):
    st = propose(fk, fk.alice, "fork", "attempt:a1", base_sha=H0)
    t = tgt(fk, fk.alice, "attempt:a1")
    assert code(fk.s.confirm_steering, fk.alice, fk.room, st["steering_id"],
                {"digest": st["digest"], "expected_fence": t["fence"]}) == 403
    st2 = propose(fk, fk.bob, "fork", "attempt:a1", base_sha=H0)
    assert code(fk.s.confirm_steering, fk.alice, fk.room, st2["steering_id"],
                {"digest": "0" * 64, "expected_fence": t["fence"]}) == 400
    confirm(fk, fk.alice, st2)
    fk.s.steering_ack("key:writer", st2["steering_id"], {"outcome": "applied"})
    assert pending(fk) == []


def test_fork_flood_is_capped_per_origin_and_per_room(fk):
    # 3 per origin, counting forks that exist and requests still alive
    mkfork(fk, 1)
    s1 = propose(fk, fk.bob, "fork", "attempt:a1", base_sha=H0)
    propose(fk, fk.bob, "fork", "attempt:a1", base_sha=H0)
    t = tgt(fk, fk.bob, "attempt:a1")
    assert t["fork"]["existing_forks"] == 1
    st, c, _ = err(fk.s.propose_steering, fk.bob, fk.room,
                   {"action": "fork", "node_id": "attempt:a1", "expected_fence": t["fence"],
                    "idempotency_key": k(), "base_sha": H0})
    assert (st, c) == (429, "fork_limit")
    # withdrawing one frees a slot
    fk.s.reject_steering(fk.bob, fk.room, s1["steering_id"], {})
    assert propose(fk, fk.bob, "fork", "attempt:a1", base_sha=H0)["status"] == "pending"


def test_a_fork_is_refused_at_confirmation_when_the_limit_filled_meanwhile(fk):
    fk.s.steering = normalize_steering({**ALL, "forks": {**ALL["forks"], "max_forks_per_origin": 2}})
    s1 = propose(fk, fk.bob, "fork", "attempt:a1", base_sha=H0)
    mkfork(fk, 1)
    mkfork(fk, 2)                                                           # emitters made two while it was pending
    t = tgt(fk, fk.alice, "attempt:a1")
    st, c, _ = err(fk.s.confirm_steering, fk.alice, fk.room, s1["steering_id"],
                   {"digest": s1["digest"], "expected_fence": t["fence"]})
    assert st == 409
    assert fk.s._steers[s1["steering_id"]]["status"] in ("fenced", "rejected")


def test_a_fork_is_not_delivered_if_the_base_vanishes_before_delivery(fk):
    st = propose(fk, fk.bob, "fork", "attempt:a1", base_sha=H0)
    confirm(fk, fk.alice, st)
    node(fk.feed, "attempt:a1", state="failed", attrs={"head_sha": H2}, room_id=fk.room)      # head moved: fence moves
    assert pending(fk) == []
    assert fk.s._steers[st["steering_id"]]["status"] == "fenced"


# ----------------------------------------------------------------------------- comparison
def test_comparison_shows_every_sibling_with_provenance_and_never_trusts_the_forks_own_numbers(fk):
    a, b = mkfork(fk, 1), mkfork(fk, 2, head=H2, touched_paths=["src/a.py", "web/b.js"])
    validate(fk, a, H1)
    out = compare(fk, fk.carol, a)                                          # a viewer may read; from an approach too
    assert out["origin"]["id"] == "attempt:a1" and {c["approach_id"] for c in out["forks"]} == {a, b}
    ca = next(c for c in out["forks"] if c["approach_id"] == a)
    assert ca["diff"] == {"files_changed": 2, "insertions": 10, "deletions": 1, "source": "approach_owner",
                          "writer": "key:writer", "verified": False}
    assert ca["impact"]["modules"] == ["docs", "src"] and ca["provenance"]["verified"] is False
    assert ca["evidence"]["counted_validations"] == 1 and ca["readiness"]["ready"] is True
    cb = next(c for c in out["forks"] if c["approach_id"] == b)
    assert cb["evidence"]["counted_validations"] == 0 and cb["readiness"]["ready"] is False
    assert out["overlap"] == [{"a": a, "b": b, "paths": ["src/a.py"], "source": "approach_owner",
                               "writer": "key:writer", "verified": False}]
    assert out["quorum"]["status"] == "unavailable"
    assert out["policy"]["trusted_evidence_configured"] is True and out["can_confirm"] is False
    assert ca["candidate"]["revision"] == H1 and ca["candidate"]["ci_state"] == "passed"
    assert compare(fk, fk.alice, "attempt:a1")["can_confirm"] is True


def test_the_forks_own_agent_cannot_forge_evidence(fk):
    a = mkfork(fk, 1)
    # the fork's emitter (WRITER) claims it is validated and reviewed, and writes a self-serving attribute
    validate(fk, a, H1, who=WRITER)
    review(fk, a, "lgtm", H1, who=WRITER)
    node(fk.feed, a, attrs={"validated": True, "ci_state": "passed", "review_state": "approved"}, room_id=fk.room)
    card = compare(fk, fk.alice, a)["forks"][0]
    assert card["evidence"]["counted_validations"] == 0 and card["evidence"]["counted_reviews"] == 0
    assert all(not v["counted"] and "writer_not_trusted" in v["not_counted_because"]
               for v in card["evidence"]["validated"])
    assert card["readiness"]["ready"] is False
    codes = {b["code"] for b in card["readiness"]["blockers"]}
    assert "no_validation" in codes
    # evidence bound to another head, or stale, does not count even from a trusted writer
    validate(fk, a, H0, n=2)
    ev = compare(fk, fk.alice, a)["forks"][0]["evidence"]["validated"]
    assert any("bound_to_another_head" in v["not_counted_because"] for v in ev)
    assert compare(fk, fk.alice, a)["forks"][0]["evidence"]["counted_validations"] == 0


def test_no_trusted_writers_configured_means_nothing_can_ever_validate(fk):
    fk.s.steering = normalize_steering({**ALL, "forks": {"enabled": True}})
    a = mkfork(fk, 1)
    validate(fk, a, H1)
    r = compare(fk, fk.alice, a)["forks"][0]["readiness"]
    assert not r["ready"] and "no_trusted_evidence" in {b["code"] for b in r["blockers"]}
    assert compare(fk, fk.alice, a)["policy"]["trusted_evidence_configured"] is False


def test_comparison_hides_what_the_reader_cannot_see_and_is_bounded(fk):
    a = mkfork(fk, 1)
    node(fk.feed, "approach:fp", state="running", scope="private", attrs={"head_sha": H1}, room_id=fk.room)
    edge(fk.feed, "fork", "attempt:a1", "approach:fp", scope="private", room_id=fk.room)
    out = compare(fk, fk.bob, "attempt:a1")
    assert [c["approach_id"] for c in out["forks"]] == [a]
    assert "approach:fp" not in repr(out)
    fk.s.steering = normalize_steering({**ALL, "forks": {**ALL["forks"], "max_compared": 2}})
    for i in range(2, 6):
        mkfork(fk, i)
    big = compare(fk, fk.bob, "attempt:a1")
    assert len(big["forks"]) == 2 and big["truncated"] is True


def test_hostile_text_in_a_fork_is_neutralised(fk):
    a = mkfork(fk, 1, branch="x‮\x00evil", touched_paths=["ok.py", "bad path", "a" * 500],
               insertions=-5, deletions="many")
    node(fk.feed, a, label="L‮abel", room_id=fk.room)
    c = compare(fk, fk.alice, a)["forks"][0]
    assert "‮" not in repr(c) and "\x00" not in repr(c) and " " not in repr(c)
    assert c["diff"]["insertions"] is None and c["diff"]["deletions"] is None
    assert all(len(p) <= 200 for p in c["impact"]["touched_paths"])


def test_downstream_impact_reports_evidence_left_on_another_head(fk):
    a = mkfork(fk, 1)
    node(fk.feed, "pr:github.com/o/r#7", state="open", room_id=fk.room)
    edge(fk.feed, "issue_link", "attempt:a1", "pr:github.com/o/r#7", room_id=fk.room)
    node(fk.feed, "review:r9", state="done", room_id=fk.room, principal=OPERATOR)
    edge(fk.feed, "reviewed", "review:r9", "pr:github.com/o/r#7", {"commit": H0, "verdict": "lgtm"},
         room_id=fk.room, principal=OPERATOR)
    d = compare(fk, fk.alice, a)["downstream"]
    assert [(n["id"], n["node_type"]) for n in d["nodes"]] == [("pr:github.com/o/r#7", "pr")]
    assert d["per_fork"][0]["evidence_left_on_other_head"][0]["commit"] == H0


# ----------------------------------------------------------------------------- promotion
def test_promotion_is_refused_until_trusted_head_bound_validation_exists(fk):
    a = mkfork(fk, 1)
    t = tgt(fk, fk.bob, a)
    assert "promote" in t["actions"]
    st, c, exc = err(fk.s.propose_steering, fk.bob, fk.room,
                     {"action": "promote", "node_id": a, "expected_fence": t["fence"], "idempotency_key": k(),
                      "expected_head": H1})
    assert (st, c) == (409, "not_ready")
    assert {b["code"] for b in exc.extra["blockers"]} == {"no_validation"}
    validate(fk, a, H1)
    st = propose(fk, fk.bob, "promote", a, expected_head=H1)
    assert st["params"] == {"expected_head": H1, "origin": "attempt:a1"}


def test_promote_needs_the_exact_head_the_person_saw(fk):
    a = mkfork(fk, 1)
    validate(fk, a, H1)
    t = tgt(fk, fk.bob, a)

    def go(**kw):
        return err(fk.s.propose_steering, fk.bob, fk.room,
                   {"action": "promote", "node_id": a, "expected_fence": t["fence"], "idempotency_key": k(), **kw})
    assert go()[0] == 400 and go(expected_head="main")[0] == 400 and go(expected_head=H1[:7])[0] == 400
    st, c, exc = go(expected_head=H2)
    assert (st, c) == (409, "not_ready") and "stale_head" in {b["code"] for b in exc.extra["blockers"]}
    assert go(expected_head=H1, base_sha=H0)[0] == 400


def test_only_approaches_can_be_promoted_and_a_losing_fork_cannot(fk):
    t = tgt(fk, fk.bob, "attempt:a1")
    assert "promote" not in t["actions"]
    a, b = mkfork(fk, 1), mkfork(fk, 2)
    validate(fk, a)
    validate(fk, b, H1)
    edge(fk.feed, "supersession", a, b, room_id=fk.room)                    # winner -> loser
    assert tgt(fk, fk.bob, b)["superseded"] and "promote" not in tgt(fk, fk.bob, b)["actions"]
    st, c, _ = err(fk.s.propose_steering, fk.bob, fk.room,
                   {"action": "promote", "node_id": b, "expected_fence": tgt(fk, fk.bob, b)["fence"],
                    "idempotency_key": k(), "expected_head": H1})
    assert (st, c) == (409, "stale_target")


def test_stale_head_after_confirmation_never_reaches_the_pusher(fk):
    a = mkfork(fk, 1)
    validate(fk, a, H1)
    st = propose(fk, fk.bob, "promote", a, expected_head=H1)
    confirm(fk, fk.alice, st)
    node(fk.feed, a, attrs={"head_sha": H2}, room_id=fk.room)             # the fork moved on after the human confirmed
    assert pending(fk) == []
    assert fk.s._steers[st["steering_id"]]["status"] == "fenced"


def test_revoked_evidence_between_confirmation_and_delivery_blocks_delivery(fk):
    a = mkfork(fk, 1)
    validate(fk, a, H1)
    st = propose(fk, fk.bob, "promote", a, expected_head=H1)
    confirm(fk, fk.alice, st)
    # a trusted reviewer now blocks this head; the target node itself did not change so its fence is unchanged
    review(fk, a, "blocked", H1)
    assert pending(fk) == []
    assert fk.s._steers[st["steering_id"]]["status"] == "fenced"


def test_trusted_evidence_removed_after_proposal_blocks_confirmation(fk):
    a = mkfork(fk, 1)
    validate(fk, a, H1)
    st = propose(fk, fk.bob, "promote", a, expected_head=H1)
    edge(fk.feed, "validated", "ci:f1-1", a, {"commit": H1}, room_id=fk.room, nodes=False, principal=OPERATOR) \
        if False else fk.feed.send("edge.remove", {"edge_type": "validated", "from_id": "ci:f1-1", "to_id": a,
                                                   "reason": "retracted"}, "room", room_id=fk.room, principal=OPERATOR)
    t = tgt(fk, fk.alice, a)
    s, c, exc = err(fk.s.confirm_steering, fk.alice, fk.room, st["steering_id"],
                    {"digest": st["digest"], "expected_fence": t["fence"]})
    assert (s, c) == (409, "not_ready") and fk.s._steers[st["steering_id"]]["status"] == "fenced"


def test_min_reviews_policy(fk):
    fk.s.steering = normalize_steering({**ALL, "forks": {**ALL["forks"], "min_reviews": 1}})
    a = mkfork(fk, 1)
    validate(fk, a, H1)
    assert err(fk.s.propose_steering, fk.bob, fk.room,
               {"action": "promote", "node_id": a, "expected_fence": tgt(fk, fk.bob, a)["fence"],
                "idempotency_key": k(), "expected_head": H1})[1] == "not_ready"
    review(fk, a, "lgtm", H0, n=1)                                         # right reviewer, wrong head
    assert err(fk.s.propose_steering, fk.bob, fk.room,
               {"action": "promote", "node_id": a, "expected_fence": tgt(fk, fk.bob, a)["fence"],
                "idempotency_key": k(), "expected_head": H1})[1] == "not_ready"
    review(fk, a, "lgtm", H1, n=2)
    assert propose(fk, fk.bob, "promote", a, expected_head=H1)["status"] == "pending"


def test_promotion_race_only_one_sibling_can_be_confirmed_and_delivered(fk):
    a, b = mkfork(fk, 1), mkfork(fk, 2)
    validate(fk, a, H1)
    validate(fk, b, H1)
    sa = propose(fk, fk.bob, "promote", a, expected_head=H1)
    sb = propose(fk, fk.bob, "promote", b, expected_head=H1)               # both can be proposed
    confirm(fk, fk.alice, sa)
    t = tgt(fk, fk.alice, b)
    st, c, _ = err(fk.s.confirm_steering, fk.alice, fk.room, sb["steering_id"],
                   {"digest": sb["digest"], "expected_fence": t["fence"]})
    assert (st, c) == (409, "promotion_in_flight")
    assert fk.s._steers[sb["steering_id"]]["status"] == "fenced"
    # a third proposal while the first is confirmed is refused up front
    sc = err(fk.s.propose_steering, fk.bob, fk.room,
             {"action": "promote", "node_id": b, "expected_fence": tgt(fk, fk.bob, b)["fence"],
              "idempotency_key": k(), "expected_head": H1})
    assert sc[1] == "promotion_in_flight"
    item = pending(fk)
    assert [i["steering_id"] for i in item] == [sa["steering_id"]]
    assert item[0]["params"]["siblings"] == [b] and item[0]["params"]["origin"] == "attempt:a1"
    assert item[0]["params"]["head"] == H1 and item[0]["action"] == "promote"
    # once the winner reports it is selected the other can never be promoted
    fk.s.steering_ack("key:writer", sa["steering_id"], {"outcome": "applied"})
    node(fk.feed, a, state="selected", room_id=fk.room)
    edge(fk.feed, "supersession", a, b, room_id=fk.room)
    assert "promote" not in tgt(fk, fk.bob, b)["actions"]
    assert promotion_check(fk.s.graph_source(), fk.s._room_reader(fk.s.graph_access(fk.bob), fk.room, "bob"), b,
                           cfg(fk))["ready"] is False


def test_an_applied_promotion_closes_the_group_even_if_the_state_is_not_reported_yet(fk):
    a, b = mkfork(fk, 1), mkfork(fk, 2)
    validate(fk, a, H1)
    validate(fk, b, H1)
    sa = propose(fk, fk.bob, "promote", a, expected_head=H1)
    sb = propose(fk, fk.bob, "promote", b, expected_head=H1)
    confirm(fk, fk.alice, sa)
    fk.s.steering_ack("key:writer", sa["steering_id"], {"outcome": "applied"})
    t = tgt(fk, fk.alice, b)
    assert err(fk.s.confirm_steering, fk.alice, fk.room, sb["steering_id"],
               {"digest": sb["digest"], "expected_fence": t["fence"]})[1] == "promotion_in_flight"


def test_a_refused_or_stale_promotion_does_not_block_the_next_one(fk):
    a, b = mkfork(fk, 1), mkfork(fk, 2)
    validate(fk, a, H1)
    validate(fk, b, H1)
    sa = propose(fk, fk.bob, "promote", a, expected_head=H1)
    confirm(fk, fk.alice, sa)
    fk.s.steering_ack("key:writer", sa["steering_id"], {"outcome": "refused", "note": "dirty worktree"})
    sb = propose(fk, fk.bob, "promote", b, expected_head=H1)
    assert confirm(fk, fk.alice, sb)["status"] == "confirmed"


def test_a_promotion_in_another_origin_is_independent(fk):
    node(fk.feed, "attempt:a2", state="done", attrs={"head_sha": H0}, room_id=fk.room)
    a, b = mkfork(fk, 1), mkfork(fk, 2, origin="attempt:a2")
    validate(fk, a, H1)
    validate(fk, b, H1)
    confirm(fk, fk.alice, propose(fk, fk.bob, "promote", a, expected_head=H1))
    assert confirm(fk, fk.alice, propose(fk, fk.bob, "promote", b, expected_head=H1))["status"] == "confirmed"


def test_nothing_in_a_fork_or_promote_delivery_is_a_command(fk):
    a = mkfork(fk, 1)
    validate(fk, a, H1)
    confirm(fk, fk.alice, propose(fk, fk.bob, "promote", a, expected_head=H1, reason="looks good"))
    item = pending(fk)[0]
    assert set(item["params"]) == {"expected_head", "origin", "head", "siblings"}
    for word in ("argv", "shell", "command", "merge", "force"):
        assert word not in item and word not in item["params"]
    assert "--" not in repr(item["params"])
    assert fk.s.steering_pending("key:writer")["notice"].count("fork, promote")


def test_http_routes(fk):
    a = mkfork(fk, 1)
    validate(fk, a, H1)

    def call(who, method, parts, body=None, query=None):
        return fk.s.dispatch(method, parts, query or {}, body or {}, f"Bearer {USERS[who]}", None)
    st, out = call("carol", "GET", ["rooms", fk.room, "forks"], query={"node_id": "attempt:a1"})
    assert st == 200 and out["forks"][0]["approach_id"] == a and out["can_propose"] is False
    assert call("carol", "GET", ["rooms", fk.room, "forks"])[0] == 400
    assert call("mallory", "GET", ["rooms", fk.room, "forks"], query={"node_id": "attempt:a1"})[0] in (403, 404)
    st, t = call("bob", "GET", ["rooms", fk.room, "steering", "target"], query={"node_id": "attempt:a1"})
    st, out = call("bob", "POST", ["rooms", fk.room, "steering"],
                   {"action": "fork", "node_id": "attempt:a1", "expected_fence": t["fence"], "base_sha": H0,
                    "label": "alt", "idempotency_key": k()})
    assert st == 201 and out["steering"]["params"]["base_sha"] == H0
    st, bad = call("bob", "POST", ["rooms", fk.room, "steering"],
                   {"action": "fork", "node_id": "attempt:a1", "expected_fence": t["fence"], "base_sha": H0,
                    "branch": "x", "idempotency_key": k()})
    assert st == 400


def test_audit_records_fork_and_promote_without_free_text(fk):
    st = propose(fk, fk.bob, "fork", "attempt:a1", base_sha=H0, label="SECRET-LABEL")
    confirm(fk, fk.alice, st)
    audit = repr(fk.s.audit(fk.alice, fk.s._rooms[fk.room]["project_id"], 0, 200))
    assert "steer.propose" in audit and "SECRET-LABEL" not in audit
