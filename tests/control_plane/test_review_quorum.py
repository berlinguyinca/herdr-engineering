"""Head-bound post-PR review quorum derived from the lineage graph (T06)."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
import hashlib

from herdr_engineering.control_plane.review_quorum import (QuorumConfig, parse_config, pr_review_quorum,
                                                           receipt_binding)

from .test_graph import Feed

PR = "pr:github.com/o/r#7"


def actor(name, kind="agent"):
    return {"kind": kind, "id": f"key:{name}", "authenticated_by": "api_key"}


PI, R1, R2, R3, HUMAN = (actor(n) for n in ("pi", "r1", "r2", "r3", "human"))
ATTACKER = actor("attacker")
VIEWER = actor("viewer", "viewer")
OPERATOR = actor("op", "operator")


def sha(n):
    return f"{n:02x}" * 20


def cfg(required=2, reviewers=None, writers=("key:pi",), groups=()):
    reviewers = reviewers or {"key:r1": {}, "key:r2": {}, "key:r3": {}, "key:pi": {}, "key:human": {"login": "Author-Login"}}
    return QuorumConfig(required, {k: {"login": (v.get("login") or "").lower() or None} for k, v in reviewers.items()},
                        frozenset(writers), tuple(tuple(g) for g in groups))


def hexdigest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class World:
    def __init__(self):
        self.repo = ControlPlaneRepo()
        self.feed = Feed(IngestService(self.repo))

    def head(self, value, who=PI):
        self.feed.node(PR, attrs={"head_sha": value}, principal=who)

    def review(self, who, node, commit, verdict="lgtm", digest=True, scope="org", binding=True, pr=PR):
        attrs = {"commit": commit, "verdict": verdict}
        if digest:
            d = digest if isinstance(digest, str) else hexdigest(f"{node}-{commit}")
            attrs["receipt_digest"] = d
            if binding:
                attrs["receipt_binding"] = binding if isinstance(binding, str) else receipt_binding(
                    pr, commit, who["id"], d)
        self.feed.node(node, principal=who, scope=scope)
        return self.feed.edge("reviewed", node, pr, attrs, principal=who, scope=scope)

    def q(self, config=None, principal=OPERATOR):
        return pr_review_quorum(self.repo.graph, principal, PR, config or cfg())


@pytest.fixture
def w():
    world = World()
    world.feed.node(PR, label="PR 7", principal=PI, attrs={"author": "somebody-else"})
    return world


def test_two_distinct_reviewers_at_the_current_head_satisfy(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    assert w.q()["status"] == "insufficient"
    w.review(R2, "review:b", sha(1))
    out = w.q()
    assert out["status"] == "satisfied" and out["counted"] == ["key:r1", "key:r2"]
    assert not out["rereview_required"]


def test_head_change_invalidates_prior_verdicts_and_requests_rereview(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    assert w.q()["status"] == "satisfied"
    w.head(sha(2))                                     # repair pushed
    out = w.q()
    assert out["status"] == "insufficient" and out["counted"] == []
    assert out["stale_reviewers"] == ["key:r1", "key:r2"]
    assert out["rereview_required"] and out["rereview_needed"] == 2
    w.review(R1, "review:c", sha(2))
    w.review(R3, "review:d", sha(2))
    assert w.q()["status"] == "satisfied"


def test_verdict_arriving_after_the_head_moved_is_not_counted(w):
    w.head(sha(1))
    w.head(sha(2))
    w.review(R1, "review:a", sha(1))                   # issued for sha(1), lands late
    w.review(R2, "review:b", sha(1))
    out = w.q()
    assert out["status"] == "insufficient" and out["counted"] == []


def test_a_verdict_for_a_head_not_yet_observed_waits_for_the_head(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(2))
    w.review(R2, "review:b", sha(2))
    assert w.q()["status"] == "insufficient"
    w.head(sha(2))
    assert w.q()["status"] == "satisfied"


def test_force_push_back_restores_only_that_exact_commit_and_both_views_agree(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    w.review(R1, "review:a2", sha(2))                   # r1 reviewed the next head too
    w.head(sha(2))
    assert w.q()["counted"] == ["key:r1"]               # only the exact current commit counts
    w.head(sha(1))                                      # force-push back to the reviewed SHA
    out = w.q()
    assert out["status"] == "satisfied" and out["counted"] == ["key:r1", "key:r2"]
    # the graph's own flag is arrival-order based and says the sha(1) edges are stale; the quorum
    # is head-relative. They are different questions and the docs say so.
    edge = w.repo.graph.get_edge(OPERATOR, "reviewed", "review:a", PR)["edges"][0]
    assert edge["stale"] is True and out["counted"] == ["key:r1", "key:r2"]


def test_only_trusted_writers_define_the_head(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    w.head(sha(9), who=ATTACKER)                        # commons writer rolls the head elsewhere
    assert w.q()["head"] == sha(1) and w.q()["status"] == "satisfied"
    fresh = World()
    fresh.feed.node(PR, principal=ATTACKER, attrs={"head_sha": sha(1)})
    assert fresh.q()["status"] == "unknown_head"        # nobody trusted has spoken: nothing passes
    assert not fresh.q()["rereview_required"]


def test_malformed_or_short_head_is_unknown(w):
    w.head("abc1234")
    w.review(R1, "review:a", "abc1234")
    w.review(R2, "review:b", "abc1234")
    assert w.q()["status"] == "unknown_head"


def test_the_producer_of_the_pr_and_the_author_login_cannot_approve(w):
    w.head(sha(1))
    w.feed.edge("produced", "attempt:1", PR, {"commit": sha(1)}, principal=PI)
    w.review(PI, "review:self", sha(1))                 # the implementer's own key
    w.feed.node(PR, principal=PI, attrs={"author": "AUTHOR-login"})
    w.review(HUMAN, "review:human", sha(1))
    w.review(R1, "review:a", sha(1))
    out = w.q()
    assert out["counted"] == ["key:r1"] and out["status"] == "insufficient"
    assert {(r["reviewer"], r["reason"]) for r in out["rejected"]} == {("key:pi", "author"), ("key:human", "author")}


def test_author_attr_from_an_untrusted_writer_is_ignored(w):
    w.head(sha(1))
    w.feed.node(PR, principal=ATTACKER, attrs={"author": "someone-else"})
    w.review(HUMAN, "review:human", sha(1))
    w.review(R1, "review:a", sha(1))
    assert w.q()["status"] == "satisfied"


def test_one_identity_with_many_review_nodes_counts_once(w):
    w.head(sha(1))
    for i in range(3):
        w.review(R1, f"review:dup{i}", sha(1))
    out = w.q()
    assert out["counted"] == ["key:r1"] and out["status"] == "insufficient"


def test_unconfigured_principals_are_ignored_both_ways(w):
    w.head(sha(1))
    w.review(ATTACKER, "review:x1", sha(1))
    w.review(ATTACKER, "review:x2", sha(1), verdict="blocked")
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    assert w.q()["status"] == "satisfied"               # junk cannot block the gate either


def test_a_shared_receipt_digest_voids_both_approvals(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1), digest=hexdigest("same-receipt"))
    w.review(R2, "review:b", sha(1), digest=hexdigest("same-receipt"))
    out = w.q()
    assert out["status"] == "insufficient" and out["counted"] == []
    assert {r["reason"] for r in out["rejected"]} == {"replayed_receipt"}


def test_approval_without_a_receipt_digest_does_not_count(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1), digest=False)
    assert w.q(cfg(1))["status"] == "insufficient"


def test_a_configured_reviewers_block_blocks_and_a_repair_clears_it(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    w.review(R3, "review:c", sha(1), verdict="blocked")
    out = w.q()
    assert out["status"] == "blocked" and out["blocking"] == ["key:r3"] and not out["rereview_required"]
    w.head(sha(2))
    assert w.q()["status"] == "insufficient"            # old block gone, old approvals gone


def test_a_hidden_block_still_blocks_and_is_not_named_to_a_viewer(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    w.review(R3, "review:c", sha(1), verdict="blocked", scope="private")
    out = w.q(principal=actor("viewer", "viewer"))
    assert out["status"] == "blocked"
    assert out["blocking"] == [] and out["has_hidden_reviews"] and out["blocked_by_hidden"]
    assert "hidden_reviewers" not in out and "blocking_total" not in out and "counted_total" not in out
    assert w.q(principal=R3)["blocking"] == ["key:r3"]


def test_a_forged_edge_from_someone_elses_review_node_is_refused(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    with pytest.raises(PermissionError):
        w.feed.edge("reviewed", "review:a", PR, {"commit": sha(1), "verdict": "lgtm", "receipt_digest": "z"},
                    principal=ATTACKER, nodes=False)


def test_replaying_an_event_changes_nothing(w):
    w.head(sha(1))
    edge = {"edge_type": "reviewed", "from_id": "review:a", "to_id": PR,
            "attrs": {"commit": sha(1), "verdict": "lgtm", "receipt_digest": "x"}}
    w.feed.node("review:a", principal=R1)
    first = w.feed.send("edge.add", edge, seq=500, principal=R1)
    before = json.dumps(w.q(cfg(1)), sort_keys=True)
    again = w.feed.send("edge.add", edge, seq=500, principal=R1)
    assert first["envelope"]["duplicate"] is False and again["envelope"]["duplicate"] is True
    assert json.dumps(w.q(cfg(1)), sort_keys=True) == before


def test_a_stale_replay_of_an_older_head_write_cannot_roll_the_head_back(w):
    w.head(sha(1))
    old = w.feed.n - 1
    w.head(sha(2))
    w.feed.send("node.upsert", {"node_type": "pr", "node_id": PR, "attrs": {"head_sha": sha(1)}},
                seq=old, principal=PI)                           # same event replayed late
    assert w.q()["head"] == sha(2)


def test_quorum_is_identical_after_the_graph_is_rebuilt_from_stored_events(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    live = w.q()
    w.repo._rebuild_graph()
    assert w.repo.graph.events_applied > 0
    assert w.q() == live and live["status"] == "satisfied"


def test_hidden_or_missing_pr_is_not_found(w):
    with pytest.raises(KeyError):
        pr_review_quorum(w.repo.graph, OPERATOR, "pr:github.com/o/r#404", cfg())
    w.feed.node("pr:github.com/o/r#8", scope="private", principal=PI)
    with pytest.raises(KeyError):
        pr_review_quorum(w.repo.graph, VIEWER, "pr:github.com/o/r#8", cfg())
    w.feed.node("task:not-a-pr", principal=PI)
    with pytest.raises(KeyError):
        pr_review_quorum(w.repo.graph, OPERATOR, "task:not-a-pr", cfg())


def test_config_validation():
    ok = {"required": 2, "reviewers": ["key:a", "key:b"], "trusted_head_writers": ["key:pi"]}
    assert parse_config(ok).required == 2 and parse_config(None) is None
    for bad in ({**ok, "required": 0}, {**ok, "required": True}, {**ok, "required": "2"},
                {**ok, "reviewers": []}, {**ok, "trusted_head_writers": []}, {**ok, "extra": 1},
                {**ok, "reviewers": {"key:a": {"login": ""}}}, "nope"):
        with pytest.raises(ValueError):
            parse_config(bad)


@pytest.fixture
def http():
    repo = ControlPlaneRepo()
    keys = {"opkey": "operator", "viewkey": "viewer", "pikey": "ingest"}
    q = parse_config({"required": 1, "reviewers": ["key:x"], "trusted_head_writers": ["key:x"]})
    server = ControlPlaneServer(repo, api_keys=keys, port=0, review_quorum=q)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    base = f"http://127.0.0.1:{server.port}"

    def call(path, key):
        req = urllib.request.Request(base + path, headers={"X-API-Key": key})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    yield repo, call, server
    server.shutdown()


def test_http_endpoint_requires_auth_and_hides_unknown_prs(http):
    repo, call, server = http
    path = "/api/v1/graph/nodes/pr%3Agithub.com%2Fo%2Fr%237/review-quorum"
    assert call(path, "")[0] == 401
    assert call(path, "viewkey")[0] == 404
    svc = IngestService(repo)
    Feed(svc).node(PR, principal=OPERATOR, attrs={"head_sha": sha(1)})
    s, body = call(path, "viewkey")
    assert s == 200 and body["status"] == "unknown_head" and body["required"] == 1
    server.review_quorum = None
    s, body = call(path, "viewkey")
    assert s == 404 and "not configured" in body["error"]


def test_an_operator_or_cowriter_cannot_change_a_reviewers_verdict_under_their_identity(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    assert w.q()["status"] == "satisfied"
    flip = {"edge_type": "reviewed", "from_id": "review:a", "to_id": PR,
            "attrs": {"commit": sha(1), "verdict": "blocked", "receipt_digest": "forged"}}
    try:
        w.feed.send("edge.add", flip, principal=OPERATOR)
    except PermissionError:
        pass
    assert w.q()["status"] == "satisfied" and w.q()["counted"] == ["key:r1", "key:r2"]
    w.feed.send("node.upsert", {"node_type": "review", "node_id": "review:a",
                                "attrs": {"co_writers": ["key:attacker"]}}, principal=OPERATOR)
    try:
        w.feed.send("edge.add", flip, principal=ATTACKER)
    except PermissionError:
        pass
    out = w.q()
    assert out["status"] == "satisfied" and out["blocking"] == []


def test_a_non_owner_cannot_retract_a_block(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R3, "review:c", sha(1), verdict="blocked")
    assert w.q()["status"] == "blocked"
    rm = {"edge_type": "reviewed", "from_id": "review:c", "to_id": PR, "reason": "retract", "attrs": {"commit": sha(1)}}
    for who in (OPERATOR, ATTACKER, R1):
        try:
            w.feed.send("edge.remove", rm, principal=who)
        except PermissionError:
            pass
    wild = {k: v for k, v in rm.items() if k != "attrs"}
    for who in (OPERATOR, ATTACKER, R1):
        try:
            w.feed.send("edge.remove", wild, principal=who)
        except PermissionError:
            pass
    assert w.q()["status"] == "blocked"


def test_the_reviewer_can_retract_their_own_block(w):
    w.head(sha(1))
    w.review(R3, "review:c", sha(1), verdict="blocked")
    assert w.q(cfg(1))["status"] == "blocked"
    w.feed.send("edge.remove", {"edge_type": "reviewed", "from_id": "review:c", "to_id": PR,
                                "reason": "withdrawn", "attrs": {"commit": sha(1)}}, principal=R3)
    assert w.q(cfg(1))["status"] == "insufficient"


# --------------------------------------------------------------------------
# shared conformance vectors (also loaded by autospec's cargo suite)
# --------------------------------------------------------------------------
VECTORS = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" /
                      "review-quorum-vectors.json").read_text())["cases"]


@pytest.mark.parametrize("case", [c for c in VECTORS if "herdr" in c["applies"]], ids=lambda c: c["name"])
def test_shared_policy_vectors(case):
    w = World()
    target = case["target"]
    w.feed.node(target, principal=PI)
    w.feed.node(target, principal=PI, attrs={"head_sha": case["head"]})
    pol = case["policy"]
    if pol["pr_author"]:
        w.feed.node(target, principal=PI, attrs={"author": pol["pr_author"]})
    ids = {v["reviewer"] for v in case["votes"]}
    eligible = pol.get("eligible_reviewers") or sorted(ids)
    logins = pol.get("reviewer_logins", {})
    reviewers = {r: {"login": logins.get(r)} for r in set(eligible) | set(logins)}
    groups = [[g for g in grp] for grp in pol.get("reviewer_groups", [])]
    # a voter outside the configured set is simply never read, which is the policy for ineligible ones
    for g in groups:
        for member in g:
            reviewers.setdefault(member, {"login": None})
    config = QuorumConfig(pol["required"], {k: {"login": (v["login"] or "").lower() or None}
                                            for k, v in reviewers.items()},
                          frozenset({"key:pi"}), tuple(tuple(g) for g in groups))
    for n, v in enumerate(case["votes"]):
        who = {"kind": "agent", "id": v["reviewer"], "authenticated_by": "api_key"}
        node = f"review:v{n}"
        attrs = {"commit": v["commit"], "verdict": v["verdict"]}
        for src, dst in (("receipt_digest", "receipt_digest"), ("receipt_binding", "receipt_binding")):
            if v.get(src) is not None:
                attrs[dst] = v[src]
        w.feed.node(node, principal=who)
        w.feed.edge("reviewed", node, v["target"], attrs, principal=who)
    out = pr_review_quorum(w.repo.graph, OPERATOR, target, config)
    exp = case["expect"]
    assert (out["status"], out["counted"], out["blocking"]) == (exp["status"], exp["counted"], exp["blocking"]), out


def test_every_vector_names_the_implementations_it_applies_to():
    assert len(VECTORS) >= 20
    assert all(set(c["applies"]) <= {"rust", "herdr"} and c["applies"] for c in VECTORS)
    assert sum("herdr" in c["applies"] for c in VECTORS) >= 20


def test_a_trusted_emitter_restart_that_resets_its_sequence_freezes_the_head_as_unverified(w):
    w.feed.send("node.upsert", {"node_type": "pr", "node_id": PR, "attrs": {"head_sha": sha(1)}},
                principal=PI, seq=100)
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    assert w.q()["status"] == "satisfied"
    # Pi restarts, its sequence restarts at 1 and it reports the REAL new head
    w.feed.send("node.upsert", {"node_type": "pr", "node_id": PR, "attrs": {"head_sha": sha(2)}},
                principal=PI, seq=1)
    out = w.q()
    assert out["status"] == "head_unverified" and out["status"] != "satisfied"
    # a write at a higher sequence than the frozen one re-syncs it
    w.feed.send("node.upsert", {"node_type": "pr", "node_id": PR, "attrs": {"head_sha": sha(2)}},
                principal=PI, seq=101)
    out = w.q()
    assert out["head"] == sha(2) and out["status"] == "insufficient"


def test_head_unverified_is_stable_across_rebuild(w):
    w.feed.send("node.upsert", {"node_type": "pr", "node_id": PR, "attrs": {"head_sha": sha(1)}},
                principal=PI, seq=100)
    w.feed.send("node.upsert", {"node_type": "pr", "node_id": PR, "attrs": {"head_sha": sha(2)}},
                principal=PI, seq=1)
    before = w.q()["status"]
    w.repo._rebuild_graph()
    assert before == w.q()["status"] == "head_unverified"


def test_clearing_the_author_with_an_empty_string_does_not_forget_it_and_unknown_fails_closed(w):
    w.head(sha(1))
    w.review(HUMAN, "review:h", sha(1))
    w.review(R1, "review:a", sha(1))
    c = cfg(1, reviewers={"key:r1": {}, "key:human": {"login": "somebody-else"}})
    assert w.q(c)["counted"] == ["key:r1"]              # author excluded
    w.feed.node(PR, principal=PI, attrs={"author": ""})  # trusted writer blanks it
    out = w.q(c)
    assert out["status"] == "unknown_author" and out["counted"] == []


def test_a_pr_without_any_author_never_passes(w):
    fresh = World()
    fresh.feed.node(PR, principal=PI, attrs={"head_sha": sha(1)})
    fresh.review(R1, "review:a", sha(1))
    assert fresh.q(cfg(1))["status"] == "unknown_author"


def test_bot_author_matches_its_alias_and_verdict_case_is_normalised(w):
    w.head(sha(1))
    w.feed.node(PR, principal=PI, attrs={"author": "Dependabot[bot]"})
    w.review(R1, "review:a", sha(1), verdict="LGTM")
    c = cfg(1, reviewers={"key:r1": {"login": "dependabot"}, "key:r2": {}})
    assert w.q(c)["status"] == "insufficient"
    w.review(R2, "review:b", sha(1), verdict=" Lgtm ")
    assert w.q(c)["counted"] == ["key:r2"]


def test_digest_must_be_hex_and_bound_to_target_commit_and_reviewer(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1), digest="x1", binding=False)
    d = hexdigest("shared")
    w.review(R2, "review:b", sha(1), digest=d, binding=receipt_binding(PR, sha(1), "key:r1", d))  # copied binding
    assert w.q()["counted"] == []
    w.review(R3, "review:c", sha(1), digest=d)
    assert w.q()["counted"] == ["key:r3"]


def test_one_person_with_two_keys_counts_once_when_grouped(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1))
    assert w.q(cfg(2))["status"] == "satisfied"
    assert w.q(cfg(2, groups=[["key:r1", "key:r2"]]))["status"] == "insufficient"
    with pytest.raises(ValueError):
        parse_config({"required": 1, "reviewers": ["a", "b"], "trusted_head_writers": ["p"],
                      "reviewer_groups": [["a", "b"], ["b"]]})


def test_a_private_approval_is_not_disclosed_or_named_to_a_viewer(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1))
    w.review(R2, "review:b", sha(1), scope="private")
    seen = w.q(principal=VIEWER)
    assert seen["counted"] == ["key:r1"] and "counted_total" not in seen and seen["has_hidden_reviews"]
    assert w.q(principal=R2)["counted"] == ["key:r1", "key:r2"]


def test_route_accepts_a_differently_cased_pr_id(w):
    w.head(sha(1))
    out = pr_review_quorum(w.repo.graph, OPERATOR, PR.upper().replace("PR:", "pr:", 1), cfg())
    assert out["pr"] == PR


def test_many_edges_from_one_reviewer_stay_cheap(w):
    import time
    w.head(sha(1))
    for i in range(1500):
        w.feed.node(f"review:{i}", principal=R1)
        w.feed.edge("reviewed", f"review:{i}", PR, {"commit": sha(1), "verdict": "lgtm",
                                                    "receipt_digest": hexdigest(str(i))}, principal=R1, nodes=False)
    t = time.time()
    out = w.q()
    assert time.time() - t < 5 and out["status"] == "insufficient"


# ---------------------------------------------------------------- head: reorder vs restart
def head_at(w, value, seq, who=PI):
    w.feed.send("node.upsert", {"node_type": "pr", "node_id": PR, "attrs": {"head_sha": value}},
                principal=who, seq=seq)


def test_a_late_redelivery_of_an_old_head_or_the_same_head_is_ignored(w):
    head_at(w, sha(1), 10)
    head_at(w, sha(2), 12)
    head_at(w, sha(1), 11)          # an old head the writer already reported, delivered late
    head_at(w, sha(2), 3)           # the applied head again, far below: a plain duplicate
    out = w.q()
    assert out["head"] == sha(2) and out["head_unverified"] is False and out["status"] == "insufficient"


def test_a_nearby_late_write_with_a_new_head_is_flagged_until_the_writer_moves_on(w):
    head_at(w, sha(1), 10)
    head_at(w, sha(3), 12)
    head_at(w, sha(2), 11)          # never reported before and lower than the applied one
    out = w.q()
    assert out["status"] == "head_unverified" and out["head_unverified"] is True and out["alert"]
    head_at(w, sha(4), 13)          # the writer carries on: the applied sequence moves past it
    out = w.q()
    assert out["head"] == sha(4) and out["head_unverified"] is False


def test_same_head_reemits_cannot_evict_the_evidence_of_a_suspect_write(w):
    head_at(w, sha(1), 100)
    head_at(w, sha(2), 1)
    assert w.q()["status"] == "head_unverified"
    for i in range(12):
        head_at(w, sha(1), 2 + i)               # re-emits of the applied head
    assert w.q()["status"] == "head_unverified"
    assert w.q()["head"] == sha(1)


def test_two_distinct_new_heads_below_the_applied_one_are_flagged(w):
    head_at(w, sha(1), 100)
    head_at(w, sha(2), 1)
    head_at(w, sha(3), 2)
    out = w.q()
    assert out["status"] == "head_unverified"
    assert w.q()["resync_event_id"] == "pi-2"


def test_a_suspected_restart_is_flagged_loudly_and_logged_once(w, caplog):
    head_at(w, sha(1), 100)
    head_at(w, sha(2), 1)
    with caplog.at_level("WARNING"):
        out = w.q()
        again = w.q()
    assert out["status"] == "head_unverified" and out["head_unverified"] is True
    assert out["counted"] == [] and "re-sync" in out["alert"] and again["alert"] == out["alert"]
    assert out["resync_event_id"] == "pi-1"                       # the operator sees what to acknowledge
    assert "resync_event_id" not in w.q(principal=VIEWER)         # nobody else does
    assert len([r for r in caplog.records if "unverified" in r.getMessage()]) == 1


def test_a_run_of_post_restart_writes_is_never_adopted_without_an_operator(w):
    head_at(w, sha(1), 100)
    head_at(w, sha(2), 1)
    head_at(w, sha(2), 2)
    head_at(w, sha(2), 3)
    out = w.q()
    assert out["status"] == "head_unverified" and out["head"] == sha(1) and "head_rebased" not in out
    w.repo._rebuild_graph()
    assert w.q()["status"] == "head_unverified"
    head_at(w, sha(2), 101)               # the writer passes the old sequence: ordinary again
    out = w.q()
    assert out["head"] == sha(2) and out["status"] == "insufficient" and out["head_unverified"] is False


def test_an_operator_can_acknowledge_the_candidate_write_and_only_that_one(w):
    head_at(w, sha(1), 100)
    head_at(w, sha(2), 1)
    w.feed.node(PR, principal=OPERATOR, attrs={"head_resync": "pi-999"})      # not what was observed
    assert w.q()["status"] == "head_unverified"
    w.feed.node(PR, principal=ATTACKER, attrs={"head_resync": "pi-1"})        # not an operator
    assert w.q()["status"] == "head_unverified"
    w.feed.node(PR, principal=OPERATOR, attrs={"head_resync": "pi-1"})
    out = w.q()
    assert out["head"] == sha(2) and out["head_resynced"] is True and out["status"] == "insufficient"
    w.repo._rebuild_graph()
    assert w.q()["head"] == sha(2) and w.q()["head_resynced"] is True
    # a NEW suspect write is not covered by the old acknowledgement
    head_at(w, sha(3), 2)
    out = w.q()
    assert out["status"] == "head_unverified" and w.q()["resync_event_id"] == "pi-2"


def test_a_viewers_rereview_count_comes_only_from_what_the_viewer_can_see(w):
    w.head(sha(1))
    w.review(R1, "review:a", sha(1), scope="private")
    w.review(R2, "review:b", sha(1))
    c = cfg(3)
    seen = w.q(c, principal=R2)
    assert seen["status"] == "insufficient" and seen["counted"] == ["key:r2"]
    assert seen["rereview_needed"] == 2 and seen["has_hidden_reviews"] is True   # not 1: no hidden shift
    own = w.q(c, principal=R1)
    assert own["counted"] == ["key:r1", "key:r2"] and own["rereview_needed"] == 1


def test_matching_is_ascii_only_like_autospec():
    from herdr_engineering.control_plane.review_quorum import ascii_norm, canonical_identity, canonical_target
    assert canonical_identity("Key:r1") is None and canonical_identity("key:r1 ") is None
    assert canonical_identity("  KEY:R1[BOT]\t") == "key:r1"
    assert ascii_norm("K") == "K" and ascii_norm("İ") == "İ"
    assert canonical_target("PR:GitHub.com/O/R#7") == "pr:github.com/o/r#7"
    assert canonical_target("pr:github.com/o/r#7K") is None and canonical_target("pr: x") is None
    assert canonical_target("") is None and canonical_target(None) is None
    assert receipt_binding("pr:github.com/o/r#7K", sha(1), "key:r1", "a" * 64) == \
        receipt_binding("", sha(1), "key:r1", "a" * 64)
