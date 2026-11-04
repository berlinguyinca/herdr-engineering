"""Approved proposals execute once, for exactly the approved payload, and only when the execute flag is on."""
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import intel_assistant as ia
from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane.intel_executor import ProposalExecutor

T = datetime(2026, 10, 2, 12, tzinfo=UTC)
ON = {"enabled": True, "ingest": True, "suggest": True, "execute": True}


def rig(section=None, **kw):
    sect = dict(ON if section is None else section)
    store = pi.IntelligenceStore(clock=lambda: T)
    board = ia.ActionBoard(clock=lambda: T)
    return store, board, sect, ProposalExecutor(board, store, section_fn=lambda: sect, **kw)


def approved(board, action, payload):
    p = board.request(org_id="o", requester="agent", action=action, payload=payload, resource_version=1)
    return board.approve(p.id, approver="alice", org_id="o", payload_sha256=p.payload_sha256, resource_version=1)


LINK = {"project": "p1", "from_ref": "email:m1", "to_ref": "issue:acme/app#12", "predicate": "mentions"}


def test_off_by_default_an_approval_is_permission_only():
    store, board, sect, ex = rig({"enabled": True, "suggest": True})
    p = approved(board, "link_entities", LINK)
    assert ex.execute(p.id, "o").state == "approved"
    sect.update(execute=True, kill_switch=True)
    assert ex.execute(p.id, "o").state == "approved"
    assert store.neighbours("email:m1", pi.Viewer("o")) == []


def test_link_is_created_once_with_the_approver_as_verifier_and_a_second_run_is_refused():
    store, board, sect, ex = rig()
    p = approved(board, "link_entities", LINK)
    assert ex.execute(p.id, "o").state == "executed"
    rels = store.neighbours("email:m1", pi.Viewer("o"))
    assert len(rels) == 1 and rels[0].derivation == "human_verified" and rels[0].verified_by == "alice"
    with pytest.raises(pi.Conflict):
        ex.execute(p.id, "o")
    assert len(store.neighbours("email:m1", pi.Viewer("o"))) == 1


def test_tampered_payload_after_approval_is_not_executed():
    store, board, _, ex = rig()
    p = approved(board, "pause_ingestion", {"project": "p1", "source": "github"})
    p.payload["source"] = "gmail"
    out = ex.execute(p.id, "o")
    assert out.state == "failed" and "changed after approval" in out.history[-1]
    assert not store._paused


def test_pause_and_unwired_handlers_and_bad_payloads():
    store, board, _, ex = rig()
    assert ex.execute(approved(board, "pause_ingestion", {"project": "p1", "source": "github"}).id, "o").state == "executed"
    assert ("o", "github") in store._paused
    for action, payload in (("request_ci_repair", {"project": "p", "repo": "a/b", "run": 5}),
                            ("propose_issue", {"project": "p", "repo": "a/b", "title": "t"}),
                            ("link_entities", {"project": "p", "from_ref": "x y", "to_ref": "z", "predicate": "mentions"})):
        out = ex.execute(approved(board, action, payload).id, "o")
        assert out.state == "failed", action


def test_ci_repair_and_issue_use_the_injected_hooks_with_the_proposal_id_as_idempotency_key():
    calls = []
    store, board, _, ex = rig(ci_repair=lambda *a: calls.append(("repair", a)),
                              create_issue=lambda *a: calls.append(("issue", a)))
    r = approved(board, "request_ci_repair", {"project": "p", "repo": "a/b", "run": 5})
    i = approved(board, "propose_issue", {"project": "p", "repo": "a/b", "title": "Peak shift", "body": "see run"})
    assert ex.execute(r.id, "o").state == "executed" and ex.execute(i.id, "o").state == "executed"
    assert calls == [("repair", ("a/b", 5, r.id, "alice")), ("issue", ("a/b", "Peak shift", "see run", i.id))]
    bad = approved(board, "request_ci_repair", {"project": "p", "repo": "a/b", "run": True})
    assert ex.execute(bad.id, "o").state == "failed"


def test_other_org_cannot_execute():
    _, board, _, ex = rig()
    p = approved(board, "pause_ingestion", {"project": "p1", "source": "github"})
    with pytest.raises(pi.NotFound):
        ex.execute(p.id, "other")


def test_ci_rerun_restarts_exactly_the_approved_run_once():
    calls = []
    store, board, _, ex = rig(ci_rerun=lambda *a: calls.append(a))
    p = approved(board, "ci_rerun", {"project": "p", "repo": "a/b", "run": 7})
    assert ex.execute(p.id, "o").state == "executed"
    assert calls == [("a/b", 7)]
    with pytest.raises(pi.Conflict):
        ex.execute(p.id, "o")
    assert calls == [("a/b", 7)]


def test_ci_rerun_refuses_bad_payloads_unwired_hooks_and_provider_refusals():
    calls = []
    _, board, _, ex = rig(ci_rerun=lambda *a: calls.append(a))
    for payload in ({"project": "p", "repo": "a/b", "run": True}, {"project": "p", "repo": "a/b", "run": 0},
                    {"project": "p", "repo": "../x", "run": 3}, {"project": "p", "run": 3}):
        assert ex.execute(approved(board, "ci_rerun", payload).id, "o").state == "failed", payload
    assert calls == []
    _, board2, _, unwired = rig()
    assert unwired.execute(approved(board2, "ci_rerun", {"project": "p", "repo": "a/b", "run": 3}).id, "o").state == "failed"

    def refuse(*_a):
        raise PermissionError("Woodpecker does not permit this action for this pipeline")
    _, board3, _, refusing = rig(ci_rerun=refuse)
    out = refusing.execute(approved(board3, "ci_rerun", {"project": "p", "repo": "a/b", "run": 3}).id, "o")
    assert out.state == "failed" and "PermissionError" in out.history[-1]


def test_ci_rerun_waits_for_the_execute_flag():
    calls = []
    _, board, sect, ex = rig({"enabled": True, "suggest": True}, ci_rerun=lambda *a: calls.append(a))
    p = approved(board, "ci_rerun", {"project": "p", "repo": "a/b", "run": 7})
    assert ex.execute(p.id, "o").state == "approved" and calls == []
