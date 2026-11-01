"""Assistant types, create, link and unlink (docs/specs/assistant-management-20261031). Real routes, real service."""
import pytest

from herdr_engineering.control_plane.assistant import assistant_config
from tests.control_plane.test_assistant_dispatch import world  # noqa: F401

ROLE = {"id": "procedure:secretary-system", "version": "1.0.0",
        "digest": "sha256:942e5b1002d74a9d52ae2fb7c2f8ef07f4f6b08aa1b9e08a36b8e0504ffab120"}


@pytest.fixture()
def hz(world):  # noqa: F811
    h, svc, svr = world
    svc.config["delegation"]["targets"]["pi-secretary"] = {"role_ref": ROLE, "requires": ["os:linux"],
                                                           "resources": {"cpus": 1, "memory_mb": 1024}}
    return h, svc


def make(h, name, who="alice", **body):
    st, out = h.call(who, "POST", "/api/v1/assistants", {"name": name, **body})
    return st, out


def link(h, child, parent, who="alice"):
    return h.call(who, "POST", f"/api/v1/assistants/{child}/link", {"parent_id": parent})


def test_types_come_from_configuration_and_report_requirements(hz):
    h, _ = hz
    st, out = h.call("alice", "GET", "/api/v1/assistant-types")
    assert st == 200
    types = {t["type"]: t for t in out["types"]}
    assert types["rakazo"]["can_be_parent"] is True and types["rakazo"]["kind"] == "assistant"
    sec = types["pi-secretary"]
    assert sec["kind"] == "specialist" and sec["can_be_parent"] is False
    assert sec["requires"] == ["os:linux"] and sec["resources"] == {"cpus": 1, "memory_mb": 1024}
    assert sec["role"] == "procedure:secretary-system@1.0.0"


def test_target_config_rejects_unknown_capabilities_and_resources():
    base = {"role_ref": None}
    cfg = lambda spec: assistant_config({"personal_assistant": {"enabled": True, "delegation": {
        "targets": {"pi": base, "scout": spec}}}})
    assert cfg({"role_ref": None, "requires": ["desktop"]})["delegation"]["targets"]["scout"]["requires"] == [
        "desktop", "display"]                                                    # desktop implies display
    with pytest.raises(ValueError):
        cfg({"role_ref": None, "requires": ["displya"]})                         # a typo is an error, not a match
    with pytest.raises(ValueError):
        cfg({"role_ref": None, "resources": {"cpus": -1}})
    with pytest.raises(ValueError):
        cfg({"role_ref": None, "resources": {"disk": 5}})


def test_create_defaults_to_rakazo_and_specialists_need_setup(hz):
    h, _ = hz
    st, a = make(h, "Fleet PA")
    assert st == 201 and a["type"] == "rakazo" and a["parent_id"] is None and a["status"] == "ready"
    st, s = make(h, "Secretary", type="pi-secretary", parent_id=a["assistant_id"])
    assert st == 201 and s["type"] == "pi-secretary" and s["parent_id"] == a["assistant_id"]
    assert s["status"] == "needs_setup"
    assert make(h, "X", type="no-such-type")[0] == 400


def test_create_is_idempotent_and_conflicting_reuse_is_refused(hz):
    h, svc = hz
    st1, a = make(h, "Fleet PA", idempotency_key="k1")
    st2, again = make(h, "Fleet PA", idempotency_key="k1")
    assert (st1, st2) == (201, 201) and again["assistant_id"] == a["assistant_id"]
    assert len(svc.store.assistants) == 1
    assert [e["action"] for e in svc.store.audit].count("assistant.created") == 1
    assert make(h, "Other name", idempotency_key="k1")[0] == 400


def test_link_and_unlink_are_recorded_audited_and_replay_safe(hz):
    h, svc = hz
    parent, child = make(h, "Fleet PA")[1], make(h, "Mail assistant")[1]
    st, out = link(h, child["assistant_id"], parent["assistant_id"])
    assert st == 200 and out["parent_id"] == parent["assistant_id"] and out["version"] == child["version"] + 1
    again = link(h, child["assistant_id"], parent["assistant_id"])[1]
    assert again["version"] == out["version"]                                    # replay changes nothing
    st, out = h.call("alice", "POST", f"/api/v1/assistants/{child['assistant_id']}/unlink", {})
    assert st == 200 and out["parent_id"] is None
    actions = [e["action"] for e in svc.store.audit]
    assert actions.count("assistant.linked") == 1 and actions.count("assistant.unlinked") == 1


def test_link_rules_parent_type_self_cycle_depth_and_archived(hz):
    h, _ = hz
    a, b, c, d = (make(h, n)[1]["assistant_id"] for n in "ABCD")
    spec = make(h, "Secretary", type="pi-secretary")[1]["assistant_id"]
    assert link(h, a, a)[0] == 400                                               # not its own parent
    assert link(h, b, spec)[0] == 400                                            # a specialist cannot be a parent
    assert link(h, b, a)[0] == 200 and link(h, c, b)[0] == 200                   # A <- B <- C is depth 3
    assert link(h, a, c)[0] == 400                                               # cycle
    assert link(h, d, c)[0] == 400                                               # depth 4
    st, _ = h.call("alice", "POST", f"/api/v1/assistants/{d}", {"archive": True})
    assert st == 200 and link(h, d, a)[0] == 400                                 # archived cannot be linked


def test_link_grants_nothing_and_changes_no_connection(hz):
    h, svc = hz
    parent, child = make(h, "Fleet PA")[1], make(h, "Secretary", type="pi-secretary")[1]
    before = (len(svc.store.grants), len(svc.store.approvals), len(svc.store.connectors), len(svc.store.delegations))
    assert link(h, child["assistant_id"], parent["assistant_id"])[0] == 200
    after = (len(svc.store.grants), len(svc.store.approvals), len(svc.store.connectors), len(svc.store.delegations))
    assert before == after
    st, g = h.call("alice", "GET", "/api/v1/assistant-graph")
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes[parent["assistant_id"]]["can_delegate"] is False                # still gated by the approval flow


def test_other_users_cannot_link_into_or_see_your_tree(hz):
    h, _ = hz
    mine, theirs = make(h, "Mine")[1], make(h, "Theirs", who="bea")[1]
    assert link(h, mine["assistant_id"], theirs["assistant_id"])[0] == 404       # a different tenant's parent
    assert link(h, mine["assistant_id"], mine["assistant_id"], who="bea")[0] == 404


def test_archiving_a_parent_unlinks_its_children(hz):
    h, svc = hz
    parent, child = make(h, "Fleet PA")[1], make(h, "Mail")[1]
    link(h, child["assistant_id"], parent["assistant_id"])
    assert h.call("alice", "POST", f"/api/v1/assistants/{parent['assistant_id']}", {"archive": True})[0] == 200
    st, got = h.call("alice", "GET", f"/api/v1/assistants/{child['assistant_id']}")
    assert st == 200 and got["parent_id"] is None


def test_graph_reflects_type_parent_and_linked_edges(hz):
    h, _ = hz
    parent, child = make(h, "Fleet PA")[1], make(h, "Secretary", type="pi-secretary")[1]
    link(h, child["assistant_id"], parent["assistant_id"])
    st, g = h.call("alice", "GET", "/api/v1/assistant-graph")
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes[child["assistant_id"]]["type"] == "pi-secretary"
    assert nodes[child["assistant_id"]]["parent_id"] == parent["assistant_id"]
    edges = {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
    assert (parent["assistant_id"], child["assistant_id"], "linked") in edges


def test_specialist_setup_steps_report_binding_and_placement_and_change_nothing(hz):
    h, svc = hz
    _, s = make(h, "Secretary", type="pi-secretary")
    before = (len(svc.store.grants), len(svc.store.delegations), len(svc.store.audit))
    st, g = h.call("alice", "GET", "/api/v1/assistant-graph")
    node = next(n for n in g["nodes"] if n["id"] == s["assistant_id"])
    steps = {x["id"]: x for x in node["setup"]}
    assert st == 200 and node["status"] == "needs_setup"
    assert steps["binding"]["state"] == "todo"
    assert steps["placement"]["state"] == "blocked" and "no host can carry it" in steps["placement"]["detail"]
    assert (len(svc.store.grants), len(svc.store.delegations), len(svc.store.audit)) == before


class _FakeSetup:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def provision(self, type_name):
        from herdr_engineering.assistant_self_setup import SelfSetupError
        self.calls.append(type_name)
        if self.fail:
            raise SelfSetupError(self.fail)
        return {"room_id": "room_1", "agent_id": "agent_1"}


def test_setup_endpoint_provisions_audits_and_refuses_what_it_cannot_do(hz):
    h, svc = hz
    _, s = make(h, "Secretary", type="pi-secretary")
    _, r = make(h, "Fleet PA")
    path = f"/api/v1/assistants/{s['assistant_id']}/setup"
    assert h.call("alice", "POST", path, {})[0] == 400                          # no collaboration operator wired
    svc.self_setup = _FakeSetup()
    st, out = h.call("alice", "POST", path, {})
    assert st == 200 and (out["room_id"], out["agent_id"]) == ("room_1", "agent_1")
    assert svc.self_setup.calls == ["pi-secretary"]
    assert [e["action"] for e in svc.store.audit].count("assistant.setup") == 1
    assert h.call("alice", "POST", f"/api/v1/assistants/{r['assistant_id']}/setup", {})[0] == 400   # rakazo needs none
    svc.self_setup = _FakeSetup(fail="no active agent named pi-secretary is enrolled for you; enroll it first")
    st, out = h.call("alice", "POST", path, {})
    assert st == 400 and "enroll it first" in str(out)
    assert h.call("bea", "POST", path, {})[0] in (403, 404)                     # someone else's assistant
