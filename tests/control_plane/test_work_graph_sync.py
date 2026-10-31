# ruff: noqa: F811  (pytest fixtures are imported by name from wg_pg)
"""Work Graph producers on real PostgreSQL: reconciliation, replay-from-clean gate, retention, orphans, migrations."""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

import pytest

from herdr_engineering.control_plane import work_graph_core as core
from herdr_engineering.control_plane import workspace_contracts as wc
from herdr_engineering.control_plane.events import Event, EventStore
from herdr_engineering.control_plane.work_graph_store import SnapshotRequired, WorkGraphStore
from herdr_engineering.control_plane.work_graph_sync import (
    PgSources,
    WorkGraphReconciler,
    WorkGraphWorker,
)

from .wg_pg import wg, wg_pg  # noqa: F401
from .work_graph_events import scenario

OP = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}
MIGRATIONS = Path(__file__).resolve().parents[2] / "deploy/control-plane/migrations"


def put_fabric(wg, events):
    """Insert source events into the REAL fabric_events table, as DurableEventSink would."""
    async def go():
        async with wg.pool.acquire() as c:
            for e in events:
                actor = e.get("actor")
                await c.execute(
                    "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, payload, source_timestamp, "
                    "schema_version, source, source_sequence, actor, visibility_scope, room_id, workspace_id, task_id, agent_id, "
                    "mission_id) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,2,$8,$9,$10::jsonb,$11,$12,$13,$14,$15,$16) "
                    "ON CONFLICT DO NOTHING",
                    e["event_id"], e["sequence"], e["event_type"], e["entity_type"], e["entity_id"], e["payload"],
                    e["source_timestamp"], e.get("source"), e.get("source_sequence"), actor if actor else None,
                    e.get("visibility_scope"), e.get("room_id"), e.get("workspace_id"), e.get("task_id"), e.get("agent_id"),
                    e.get("mission_id"))
    wg.run(go())


@pytest.fixture()
def env(wg):
    wg.run(wg.pool.execute("DELETE FROM fabric_events"))
    wg.run(wg.pool.execute("TRUNCATE artifact_bindings, artifact_versions, artifacts CASCADE"))
    st = WorkGraphStore(wg.pool)
    return wg, st, PgSources(wg.pool), WorkGraphReconciler(st, PgSources(wg.pool))


def pure(events):
    p = core.Projection()
    p.apply_all(events)
    return p


def test_the_deployed_but_empty_graph_is_filled_by_the_producer(env):
    wg, st, src, rec = env
    assert wg.run(st.status())["counts"]["entities"] == 0                      # what production looks like today
    events = scenario().events
    put_fabric(wg, events)
    report = wg.run(rec.reconcile())
    assert report["repaired_events"] == len(events) and report["poison"] == 0
    d = wg.run(st.dump())
    assert len([e for e in d["entities"].values() if e["kind"] == "task"]) == 12
    assert d == pure(events).dump()
    assert wg.run(rec.reconcile())["repaired_events"] == 0                     # a second pass finds nothing to do


def test_a12_dropped_late_and_repeated_events_are_repaired_without_duplicates(env):
    wg, st, src, rec = env
    events = scenario().events
    put_fabric(wg, events)
    live = [e for i, e in enumerate(events) if i % 7 != 3]                     # the live path DROPPED every 7th event
    random.Random(2).shuffle(live)
    wg.run(st.apply_events(live + live[:30]))                                  # reordered and repeated deliveries
    before = wg.run(st.dump())
    assert before != pure(events).dump()
    report = wg.run(rec.reconcile())
    assert report["repaired_events"] == len(events) - len(live)
    after = wg.run(st.dump())
    assert after == pure(events).dump()
    assert len([e for e in after["entities"].values() if e["kind"] == "task"]) == 12
    assert len({tuple(a) for e in after["entities"].values() for a in e["aliases"] if a[0] == "collab_task"}) == 12


def test_the_worker_projects_incrementally_and_late_arrivals_inside_the_window_are_caught(env):
    wg, st, src, rec = env
    s = scenario()
    first, late = s.events[:40], s.events[40:60]
    put_fabric(wg, first)
    w = WorkGraphWorker(st, src, reconcile_every=10_000)
    wg.run(w.step())
    put_fabric(wg, late)
    edge = first[-1]["source_timestamp"]
    from datetime import timedelta
    inside = {**first[5], "event_id": "ev-late-in", "sequence": 998, "source_timestamp": edge - timedelta(minutes=2)}
    outside = {**first[5], "event_id": "ev-late-out", "sequence": 999, "source_timestamp": edge - timedelta(hours=3)}
    put_fabric(wg, [inside, outside])
    wg.run(w.step())
    keys = wg.run(st.ledger_keys())
    assert "ev-late-in" in keys                             # inside the look-back window: picked up by the live path
    assert "ev-late-out" not in keys                        # older than the window: only reconciliation can find it
    assert wg.run(st.get_cursor("fabric")) is not None
    assert wg.run(rec.reconcile())["repaired_events"] >= 1
    assert "ev-late-out" in wg.run(st.ledger_keys())


def test_replay_from_clean_rebuilds_the_identical_graph(env):
    """THE GATE: build incrementally with noise, then drop the projection and replay from the sources."""
    wg, st, src, rec = env
    events = scenario().events
    put_fabric(wg, events)
    wg.run(st.apply_events(random.Random(9).sample(events, 50)))
    wg.run(rec.reconcile())
    wg.run(st.put_layout(OP, "project:p1", {"nodes": {wc.urn_for("task", "p1-r0-t0"): {"x": 3, "y": 4}}}, shared=True,
                         if_match=wg.run(st.get_layout(OP, "project:p1", which="default"))["etag"]))
    live = wg.run(st.dump())
    live_digest = core.dump_digest(live)
    report = wg.run(rec.rebuild())
    replayed = wg.run(st.dump())
    assert core.dump_digest(replayed) == live_digest, "clean replay must equal the live graph"
    assert replayed == live
    assert report["repaired_events"] == len(events)
    assert wg.run(wg.pool.fetchval("SELECT count(*) FROM wg_layout")) == 1             # layouts survive a rebuild
    # the same fold, three ways: pure, live PG, replayed PG
    assert core.dump_digest(pure(events).dump()) == live_digest


def test_rebuild_refuses_when_retention_pruned_events_the_graph_was_built_from(env):
    wg, st, src, rec = env
    events = scenario().events
    put_fabric(wg, events)
    wg.run(rec.reconcile())
    before = wg.run(st.dump())
    wg.run(wg.pool.execute("DELETE FROM fabric_events WHERE entity_type = 'session'"))        # the retention prune
    with pytest.raises(SnapshotRequired):
        wg.run(rec.rebuild())
    assert wg.run(st.dump()) == before                                                 # nothing was dropped
    st_after = wg.run(st.status())
    assert st_after["snapshot_required"] is True and st_after["open_gaps"]["retention_gap"] >= 1
    wg.run(rec.rebuild(force=True))                                                    # an explicit, informed override
    assert len(wg.run(st.dump())["entities"]) < len(before["entities"])


def test_poison_event_is_isolated_and_not_retried_forever(env, monkeypatch):
    wg, st, src, rec = env
    events = scenario().events[:30]
    put_fabric(wg, events)
    real = core.assertions_for_event

    def boom(ev, ops=None):
        if ev["event_id"] == "ev-collab-5":
            raise RuntimeError("bad event")
        return real(ev, ops)
    monkeypatch.setattr(core, "assertions_for_event", boom)
    out = wg.run(st.apply_events(events, batch=10))
    assert out["poison"] == 1 and out["applied"] == len(events) - 1
    assert wg.run(rec.reconcile())["poison"] == 0                       # it is in the ledger: found-and-failed once
    assert wg.run(st.status())["open_gaps"]["poison_event"] == 1


def _registry(wg):
    wg.run(wg.pool.execute(
        "INSERT INTO artifacts (artifact_id, created_by, display_name, artifact_type, status) VALUES "
        "('art-1','alice','CI report','report','available'), ('art-2','alice','Plan','plan','available'), "
        "('art-3','alice','Loose','other','available')"))
    wg.run(wg.pool.execute(
        "INSERT INTO artifact_versions (version_id, artifact_id, version_number, state, created_by, created_at, committed_at, parent_version_id) VALUES "
        "('ver-a','art-1',1,'committed','alice','2026-10-01T09:00:00Z','2026-10-01T09:00:00Z',NULL), "
        "('ver-b','art-1',2,'committed','alice','2026-10-01T10:00:00Z','2026-10-01T10:00:00Z','ver-a'), "
        "('ver-c','art-2',1,'committed','alice','2026-10-01T09:30:00Z','2026-10-01T09:30:00Z',NULL)"))
    wg.run(wg.pool.execute(
        "INSERT INTO artifact_bindings (artifact_id, room_id, issue_ref, created_by) VALUES "
        "('art-1','p1-r0','acme/app#7','alice'), ('art-2','p1-r1',NULL,'alice')"))


def test_no_orphan_artifacts_after_reconciliation(env):
    wg, st, src, rec = env
    put_fabric(wg, scenario().events)
    _registry(wg)
    first = wg.run(rec.orphans())
    assert first["missing"] == ["art-1", "art-2", "art-3"]                              # empty graph: everything is an orphan
    report = wg.run(rec.reconcile())
    assert report["registry_events"] >= 3
    o = report["orphans"]
    assert o["missing"] == [] and o["unlinked"] == [] and o["dangling"] == []
    assert o["unbound"] == ["art-3"]                                                  # nothing to link it to: reported, not hidden
    d = wg.run(st.dump())
    v = d["entities"][wc.urn_for("artifact_version", "ver-b")]
    assert v["attrs"]["parent_version_id"] == "ver-a"
    live = {(e["edge_type"], e["from_id"], e["to_id"]) for e in d["edges"].values() if e["live"]}
    assert ("contains", wc.urn_for("room", "p1-r0"), wc.urn_for("artifact", "art-1")) in live
    assert ("contains", wc.urn_for("issue", "acme/app#7"), wc.urn_for("artifact", "art-1")) in live
    assert ("supersedes", wc.urn_for("artifact_version", "ver-b"), wc.urn_for("artifact_version", "ver-a")) in live
    # a binding added later is linked by the next pass (additive), and an unchanged registry is a no-op
    wg.run(wg.pool.execute("INSERT INTO artifact_bindings (artifact_id, room_id, created_by) VALUES ('art-3','p1-r1','alice')"))
    again = wg.run(rec.reconcile())
    assert again["orphans"]["unbound"] == [] and again["orphans"]["unlinked"] == []
    assert wg.run(rec.reconcile())["registry_events"] == 0
    # an artifact deleted from the registry but still in the graph is a dangling node, reported
    wg.run(wg.pool.execute("DELETE FROM artifact_bindings WHERE artifact_id='art-3'; DELETE FROM artifacts WHERE artifact_id='art-3'"))
    assert wg.run(rec.orphans())["dangling"] == ["art-3"]


def test_issue_nodes_are_enriched_from_the_github_catalog(env):
    wg, st, src, rec = env
    put_fabric(wg, scenario().events)
    _registry(wg)
    wg.run(wg.pool.execute(
        "INSERT INTO github_issues (repository_id, github_id, data, rev) VALUES ('acme/app','I_1',$1::jsonb,1), "
        "('acme/other','I_2',$2::jsonb,1)",
        json.dumps({"repository_id": "acme/app", "number": 7, "title": "CI is red", "state": "OPEN", "url": "https://x/7",
                    "updated_at": "2026-10-01T11:00:00Z", "labels": ["ci"]}),
        json.dumps({"repository_id": "acme/other", "number": 1, "title": "unrelated", "state": "OPEN", "updated_at": "2026-10-01T11:00:00Z"})))
    wg.run(rec.reconcile())
    issue = wg.run(st.get_entity(OP, "github_issue:acme/app#7"))
    assert issue["display_name"] == "CI is red" and issue["status"] == "open" and issue["attrs"]["url"] == "https://x/7"
    assert wg.run(st.search(OP, "unrelated"))["hits"] == []                    # only issues the graph already refers to


def test_a14_a_thousand_node_graph_stays_bounded(env):
    wg, st, src, rec = env
    s = scenario(rooms_per_project=5, tasks_per_room=100)                      # 2 projects x 5 rooms x 100 tasks + the rest
    events = s.events
    t0 = time.perf_counter()
    wg.run(st.apply_events(events))
    load_s = time.perf_counter() - t0
    n = wg.run(st.status())["counts"]
    assert n["entities"] >= 1000
    t0 = time.perf_counter()
    snap = wg.run(st.snapshot(OP, "project:p1", depth=2, max_nodes=300))
    snap_s = time.perf_counter() - t0
    assert len(snap["nodes"]) == 300 and snap["has_more"] is True                # the collapsed view is bounded, with a more flag
    assert snap["queries"] <= 2 + 3                                              # round trips depend on depth, not on node count
    small = wg.run(st.snapshot(OP, "room:p1-r0", depth=1, max_nodes=50))
    assert len(small["nodes"]) <= 50
    t0 = time.perf_counter()
    page = wg.run(st.list_entities(OP, kinds=["task"], limit=200))
    page_s = time.perf_counter() - t0
    assert len(page["items"]) == 200 and page["has_more"]
    print(f"\nA14 entities={n['entities']} edges={n['edges']} load={load_s:.2f}s snapshot300={snap_s:.3f}s page200={page_s:.3f}s "
          f"snapshot_queries={snap['queries']}")


def test_trace_id_round_trips_and_writers_work_without_the_column(wg):
    async def go():
        async with wg.pool.acquire() as c:
            store = EventStore(c)
            await store.append(Event(event_id="t-1", schema_version=2, entity_type="collab", entity_id="log", event_type="x",
                                     source="hcn", source_sequence=1, actor={"id": "a"}, trace_id="trace-77"))
            assert await c.fetchval("SELECT trace_id FROM fabric_events WHERE event_id='t-1'") == "trace-77"
            back = (await store.read_stream("collab", "log"))[-1]
            assert back.trace_id == "trace-77"
            tr = c.transaction()
            await tr.start()
            await c.execute("DROP INDEX ix_fabric_events_trace_id; ALTER TABLE fabric_events DROP COLUMN trace_id")
            ok = await store.append(Event(event_id="t-2", schema_version=2, entity_type="collab", entity_id="log",
                                          event_type="x", source="hcn", source_sequence=2, actor={"id": "a"}))
            await tr.rollback()
            return ok
    assert wg.run(go()) is True                                # a database without 0033 still accepts events without a trace id


def _sql_blocks(path: Path):
    text = path.read_text()
    up = text                                  # the DOWN block is comments, so the whole file is the UP script
    down = "\n".join(re.sub(r"^--\s?", "", ln) for ln in text.split("-- DOWN:")[1].splitlines() if ln.startswith("--"))
    return up, down


@pytest.mark.parametrize("name", ["0033_event_trace_id.sql", "0034_work_graph.sql"])
def test_migrations_are_idempotent_and_the_down_script_undoes_exactly_the_up(wg, name):
    up, down = _sql_blocks(MIGRATIONS / name)
    assert re.findall(r"CREATE TABLE (?!IF NOT EXISTS)", up) == []
    assert re.findall(r"CREATE (UNIQUE )?INDEX (?!IF NOT EXISTS)", up) == []

    async def go():
        async with wg.pool.acquire() as c:
            tr = c.transaction()
            await tr.start()
            await c.execute((MIGRATIONS / name).read_text())            # re-run on the migrated database: no error, no change
            before = await c.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'wg\\_%'")
            await c.execute(down)
            after = await c.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'wg\\_%'")
            col = await c.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_name='fabric_events' AND column_name='trace_id'")
            await c.execute(up)                                           # and UP after DOWN restores it
            col2 = await c.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_name='fabric_events' AND column_name='trace_id'")
            back = await c.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'wg\\_%'")
            await tr.rollback()
            return before, after, col, col2, back
    before, after, col, col2, back = wg.run(go())
    if name.startswith("0034"):
        assert before == 10 and after == 0 and back == 10
    else:
        assert col == 0 and col2 == 1
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", up))
    dropped = set(re.findall(r"\w+", down.split("DROP TABLE IF EXISTS")[1])) if "DROP TABLE" in down else set()
    assert created <= dropped | {"TABLE", "IF", "EXISTS", "CASCADE"}
