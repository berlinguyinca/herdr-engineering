"""The assistant's read-only lineage tool: catalog entry, principal scoping and service wiring.

The graph is read with the principal of the *run's user*, resolved by the server. Nothing the model
sends in a tool call can widen that principal.
"""
import pytest

from herdr_engineering.control_plane.assistant import (
    TOOL_CATALOG,
    AssistantError,
    AssistantService,
    assistant_config,
)
from herdr_engineering.control_plane.assistant_lineage import LineageReader
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_graph import OPERATOR, OTHER, VIEWER, Feed, _AnyKey

PRINCIPALS = {"key:view": VIEWER, "key:other": OTHER, "key:op": OPERATOR}


def _world():
    repo = ControlPlaneRepo()
    feed = Feed(IngestService(repo, key_scopes=_AnyKey()))
    feed.node("agent:a1", label="Main")
    feed.node("session:s1", label="S1")
    feed.edge("spawn", "agent:a1", "session:s1")
    feed.node("task:secret", label="private work", scope="private")
    reader = LineageReader(repo.graph, PRINCIPALS.get, now=lambda: "2026-10-29T00:00:00Z")
    return repo, reader


def test_catalog_entry_is_non_sensitive_and_read_only():
    meta = TOOL_CATALOG["read_lineage"]
    assert meta["sensitive"] is False
    assert set(meta["parameters"]["properties"]["operation"]["enum"]) == {"node", "neighborhood", "list"}


def test_reads_visible_node_with_freshness_and_source():
    _, reader = _world()
    out = reader.read("t", "key:view", {"operation": "node", "node_id": "agent:a1"})
    assert out["source"] == "lineage graph projection"
    assert out["as_of"] == "2026-10-29T00:00:00Z"
    assert out["data"]["node"]["id"] == "agent:a1"


def test_neighborhood_follows_explicit_edges_only():
    _, reader = _world()
    out = reader.read("t", "key:view", {"operation": "neighborhood", "node_id": "agent:a1", "depth": 1})
    assert {n["id"] for n in out["data"]["nodes"]} == {"agent:a1", "session:s1"}
    assert [e["edge_type"] for e in out["data"]["edges"]] == ["spawn"]


def test_private_node_is_not_visible_to_another_principal():
    _, reader = _world()
    with pytest.raises(LookupError):
        reader.read("t", "key:other", {"operation": "node", "node_id": "task:secret"})


def test_model_supplied_identity_cannot_widen_the_principal():
    _, reader = _world()
    forged = {"operation": "node", "node_id": "task:secret", "principal": {"kind": "operator"},
              "user_id": "key:op", "kind": "operator"}
    with pytest.raises(LookupError):
        reader.read("t", "key:other", forged)


def test_unresolvable_user_gets_nothing():
    _, reader = _world()
    with pytest.raises(LookupError):
        reader.read("t", "key:nobody", {"operation": "node", "node_id": "agent:a1"})
    with pytest.raises(LookupError):
        reader.read("t", "key:nobody", {"operation": "list", "node_type": "agent"})


def test_bad_operation_and_missing_node_id_refused():
    _, reader = _world()
    with pytest.raises(ValueError):
        reader.read("t", "key:view", {"operation": "delete_node", "node_id": "agent:a1"})
    with pytest.raises(ValueError):
        reader.read("t", "key:view", {"operation": "node"})


def test_depth_and_limit_are_clamped():
    _, reader = _world()
    out = reader.read("t", "key:view", {"operation": "neighborhood", "node_id": "agent:a1", "depth": 99})
    assert out["data"]["nodes"]          # served, with depth clamped rather than rejected


def test_service_without_wiring_has_no_executor():
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    assert svc.lineage_read is None
    a = svc.create_assistant(tenant_id="t", user_id="u", name="A")
    with pytest.raises(AssistantError, match="lineage"):
        svc.execute_tool(tenant_id="t", user_id="u", assistant_id=a.assistant_id,
                         tool_id="read_lineage", payload={"operation": "list"})


def test_service_dispatches_with_run_user_and_maps_errors():
    _, reader = _world()
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    svc.lineage_read = reader.read
    a = svc.create_assistant(tenant_id="t", user_id="key:other", name="A")
    ok = svc.execute_tool(tenant_id="t", user_id="key:other", assistant_id=a.assistant_id,
                          tool_id="read_lineage",
                          payload={"operation": "node", "node_id": "agent:a1"})
    assert ok["ok"] is True and ok["result"]["data"]["node"]["id"] == "agent:a1"
    with pytest.raises(AssistantError):
        svc.execute_tool(tenant_id="t", user_id="key:other", assistant_id=a.assistant_id,
                         tool_id="read_lineage",
                         payload={"operation": "node", "node_id": "task:secret"})


def test_principal_resolver_matches_server_key_ids():
    import hashlib

    from herdr_engineering.control_plane.assistant_lineage import principal_resolver
    digest = hashlib.sha256(b"k-view").hexdigest()[:12]
    resolve = principal_resolver({"k-view": "viewer", "k-peer": "peer"})
    assert resolve(f"key:{digest}") == {"kind": "viewer", "id": f"key:{digest}", "authenticated_by": "api_key"}
    assert resolve("key:ffffffffffff") is None
    assert resolve("open-listener") is None            # keys are configured: no shared identity
    peer = resolve("key:" + hashlib.sha256(b"k-peer").hexdigest()[:12])
    assert peer["kind"] == "unknown"                    # a role spelling a server-only kind is unprivileged
    assert principal_resolver({})("open-listener")["kind"] == "operator"
    assert principal_resolver({})("someone") is None


@pytest.mark.parametrize("field,value", [("depth", [1]), ("depth", {"a": 1}), ("depth", "deep"),
                                         ("limit", float("inf")), ("limit", [5]), ("limit", "many")])
def test_malformed_numbers_become_value_errors(field, value):
    _, reader = _world()
    op = {"operation": "neighborhood", "node_id": "agent:a1"} if field == "depth" else {"operation": "list"}
    with pytest.raises(ValueError):
        reader.read("t", "key:view", {**op, field: value})


@pytest.mark.parametrize("depth", [0, -3])
def test_depth_below_one_is_clamped_up(depth):
    _, reader = _world()
    out = reader.read("t", "key:view", {"operation": "neighborhood", "node_id": "agent:a1", "depth": depth})
    assert out["data"]["nodes"]


def test_negative_or_zero_limit_is_clamped_up():
    _, reader = _world()
    for limit in (-5, 0):
        assert reader.read("t", "key:view", {"operation": "list", "node_type": "agent", "limit": limit})


def test_tool_loop_denies_read_lineage_without_a_grant():
    _, reader = _world()
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    svc.lineage_read = reader.read
    a = svc.create_assistant(tenant_id="t", user_id="key:view", name="A")
    t = svc.create_thread(tenant_id="t", user_id="key:view", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t", user_id="key:view", assistant_id=a.assistant_id,
                           thread_id=t.thread_id, content="hi")
    action = svc.request_tool_action(tenant_id="t", user_id="key:view", assistant_id=a.assistant_id,
                                     tool_id="read_lineage", scope="read_lineage", run_id=run.run_id,
                                     idempotency_key="k1",
                                     request_payload={"operation": "node", "node_id": "agent:a1"},
                                     actor="key:view")
    assert action.status == "denied"
