"""The vision-delegation wire contract with Pi, pinned by envelopes Pi's real LineageEmitter produced.

``fixtures/vision_lineage_envelopes.json`` was captured from pi-engineering-runtime's ``emitVisionLineage`` driving the real
``createLineageFromConnection`` emitter (outbox + ControlPlaneIngest) with a recording fake fetch; see
docs/evidence/vision-delegation-dashboard.md. The envelopes are replayed as-is through this control plane's HTTP ingest and read
back through the same ``/api/v1/handoffs`` endpoints the Handoffs page uses.

``fixtures/vision_lineage_api_rows.json`` (what the JS contract tests in packages/vision-lineage-contract.test.js render) is the
live output of that replay: the page rows, the single-row details and the overview attention set. The default run COMPARES the
live output with the file (receipt times masked) and never writes it; ``HERDR_REGEN_VISION_FIXTURES=1`` REWRITES the file from the
live output instead, so the fixture is refreshed here, in the repo, never by an outside script.
"""
import json
import os
import pathlib
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "vision_lineage_envelopes.json"
API_ROWS = pathlib.Path(__file__).parent / "fixtures" / "vision_lineage_api_rows.json"
REGEN = os.environ.get("HERDR_REGEN_VISION_FIXTURES") == "1"
# The control plane's receipt times of the replay: the only fields that differ from run to run.
VOLATILE = {"created_at", "updated_at", "first_seen_at", "last_seen_at"}
PI_KEY, OTHER_KEY = "pi-" + "ingest-key", "dash-" + "operator-key"
# Pi's default visibility is `private`: only the key that wrote a node reads it (graph.can_see), so the reader here is Pi's key.
ATTENTION = {"ok": None, "partial": "vision result partial", "failed": "failed", "timeout": "failed"}


@pytest.fixture
def server():
    instance = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0,
                                  api_keys={PI_KEY: "ingest", OTHER_KEY: "operator"})
    threading.Thread(target=instance.serve, daemon=True).start()
    instance.wait_until_ready()
    yield instance
    instance.shutdown()


def _call(port, key, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_vision_delegation_events_from_pi_reach_the_handoffs_api(server):
    fixture = json.loads(FIXTURE.read_text())
    envelopes, delegations = fixture["envelopes"], fixture["delegations"]
    assert len(delegations) == 5 and all(e["schema_version"] == 2 for e in envelopes)

    refused = [(e["source_sequence"], s, b) for e in envelopes for s, b in [_call(server.port, PI_KEY, "/api/v1/ingest", e)] if s != 200]
    assert refused == [], f"the ingest refused Pi's envelopes as-is: {refused}"
    # Pi's outbox re-sends after a lost ack: the same envelopes again are accepted and change nothing.
    assert {_call(server.port, PI_KEY, "/api/v1/ingest", e)[0] for e in envelopes} == {200}

    status, page = _call(server.port, PI_KEY, "/api/v1/handoffs?limit=50")
    assert status == 200
    rows = {r["handoff_id"]: r for r in page["handoffs"] if r["handoff_id"].startswith("handoff:vision-")}
    assert len(rows) == 9                                       # 4 closed round trips x 2 rows + 1 open delegation's out row

    for d in delegations:
        rid, outcome = d["request_id"], d["outcome"]
        out, back = rows.get(f"handoff:vision-{rid}-out"), rows.get(f"handoff:vision-{rid}-back")
        assert out is not None, d["name"]
        assert (out["cause"], out["vision"], out["model_change"], out["state"]) == \
            ("vision_delegate", {"request_id": rid, "phase": "out", "outcome": "ok"}, "switched", "accepted")
        assert (out["from_model"], out["to_model"]) == ("local/qwen3.6-35b-a3b", "google/gemini-3-flash")
        assert out["attention"] is False and out["attention_reason"] is None
        if outcome == "open":
            assert back is None
            continue
        assert back is not None, d["name"]
        assert (back["cause"], back["vision"], back["model_change"]) == \
            ("vision_return", {"request_id": rid, "phase": "back", "outcome": outcome}, "switched")
        assert back["state"] == ("completed" if outcome in ("ok", "partial") else "failed")
        assert back["attention_reason"] == ATTENTION[outcome] and back["attention"] is (ATTENTION[outcome] is not None)
        # the back row runs vision session -> text session, the out row text -> vision
        assert (out["source"]["session_id"], out["target"]["session_id"]) == \
            (back["target"]["session_id"], back["source"]["session_id"]) == (f"pi-text-session-{d['name']}", f"pi-vision-session-{d['name']}")

    partial = next(d["request_id"] for d in delegations if d["outcome"] == "partial")
    status, detail = _call(server.port, PI_KEY, f"/api/v1/handoffs/handoff%3Avision-{partial}-back")
    assert status == 200 and detail["vision"]["outcome"] == "partial" and detail["handoff_kind"] == "vision_partial"
    status, ov = _call(server.port, PI_KEY, "/api/v1/handoffs/overview")
    flagged = {a["handoff_id"] for a in ov["attention"] if a["handoff_id"].startswith("handoff:vision-")}
    assert flagged == {f"handoff:vision-{d['request_id']}-back" for d in delegations if d["outcome"] in ("partial", "failed", "timeout")}
    assert all(a.get("vision") for a in ov["attention"] if a["handoff_id"] in flagged)

    # Pi's default visibility is private: another key, even an operator, sees none of these rows.
    status, other = _call(server.port, OTHER_KEY, "/api/v1/handoffs?limit=50")
    assert status == 200 and not [r for r in other["handoffs"] if r["handoff_id"].startswith("handoff:vision-")]


def _vision(row):
    return row["handoff_id"].startswith("handoff:vision-")


def _masked(value):
    if isinstance(value, dict):
        return {k: "<time>" if k in VOLATILE else _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


def test_the_api_rows_fixture_the_dashboard_tests_render_is_the_live_api_output(server):
    """Pins tests/control_plane/fixtures/vision_lineage_api_rows.json to the live API output so the JS side never passes on a
    stale shape. Set HERDR_REGEN_VISION_FIXTURES=1 to rewrite the file from the live output instead of comparing."""
    for e in json.loads(FIXTURE.read_text())["envelopes"]:
        assert _call(server.port, PI_KEY, "/api/v1/ingest", e)[0] == 200
    status, page = _call(server.port, PI_KEY, "/api/v1/handoffs?limit=50")
    assert status == 200
    handoffs = [r for r in page["handoffs"] if _vision(r)]
    details = []
    for r in handoffs:
        status, detail = _call(server.port, PI_KEY, "/api/v1/handoffs/" + urllib.parse.quote(r["handoff_id"], safe=""))
        assert status == 200
        details.append(detail)
    status, ov = _call(server.port, PI_KEY, "/api/v1/handoffs/overview")
    assert status == 200
    live = {"handoffs": handoffs, "details": details, "overview_attention": [a for a in ov["attention"] if _vision(a)]}
    stored = json.loads(API_ROWS.read_text())
    if REGEN:
        API_ROWS.write_text(json.dumps({"_about": stored["_about"], **live}, indent=2) + "\n")
        pytest.skip(f"HERDR_REGEN_VISION_FIXTURES=1: rewrote {API_ROWS.name} from the live API output")
    for name, rows in live.items():
        assert _masked(rows) == _masked(stored[name]), (
            f"{API_ROWS.name}[{name!r}] drifted from the live API output; rerun with HERDR_REGEN_VISION_FIXTURES=1 to refresh it")
