"""Assistant tools, typed navigation, approval binding, notification planning, and a synthetic end-to-end flow."""
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane import intel_assistant as ia
from herdr_engineering.control_plane import intel_attachments as att
from herdr_engineering.control_plane import intel_gmail as gm
from herdr_engineering.control_plane import intel_webhook as wh
from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane import repair_policy as rp

T = datetime(2026, 10, 2, 12, tzinfo=UTC)
ON = pi.config({"enabled": True, "ingest": True, "derive": True, "suggest": True})


@pytest.mark.parametrize("kind,kw", [("nope", {}), ("timeline", {}), ("event", {"project": "p"}),
                                     ("timeline", {"project": "p", "javascript": "x"}), ("timeline", {"project": "x" * 500})])
def test_navigation_rejects_anything_outside_the_schema(kind, kw):
    with pytest.raises(ValueError):
        ia.navigate(kind, **kw)


def test_navigation_is_a_specific_deep_link_not_a_generic_url():
    assert ia.navigate("event", project="p-binbase", event="pie_1") == "#/intelligence?project=p-binbase&event=pie_1"
    assert "javascript" not in ia.navigate("timeline", project='"><script>')  # encoded, inert


def seeded():
    s = pi.IntelligenceStore(clock=lambda: T)
    s.upsert_record("project", org_id="o", record_id="p-binbase", name="LC-BinBase")
    s.upsert_record("project", org_id="o", record_id="p-work", name="Work Secret")
    s.append_event({"org_id": "o", "source_system": "gmail", "source_instance": "a", "source_id": "m1", "event_type": "email.received",
                    "occurred_at": T.isoformat(), "object_ref": "email:m1", "project_ids": ["p-binbase"], "title": "plots",
                    "provenance_refs": [{"source_ref": "gmail:a:m1", "anchor_type": "email_part", "anchor_value": "1"}]}, cfg=ON)
    s.append_event({"org_id": "o", "source_system": "gmail", "source_instance": "a", "source_id": "w1", "event_type": "email.received",
                    "occurred_at": T.isoformat(), "object_ref": "email:w1", "project_ids": ["p-work"], "title": "HIDDEN"}, cfg=ON)
    return s


def test_read_tools_run_under_the_callers_scope():
    s = seeded(); tools = ia.ReadTools(s)
    guest = pi.Viewer("o", frozenset({"p-binbase"}))
    assert [p["id"] for p in tools.projects_search(guest)] == ["p-binbase"]
    out = tools.timeline_query(guest, "p-binbase")
    assert out["items"][0]["link"].startswith("#/intelligence?")
    ev = tools.evidence_open(guest, "p-binbase", out["items"][0]["event_id"])
    assert ev["evidence_refs"][0]["source_ref"] == "gmail:a:m1"
    hidden_id = s.timeline(pi.Viewer("o"), project_id="p-work")["items"][0]["event_id"]
    with pytest.raises(pi.NotFound):
        tools.evidence_open(guest, "p-work", hidden_id)
    assert tools.timeline_query(guest, "p-work")["total"] == 0


def test_approval_binds_payload_version_expiry_and_a_different_principal():
    now = [T]
    board = ia.ActionBoard(clock=lambda: now[0])
    payload = {"run": "r9", "repo": "acme/app"}
    def mk():
        return board.request(org_id="o", requester="agent-1", action="request_ci_repair", payload=payload, resource_version=3)

    p = mk()
    with pytest.raises(pi.Conflict):   # forged: different payload
        board.approve(p.id, approver="alice", org_id="o", payload_sha256=ia.payload_hash({"run": "r9", "repo": "evil/x"}), resource_version=3)
    assert p.state == "refused"
    p = mk()
    with pytest.raises(pi.Conflict):   # stale resource version
        board.approve(p.id, approver="alice", org_id="o", payload_sha256=p.payload_sha256, resource_version=2)
    p = mk()
    with pytest.raises(pi.Conflict):   # self-approval
        board.approve(p.id, approver="agent-1", org_id="o", payload_sha256=p.payload_sha256, resource_version=3)
    p = mk()
    with pytest.raises(pi.NotFound):   # wrong org
        board.approve(p.id, approver="alice", org_id="other", payload_sha256=p.payload_sha256, resource_version=3)
    p = mk()
    now[0] = T + timedelta(hours=1)
    with pytest.raises(pi.Conflict):
        board.approve(p.id, approver="alice", org_id="o", payload_sha256=p.payload_sha256, resource_version=3)
    now[0] = T
    p = mk()
    assert board.approve(p.id, approver="alice", org_id="o", payload_sha256=p.payload_sha256, resource_version=3).state == "approved"
    with pytest.raises(pi.Conflict):   # one-shot
        board.approve(p.id, approver="bob", org_id="o", payload_sha256=p.payload_sha256, resource_version=3)
    with pytest.raises(pi.IntelligenceError):
        board.request(org_id="o", requester="a", action="rm_rf", payload={}, resource_version=1)


def test_notification_planner_dedupes_and_respects_quiet_hours():
    pl = ia.NotificationPlanner()
    day, night = T.replace(hour=14), T.replace(hour=23)
    n = ia.Notice("ci:r9:red", "normal", "CI red", "#/ci")
    assert pl.plan(n, day) == "send_now" and pl.plan(n, day) == "duplicate"
    assert pl.plan(ia.Notice("a", "normal", "x", "#/x"), night) == "held_for_digest" and len(pl.digest) == 1
    assert pl.plan(ia.Notice("b", "urgent", "post-merge red", "#/ci"), night) == "send_now"


def test_end_to_end_email_to_decision_with_no_unauthorised_effect():
    """email+attachments -> triage -> timeline -> explicit link -> CI webhook -> policy says a human must decide -> notice."""
    s = pi.IntelligenceStore(clock=lambda: T)
    s.upsert_record("project", org_id="o", record_id="p-binbase", name="LC-BinBase")
    viewer = pi.Viewer("o", frozenset({"p-binbase"}), "confidential")
    message = {"id": "m1", "threadId": "t1", "historyId": "9", "internalDate": "1790000000000",
               "payload": {"headers": [{"name": "From", "value": "Alex <a@lab.example>"}, {"name": "Subject", "value": "Peak shift"}],
                           "parts": [{"mimeType": "text/plain", "body": {"data": "c2VlIGh0dHBzOi8vZ2l0aHViLmNvbS9hY21lL2FwcC9pc3N1ZXMvMTI"}},
                                     {"mimeType": "image/png", "filename": "p.png", "body": {"attachmentId": "A", "size": 40}}]}}
    for e in gm.normalise_message(message, org_id="o", account="a", project_ids=("p-binbase",)):
        s.append_event(e, cfg=ON)
    assert att.triage(b"\x89PNG\r\n\x1a\n" + b"\0" * 32, "image/png", "p.png").verdict == "accept"
    for link in gm.explicit_links(message, org_id="o", project_ids=("p-binbase",), account="a"):
        s.add_relationship(link, cfg=ON, actor="ingest")
    ci = wh.normalise("workflow_run", {"action": "completed", "repository": {"full_name": "acme/app"},
        "workflow_run": {"id": 5, "head_sha": "d" * 40, "status": "completed", "conclusion": "failure", "name": "ci",
                         "updated_at": T.isoformat()}}, org_id="o", installation="i", project_ids=("p-binbase",))
    s.append_event(ci, cfg=ON)
    tl = ia.ReadTools(s).timeline_query(viewer, "p-binbase")
    assert [i["category"] for i in tl["items"]].count("ci") == 1 and tl["total"] == 3
    assert [r.predicate for r in s.neighbours("issue:acme/app#12", viewer)] == ["mentions"]
    # A repair for the failing run exists as a one-incident key and is gated by policy: auth path -> human, never auto.
    inc = rp.Incident(rp.incident_key("p-binbase", "run-5", "v1"))
    assert inc.attach("evt-ci-5") and not inc.attach("evt-ci-5")
    policy = rp.RepairPolicy(auto_merge_enabled=True, allowed_repositories=frozenset({"acme/app"}), auto_merge_risks=frozenset({"low"}))
    decision = rp.evaluate(policy, rp.Evidence(
        repository="acme/app", base_branch="main", head_sha="d" * 40, change=rp.Change(("src/auth_guard.py",), 1), review_sha="d" * 40,
        review_passed=True, reviewer_context="r", author_context="a", reviewer_model_family="b", author_model_family="a",
        check_shas={"ci": "d" * 40}, check_status={"ci": "pass"}, required_checks=("ci",), changes_requested=False,
        human_approvals=1, provenance_signed=True))
    assert decision.outcome == "require_human" and decision.risk == "high"
    notice = ia.Notice(f"incident:{inc.key}", "normal", "Sensitive CI repair needs your approval", ia.navigate("timeline", project="p-binbase"))
    assert ia.NotificationPlanner().plan(notice, T.replace(hour=15)) == "send_now"
