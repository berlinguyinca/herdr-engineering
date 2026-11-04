"""The assistant graph is a read-only, per-person projection: assistants, delegations and the specialists they run on."""
from tests.control_plane.test_assistant_dispatch import approved_delegation, world  # noqa: F401
from tests.control_plane.test_assistant_followups import grant_via_service, setup_assistant


def graph(h, who="alice"):
    st, out = h.call(who, "GET", "/api/v1/assistant-graph")
    assert st == 200, out
    return out, {n["id"]: n for n in out["nodes"]}


def test_graph_links_you_to_assistants_delegations_and_specialists(world):  # noqa: F811
    h, svc, svr = world
    d = approved_delegation(h, svc)
    out, nodes = graph(h)
    assert nodes["you"]["kind"] == "you"
    assistant = nodes[d.assistant_id]
    assert assistant["kind"] == "assistant" and assistant["can_delegate"] is False   # only request_mission was granted
    assert nodes[d.delegation_id]["kind"] == "delegation" and nodes[d.delegation_id]["status"] == "queued"
    edges = {(e["from"], e["to"], e["kind"]) for e in out["edges"]}
    assert ("you", d.assistant_id, "owns") in edges
    assert (d.assistant_id, d.delegation_id, "delegated") in edges
    assert (d.delegation_id, "specialist:pi", "runs_on") in edges
    assert nodes["specialist:pi"]["kind"] == "specialist" and nodes["specialist:pi"]["bound"] is True


def test_graph_reflects_the_delegate_grant_per_assistant(world):  # noqa: F811
    h, svc, svr = world
    aid, _ = setup_assistant(h, "alice")
    other, _ = setup_assistant(h, "alice")
    _, nodes = graph(h)
    assert nodes[aid]["can_delegate"] is False and nodes[other]["can_delegate"] is False
    grant_via_service(svc, "alice", aid, "delegate")
    _, nodes = graph(h)
    assert nodes[aid]["can_delegate"] is True and nodes[aid]["delegate_scopes"]
    assert nodes[other]["can_delegate"] is False                         # only the assistant that was granted


def test_graph_is_scoped_to_the_caller_and_read_only(world):  # noqa: F811
    h, svc, svr = world
    d = approved_delegation(h, svc)
    before = (len(svc.store.delegations), len(svc.store.audit))
    out, nodes = graph(h, "bea")                                           # another tenant sees none of it
    assert d.delegation_id not in nodes and d.assistant_id not in nodes and nodes["you"]
    graph(h, "alice")
    assert (len(svc.store.delegations), len(svc.store.audit)) == before   # reading changes nothing
    assert h.call("alice", "POST", "/api/v1/assistant-graph", {})[0] != 200


def test_an_unbound_specialist_is_shown_as_not_bound(world):  # noqa: F811
    h, svc, svr = world
    svc.config["delegation"]["targets"]["pi-secretary"] = {"role_ref": {
        "id": "procedure:secretary-system", "version": "1.0.0", "digest": "sha256:" + "a" * 64}}
    _, nodes = graph(h)
    assert nodes["specialist:pi-secretary"]["bound"] is False             # no operator binding: delegation would fail
    assert nodes["specialist:pi-secretary"]["role"] == "procedure:secretary-system@1.0.0"
