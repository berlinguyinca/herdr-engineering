"""Pi's lineage handoffs on the Handoffs page: normalised rows, principal scoping, model change, and the merged API."""
import http.client
import json
import threading

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.handoff_lineage import row_from_node, rows_from_nodes
from herdr_engineering.control_plane.handoffs import HandoffRegistry
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_graph import Feed, _AnyKey

NOW = "2026-10-30T12:00:00+00:00"


def node(hid, state="acknowledged", **attrs):
    return {"id": f"handoff:{hid}", "node_type": "handoff", "label": None, "state": state, "state_reason": None,
            "attrs": {"handoff_kind": "context", "from_session_id": "session:s1", "to_session_id": "session:s2", **attrs},
            "visibility_scope": "org"}


def test_a_context_handoff_on_the_same_model_reads_as_same_and_quiet():
    row = row_from_node(node("h1", cause="context_pressure", from_model="qwen3.8-27b-q8", to_model="metabolomics/qwen3.8-27b-q8",
                             checkpoint_sha="abc1234"))
    assert row["origin"] == "lineage" and row["state"] == "accepted" and row["model_change"] == "same"
    assert row["source"]["session_id"] == "s1" and row["target"]["session_id"] == "s2"
    assert row["provenance"]["checkpoint"] == "abc1234" and row["goal"].startswith("Context pressure")
    reg = HandoffRegistry(ControlPlaneRepo(), now=lambda: NOW)
    assert reg._attention_reason(row, None) is None


def test_a_lineage_row_takes_its_age_from_when_the_node_was_received():
    timed = {**node("h4"), "first_seen_at": "2026-10-30T10:00:00+00:00", "last_seen_at": "2026-10-30T11:00:00+00:00"}
    row = row_from_node(timed)
    assert (row["created_at"], row["updated_at"]) == ("2026-10-30T10:00:00+00:00", "2026-10-30T11:00:00+00:00")
    assert row_from_node(node("h5"))["updated_at"] == ""        # a reader without history gets no age


def test_an_unpinned_successor_and_a_model_switch_both_ask_for_attention_with_a_reason():
    reg = HandoffRegistry(ControlPlaneRepo(), now=lambda: NOW)
    unpinned = row_from_node(node("h2", cause="context_pressure", from_model="qwen", to_model=None))
    switched = row_from_node(node("h3", cause="quota_exhausted", from_model="a/model-x", to_model="b/model-y"))
    assert unpinned["model_change"] == "not_pinned" and switched["model_change"] == "switched"
    assert "not pinned" in reg._attention_reason(unpinned, None) and reg._attention_reason(switched, None) == "model changed"


def test_lifecycle_states_use_the_registry_words_and_unusable_nodes_are_skipped():
    assert [row_from_node(node("a", s))["state"] for s in ("prepared", "acknowledged", "committed", "aborted", "failed")] == \
        ["queued", "accepted", "completed", "cancelled", "failed"]
    assert rows_from_nodes([{"id": "agent:x", "node_type": "agent"}, None, node("ok")]) and len(rows_from_nodes([{"node_type": "handoff"}])) == 0


def test_registry_reads_merge_lineage_rows_and_chain_them_by_session():
    reg = HandoffRegistry(ControlPlaneRepo(), now=lambda: NOW)
    reg.create({"handoff_id": "reg1", "source_harness": "pi", "target_harness": "pi", "goal": "g", "source_session_id": "s0", "target_session_id": "s1"}, "t")
    extra = rows_from_nodes([node("h1", cause="context_pressure", from_model="q", to_model=None, mission_id="m9")])
    ov = reg.overview(extra=extra)
    assert ov["total"] == 2 and ov["attention_total"] == 1 and ov["attention"][0]["handoff_id"] == "handoff:h1"
    assert ov["attention"][0]["attention_reason"].startswith("successor not pinned")
    assert reg.overview()["total"] == 1                         # without the lineage rows nothing changes
    assert [r["handoff_id"] for r in reg.chain("handoff:h1", extra=extra)["chain"]] == ["reg1", "handoff:h1"]
    assert {r["handoff_id"] for r in reg.page(extra=extra)["handoffs"]} == {"reg1", "handoff:h1"}


def call(port, key, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"X-API-Key": key})
    response = conn.getresponse()
    body = json.loads(response.read())
    conn.close()
    return response.status, body


def test_the_api_serves_only_the_lineage_handoffs_the_caller_may_read():
    repo = ControlPlaneRepo()
    feed = Feed(IngestService(repo, key_scopes=_AnyKey()))
    feed.node("handoff:open", state="acknowledged", attrs={"cause": "context_pressure", "from_model": "q", "from_session_id": "session:a", "to_session_id": "session:b"})
    feed.node("handoff:secret", state="acknowledged", scope="private", attrs={"cause": "quota_exhausted", "from_model": "x", "to_model": "y"})
    instance = ControlPlaneServer(repo, host="127.0.0.1", port=0, api_keys={"operator": "operator", "viewer": "viewer"})
    threading.Thread(target=instance.serve, daemon=True).start()
    instance.wait_until_ready()
    status, ov = call(instance.port, "operator", "/api/v1/handoffs/overview")
    assert status == 200 and ov["sources"]["lineage"] in ("ok", "truncated")
    ids = {a["handoff_id"] for a in ov["attention"]}
    assert "handoff:open" in ids
    status, detail = call(instance.port, "operator", "/api/v1/handoffs/handoff%3Aopen")
    assert status == 200 and detail["model_change"] == "not_pinned" and detail["origin"] == "lineage"
    status, _ = call(instance.port, "operator", "/api/v1/handoffs/handoff%3Anope")
    assert status == 404
    status, page = call(instance.port, "operator", "/api/v1/handoffs?limit=10")
    assert status == 200 and any(r["handoff_id"] == "handoff:open" for r in page["handoffs"])
    assert "handoff:secret" not in json.dumps(call(instance.port, "viewer", "/api/v1/handoffs/overview")[1])
