"""Record the real HTTP shapes the host diagnostics page reads, as a fixture the browser adapter tests against.

Run it (no arguments, no network beyond a loopback socket):

    PYTHONPATH=. python tests/control_plane/record_diagnostics_events.py

It boots the real `ControlPlaneServer` over a real socket, ingests through the real `POST /api/v1/ingest`,
reads the three requests `#/diagnostics` makes, and writes `tests/fixtures/diagnostics_events_host.json`.
`tests/control_plane/test_diagnostics_events_contract.py` fails if this file's shapes drift from the live
server's, so the fixture cannot rot into a story about what the API used to return. Ids and timestamps are
replaced by stable values from a fixed clock; every other key and value is what the server answered.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "diagnostics_events_host.json"
STABLE_KEYS = ("event_id", "source_timestamp", "ingest_timestamp", "sequence")
# Everything the server mints per run: ULID-ish row ids, opaque page cursors, wall-clock instants. Pinned so
# re-running this recorder reproduces the fixture byte for byte — a fixture a test run can rewrite is a fixture
# whose contents nobody chose. Keys the server does NOT mint per run are deliberately absent: they are recorded
# as answered.
EPHEMERAL = {"last_event_id": "event_recorded_last", "next_cursor": "recorded_cursor", "before": "recorded_cursor",
             "created_at": "2026-11-14T02:03:00+00:00", "updated_at": "2026-11-14T02:03:00+00:00",
             "last_seen": "2026-11-14T02:03:00+00:00", "first_seen_at": "2026-11-14T02:00:00+00:00"}


def pin(value):
    """Replace per-run identifiers and instants anywhere in a response, leaving every other value as answered."""
    if isinstance(value, dict):
        return {key: (EPHEMERAL[key] if key in EPHEMERAL and value[key] is not None else pin(value[key]))
                for key in value}
    if isinstance(value, list):
        return [pin(item) for item in value]
    return value


def call(server, path):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def post(server, path, body):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.load(response)


def stable(event):
    """Keep the shape and the semantics, drop the run-specific identifiers.

    The id is derived from the event's own `sequence`, which the server assigns and which is stable across pages,
    so the same event carries the same recorded id in the limit=50 page and in the limit=1 page. Renumbering by
    position would make `event_recorded_0` a different event in each page. The instants are the sequence as
    minutes past the hour, which stays valid for the 60 events a stream would have to reach before it breaks —
    well past the four this recorder posts."""
    out = dict(event)
    out["event_id"] = f"event_recorded_{event['sequence']}"
    out["source_timestamp"] = f"2026-11-14T02:{event['sequence']:02d}:00+00:00"
    out["ingest_timestamp"] = f"2026-11-14T02:{event['sequence']:02d}:01+00:00"
    return out


def record():
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        # Three reports from hive and one from farm, so a host stream holds more than one row and a
        # limit=1 page has a real has_more/next_cursor. The server decides the event types these become
        # (HostRegistered then HostHeartbeat); the fixture records that, not what was sent.
        for host_id, event_type, payload in [
            ("hive", "host.health.failed", {"step": "gpu-probe", "message": "nvidia-smi timed out"}),
            ("hive", "host.unavailable", {"message": "agent unreachable"}),
            ("hive", "host.health.recovered", {"step": "gpu-probe"}),
            ("farm", "host.registration.retired", {"reason": "identity-less"}),
        ]:
            post(server, "/api/v1/ingest", {"events": [
                {"entity_type": "host", "entity_id": host_id, "event_type": event_type, "payload": payload}]})
        bare_status, bare_body = call(server, "/api/v1/events")
        page_status, page_body = call(server, "/api/v1/events?entity_type=host&entity_id=hive&limit=50")
        first_status, first_page = call(server, "/api/v1/events?entity_type=host&entity_id=hive&limit=1")
        hosts_status, hosts_body = call(server, "/api/v1/hosts")
        fixture = {
            "_provenance": {
                "generated_by": "tests/control_plane/record_diagnostics_events.py",
                "generated_with": "PYTHONPATH=. python tests/control_plane/record_diagnostics_events.py",
                "server": "herdr_engineering.control_plane.api.ControlPlaneServer over a loopback socket",
                "ids_and_timestamps": "event ids, page cursors and wall-clock instants replaced with stable values; every other key and value is the server's own",
            },
            "events_bare": {"status": bare_status, "body": pin(bare_body)},
            "events_host_hive": {"status": page_status, "body": pin({**page_body,
                                     "events": [stable(e) for e in page_body["events"]]})},
            "events_host_hive_limit_1": {"status": first_status, "body": pin({**first_page,
                                     "events": [stable(e) for e in first_page["events"]]})},
            "hosts": {"status": hosts_status, "body": pin(hosts_body)},
        }
        OUT.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
        return fixture
    finally:
        server.shutdown()


if __name__ == "__main__":
    made = record()
    print(f"wrote {OUT}")
    print(f"events_bare {made['events_bare']['status']} events_host_hive {made['events_host_hive']['status']} "
          f"hosts {made['hosts']['status']} events={len(made['events_host_hive']['body']['events'])}")
