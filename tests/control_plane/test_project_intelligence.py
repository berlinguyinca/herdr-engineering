"""P1 project-intelligence core: dedupe/replay, scope isolation, evidence rules, controls (synthetic fixtures)."""
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane import project_intelligence as pi

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
ON = pi.config({"enabled": True, "ingest": True, "derive": True})
EV = {"type": "email_part", "source_ref": "gmail:msg-1", "anchor_type": "email_part", "anchor_value": "1.2"}


def store(now=T0 + timedelta(minutes=5)):
    return pi.IntelligenceStore(clock=lambda: now)


def evt(**kw):
    base = {"org_id": "org1", "source_system": "gmail", "source_instance": "acct-a", "source_id": "m1",
            "source_version": "1", "event_type": "email.received", "occurred_at": T0.isoformat(),
            "object_ref": "email:m1", "project_ids": ["p-binbase"], "title": "Peak plot attached",
            "payload": {"subject": "plots"}, "provenance_refs": [{k: v for k, v in EV.items() if k != "type"}]}
    base.update(kw)
    return base


def test_config_defaults_everything_off():
    cfg = pi.config(None)
    assert cfg["enabled"] is False and not any(cfg[f] for f in pi.FLAGS)
    assert pi.config({"enabled": "yes"})["enabled"] is False  # only a real boolean enables


def test_event_id_is_stable_and_key_sensitive():
    a = pi.event_id_for("o", "gmail", "i", "s", "1", "t")
    assert a == pi.event_id_for("o", "gmail", "i", "s", "1", "t")
    assert a != pi.event_id_for("o", "gmail", "i", "s", "2", "t")


def test_ingest_requires_flags():
    with pytest.raises(pi.IngestionPaused):
        store().append_event(evt(), cfg=pi.config({"enabled": True}))


def test_replay_is_idempotent_with_one_effect():
    s = store()
    first, created = s.append_event(evt(), cfg=ON)
    again, created2 = s.append_event(evt(), cfg=ON)
    assert created and not created2 and first.event_id == again.event_id
    assert s.outbox_pending("timeline") == [first.event_id]
    s.outbox_ack(first.event_id, "timeline")
    s.append_event(evt(), cfg=ON)  # replay after ack must not re-queue
    assert s.outbox_pending("timeline") == []


def test_same_key_different_content_must_be_an_amendment():
    s = store()
    first, _ = s.append_event(evt(), cfg=ON)
    with pytest.raises(pi.Conflict):
        s.append_event(evt(payload={"subject": "edited"}), cfg=ON)
    amend, created = s.append_event(evt(source_version="2", payload={"subject": "edited"},
                                        supersedes_id=first.event_id), cfg=ON)
    assert created
    items = s.timeline(pi.Viewer("org1"), project_id="p-binbase")["items"]
    assert {i["event_id"]: i["superseded"] for i in items} == {first.event_id: True, amend.event_id: False}


def test_unknown_supersedes_rejected():
    with pytest.raises(pi.IntelligenceError):
        store().append_event(evt(supersedes_id="pie_nope"), cfg=ON)


def test_late_and_out_of_order_arrival_is_shown_in_occurrence_order():
    s = store(now=T0 + timedelta(days=2))
    s.append_event(evt(source_id="m2", occurred_at=(T0 + timedelta(hours=2)).isoformat(), object_ref="email:m2"), cfg=ON)
    s.append_event(evt(source_id="m1", occurred_at=T0.isoformat()), cfg=ON)
    items = s.timeline(pi.Viewer("org1"))["items"]
    assert [i["involved_entities"][0] for i in items] == ["email:m1", "email:m2"]
    assert all(i["late_arrival"] for i in items)


def test_as_of_replays_history_by_ingestion_time():
    clock = [T0 + timedelta(minutes=1)]
    s = pi.IntelligenceStore(clock=lambda: clock[0])
    s.append_event(evt(), cfg=ON)
    clock[0] = T0 + timedelta(hours=3)
    s.append_event(evt(source_id="m2", object_ref="email:m2"), cfg=ON)
    assert s.timeline(pi.Viewer("org1"))["total"] == 2
    assert s.timeline(pi.Viewer("org1"), as_of=T0 + timedelta(hours=1))["total"] == 1


def test_pause_and_kill_switch_block_new_ingestion_immediately():
    s = store()
    s.pause_source("org1", "gmail")
    with pytest.raises(pi.IngestionPaused):
        s.append_event(evt(), cfg=ON)
    s.pause_source("org1", "gmail", False)
    s.append_event(evt(), cfg=ON)
    s.set_kill_switch(True)
    with pytest.raises(pi.IngestionPaused):
        s.append_event(evt(source_id="m9", object_ref="email:m9"), cfg=ON)


def test_scope_isolation_covers_counts_filters_and_labels():
    s = store()
    s.append_event(evt(), cfg=ON)
    s.append_event(evt(source_id="w1", object_ref="email:w1", project_ids=["p-work"],
                       security_label="confidential", title="SECRET-TITLE"), cfg=ON)
    s.append_event(evt(source_id="x1", object_ref="email:x1", org_id="org2"), cfg=ON)
    guest = pi.Viewer("org1", frozenset({"p-binbase"}))
    res = s.timeline(guest)
    assert res["total"] == 1 and "SECRET-TITLE" not in str(res)
    assert s.timeline(guest, project_id="p-work") == {"items": [], "total": 0}  # same as nonexistent
    assert s.timeline(pi.Viewer("org1", clearance="confidential"))["total"] == 2
    assert s.timeline(pi.Viewer("org2"))["total"] == 1
    assert s.timeline(pi.Viewer("org1", frozenset()))["total"] == 0  # revoked access sees nothing


def test_same_display_name_actors_are_not_merged():
    s = store()
    s.append_event(evt(actor_ref="person:alex-a"), cfg=ON)
    s.append_event(evt(source_id="m2", object_ref="email:m2", actor_ref="person:alex-b"), cfg=ON)
    assert s.timeline(pi.Viewer("org1"), actor_ref="person:alex-a")["total"] == 1


def test_tombstone_removes_content_but_keeps_the_fact():
    s = store()
    s.append_event(evt(), cfg=ON)
    assert s.tombstone_source("org1", "gmail:msg-1") == 1
    item = s.timeline(pi.Viewer("org1"))["items"][0]
    assert item["tombstoned"] and item["title"] == "[removed at source]"


def rel(**kw):
    base = {"org_id": "org1", "project_ids": ["p-binbase"], "from_ref": "email:m1", "to_ref": "issue:7",
            "predicate": "addresses_issue", "derivation": "explicit", "evidence_refs": [dict(EV, **{})]}
    base.update(kw)
    base["evidence_refs"] = [{k: v for k, v in e.items() if k != "type"} for e in base["evidence_refs"]]
    return base


def test_relationship_rules():
    s = store()
    with pytest.raises(pi.IntelligenceError):
        s.add_relationship(rel(predicate="grants_access"), cfg=ON, actor="u")  # no permission predicates
    with pytest.raises(pi.IntelligenceError):
        s.add_relationship(rel(evidence_refs=[]), cfg=ON, actor="u")
    with pytest.raises(pi.IntelligenceError):
        s.add_relationship(rel(derivation="inferred"), cfg=ON, actor="u")  # no confidence
    with pytest.raises(pi.IntelligenceError):
        s.add_relationship(rel(derivation="inferred", confidence=0.9, extractor_version="v1",
                               predicate="assigned_to"), cfg=ON, actor="u")  # authority needs verified source
    with pytest.raises(pi.IntelligenceError):
        s.add_relationship(rel(derivation="inferred", confidence=0.9, extractor_version="v1"),
                           cfg=pi.config({"enabled": True, "ingest": True}), actor="u")  # derive off
    r = s.add_relationship(rel(derivation="inferred", confidence=0.7, extractor_version="v1"), cfg=ON, actor="u")
    assert r.review_state == "provisional"
    assert s.add_relationship(rel(derivation="inferred", confidence=0.7, extractor_version="v1"), cfg=ON, actor="u").id == r.id


def test_review_is_versioned_and_scoped():
    s = store()
    r = s.add_relationship(rel(derivation="inferred", confidence=0.7, extractor_version="v1"), cfg=ON, actor="u")
    outsider = pi.Viewer("org1", frozenset({"p-other"}))
    with pytest.raises(pi.NotFound):
        s.review_relationship(r.id, accept=True, actor="a", expected_version=1, viewer=outsider)
    viewer = pi.Viewer("org1")
    with pytest.raises(pi.Conflict):
        s.review_relationship(r.id, accept=True, actor="a", expected_version=9, viewer=viewer)
    ok = s.review_relationship(r.id, accept=True, actor="a", expected_version=1, viewer=viewer)
    assert ok.review_state == "confirmed" and ok.derivation == "human_verified" and ok.version == 2
    s.review_relationship(r.id, accept=False, actor="a", expected_version=2, viewer=viewer)
    assert s.neighbours("issue:7", viewer) == []  # rejected edges disappear from traversal


def test_finding_without_evidence_is_marked_unverified():
    s = store()
    base = {"org_id": "org1", "project_ids": ["p-binbase"], "extractor_version": "v1", "model_identifier": "local"}
    s.add_finding(dict(base, statement="Peak shifted", evidence_refs=[{k: v for k, v in EV.items() if k != "type"}]))
    s.add_finding(dict(base, statement="Column degraded"))
    out = {f["statement"]: f["verified"] for f in s.findings(pi.Viewer("org1"))}
    assert out == {"Peak shifted": True, "[unverified] Column degraded": False}


def test_record_versioning_and_scope():
    s = store()
    s.upsert_record("project", org_id="org1", record_id="p-binbase", name="LC-BinBase")
    s.upsert_record("investigation", org_id="org1", record_id="inv1", name="Peak shift", project_ids=("p-binbase",))
    with pytest.raises(pi.Conflict):
        s.upsert_record("project", org_id="org1", record_id="p-binbase", name="x", expected_version=5)
    assert [r.id for r in s.records("investigation", pi.Viewer("org1", frozenset({"p-binbase"})))] == ["inv1"]
    assert s.records("project", pi.Viewer("org1", frozenset({"p-work"}))) == []


def test_relationship_review_rules_and_tombstone_reevaluation():
    s = store()
    inferred = s.add_relationship(rel(derivation="inferred", confidence=0.7, extractor_version="v1"), cfg=ON, actor="u")
    viewer = pi.Viewer("o" if False else "org1")
    s.review_relationship(inferred.id, accept=True, actor="a", expected_version=1, viewer=viewer)   # -> confirmed (human_verified)
    s.tombstone_source("org1", "gmail:msg-1")                    # the evidence's source disappears
    again = s.neighbours("issue:7", viewer)[0]
    assert again.review_state == "provisional" and again.version == 3
    s.review_relationship(inferred.id, accept=False, actor="a", expected_version=3, viewer=viewer)
    with pytest.raises(pi.Conflict):                              # rejected is final
        s.review_relationship(inferred.id, accept=True, actor="a", expected_version=4, viewer=viewer)
    verified = s.add_relationship(rel(to_ref="issue:8"), cfg=ON, actor="u")       # explicit/confirmed
    with pytest.raises(pi.Conflict):
        s.review_relationship(verified.id, accept=True, actor="a", expected_version=1, viewer=viewer)


def test_tombstone_scrubs_anchors_in_relationship_evidence():
    s = store()
    r = s.add_relationship(rel(), cfg=ON, actor="u")
    s.tombstone_source("org1", "gmail:msg-1")
    got = s.neighbours("issue:7", pi.Viewer("org1"))[0]
    assert got.id == r.id
    assert all(e.anchor_value == "removed" and e.source_ref == "gmail:msg-1" for e in got.evidence)


def test_dedupe_does_not_swallow_a_differently_labelled_relationship():
    s = store()
    a = s.add_relationship(rel(security_label="confidential"), cfg=ON, actor="u")
    b = s.add_relationship(rel(security_label="internal"), cfg=ON, actor="u")
    assert a.id != b.id


def test_get_event_by_id_is_scoped_and_not_window_limited():
    s = store()
    first, _ = s.append_event(evt(), cfg=ON)
    for i in range(600):
        s.append_event(evt(source_id=f"x{i}", object_ref=f"email:x{i}"), cfg=ON)
    guest = pi.Viewer("org1", frozenset({"p-binbase"}))
    assert s.get_event(guest, first.event_id)["event_id"] == first.event_id        # older than the newest 500
    with pytest.raises(pi.NotFound):
        s.get_event(pi.Viewer("org1", frozenset({"p-other"})), first.event_id)


def test_removing_a_source_unverifies_findings_that_only_cited_it():
    from herdr_engineering.control_plane import project_intelligence as pi
    s = pi.IntelligenceStore()
    ev = lambda ref: {"source_ref": ref, "anchor_type": "email_part", "anchor_value": "1"}  # noqa: E731
    base = {"org_id": "o", "project_ids": ["p"], "extractor_version": "v1", "model_identifier": "m"}
    s.add_finding(dict(base, statement="only mail", evidence_refs=[ev("gmail:a:1")]))
    s.add_finding(dict(base, statement="mail and ci", evidence_refs=[ev("gmail:a:1"), ev("ci:9")]))
    viewer = pi.Viewer("o", None, "restricted")
    s.tombstone_source("o", "gmail:a:1")
    by = {f["statement"].removeprefix("[unverified] "): f for f in s.findings(viewer)}
    assert by["only mail"]["verified"] is False and by["only mail"]["evidence_refs"] == []
    assert by["mail and ci"]["verified"] is True and len(by["mail and ci"]["evidence_refs"]) == 1


def test_tombstone_scrubs_actor_and_evidence_anchors_but_keeps_the_source_ref():
    from datetime import UTC, datetime

    from herdr_engineering.control_plane import project_intelligence as pi
    s = pi.IntelligenceStore()
    s.append_event({"org_id": "o", "source_system": "gmail", "source_instance": "a", "source_id": "m1", "event_type": "email.received",
                    "occurred_at": datetime(2026, 10, 1, tzinfo=UTC).isoformat(), "object_ref": "email:m1", "project_ids": ["p"],
                    "title": "Secret subject", "actor_ref": "email:alex@lab.example", "payload": {"x": 1},
                    "provenance_refs": [{"source_ref": "gmail:a:m1", "anchor_type": "email_part", "anchor_value": "1.2", "content_hash": "ab" * 32}]},
                   cfg={"enabled": True, "ingest": True})
    assert s.tombstone_source("o", "gmail:a:m1") == 1
    item = s.timeline(pi.Viewer("o", None, "restricted"), project_id="p")["items"][0]
    assert item["title"] == "[removed at source]" and item["actor"] is None
    assert item["evidence_refs"] == [{"source_ref": "gmail:a:m1", "anchor_type": "removed", "anchor_value": "removed",
                                      "redaction_policy": "source_removed"}]


def test_ingest_cannot_submit_a_removed_anchor_reference():
    import pytest

    from herdr_engineering.control_plane import project_intelligence as pi
    forged = {"source_ref": "x:1", "anchor_type": "removed", "anchor_value": "removed", "redaction_policy": "source_removed"}
    with pytest.raises(pi.IntelligenceError):
        pi.EvidenceRef.parse(forged)
    assert pi.EvidenceRef.parse(forged, stored=True).anchor_type == "removed"
