"""The event-page and host-list contract the Hosts > Diagnostics tab reads, measured against a running control plane.

`packages/diagnostics-source.js` builds one events URL (`hostEventsPath`) and maps two answers
(`toTimelineRecord`, `toIssueRecords`). Those functions are only honest while the server keeps answering in the
shape they assume, so this test measures the shape from the server itself — no Docker, no network: the
`ControlPlaneServer` over an ephemeral port, like every other file in this directory.

Two failures this catches by name:
* the events endpoint stops requiring `entity_type`/`entity_id`, or starts accepting the bare path — the page
  would then be reading a URL the review of PR #796 called broken while the test still passed;
* an event or host key the adapter reads is renamed or dropped — the adapter would silently stop drawing
  values instead of failing, which is the failure mode `tests/fixtures/diagnostics_events_host.json` exists to
  make loud.

The key lists below are the keys `packages/diagnostics-source.js` reads; if that adapter starts reading
another one, add it here so the comparison stays meaningful.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "diagnostics_events_host.json"

# The keys toTimelineRecord reads: identity, instant, type, host, payload, session.
EVENT_KEYS = {"event_id", "event_type", "entity_id", "source_timestamp", "ingest_timestamp", "payload",
              "mission_id", "session_id"}
# The top-level keys readPage reads: rows, and whether more exist.
PAGE_KEYS = {"events", "has_more", "next_cursor", "latest_sequence", "last_event_id"}
# The keys toIssueRecords and the host grid read.
HOST_KEYS = {"host_id", "status", "last_seen", "problem_count", "problems"}
PROBLEM_KEYS = {"code", "message", "severity"}


def start():
    """The pattern the other files in this directory use: a real server on an ephemeral port, shut down in a
    finally, with no Docker and no patching of the HTTP layer."""
    server = ControlPlaneServer(ControlPlaneRepo())
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    return server


def _request(server, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def _seed(server):
    """Two hosts and one report each, through the public endpoints only."""
    _request(server, "POST", "/api/v1/ingest", {"events": [
        {"entity_type": "host", "entity_id": "hive", "event_type": "host.health.failed",
         "payload": {"step": "gpu-probe", "message": "nvidia-smi timed out"}}]})
    _request(server, "POST", "/api/v1/ingest", {"events": [
        {"entity_type": "host", "entity_id": "farm", "event_type": "host.registration.retired",
         "payload": {"reason": "identity-less"}}]})


def test_events_requires_both_entity_parameters():
    """The fact the page is built around: the bare events path is not a feed. A 200 here means the page's
    "the query lives in the adapter" comment has gone stale and the adapter should be re-read."""
    server = start()
    try:
          status, body = _request(server, "GET", "/api/v1/events")
          assert status == 400, f"the bare events path answered {status}: {body}"
          assert body["error"] == "entity_type and entity_id required", body
          # One parameter alone is still not enough.
          assert _request(server, "GET", "/api/v1/events?entity_type=host")[0] == 400


    finally:
      server.shutdown()
def test_host_event_page_is_the_shape_the_adapter_maps():
    server = start()
    try:
          _seed(server)
          status, body = _request(server, "GET", "/api/v1/events?entity_type=host&entity_id=hive&limit=1")
          assert status == 200, body
          assert set(body) == PAGE_KEYS, set(body)
          assert isinstance(body["events"], list) and body["events"], "a host that reported has at least one event row"
          for event in body["events"]:
              assert EVENT_KEYS <= set(event), set(event) - EVENT_KEYS
          assert isinstance(body["has_more"], bool)
          assert isinstance(body["latest_sequence"], int)


    finally:
      server.shutdown()
def test_event_page_limit_bounds():
    """`hostEventsPath` substitutes its own page size outside 1..200 rather than sending a request the server
    refuses; those bounds are the server's, measured here so a change on the server side fails this test."""
    server = start()
    try:
          _seed(server)
          for limit in ("1", "50", "200"):
              status, body = _request(server, "GET", f"/api/v1/events?entity_type=host&entity_id=hive&limit={limit}")
              assert status == 200, (limit, body)
          for limit in ("0", "201", "1000", "abc", "-5"):
              status, body = _request(server, "GET", f"/api/v1/events?entity_type=host&entity_id=hive&limit={limit}")
              assert status == 400, (limit, body)
              assert body["error"] == "limit must be an integer between 1 and 200", body


    finally:
      server.shutdown()
def test_host_list_is_the_shape_the_adapter_maps():
    server = start()
    try:
          _seed(server)
          status, body = _request(server, "GET", "/api/v1/hosts")
          assert status == 200, body
          assert set(body) == {"hosts"}, set(body)
          seen = {host["host_id"] for host in body["hosts"]}
          assert {"hive", "farm"} <= seen, seen
          for host in body["hosts"]:
              assert HOST_KEYS <= set(host), set(host) - HOST_KEYS
              for problem in host["problems"]:
                  assert PROBLEM_KEYS <= set(problem), set(problem) - PROBLEM_KEYS
          # The problems are what the issue list shows, so a severity that is not one of H02's four would be
          # counted invalid by normalizeIncident and the issue would disappear from the page.
          assert {problem["severity"] for host in body["hosts"] for problem in host["problems"]} <= {
              "critical", "error", "warning", "info"}, [
              problem["severity"] for host in body["hosts"] for problem in host["problems"]]


    finally:
      server.shutdown()
def test_recorded_fixture_matches_the_live_shape():
    """The JS tests replay tests/fixtures/diagnostics_events_host.json; if the server's shape moves away from
    what that file recorded, those tests keep passing against a shape that no longer exists. This fails first."""
    recorded = json.loads(FIXTURE.read_text())
    server = start()
    try:
          _seed(server)
          assert set(_request(server, "GET", "/api/v1/hosts")[1]) == set(recorded["hosts"]["body"]), (
              "the hosts envelope keys the fixture recorded no longer match the server's")
          _, live_page = _request(server, "GET", "/api/v1/events?entity_type=host&entity_id=hive&limit=50")
          assert set(live_page) == set(recorded["events_host_hive"]["body"]), (
              "the events page keys the fixture recorded no longer match the server's")
          live_keys = {key for event in live_page["events"] for key in event}
          fixture_keys = {key for event in recorded["events_host_hive"]["body"]["events"] for key in event}
          assert live_keys == fixture_keys, (live_keys - fixture_keys, fixture_keys - live_keys)


    finally:
      server.shutdown()
def test_fixture_records_how_it_was_generated():
    """An empty list in a fixture is a guess about an empty server; this file was written by a recorder that
    ran the real endpoints, and the file it names must still exist for that claim to mean anything."""
    provenance = json.loads(FIXTURE.read_text())["_provenance"]
    assert "ControlPlaneServer" in provenance["server"], provenance["server"]   # answers from the real server, not typed
    assert "replaced with stable values" in provenance["ids_and_timestamps"], provenance["ids_and_timestamps"]
    generator = Path(__file__).resolve().parents[2] / provenance["generated_by"]
    assert generator.is_file(), f"{provenance['generated_by']} is missing; the fixture's provenance is unverifiable"
