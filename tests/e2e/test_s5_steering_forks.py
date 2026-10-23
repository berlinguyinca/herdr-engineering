"""Acceptance scenario 5 (server side) - a human comments on a failed review, forks an alternative from a historical
node, compares implementations and promotes the winner. These are the contracts the control plane owns and that need no
Pi: comments on a graph node, the steering authorization matrix, the forks comparison and the delivery pinning to the
reporting key and instance. The Pi half (a real receiver applying the fork, a real push) is in the Pi checkout's
test/e2e/s5-fork-compare-promote.e2e.test.ts.

Everything is real HTTP against a real `herdr-eng control-plane web` process configured only by generated YAML.
Cast: alice owns the room; bob is a contributor; carol a viewer; dave a guest; mallory is signed in but in no room.
Keys: `pi` reports the nodes (owner of the target), `ci` is the operator-trusted evidence writer, `rev` is a reviewer (both operator keys: only an operator key may attach
an edge to a node another key owns),
`other` is a second emitter that owns nothing.
"""
from __future__ import annotations

import json
import secrets
from urllib.parse import quote

import pytest
import yaml

from tests.e2e.harness import Plane

H0, H1, H2, BASE = "a" * 40, "b" * 40, "d" * 40, "c" * 40
EVERY = {"rooms": ["*"], "missions": ["*"]}


class World:
    def __init__(self, plane: Plane):
        self.p = plane
        self.seq: dict[str, int] = {}
        self.project, self.room = plane.make_room("alice", "s5", "forks")
        for u, role in (("bob", "contributor"), ("carol", "viewer"), ("dave", "guest")):
            plane.add_member("alice", self.room, u, role)
        self.rewrite_steering({"enabled": True, "self_confirm": False,
                               "actions": ["pause", "redirect", "fork", "promote"],
                               "forks": {"enabled": True, "evidence_writers": [plane.pid("ci")],
                                         "max_forks_per_origin": 4, "max_open_forks_per_room": 6}})

    # ---- hot config: the fleet file is re-read per request (no restart)
    def rewrite_steering(self, steering: dict) -> None:
        path = self.p.workdir / f"{self.p.name}-fleet.yaml"
        doc = yaml.safe_load(path.read_text())
        doc["sections"]["control_plane"]["values"]["collab"]["steering"] = steering
        path.write_text(yaml.safe_dump(doc))
        path.chmod(0o600)

    # ---- graph writes by key (room scoped, the way a Pi emitter writes them)
    def _send(self, key: str, event_type: str, entity: str, payload: dict, instance: str | None = None) -> None:
        self.seq[key] = self.seq.get(key, 0) + 1
        env = self.p.envelope(source=f"s5-{key}", seq=self.seq[key], entity_id=entity, event_type=event_type,
                              payload=payload, scope="room", room_id=self.room)
        r = self.p.ingest(key, env)
        assert r.status == 200, (event_type, entity, r.status, r.body)

    def node(self, key: str, nid: str, state: str = "done", label: str | None = None, **attrs) -> None:
        body = {"node_type": nid.split(":")[0], "node_id": nid, "state": state, "attrs": attrs}
        if label:
            body["label"] = label
        self._send(key, "node.upsert", nid, body)

    def edge(self, key: str, etype: str, a: str, b: str, **attrs) -> None:
        body = {"edge_type": etype, "from_id": a, "to_id": b}
        if attrs:
            body["attrs"] = attrs
        self._send(key, "edge.add", a, body)

    def target(self, user: str, nid: str):
        return self.p.c("GET", f"/rooms/{self.room}/steering/target?node_id={quote(nid, safe='')}", user=user)

    def propose(self, user: str, action: str, nid: str, fence: str | None = None, **kw):
        fence = fence or self.target(user, nid).get("fence") or self.target("alice", nid)["fence"]
        return self.p.c("POST", f"/rooms/{self.room}/steering",
                        {"action": action, "node_id": nid, "expected_fence": fence,
                         "idempotency_key": secrets.token_hex(6), **kw}, user=user)

    def confirm(self, user: str, st: dict, nid: str):
        fence = self.target("alice", nid)["fence"]
        return self.p.c("POST", f"/rooms/{self.room}/steering/{st['steering_id']}/confirm",
                        {"digest": st["digest"], "expected_fence": fence}, user=user)

    def pending(self, key: str = "pi", instance: str | None = None):
        q = f"?instance_id={instance}" if instance else ""
        return self.p.get("/api/v1/steering/pending" + q, key=key)

    def comments(self, user: str, nid: str):
        return self.p.c("GET", f"/rooms/{self.room}/comments?anchor_kind=node&node_id={quote(nid, safe='')}", user=user)

    def comment(self, user: str, nid: str, text: str, **kw):
        return self.p.c("POST", f"/rooms/{self.room}/comments",
                        {"anchor": {"kind": "node", "node_id": nid}, "text": text,
                         "idempotency_key": secrets.token_hex(6), **kw}, user=user)


@pytest.fixture
def w(planes):
    plane = planes("s5", keys={"pi": "ingest", "ci": "operator", "rev": "operator", "other": "ingest"},
                   users=["alice", "bob", "carol", "dave", "mallory"],
                   scopes={k: EVERY for k in ("pi", "other")})
    plane.verify_features("alice")
    return World(plane)


@pytest.fixture
def failed(w):
    """A mission attempt at a historical commit H0 whose review at H0 blocked, then advanced to a newer head."""
    w.node("pi", "attempt:a1", state="done", label="attempt 1", head_sha=H0, instance_id="inst-a")
    w.node("rev", "review:r1", state="done", label="review of a1")
    w.edge("rev", "reviewed", "review:r1", "attempt:a1", commit=H0, verdict="changes_requested")
    return w


# ----------------------------------------------------------------------------- comments on a graph node
def test_contributor_comments_on_the_failed_review_with_history_and_moderation(failed):
    w = failed
    r = w.comment("bob", "review:r1", "the retry loop can never terminate")
    assert r.status == 201, r.body
    cid = r.body.get("comment", r.body)["comment_id"]
    assert w.comment("alice", "review:r1", "agreed - try a queue").status == 201
    listed = w.comments("carol", "review:r1")                       # a viewer READS (positive control for the 403s)
    assert listed.status == 200 and len(listed.body["comments"]) == 2
    # a comment is not a request for change: nothing was proposed and the review is untouched
    assert w.p.c("GET", f"/rooms/{w.room}/steering", user="alice").body.get("steering") in ([], None)
    assert ("reviewed", "review:r1", "attempt:a1") in w.p.edges("attempt:a1")
    # edit with revision, history keeps the first text
    e = w.p.c("POST", f"/rooms/{w.room}/comments/{cid}/edit",
              {"text": "the retry loop cannot terminate under load", "expected_revision": 1}, user="bob")
    assert e.status == 200, e.body
    h = w.p.c("GET", f"/rooms/{w.room}/comments/{cid}/history", user="carol")
    assert h.status == 200 and len(h.body["comment"]["history"]) >= 2
    assert "never terminate" in json.dumps(h.body)
    stale = w.p.c("POST", f"/rooms/{w.room}/comments/{cid}/edit", {"text": "x", "expected_revision": 1}, user="bob")
    assert stale.status in (409, 400)                               # stale revision refused
    # nobody else edits it; the owner may moderate-retract
    assert w.p.c("POST", f"/rooms/{w.room}/comments/{cid}/edit", {"text": "x", "expected_revision": 2}, user="carol").status in (403, 404)
    assert w.p.c("POST", f"/rooms/{w.room}/comments/{cid}/retract", {"expected_revision": 2}, user="alice").status == 200
    after = {c["comment_id"]: c for c in w.comments("carol", "review:r1").body["comments"]}
    assert after[cid]["retracted"] is True and after[cid]["text"] is None


# ----------------------------------------------------------------------------- steering authorization matrix
def test_who_may_propose_fork_and_who_may_confirm(failed):
    w = failed
    t = w.target("bob", "attempt:a1")
    assert t.status == 200 and "fork" in t["actions"] and t["fork"]["base_candidates"].get(H0)
    assert t.get("can_propose") is True
    for who in ("carol", "dave", "mallory"):
        r = w.propose(who, "fork", "attempt:a1", fence=t["fence"], base_sha=H0, label="alt")
        assert r.status in (403, 404), (who, r.status, r.body)
    r = w.propose("bob", "fork", "attempt:a1", fence=t["fence"], base_sha=H0, label="queue based retry")
    assert r.status in (200, 201), r.body
    st = r["steering"]
    assert w.pending(instance="inst-a").body["steering"] == []               # nothing before confirmation
    assert w.confirm("bob", st, "attempt:a1").status == 403                  # proposer is not the confirmer
    assert w.confirm("carol", st, "attempt:a1").status in (403, 404)
    assert w.confirm("alice", st, "attempt:a1").status == 200                # the owner confirms
    got = w.pending(instance="inst-a").body["steering"]
    assert [g["steering_id"] for g in got] == [st["steering_id"]] and got[0]["action"] == "fork"
    assert got[0]["params"]["base_sha"] == H0 and got[0]["params"]["label"] == "queue based retry"
    assert got[0]["trust"] == "untrusted"


def test_a_base_sha_that_is_not_a_candidate_and_a_stale_fence_are_refused(failed):
    w = failed
    t = w.target("bob", "attempt:a1")
    bad = w.propose("bob", "fork", "attempt:a1", fence=t["fence"], base_sha=H2, label="dangling")
    assert bad.status in (400, 409), bad.body
    ok = w.propose("bob", "fork", "attempt:a1", fence=t["fence"], base_sha=H0, label="fine")
    assert ok.status in (200, 201), ok.body                                  # positive control for the refusal above
    w.node("pi", "attempt:a1", state="done", label="attempt 1", head_sha=H1, instance_id="inst-a")   # target moved
    st = ok["steering"]
    c = w.confirm("alice", st, "attempt:a1")
    assert c.status == 409, c.body                                            # confirm sees the fence moved on
    old = w.propose("bob", "fork", "attempt:a1", fence=t["fence"], base_sha=H0, label="late")
    assert old.status == 409, old.body


def test_reject_closes_a_proposal_and_it_is_never_delivered(failed):
    w = failed
    st = w.propose("bob", "fork", "attempt:a1", base_sha=H0, label="no thanks")["steering"]
    rej = w.p.c("POST", f"/rooms/{w.room}/steering/{st['steering_id']}/reject", {"reason": "not now"}, user="alice")
    assert rej.status == 200, rej.body
    assert w.pending().body["steering"] == []
    assert w.confirm("alice", st, "attempt:a1").status in (400, 409)


# ----------------------------------------------------------------------------- delivery pinning
def test_delivery_is_pinned_to_the_reporting_key_and_the_nodes_instance(failed):
    w = failed
    st = w.propose("bob", "fork", "attempt:a1", base_sha=H0, label="queue")["steering"]
    assert w.confirm("alice", st, "attempt:a1").status == 200
    assert w.pending(key="other").body["steering"] == []                     # another key never receives it
    assert w.pending(key="rev").body["steering"] == []
    assert w.pending(key="pi", instance="inst-b").body["steering"] == []     # node names inst-a: wrong instance
    assert w.pending(key="viewer").body["steering"] == []                    # a read-only key owns no target
    got = w.pending(key="pi", instance="inst-a").body["steering"]            # positive control: the right pair gets it
    assert [g["steering_id"] for g in got] == [st["steering_id"]]
    # ack: only the pinned key and instance
    sid = st["steering_id"]
    assert w.p.post(f"/api/v1/steering/{sid}/ack", {"outcome": "applied"}, key="other").status == 404
    assert w.p.post(f"/api/v1/steering/{sid}/ack?instance_id=inst-b", {"outcome": "applied"}, key="pi").status == 404
    ok = w.p.post(f"/api/v1/steering/{sid}/ack?instance_id=inst-a", {"outcome": "applied", "note": "fk-1"}, key="pi")
    assert ok.status == 200, ok.body
    assert w.pending(key="pi", instance="inst-a").body["steering"] == []     # acked: not redelivered
    again = w.p.post(f"/api/v1/steering/{sid}/ack?instance_id=inst-a", {"outcome": "refused"}, key="pi")
    assert again.status in (200, 409)                                        # a different outcome cannot rewrite it
    shown = w.p.c("GET", f"/rooms/{w.room}/steering", user="carol").body["steering"]
    assert [s["ack"]["outcome"] for s in shown if s["steering_id"] == sid] == ["applied"]


# ----------------------------------------------------------------------------- comparison (read side)
def approach(w, n: int, head: str, paths: list[str], origin: str = "attempt:a1") -> str:
    aid = f"approach:fk-{n}"
    w.node("pi", aid, state="running", label=f"fork {n}", head_sha=head, base_sha=H0, branch=f"pi-fork/fk-{n}",
           repo="github.com/acme/widgets", files_changed=len(paths), insertions=10 * n, deletions=1, touched_paths=paths)
    w.edge("pi", "fork", origin, aid)
    return aid


def test_comparison_counts_only_trusted_evidence_bound_to_the_current_head(failed):
    w = failed
    a1 = approach(w, 1, H1, ["src/a.py", "docs/x.md"])
    a2 = approach(w, 2, H2, ["src/a.py", "src/b.py"])
    w.node("ci", "ci:1", state="done")
    w.edge("ci", "validated", "ci:1", a1, commit=H1)                         # trusted writer, current head
    w.node("other", "ci:forged", state="done")                               # an ingest key cannot even attach evidence
    forged = w.p.ingest("other", w.p.envelope(source="s5-other", seq=99, entity_id="ci:forged", event_type="edge.add",
                                              scope="room", room_id=w.room,
                                              payload={"edge_type": "validated", "from_id": "ci:forged", "to_id": a2,
                                                       "attrs": {"commit": H2}}))
    assert forged.status == 403 and forged.body.get("reason") == "lineage_forbidden"
    w.node("ci", "ci:old", state="done")
    w.edge("ci", "validated", "ci:old", a2, commit=H1)                       # trusted but for another head
    r = w.p.c("GET", f"/rooms/{w.room}/forks?node_id={quote('attempt:a1', safe='')}", user="carol")
    assert r.status == 200, r.body
    cards = {c["approach_id"]: c for c in r.body["forks"]}
    assert set(cards) == {a1, a2}
    counted = {aid: c["evidence"]["counted_validations"] for aid, c in cards.items()}
    assert counted == {a1: 1, a2: 0}                                         # forged/foreign-head evidence never counts
    assert cards[a1]["readiness"]["ready"] is True and cards[a2]["readiness"]["ready"] is False
    assert "overlap" in r.body and "src/a.py" in json.dumps(r.body["overlap"])
    # outsiders see nothing, and the refusal is the same as for a node that does not exist
    deny = w.p.c("GET", f"/rooms/{w.room}/forks?node_id={quote('attempt:a1', safe='')}", user="mallory")
    none = w.p.c("GET", f"/rooms/{w.room}/forks?node_id={quote('attempt:nope', safe='')}", user="carol")
    assert deny.status in (403, 404) and none.status in (400, 404)


def test_downstream_is_a_read_only_report_that_invalidates_nothing(failed):
    """What the "downstream impact check" does: it LISTS downstream nodes and evidence left on another head. Herdr
    never edits or invalidates a gate, review or validation; that is for the reader to act on."""
    w = failed
    w.node("pi", "task:A", state="done")
    w.node("pi", "task:B", state="done")
    w.edge("pi", "dependency", "task:A", "attempt:a1")                       # A depends on the forked attempt
    w.node("pi", "pr:github.com/acme/widgets#7", state="open", head_sha=H0)
    w.edge("pi", "produced", "attempt:a1", "pr:github.com/acme/widgets#7", commit=H0)
    w.node("ci", "ci:gateA", state="done")
    w.edge("ci", "validated", "ci:gateA", "pr:github.com/acme/widgets#7", commit=H0)
    approach(w, 1, H1, ["src/a.py"])
    before = w.p.edges("pr:github.com/acme/widgets#7")
    r = w.p.c("GET", f"/rooms/{w.room}/forks?node_id={quote('attempt:a1', safe='')}", user="bob")
    assert r.status == 200
    down = json.dumps(r.body.get("downstream"))
    assert "task:A" in down and "task:B" not in down                         # only what is connected
    assert w.p.edges("pr:github.com/acme/widgets#7") == before               # nothing was touched
    gate = w.p.node("ci:gateA").body
    assert gate["node"]["state"] == "done"


@pytest.mark.xfail(strict=True, reason="GAP: Herdr reports downstream evidence left on another head but never marks "
                                       "a dependent gate/review stale or invalidated after a fork is promoted")
def test_promoting_a_fork_marks_only_the_dependent_gate_stale(failed):
    w = failed
    w.node("pi", "task:A", state="done")
    w.node("pi", "task:B", state="done")
    w.edge("pi", "dependency", "task:A", "attempt:a1")
    w.node("ci", "gate:A", state="done")
    w.node("ci", "gate:B", state="done")
    w.edge("ci", "validated", "gate:A", "task:A", commit=H0)
    w.edge("ci", "validated", "gate:B", "task:B", commit=H0)
    a1 = approach(w, 1, H1, ["src/a.py"])
    w.edge("pi", "supersession", a1, "attempt:a1")
    stale = [e for e in w.p.hood("gate:A").body["edges"] if e.get("stale")]
    other = [e for e in w.p.hood("gate:B").body["edges"] if e.get("stale")]
    assert stale and not other


# ----------------------------------------------------------------------------- kill switch, hot
def test_kill_switch_is_read_per_request_without_a_restart(failed):
    w = failed
    pid = w.p.proc.pid
    assert w.propose("bob", "fork", "attempt:a1", base_sha=H0, label="before").status in (200, 201)   # control
    cfg = {"enabled": True, "self_confirm": False, "kill_switch": True, "actions": ["fork", "promote"],
           "forks": {"enabled": True, "evidence_writers": [w.p.pid("ci")]}}
    w.rewrite_steering(cfg)
    t = w.target("bob", "attempt:a1")
    killed = w.propose("bob", "fork", "attempt:a1", fence=w.target("alice", "attempt:a1").get("fence") or "x",
                       base_sha=H0, label="during")
    assert killed.status == 403 and killed.body.get("code") == "steering_disabled", (t.status, killed.body)
    assert w.pending().body["steering"] == []
    cfg["kill_switch"] = False
    w.rewrite_steering(cfg)
    assert w.propose("bob", "fork", "attempt:a1", base_sha=H0, label="after").status in (200, 201)
    assert w.p.proc.pid == pid and w.p.alive()
