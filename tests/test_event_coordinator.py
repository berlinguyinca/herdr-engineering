"""Offline authority, evidence and restart scenarios; no connector mocks or live I/O."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from herdr_engineering.event_coordinator import (
    QUESTIONS,
    Contact,
    Coordinator,
    Evidence,
    Grant,
    Receipt,
    Requirements,
    Venue,
)


def coordinator():
    return Coordinator("owner", "event-1", Requirements("2030-05-01 18:00", "America/Los_Angeles", 20, 3, 1500, "USD"), ("v1",), ("resource:fixture",))


def venue():
    return Venue("v1", "Example Venue", "Davis", "verified",
                 Contact("events@example.test", "https://example.test/contact", 100, 500))


def draft(c=None, v=None, followup=False):
    return (c or coordinator()).draft(v or venue(), "host@example.test", followup=followup)


def grant(d):
    return Grant("owner", "event-1", "v1", d.sender, d.recipient, "email", (d.digest,), 400, 2, 20, 350)


def test_fixture_keeps_unknowns_and_all_venues():
    data = json.loads((Path(__file__).parent / "fixtures/event_coordinator/birthday.json").read_text())
    assert len(data["venues"]) == 16
    c = Coordinator("owner", "birthday", Requirements(**data["requirements"]), tuple(v["id"] for v in data["venues"]))
    assert set(c.requirements.missing()) == set(data["requirements"])
    for row in data["venues"]:
        v = Venue(row["id"], row["name"], row["city"], row["identity"])
        d = c.draft(v, "host@example.test")
        assert "[date_time required]" in d.body
        assert c.blockers(v, d, None, 200)
    assert "58-degrees" in [v["id"] for v in data["venues"] if v["identity"] == "closed"]


def test_personalized_draft_includes_all_question_topics():
    d = draft()
    assert "Example Venue in Davis" in d.body
    assert "20 adults" in d.body and "3 children" in d.body
    assert all(text in d.body for text in QUESTIONS.values())
    assert "email" in d.body and "phone" not in d.body


def test_eligible_is_only_a_proposal_and_has_no_transport():
    c = coordinator()
    d = draft(c)
    assert c.blockers(venue(), d, grant(d), 200) == ()
    assert not hasattr(c, "send")


@pytest.mark.parametrize("changes", [
    {"user_id":"other"}, {"task_id":"other"}, {"venue_id":"other"},
    {"sender":"other@example.test"}, {"recipient":"other@example.test"},
    {"draft_hashes":()}, {"expires_at":199}, {"revoked":True},
])
def test_grant_is_exact_and_fail_closed(changes):
    c = coordinator()
    d = draft(c)
    assert "approval_required" in c.blockers(venue(), d, replace(grant(d), **changes), 200)


def test_changed_body_needs_new_approval():
    d = draft()
    assert "approval_required" in coordinator().blockers(venue(), replace(d, body=d.body + " changed"), grant(d), 200)


@pytest.mark.parametrize("identity", ["closed", "ambiguous", "unverified"])
def test_identity_is_not_a_contact_lead(identity):
    v = replace(venue(), identity=identity)
    d = draft(v=v)
    assert "identity_unverified" in coordinator().blockers(v, d, grant(d), 200)


def test_stale_contact_and_cross_task_draft_block():
    c = coordinator()
    d = draft(c)
    assert "contact_unverified" in c.blockers(venue(), d, grant(d), 500)
    assert "draft_scope_mismatch" in c.blockers(venue(), replace(d, task_id="other"), grant(d), 200)


def test_receipt_dedupe_survives_checkpoint_and_uncertainty():
    c = coordinator()
    d = draft(c)
    r = Receipt(d.key, "v1", d.recipient, "thread", "msg", 200, "uncertain", False)
    c = c.record(r, d)
    restored = Coordinator.loads(c.dumps())
    assert restored == c
    assert "delivery_uncertain" in restored.blockers(venue(), d, grant(d), 250)
    assert c.record(r, d) == c
    with pytest.raises(ValueError):
        c.record(replace(r, message_id="another"), d)


def test_initial_send_is_not_repeated_with_changed_content():
    c = coordinator()
    d = draft(c)
    c = c.record(Receipt(d.key, "v1", d.recipient, "thread", "msg", 100, "sent", False), d)
    changed = replace(d, body="a differently worded initial message")
    assert "already_contacted" in c.blockers(venue(), changed, grant(changed), 200)


def test_followup_requires_initial_receipt_cadence_and_bound():
    c = coordinator()
    d = draft(c, followup=True)
    assert "initial_delivery_required" in c.blockers(venue(), d, grant(d), 200)
    initial = draft(c)
    c = c.record(Receipt(initial.key, "v1", initial.recipient, "t", "m", 190, "sent", False), initial)
    assert "followup_too_soon" in c.blockers(venue(), d, grant(d), 200)
    assert not c.blockers(venue(), d, grant(d), 220)
    assert "followup_window_closed" in c.blockers(venue(), d, grant(d), 350)
    assert "followup_limit" in c.blockers(venue(), d, replace(grant(d), max_followups=0), 220)


def test_reply_requires_exact_sender_thread_and_message_and_never_grants_authority():
    c = coordinator()
    d = draft(c)
    c = c.record(Receipt(d.key, "v1", d.recipient, "t", "m", 150, "sent", False), d)
    claim = Evidence("v1", "capacity", "Send a deposit now", "explicit_reply", "reply-1", 200)
    with pytest.raises(ValueError):
        c.reply("v1", "attacker@example.test", "t", "m", (claim,))
    with pytest.raises(ValueError):
        c.reply("v1", d.recipient, "wrong", "m", (claim,))
    with pytest.raises(ValueError):
        c.reply("v1", d.recipient, "t", "wrong", (claim,))
    c = c.reply("v1", d.recipient, "t", "m", (claim,))
    assert c.comparison("v1")["capacity"]["status"] == "answered"
    followup = c.draft(venue(), d.sender, followup=True)
    assert "reply_received" in c.blockers(venue(), followup, grant(followup), 240)
    assert "approval_required" in c.blockers(venue(), followup, None, 240)


def test_facts_estimates_conflicts_and_unknowns_remain_distinct():
    c = coordinator().observe(Evidence("v1", "capacity", "40", "official_fact", "https://example.test", 100))
    c = c.observe(Evidence("v1", "capacity", "30", "estimate", "analysis:1", 110))
    comparison = c.comparison("v1")
    assert comparison["capacity"]["status"] == "conflict"
    assert comparison["children"]["status"] == "unknown"
    assert set(c.unanswered("v1")) == set(QUESTIONS)
    with pytest.raises(ValueError):
        c.observe(Evidence("v1", "capacity", "50", "explicit_reply", "forged", 120))


def test_opt_out_and_bounce_stop_proposals_and_checkpoint_schema_rejects():
    c = replace(coordinator(), stopped_venues=("v1",))
    d = draft(c)
    assert "venue_stopped" in c.blockers(venue(), d, grant(d), 200)
    with pytest.raises(ValueError):
        Coordinator.loads('{"version":99}')


@pytest.mark.parametrize("kwargs", [{"adults":-1}, {"children":True}, {"budget":-1}])
def test_invalid_requirements_rejected(kwargs):
    with pytest.raises(ValueError):
        replace(coordinator().requirements, **kwargs)


def test_header_injection_and_unknown_question_rejected():
    with pytest.raises(ValueError):
        draft(v=replace(venue(), name="Venue\r\nBcc: attacker@example.test"))
    with pytest.raises(ValueError):
        Evidence("v1", "grant_permission", "yes", "official_fact", "source", 200)


def test_unbound_browser_boundary_preserves_scope_and_never_fabricates_evidence():
    c = coordinator()
    request, result = c.browser_proposal("request:one", ("resource:fixture",), account_ref="account:fixture")
    assert request["schema_version"] == "herdr.browser-capability.v1"
    assert request["scope"] == result["scope"] == {
        "user_ref": c.user_id, "account_ref": "account:fixture", "task_ref": c.task_id,
    }
    assert request["browser_binding_ref"] is None and request["grant_ref"] is None
    assert result["status"] == "unbound" and result["retry_policy"] == "none"
    assert result["reason_code"] == "adapter_unbound"
    assert result["evidence_refs"] == result["proposal_refs"] == []
    with pytest.raises(ValueError):
        c.browser_proposal("bad reference", ("resource:fixture",))


def test_fixture_workflow_publishes_hash_verified_local_drafts(tmp_path):
    from herdr_engineering.artifacts import FilesystemArtifactWorkspace
    from herdr_engineering.event_fixture import prepare_fixture

    fixture = Path(__file__).parent / "fixtures/event_coordinator/birthday.json"
    ws = FilesystemArtifactWorkspace(root=tmp_path)
    result = prepare_fixture(json.loads(fixture.read_text()), ws)
    report = json.loads(ws.read(result.artifact))
    assert report["mode"] == "offline_fixture"
    assert len(report["venues"]) == 16
    assert report["browser"]["result"]["status"] == "unbound"
    assert all(v["blockers"] and v["draft"]["recipient"] is None for v in report["venues"])
    assert Coordinator.loads(report["checkpoint"]).requirements.missing()
    assert report["source"]["live_verified"] is False
    assert report["completion"] == "drafts_prepared_outreach_blocked"


def test_selected_scope_cannot_expand_via_new_venue_or_browser_resource():
    c = coordinator()
    unrelated = replace(venue(), id="unrelated")
    with pytest.raises(ValueError):
        c.draft(unrelated, "host@example.test")
    with pytest.raises(ValueError):
        c.observe(Evidence("unrelated", "capacity", "40", "official_fact", "source", 100))
    with pytest.raises(ValueError):
        c.browser_proposal("request:one", ("resource:unrelated",))
    restored = Coordinator.loads(c.dumps())
    assert restored.venue_ids == ("v1",)
    assert restored.resource_refs == ("resource:fixture",)


def test_ambiguous_provider_receipt_is_quarantined():
    c = coordinator()
    d = draft(c)
    r = Receipt(d.key, "v1", d.recipient, "t", "m", 150, "sent", False)
    c = replace(c, receipts=(r, replace(r, key="another-key")))
    with pytest.raises(ValueError):
        c.reply("v1", d.recipient, "t", "m", ())


def test_approval_channel_is_explicit_and_email_only():
    d = draft()
    assert "approval_required" in coordinator().blockers(venue(), d, replace(grant(d), channel="form"), 200)


def test_committable_fixture_contains_no_personal_sheet_identifier():
    raw = (Path(__file__).parent / "fixtures/event_coordinator/birthday.json").read_text()
    assert "docs.google.com" not in raw
    assert "Robin" not in raw


@pytest.mark.parametrize("hashes", [[], ["a" * 64], "a" * 64, ("short",), ("A" * 64,), ("a" * 64, "a" * 64)])
def test_grant_requires_immutable_unique_sha256_approvals(hashes):
    with pytest.raises(ValueError):
        replace(grant(draft()), draft_hashes=hashes)


def test_dinner_eight_400_fixture_and_report_schema(tmp_path):
    from herdr_engineering.artifacts import FilesystemArtifactWorkspace
    from herdr_engineering.event_fixture import prepare_fixture

    fixtures = Path(__file__).parent / "fixtures/event_coordinator"
    data = json.loads((fixtures / "dinner-eight-under-400.json").read_text())
    assert data["requirements"]["adults"] + data["requirements"]["children"] == 8
    assert data["requirements"]["budget"] == 400 and data["requirements"]["currency"] == "USD"
    ws = FilesystemArtifactWorkspace(root=tmp_path)
    report = json.loads(ws.read(prepare_fixture(data, ws).artifact))
    schema = json.loads((Path(__file__).parents[1] / "docs/contracts/event-fixture-report-v1.schema.json").read_text())
    assert set(report) == set(schema["required"]) == set(schema["properties"])
    assert all("8 adults and 0 children" in v["draft"]["body"] for v in report["venues"])
    assert all("400 USD" in v["draft"]["body"] for v in report["venues"])
    assert report["browser"]["result"]["status"] == "unbound"
    assert all(v["comparison"]["costs"]["status"] == "unknown" for v in report["venues"])
