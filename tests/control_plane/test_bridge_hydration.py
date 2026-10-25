"""Events replayed from Postgres reach the repo with dict payloads, not the jsonb text asyncpg returns."""
import json

from herdr_engineering.control_plane import bridge
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def test_rebuild_events_decodes_jsonb_text_payloads():
    payload = {"repository": "o/r", "number": 3, "nested": {"a": [1, 2]}}
    rows = [
        {"event_id": "e1", "sequence": 0, "event_type": "CIFixRequested", "entity_type": "ci_fix",
         "entity_id": "cifix_1", "payload": json.dumps(payload), "source_timestamp": "2026-10-24T00:00:00+00:00"},
        {"event_id": "e2", "sequence": 1, "event_type": "CIFixCancelRequested", "entity_type": "ci_fix",
         "entity_id": "cifix_1", "payload": None, "source_timestamp": "2026-10-24T00:01:00+00:00"},
        {"event_id": "e3", "sequence": 2, "event_type": "Odd", "entity_type": "ci_fix",
         "entity_id": "cifix_1", "payload": "not json", "source_timestamp": "2026-10-24T00:02:00+00:00"},
    ]
    streams, order = bridge._rebuild_events(rows)
    events = streams["ci_fix|cifix_1"]
    assert events[0]["payload"] == payload
    assert events[1]["payload"] == {}
    assert events[2]["payload"] == {}
    repo = ControlPlaneRepo()
    repo.import_state({"events": streams, "event_order": order})
    assert repo.stream("ci_fix", "cifix_1")[0]["payload"]["repository"] == "o/r"
