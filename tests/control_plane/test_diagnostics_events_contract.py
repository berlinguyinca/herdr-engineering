"""The event-page and host-list contract the Hosts > Diagnostics tab reads, measured against a running server.

`packages/diagnostics-source.js` builds one events URL (`hostEventsPath`) and maps two answers
(`toTimelineRecord`, `toIssueRecords`). Those mappings are only honest while the server keeps answering in the
shape they assume, so this file measures that shape from the server itself — no Docker, no network: the
`ControlPlaneServer` over an ephemeral loopback port, the pattern every other file here uses.

Two failures this catches by name:

* the events endpoint stops requiring `entity_type`/`entity_id`, or starts accepting the bare path — the page
  would then be built around a URL its reviewers called broken, and the JS tests would still pass;
* an event or host key the adapter reads is renamed or dropped — the adapter would quietly stop drawing values.

The key lists below are exactly the keys `packages/diagnostics-source.js` reads. If that adapter starts reading
another one, add it here, so the comparison keeps meaning "the adapter's assumptions still hold".

These tests assert key sets and enumerated values, not particular row contents: row contents are the fixture's
job (`tests/fixtures/diagnostics_events_host.json`, replayed by the JS tests), and this file's job is to fail
when the live shape no longer matches what that fixture recorded.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "diagnostics_events_host.json"

# Keys toTimelineRecord reads: identity, instants, type, host, payload, session.
EVENT_KEYS = {"event_id", "event_type", "entity_id", "source_timestamp", "ingest_timestamp",
              "payload", "mission_id", "session_id"}
# Top-level keys readPage reads: the rows, and whether more exist.
PAGE_KEYS = {"events", "has_more", "next_cursor", "latest_sequence", "last_event_id"}
# Keys the host grid and toIssueRecords read.
HOST_KEYS = {"host_id", "status", "last_seen", "problem_count", "problems"}
PROBLEM_KEYS = {"code", "message", "severity"}
# H02's severity vocabulary: anything else would be counted invalid by normalizeIncident, and the issue would
# vanish from the page instead of being shown as rejected.
SEVERITIES = {"critical", "error", "warning", "info"}


def start():
    """A real control plane on an ephemeral port; the caller shuts it down in a finally."""
    server = ControlPlaneServer(ControlPlaneRepo())
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    return server


def call(server, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}", data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def seed(server):
    """Two hosts, each reporting through the public endpoint only. The server decides the event types its
    ingest turns these into; nothing here asserts what it chose, only that a host that reported has a stream."""
    for host_id, event_type, payload in [
        ("hive", "host.health.failed", {"step": "gpu-probe", "message": "nvidia-smi timed out"}),
        ("farm", "host.registration.retired", {"reason": "identity-less"}),
    ]:
        call(server, "/api/v1/ingest", {"events": [
            {"entity_type": "host", "entity_id": host_id, "event_type": event_type, "payload": payload}]})


def test_events_requires_both_entity_parameters():
    """The fact the page is built around: the bare events path is not a feed."""
    server = start()
    try:
        status, body = call(server, "/api/v1/events")
        assert status == 400, f"the bare events path answered {status}: {body}"
        assert body["error"] == "entity_type and entity_id required", body
        assert call(server, "/api/v1/events?entity_type=host")[0] == 400, "one parameter alone is not enough"
    finally:
        server.shutdown()


def test_host_event_page_is_the_shape_the_adapter_maps():
    server = start()
    try:
        seed(server)
        status, body = call(server, "/api/v1/events?entity_type=host&entity_id=hive&limit=1")
        assert status == 200, body
        assert set(body) == PAGE_KEYS, set(body)
        assert isinstance(body["events"], list) and body["events"], "a host that reported has at least one row"
        assert isinstance(body["has_more"], bool)
        assert isinstance(body["latest_sequence"], int)
        for event in body["events"]:
            assert EVENT_KEYS <= set(event), set(event) - EVENT_KEYS
    finally:
        server.shutdown()


def test_event_page_limit_bounds():
    """`hostEventsPath` substitutes its own page size outside these bounds rather than sending a request the
    server refuses. The bounds are the server's, measured here so a change there fails this test first."""
    server = start()
    try:
        seed(server)
        for limit in ("1", "50", "200"):
            status, body = call(server, f"/api/v1/events?entity_type=host&entity_id=hive&limit={limit}")
            assert status == 200, (limit, body)
        for limit in ("0", "201", "1000", "abc", "-5"):
            status, body = call(server, f"/api/v1/events?entity_type=host&entity_id=hive&limit={limit}")
            assert status == 400, (limit, body)
            assert body["error"] == "limit must be an integer between 1 and 200", body
    finally:
        server.shutdown()


def test_host_list_is_the_shape_the_adapter_maps():
    server = start()
    try:
        seed(server)
        status, body = call(server, "/api/v1/hosts")
        assert status == 200, body
        assert set(body) == {"hosts"}, set(body)
        assert {"hive", "farm"} <= {host["host_id"] for host in body["hosts"]}, body
        for host in body["hosts"]:
            assert HOST_KEYS <= set(host), set(host) - HOST_KEYS
            assert isinstance(host["problem_count"], int)
            for problem in host["problems"]:
                assert PROBLEM_KEYS <= set(problem), set(problem) - PROBLEM_KEYS
                assert problem["severity"] in SEVERITIES, problem
    finally:
        server.shutdown()


def test_recorded_fixture_matches_the_live_shape():
    """The JS tests replay tests/fixtures/diagnostics_events_host.json. If the server's shape moves away from
    what that file recorded, those tests would keep passing against a shape that no longer exists; this fails."""
    recorded = json.loads(FIXTURE.read_text())
    server = start()
    try:
        seed(server)
        assert set(call(server, "/api/v1/hosts")[1]) == set(recorded["hosts"]["body"]), \
            "the hosts envelope keys the fixture recorded no longer match the server's"
        _, live_page = call(server, "/api/v1/events?entity_type=host&entity_id=hive&limit=50")
        fixture_page = recorded["events_host_hive"]["body"]
        assert set(live_page) == set(fixture_page), "the events page keys no longer match the fixture's"
        live_keys = {key for event in live_page["events"] for key in event}
        fixture_keys = {key for event in fixture_page["events"] for key in event}
        assert live_keys == fixture_keys, (live_keys - fixture_keys, fixture_keys - live_keys)
        assert call(server, "/api/v1/events")[1] == recorded["events_bare"]["body"], \
            "the 400 body the fixture recorded is no longer what the server answers"
    finally:
        server.shutdown()


def test_fixture_records_how_it_was_generated():
    """An empty list in a fixture is a guess about an empty server. This file was written by a recorder that ran
    the real endpoints, so its provenance block must name a generator that still exists, and every recorded row
    must carry the id its own sequence derives — the property that keeps one event the same event in both the
    limit=50 page and the limit=1 page."""
    fixture = json.loads(FIXTURE.read_text())
    provenance = fixture["_provenance"]
    assert "ControlPlaneServer" in provenance["server"], provenance["server"]
    assert "replaced with stable values" in provenance["ids_and_timestamps"], provenance["ids_and_timestamps"]
    generator = ROOT / provenance["generated_by"]
    assert generator.is_file(), f"{provenance['generated_by']} is missing; the fixture's provenance is unverifiable"
    for page in ("events_host_hive", "events_host_hive_limit_1"):
        for event in fixture[page]["body"]["events"]:
            assert event["event_id"] == f"event_recorded_{event['sequence']}", (page, event["event_id"])
