"""Mission records derive telemetry_state (live/stale/unknown) from the real
collection-freshness of the bounded mission-activity read model (issue #28).

The derivation reflects transport freshness, never fabricated worker health: a
mission with no collector is ``unknown``, not stalled. It reuses the freshness
the activity store already computes and never invents a heartbeat.
"""

import copy
from datetime import UTC, datetime

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1_800_000_000
KEY = "pi:MSN-one"


def stamp(epoch=NOW):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def envelope(**overrides):
    payload = {
        "contract_version": 1,
        "host_id": "bender",
        "instance_id": "i1",
        "session_id": "s1",
        "observed_at": stamp(),
        "snapshot_at": stamp(),
        "mission_revision": 3,
        "connection": "connected",
        "mission": {"id": "MSN-one", "title": "Fix login", "status": "active"},
        "activity": {
            "health": "healthy",
            "currentObjective": "Run tests",
            "lastHeartbeatAt": stamp(NOW - 100),
        },
    }
    payload.update(overrides)
    return {
        "entity_type": "mission_activity",
        "entity_id": KEY,
        "event_type": "mission_activity.snapshot",
        "source": "pi",
        "payload": payload,
    }


class TestMissionTelemetryState:
    def test_mission_with_no_collector_is_unknown(self):
        repo = ControlPlaneRepo(now=lambda: NOW)
        repo.upsert_mission("MSN-one", title="No collector", source="herdr")
        rec = repo.get_mission("MSN-one")
        assert rec["telemetry_state"] == "unknown"
        assert [m["telemetry_state"] for m in repo.list_missions()] == ["unknown"]

    def _pi_mission(self, repo, title="Fix login"):
        repo.upsert_mission("MSN-one", title=title, source="pi")

    def test_live_collection_is_live(self):
        repo = ControlPlaneRepo(now=lambda: NOW)
        self._pi_mission(repo)
        IngestService(repo).ingest(envelope())
        assert repo.get_mission("pi:MSN-one")["telemetry_state"] == "live"

    def test_stale_collection_is_stale(self):
        clock = [NOW]
        repo = ControlPlaneRepo(now=lambda: clock[0])
        self._pi_mission(repo)
        IngestService(repo).ingest(envelope())
        clock[0] += 31
        assert repo.get_mission("pi:MSN-one")["telemetry_state"] == "stale"

    def test_disconnected_collection_is_stale(self):
        clock = [NOW]
        repo = ControlPlaneRepo(now=lambda: clock[0])
        self._pi_mission(repo)
        IngestService(repo).ingest(envelope())
        clock[0] += 130
        assert repo.get_mission("pi:MSN-one")["telemetry_state"] == "stale"

    def test_explicit_disconnected_connection_is_stale(self):
        repo = ControlPlaneRepo(now=lambda: NOW)
        self._pi_mission(repo)
        IngestService(repo).ingest(
            envelope(connection="disconnected", activity={}, snapshot_at=None))
        assert repo.get_mission("pi:MSN-one")["telemetry_state"] == "stale"

    def test_updating_connection_is_live(self):
        repo = ControlPlaneRepo(now=lambda: NOW)
        self._pi_mission(repo)
        IngestService(repo).ingest(
            envelope(connection="updating", activity={}, snapshot_at=None))
        assert repo.get_mission("pi:MSN-one")["telemetry_state"] == "live"

    def test_herdr_mission_ignores_pi_activity_key(self):
        # A herdr mission and a pi mission share the raw id but not the row;
        # the herdr record must not inherit the pi collector's freshness.
        repo = ControlPlaneRepo(now=lambda: NOW)
        repo.upsert_mission("MSN-one", title="Herdr one", source="herdr")
        self._pi_mission(repo)
        IngestService(repo).ingest(envelope())
        assert repo.get_mission("MSN-one")["telemetry_state"] == "unknown"
        assert repo.get_mission("pi:MSN-one")["telemetry_state"] == "live"

    def test_list_missions_carries_state_per_row(self):
        repo = ControlPlaneRepo(now=lambda: NOW)
        repo.upsert_mission("a", title="A", source="herdr")
        repo.upsert_mission("b", title="B", source="pi")
        IngestService(repo).ingest(
            {
                "entity_type": "mission_activity",
                "entity_id": "pi:b",
                "event_type": "mission_activity.snapshot",
                "source": "pi",
                "payload": {
                    "contract_version": 1,
                    "host_id": "h",
                    "instance_id": "i",
                    "session_id": "s",
                    "observed_at": stamp(),
                    "snapshot_at": stamp(),
                    "mission_revision": 1,
                    "connection": "connected",
                    "mission": {"id": "b", "title": "B", "status": "active"},
                    "activity": {"health": "healthy"},
                },
            }
        )
        by_id = {m["mission_key"]: m["telemetry_state"] for m in repo.list_missions()}
        assert by_id["a"] == "unknown"
        assert by_id["pi:b"] == "live"

    def test_activity_ingestion_does_not_mutate_mission_record(self):
        repo = ControlPlaneRepo(now=lambda: NOW)
        repo.upsert_mission("MSN-one", title="Stable", source="herdr")
        before = copy.deepcopy(repo.get_mission("MSN-one"))
        IngestService(repo).ingest(envelope())
        after = repo.get_mission("MSN-one")
        assert after["mission_id"] == before["mission_id"]
        assert after["title"] == before["title"]
        # only telemetry_state is added; no lifecycle fields are changed
        assert after["telemetry_state"] == "unknown"
