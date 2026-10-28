"""Durable store + live-event bridge against a PRIVATE throwaway Postgres (integration marker; self-contained)."""
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_engineering.control_plane import intel_bridge as br
from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane.repo import ControlPlaneRepo

from ._private_pg import private_postgres

MIG = Path(__file__).parents[2] / "deploy/control-plane/migrations/0020_project_intelligence.sql"
T = datetime(2026, 10, 2, 12, tzinfo=UTC)
ON = pi.config({"enabled": True, "ingest": True, "derive": True, "suggest": True})
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def dsn():
    with private_postgres(MIG) as d:
        if d is None:
            pytest.skip("postgres binaries not installed")
        yield d


@pytest.fixture()
def clean(dsn):
    p = br.PgPersistence(dsn)
    p._do(lambda c: c.execute("truncate pi_audit, pi_finding, pi_relationship, pi_outbox, pi_event, pi_source_state, pi_record cascade"))
    return p


def raw(sid="m1", **kw):
    base = {"org_id": "o", "source_system": "gmail", "source_instance": "a", "source_id": sid, "event_type": "email.received",
            "occurred_at": T.isoformat(), "object_ref": f"email:{sid}", "project_ids": ["p1"], "title": "plots", "payload": {"k": 1},
            "provenance_refs": [{"source_ref": f"gmail:a:{sid}", "anchor_type": "email_part", "anchor_value": "1"}]}
    base.update(kw)
    return base


def test_restart_rebuilds_the_same_timeline_and_replay_is_a_noop(clean):
    s1 = br.DurableIntelligenceStore(clean, clock=lambda: T)
    s1.upsert_record("project", org_id="o", record_id="p1", name="P1")
    ev, created = s1.append_event(raw(), cfg=ON)
    assert created
    rel = s1.add_relationship({"org_id": "o", "project_ids": ["p1"], "from_ref": "email:m1", "to_ref": "issue:7", "predicate": "mentions",
                               "derivation": "inferred", "confidence": 0.6, "extractor_version": "v1",
                               "evidence_refs": [{"source_ref": "s", "anchor_type": "object", "anchor_value": "1"}]}, cfg=ON, actor="x")
    s1.review_relationship(rel.id, accept=True, actor="alice", expected_version=1, viewer=pi.Viewer("o"))
    s1.add_finding({"org_id": "o", "project_ids": ["p1"], "statement": "claim", "extractor_version": "v1", "model_identifier": "local"})
    s1.pause_source("o", "slack")
    before = s1.timeline(pi.Viewer("o"))

    s2 = br.DurableIntelligenceStore(br.PgPersistence(clean._dsn), clock=lambda: T)           # "restart"
    assert s2.timeline(pi.Viewer("o")) == before
    assert s2.append_event(raw(), cfg=ON)[1] is False                                           # replay: no new effect
    assert [r.review_state for r in s2.neighbours("issue:7", pi.Viewer("o"))] == ["confirmed"]
    assert s2.findings(pi.Viewer("o"))[0]["verified"] is False
    assert [r.id for r in s2.records("project", pi.Viewer("o"))] == ["p1"]
    with pytest.raises(pi.IngestionPaused):                                                      # pause survived restart
        s2.append_event(raw("s1", source_system="slack"), cfg=ON)
    n = clean._do(lambda c: c.fetchval("select count(*) from pi_event"))
    o = clean._do(lambda c: c.fetchval("select count(*) from pi_outbox"))
    assert (n, o) == (1, 2)


def test_outbox_ack_and_tombstone_are_durable(clean):
    s = br.DurableIntelligenceStore(clean, clock=lambda: T)
    ev, _ = s.append_event(raw(), cfg=ON)
    s.outbox_ack(ev.event_id, "timeline")
    s.tombstone_source("o", "gmail:a:m1")
    s2 = br.DurableIntelligenceStore(br.PgPersistence(clean._dsn), clock=lambda: T)
    assert s2.outbox_pending("timeline") == [] and s2.outbox_pending("intelligence") == [ev.event_id]
    item = s2.timeline(pi.Viewer("o"))["items"][0]
    assert item["tombstoned"] and item["title"] == "[removed at source]"


def test_failed_write_rolls_memory_back_so_replay_can_succeed(clean):
    s = br.DurableIntelligenceStore(clean, clock=lambda: T)
    real = clean.save_event
    def boom(ev):
        raise br.StoreUnavailable("db down")
    clean.save_event = boom
    with pytest.raises(br.StoreUnavailable):
        s.append_event(raw(), cfg=ON)
    assert s.timeline(pi.Viewer("o"))["total"] == 0        # memory never ahead of the database
    clean.save_event = real
    assert s.append_event(raw(), cfg=ON)[1] is True         # the same fact is accepted after recovery


def test_concurrent_duplicate_appends_yield_one_row(clean):
    s = br.DurableIntelligenceStore(clean, clock=lambda: T)
    results = []
    ts = [threading.Thread(target=lambda: results.append(s.append_event(raw(), cfg=ON)[1])) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert results.count(True) == 1
    assert clean._do(lambda c: c.fetchval("select count(*) from pi_event")) == 1


def test_not_migrated_is_reported_distinctly(dsn):
    with private_postgres() as bare:                       # a second private server with NO migration
        with pytest.raises(br.StoreNotMigrated):
            br.DurableIntelligenceStore(br.PgPersistence(bare))


# ---- bridge from live repo events ----------------------------------------------------------------------------
class FakeRepo:
    def __init__(self):
        self.streams = {("ci_fix", "fix1"): [
            {"event_id": "e1", "event_type": "CIFixRequested", "sequence": 1, "source_timestamp": T.isoformat(),
             "payload": {"repository": "acme/app", "number": 7, "run": "r9", "revision": "a" * 40}},
            {"event_id": "e2", "event_type": "CIFixStateChanged", "sequence": 2, "source_timestamp": T.isoformat(),
             "payload": {"state": "running", "host": "bender"}},
            {"event_id": "e3", "event_type": "CIFixResult", "sequence": 3, "source_timestamp": T.isoformat(),
             "payload": {"pr_url": "https://example/pr/7"}}],
            ("artifact", "a1"): [{"event_id": "e9", "event_type": "ArtifactRegistered", "sequence": 1, "source_timestamp": T.isoformat(),
                                  "payload": {"filename": "plot.png", "sha256": "f" * 64, "size_bytes": 10, "mime_type": "image/png"}}]}
    def entity_ids(self, entity):
        return [i for (e, i) in self.streams if e == entity]
    def stream(self, entity, eid):
        return list(self.streams.get((entity, eid), []))


def test_bridge_backfill_is_dry_by_default_repeatable_and_never_guesses_projects(clean):
    repo = FakeRepo()
    store = br.DurableIntelligenceStore(clean, clock=lambda: T)
    bridge = br.EventBridge(store, lambda: {"enabled": True, "ingest": True}, br.make_resolver(repo, forge_host="github.com"), org_id="o", instance="bender")
    dry = bridge.backfill(repo)
    assert dry["dry_run"] and dry["would_append"] == 3 and dry["unmapped"] == 1 and store.timeline(pi.Viewer("o"))["total"] == 0
    first = bridge.backfill(repo, dry_run=False)
    assert first["would_append"] == 3 and store.timeline(pi.Viewer("o"))["total"] == 3
    bridge.backfill(repo, dry_run=False)
    assert store.timeline(pi.Viewer("o"))["total"] == 3 and bridge.stats["replayed"] == 3   # repeatable, no duplicates
    assert bridge.stats["unmapped"] == 2                    # the artifact (once per real run): the artifact has no repository: skipped, not guessed
    assert clean._do(lambda c: c.fetchval("select count(*) from pi_event")) == 3
    items = store.timeline(pi.Viewer("o", frozenset({"github.com/acme/app"})))["items"]
    assert len(items) == 3 and all(i["category"] == "ci" for i in items)


def test_bare_repository_without_a_configured_forge_is_unmapped_not_guessed(clean):
    repo = FakeRepo()
    store = br.DurableIntelligenceStore(clean, clock=lambda: T)
    bridge = br.EventBridge(store, lambda: {"enabled": True, "ingest": True}, br.make_resolver(repo), org_id="o")
    assert bridge.backfill(repo, dry_run=False)["would_append"] == 0 and store.timeline(pi.Viewer("o"))["total"] == 0


def test_bridge_listener_never_blocks_and_flags_gate_it(clean):
    repo = FakeRepo()
    store = br.DurableIntelligenceStore(clean, clock=lambda: T)
    cfg = {"enabled": False}
    bridge = br.EventBridge(store, lambda: cfg, br.make_resolver(repo, forge_host="github.com"), org_id="o", capacity=2)
    ev = dict(repo.streams[("ci_fix", "fix1")][0], entity_type="ci_fix", entity_id="fix1")
    bridge.on_event(ev)
    assert bridge._q.qsize() == 0 and bridge.stats["dropped"] == 0   # disabled (the default): not even queued
    cfg["enabled"] = True                                    # enabled but ingest still off
    for _ in range(5):
        bridge.on_event(ev)                                 # no worker running: must not block or raise
    assert bridge.stats["dropped"] == 3
    assert bridge.process(ev) == "off"                      # ingest flag not set
    cfg.update(enabled=True, ingest=True, kill_switch=True)
    assert bridge.process(ev) == "off"                      # hot kill switch
    cfg.update(kill_switch=False)
    bridge.start()
    bridge.drain()
    assert store.timeline(pi.Viewer("o"))["total"] >= 1
    bridge.stop()


def test_real_repo_events_flow_through_the_listener(clean):
    repo = ControlPlaneRepo()
    store = br.DurableIntelligenceStore(clean, clock=lambda: T)
    bridge = br.EventBridge(store, lambda: {"enabled": True, "ingest": True}, br.make_resolver(repo, forge_host="github.com"), org_id="o")
    repo.subscribe(bridge.on_event)
    bridge.start()
    repo.record_event("ci_fix", "fix7", "CIFixRequested", {"repository": "acme/app", "number": 1, "run": "r1", "revision": "b" * 40})
    repo.record_event("ci_fix", "fix7", "CIFixStateChanged", {"state": "running"})
    repo.record_event("unrelated", "x", "Whatever", {})
    bridge.drain()
    bridge.stop()
    assert store.timeline(pi.Viewer("o"))["total"] == 2 and bridge.stats["ignored"] >= 1


def test_every_mutator_rolls_memory_back_when_the_database_write_fails(clean):
    s = br.DurableIntelligenceStore(clean, clock=lambda: T)
    ev, _ = s.append_event(raw(), cfg=ON)

    def fail(*a, **k):
        raise br.StoreUnavailable("db down")
    for name in ("save_pause", "save_record", "save_relationship", "save_finding", "ack", "mark_tombstoned"):
        setattr(clean, name, fail)
    with pytest.raises(br.StoreUnavailable):
        s.pause_source("o", "gmail")
    assert ("o", "gmail") not in s._paused                       # a failed pause must not look paused (or vice versa)
    with pytest.raises(br.StoreUnavailable):
        s.upsert_record("project", org_id="o", record_id="p1", name="P1")
    assert s.records("project", pi.Viewer("o")) == []
    with pytest.raises(br.StoreUnavailable):
        s.add_finding({"org_id": "o", "statement": "x", "extractor_version": "v", "model_identifier": "m"})
    assert s.findings(pi.Viewer("o")) == []
    with pytest.raises(br.StoreUnavailable):
        s.outbox_ack(ev.event_id, "timeline")
    assert s.outbox_pending("timeline") == [ev.event_id]
    with pytest.raises(br.StoreUnavailable):
        s.tombstone_source("o", "gmail:a:m1")
    assert s.timeline(pi.Viewer("o"))["items"][0]["tombstoned"] is False


def test_install_does_nothing_while_disabled_and_builds_lazily_when_enabled():
    class Repo(FakeRepo):
        subs = []
        def subscribe(self, fn):
            self.subs.append(fn)
    repo, section = Repo(), {}
    routes, bridge = br.install(repo, lambda: section, dsn="postgresql://unused-never-connected")
    ev = dict(repo.streams[("ci_fix", "fix1")][0], entity_type="ci_fix", entity_id="fix1")
    for _ in range(50):
        bridge.on_event(ev)
    assert bridge._thread is None and routes._store is None and bridge._q.qsize() == 0   # no thread, no DB, no queue
    assert repo.subs == [bridge.on_event]


@pytest.mark.integration
def test_ci_fix_events_fold_into_the_incident_board():
    from herdr_engineering.control_plane import project_intelligence as pi
    from herdr_engineering.control_plane.intel_bridge import EventBridge
    store = pi.IntelligenceStore()
    br = EventBridge(store, lambda: {"enabled": True, "ingest": True}, lambda ev: ("p1",), org_id="o")
    for n, state in enumerate(("queued", "running", "succeeded")):
        ev = {"event_id": f"e{n}", "sequence": n, "event_type": "CIFixStateChanged", "entity_type": "ci_fix", "entity_id": "f1",
              "source_timestamp": "2026-10-28T00:00:0%dZ" % n, "payload": {"repository": "o/r", "run": 7, "state": state}}
        assert br.process(ev) == "accepted"
    assert [i["state"] for i in br.board.summary()] == ["REVIEWING"]
