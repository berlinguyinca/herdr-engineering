"""Memory Explorer endpoints (P04). Mode: MOCKED adapter; no herdr-memory CLI or Hindsight is contacted."""
import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.memory_explorer import (
    MemoryAdapter,
    MemoryNotFound,
    MemoryUnavailable,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

SECRET_TEXT = "deploy token sk-abcdefghijklmnopqrstuvwxyz123456 for the fleet"


def rec(mid, bank, text="a fact", **kw):
    return {"memory_id": mid, "bank_id": bank, "revision": 2, "kind": "fact", "status": "verified", "text": text,
            "provenance": {"session_id": "sess-1", "mission_id": "m-1", "harness_id": "claude", "host_id": "bender",
                           "commit_sha": "abc123", "evidence_refs": ["handoff:h-9"], "secret_extra": "x"}, **kw}


class FakeAdapter:
    def __init__(self, records=None, fail=False):
        self.records, self.fail, self.calls = records or [], fail, []

    def recall(self, query, *, scope=None, project=None, max_results=200):
        self.calls.append(("recall", scope, project))
        if self.fail:
            raise MemoryUnavailable("down")
        return {"results": list(self.records), "degraded": False, "offline": False, "timing_ms": 3}

    def get(self, memory_id):
        self.calls.append(("get", memory_id))
        if self.fail:
            raise MemoryUnavailable("down")
        for r in self.records:
            if r["memory_id"] == memory_id:
                return r
        raise MemoryNotFound()

    def status(self):
        if self.fail:
            raise MemoryUnavailable("down")
        return {"healthy": True, "outbox_pending": 2, "dead_letter": 0, "server_version": "1.2", "contract": 1}


def key_id(key):
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:12]


def start(adapter, viewers, enabled=True, keys=None):
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys=keys or {"v1": "viewer", "v2": "viewer", "ing": "ingest", "op": "operator"},
                                memory_adapter=adapter,
                                memory_config={"enabled": enabled, "viewers": viewers})
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    return server


def get(server, path, key="v1"):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", headers={"X-API-Key": key} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


@pytest.fixture
def server():
    records = [rec("m1", "alpha-facts", SECRET_TEXT), rec("m2", "beta-facts"), rec("m3", "herdr-global"),
               rec("m4", "rakazo-facts"), rec("m5", "personal-notes"), rec("m6", "alpha-mission-m-1")]
    s = start(FakeAdapter(records), {key_id("v1"): {"projects": ["alpha"], "scopes": ["global"]},
                                     key_id("v2"): {"projects": "*", "scopes": ["global", "infra"]},
                                     key_id("ing"): {"projects": "*", "scopes": ["global"]}})
    yield s
    s.shutdown()


def ids(body):
    return sorted(r["memory_id"] for r in body["results"])


def test_requires_auth_and_feature_flag():
    s = start(FakeAdapter(), {}, enabled=False)
    try:
        assert get(s, "/api/v1/memory/search?q=x", key=None)[0] == 401
        assert get(s, "/api/v1/memory/status")[0] == 404
        assert "memory_v1" not in get(s, "/api/v1/health")[1].get("capabilities", [])
    finally:
        s.shutdown()


def test_unmapped_and_wrong_role_denied_by_default(server):
    assert get(server, "/api/v1/memory/search?q=x", key="op")[0] == 403      # authenticated but unmapped
    assert get(server, "/api/v1/memory/search?q=x", key="ing")[0] == 403     # mapped, but ingest role cannot read
    assert get(server, "/api/v1/memory/m1", key="op")[0] == 403
    assert get(server, "/api/v1/memory/status", key="op")[0] == 403


def test_search_filters_to_authorized_banks_only(server):
    status, body = get(server, "/api/v1/memory/search?q=fact")
    assert status == 200
    assert ids(body) == ["m1", "m3", "m6"]        # alpha project + mission + global; never beta, rakazo or personal
    assert body["untrusted"] is True


def test_star_viewer_still_cannot_reach_personal_banks(server):
    status, body = get(server, "/api/v1/memory/search?q=fact", key="v2")
    assert ids(body) == ["m1", "m2", "m3", "m6"]


def test_forbidden_filters_do_not_leak(server):
    assert get(server, "/api/v1/memory/search?q=x&project=beta")[0] == 403
    assert get(server, "/api/v1/memory/search?q=x&project=rakazo", key="v2")[0] == 403
    assert get(server, "/api/v1/memory/search?q=x&scope=infra")[0] == 403
    assert get(server, "/api/v1/memory/search?q=x&scope=bogus")[0] == 400


def test_detail_unreadable_equals_missing(server):
    assert get(server, "/api/v1/memory/m1")[0] == 200
    forbidden, missing = get(server, "/api/v1/memory/m2"), get(server, "/api/v1/memory/nope")
    assert forbidden == missing == (404, {"error": "not found"})
    assert get(server, "/api/v1/memory/m4", key="v2")[0] == 404
    assert get(server, "/api/v1/memory/-rf")[0] == 404                       # not even passed to the CLI
    assert ("get", "-rf") not in server.memory.adapter().calls


def test_text_is_redacted_and_provenance_whitelisted(server):
    _, body = get(server, "/api/v1/memory/m1")
    memory = body["memory"]
    assert "sk-abcdefghij" not in json.dumps(body) and "[REDACTED]" in memory["text"]
    assert "secret_extra" not in memory["provenance"]
    assert memory["provenance"]["session_id"] == "sess-1" and memory["provenance"]["evidence_refs"] == ["handoff:h-9"]
    assert body["retrieval_history"]["state"] == "unknown" and body["retrieval_history"]["events"] == []
    assert memory["revisions"] is None                                         # CLI gave none: nothing invented


def test_filters_and_paging(server):
    _, body = get(server, "/api/v1/memory/search?q=x&host=bender&harness=claude&limit=1&offset=1")
    assert len(body["results"]) == 1 and body["has_more"] is True and body["offset"] == 1
    assert get(server, "/api/v1/memory/search?q=x&host=other")[1]["results"] == []
    assert get(server, "/api/v1/memory/search?q=x&kind=decision")[1]["results"] == []
    assert get(server, "/api/v1/memory/search?q=x&status=superseded")[1]["note"]
    assert get(server, "/api/v1/memory/search")[0] == 400
    assert get(server, "/api/v1/memory/search?q=x&limit=abc")[0] == 400


def test_degraded_when_service_down():
    s = start(FakeAdapter(fail=True), {key_id("v1"): {"projects": ["alpha"], "scopes": []}})
    try:
        for path in ("/api/v1/memory/search?q=x", "/api/v1/memory/m1", "/api/v1/memory/status"):
            status, body = get(s, path)
            assert status == 503 and body["error"] == "memory service unavailable"
    finally:
        s.shutdown()


def test_missing_cli_is_honestly_unavailable_not_sample_data():
    def runner(argv, timeout):
        raise FileNotFoundError(argv[0])
    s = start(MemoryAdapter(runner=runner), {key_id("v1"): {"projects": ["alpha"], "scopes": []}})
    try:
        status, body = get(s, "/api/v1/memory/search?q=x")
        assert status == 503 and "results" not in body
    finally:
        s.shutdown()


def test_status_reports_service_and_unknown_integrations(server):
    status, body = get(server, "/api/v1/memory/status")
    assert status == 200 and body["healthy"] is True and body["outbox_pending"] == 2 and body["contract"] == 1
    assert body["integrations"]["state"] == "unknown" and body["integrations"]["rows"] == []


def test_audit_events_are_metadata_only(server):
    get(server, "/api/v1/memory/search?q=very-private-query")
    get(server, "/api/v1/memory/m1")
    get(server, "/api/v1/memory/search?q=x&project=beta")
    events = server.repo.events_of_type("memory_explorer")
    assert {e["payload"]["outcome"] for e in events} >= {"ok", "forbidden"}
    blob = json.dumps(events)
    assert "very-private-query" not in blob and "sk-abcdefghij" not in blob and "a fact" not in blob
    assert any(e["payload"].get("memory_id") == "m1" for e in events)


def test_adapter_parses_contract_json_and_rejects_garbage():
    out = {"results": [], "degraded": False}
    a = MemoryAdapter(runner=lambda argv, t: (0, json.dumps(out)))
    assert a.recall("q", scope="global")["results"] == []
    for run in (lambda argv, t: (0, "not json"), lambda argv, t: (1, "{}"), lambda argv, t: (0, "[]")):
        with pytest.raises(MemoryUnavailable):
            MemoryAdapter(runner=run).recall("q")
    with pytest.raises(MemoryNotFound):
        MemoryAdapter(runner=lambda argv, t: (3, '{"error": "not_found"}')).get("m1")
    with pytest.raises(MemoryUnavailable):
        MemoryAdapter(runner=lambda argv, t: (0, '{"contract": 2}')).status()
