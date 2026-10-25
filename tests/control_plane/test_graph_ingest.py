"""P2-P4 live-ingest wiring tests.

Verifies GraphIngestAdapter.emit() builds clean lineage envelopes, and that the
opt-in IngestService hook routes mission/session/repository events into the
running graph through the real ingest path (admit + persist + apply), so the
lifecycle shows up in GraphProjection.snapshot().
"""
import pytest

from herdr_engineering.control_plane.graph import GraphProjection
from herdr_engineering.control_plane.graph_ingest import GraphIngestAdapter
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class _FakeRepo:
    """Minimal repo exposing graph for adapter emit tests."""

    def __init__(self):
        self.graph = GraphProjection()


def make_ingest(system_graph=True):
    repo = ControlPlaneRepo()
    return IngestService(repo, system_graph=system_graph), repo


PRINCIPAL = {"id": "key:w", "kind": "agent", "authenticated_by": "api_key"}


def ingest_lifecycle(svc, entity_type, entity_id, event_type, payload, n=0):
    """Feed a lifecycle event through the REAL dispatch path: a valid lifecycle
    envelope to svc.ingest, which routes to _dispatch and (when enabled) the
    adapter re-enters ingest with lineage envelopes. ``n`` disambiguates the
    envelope's source_sequence across repeated calls."""
    svc.ingest({"schema_version": 2, "event_id": f"w-{n}", "source": "w",
                "source_sequence": n + 1, "entity_type": entity_type,
                "entity_id": entity_id, "event_type": event_type,
                "payload": payload, "visibility_scope": "org",
                "occurred_at": "2026-10-22T10:00:00Z"}, PRINCIPAL)


class TestAdapterEmit:
    def test_emit_mission_builds_lineage_envelope(self):
        a = GraphIngestAdapter(_FakeRepo())
        envs = a.emit("mission", "m1", "mission.created",
                      {"stage": "plan", "progress": 0.4})
        assert envs
        assert all(e["entity_type"] == "lineage" for e in envs)
        assert all(e["schema_version"] == 2 for e in envs)
        # clean payloads carry no server-side `_envelope` metadata
        assert all(not k.startswith("_")
                   for e in envs for k in e["payload"])
        assert any(e["event_type"] == "node.upsert"
                   and e["payload"]["node_id"] == "mission:m1" for e in envs)

    def test_emit_repository_builds_dependency_edge(self):
        a = GraphIngestAdapter(_FakeRepo())
        envs = a.emit("repository", "acme/api", "repository.created",
                      {"depends_on": ["acme/lib"]})
        assert any(e["event_type"] == "node.upsert"
                   and e["payload"]["node_id"] == "repository:acme/api" for e in envs)
        assert any(e["event_type"] == "edge.add"
                   and e["payload"]["edge_type"] == "dependency" for e in envs)

    def test_emit_unknown_entity_returns_empty(self):
        a = GraphIngestAdapter(_FakeRepo())
        assert a.emit("host", "h1", "host.heartbeat", {}) == []

    def test_emit_orphan_span_never_requests_none(self):
        a = GraphIngestAdapter(_FakeRepo())
        envs = a.emit("span", "s0", "span.created", {"request_id": None})
        # no envelope referencing request:None
        assert not any(e["payload"].get("to_id") == "request:None" for e in envs)


class TestIngestWiring:
    def test_mission_lands_in_graph_when_enabled(self):
        svc, repo = make_ingest(system_graph=True)
        ingest_lifecycle(svc, "mission", "m1", "mission.created",
                         {"stage": "plan"}, n=0)
        snap = repo.graph.snapshot()
        assert "mission:m1" in snap["nodes"]

    def test_repository_dependency_lands_in_graph_when_enabled(self):
        svc, repo = make_ingest(system_graph=True)
        ingest_lifecycle(svc, "repository", "acme/api", "repository.created",
                         {"depends_on": ["acme/lib"]}, n=0)
        snap = repo.graph.snapshot()
        assert "repository:acme/api" in snap["nodes"]
        triples = {(e.get("edge_type"), e.get("from_id"), e.get("to_id"))
                   for e in snap["edges"].values()}
        assert ("dependency", "repository:acme/api", "repository:acme/lib") in triples

    def test_graph_unaffected_when_disabled(self):
        svc, repo = make_ingest(system_graph=False)
        assert svc.system_graph is None
        svc.ingest({"schema_version": 2, "event_id": "w-1", "source": "w",
                    "source_sequence": 1, "entity_type": "mission",
                    "entity_id": "m1", "event_type": "mission.created",
                    "payload": {}, "visibility_scope": "org",
                    "occurred_at": "2026-10-22T10:00:00Z"}, PRINCIPAL)
        assert repo.graph.snapshot()["nodes"] == {}

    def test_session_lands_in_graph_when_enabled(self):
        svc, repo = make_ingest(system_graph=True)
        ingest_lifecycle(svc, "session", "s1", "session.started",
                         {"mission_id": "m1", "status": "active"}, n=0)
        snap = repo.graph.snapshot()
        assert "session:s1" in snap["nodes"]

    def test_multiple_lifecycle_events_no_sequence_collision(self):
        """Consecutive lifecycle events through the real dispatch path must not
        collide on the (actor, source) ledger sequence domain."""
        svc, repo = make_ingest(system_graph=True)
        for n, eid in enumerate(("m1", "m2", "m3")):
            ingest_lifecycle(svc, "mission", eid, "mission.created",
                             {"stage": "plan"}, n=n)
        snap = repo.graph.snapshot()
        assert "mission:m1" in snap["nodes"]
        assert "mission:m2" in snap["nodes"]
        assert "mission:m3" in snap["nodes"]

    def test_mission_completed_maps_state(self):
        """A dotted mission.completed lifecycle event maps to graph state done."""
        svc, repo = make_ingest(system_graph=True)
        ingest_lifecycle(svc, "mission", "m1", "mission.completed", {}, n=0)
        assert repo.graph.snapshot()["nodes"]["mission:m1"]["state"] == "done"
