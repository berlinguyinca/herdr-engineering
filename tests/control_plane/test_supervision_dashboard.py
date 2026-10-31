from copy import deepcopy

from herdr_engineering.control_plane.supervision_dashboard import (
    SupervisionDashboardProvider,
    project_supervision,
)
from herdr_engineering.control_plane.tab_supervision import Actor, Binding, Policy, Supervision


class Sink:
    def __init__(self):
        self.events = []

    def record_required(self, event):
        self.events.append(deepcopy(event))


HUMAN = Actor("u", None, frozenset({"repo"}), frozenset({"pi/a"}))
ROOT = Actor("u", "root", frozenset({"repo"}), frozenset({"pi/a"}))
POLICY = Policy(frozenset({"repo"}), frozenset({"pi/a"}), 100, 1000)


def supervision():
    sink = Sink()
    service = Supervision(sink, now=lambda: 10)
    service.create_job(HUMAN, "job", "server/tab", "root", "task-root", POLICY)
    service.delegate(ROOT, "child", "task-child", POLICY)
    service.link_job(HUMAN, "job", "room-1", "workspace-1")
    return service, sink


def flow(*agent_ids):
    nodes = []
    for aid in agent_ids:
        nodes.append({"id": "a:" + aid, "kind": "agent", "agent_id": aid,
                      "name": aid, "owner_user_id": "u", "family": "pi",
                      "status": "active", "actions": ["read"]})
    return {"room_id": "room-1", "nodes": nodes, "edges": [], "spawns": [], "tasks": []}


def test_projection_returns_collapsible_job_tree_with_same_collaboration_ids():
    service, _ = supervision()
    room_flow = flow("root", "child")
    room_flow["tasks"] = [{"task_id": "task-child", "from": "a:root", "to": "a:child",
                           "status": "accepted", "text": "Review the implementation"}]
    room_flow["nodes"][1]["fabric"] = {"agent_role": "reviewer", "status": "active"}
    out = project_supervision(service, room_flow, room_id="room-1", workspace_id="workspace-1",
                              viewer_owner_id="u")
    assert out["room_id"] == "room-1" and out["workspace_id"] == "workspace-1"
    assert len(out["jobs"]) == 1
    job = out["jobs"][0]
    assert job["job_id"] == "job" and job["tab_id"] == "server/tab"
    assert job["root_agent_id"] == "root"
    nodes = {node["agent_id"]: node for node in job["agents"]}
    assert set(nodes) == {"root", "child"}
    assert nodes["child"]["role"] == "reviewer"
    assert nodes["child"]["responsibilities"] == [{"task_id": "task-child", "status": "accepted",
                                                     "text": "Review the implementation"}]
    assert job["supervision_edges"] == [{"from": "root", "to": "child", "type": "supervisor"}]
    assert out["unassigned"] == []


def test_projection_is_room_scoped_and_does_not_leak_hidden_agents_or_bindings():
    service, _ = supervision()
    service.bind(ROOT, "child", Binding("server", "session", "runtime", "pi", "server/tab", "pane"))
    out = project_supervision(service, flow("root"), room_id="room-1", workspace_id="workspace-1")
    assert [node["agent_id"] for node in out["jobs"][0]["agents"]] == ["root"]
    assert out["jobs"][0]["supervision_edges"] == []
    assert "binding" not in out["jobs"][0]["agents"][0]
    assert "child" not in repr(out)
    assert project_supervision(service, flow("root"), room_id="other", workspace_id="workspace-1")["jobs"] == []
    assert project_supervision(service, flow("root"), room_id="room-1", workspace_id="other")["jobs"] == []


def test_missing_supervisor_is_unresolved_never_promoted_to_root():
    service, _ = supervision()
    out = project_supervision(service, flow("child"), room_id="room-1", workspace_id="workspace-1")
    assert out["jobs"] == []
    assert out["unassigned"] == [{"agent_id": "child", "name": "child", "role": "pi",
                                   "provenance": "supervisor_hidden_or_unavailable"}]


def test_unregistered_collaboration_agent_has_explicit_unknown_parentage():
    service, _ = supervision()
    out = project_supervision(service, flow("root", "research"),
                              room_id="room-1", workspace_id="workspace-1")
    assert out["unassigned"] == [{"agent_id": "research", "name": "research", "role": "pi",
                                   "provenance": "not_registered_in_supervision"}]


def test_owner_visible_runtime_is_projected_without_using_native_parentage():
    service, _ = supervision()
    service.bind(HUMAN, "root", Binding("server", "session", "runtime", "pi", "server/tab", "pane"))
    room_flow = flow("root", "research")
    room_flow["nodes"][0]["instance_id"] = "runtime"
    room_flow["nodes"][0]["fabric"] = {"session_id": "session", "status": "active",
                                         "parent_session_id": "untrusted-native-parent"}
    room_flow["nodes"][1]["parent_agent_id"] = "root"
    out = project_supervision(service, room_flow, room_id="room-1", workspace_id="workspace-1",
                              viewer_owner_id="u")
    node = out["jobs"][0]["agents"][0]
    assert node["runtime"]["binding_status"] == "fabric_observed"
    assert node["runtime"]["session_id"] == "session"
    assert node["runtime"]["pane_id"] == "pane"
    assert node["runtime"]["status"] == "active"
    assert "parent_session_id" not in repr(out)
    assert out["unassigned"][0]["provenance"] == "not_registered_in_supervision"


def test_owner_sees_recorded_binding_as_unconfirmed_without_fabric_enrichment():
    service, _ = supervision()
    service.bind(HUMAN, "root", Binding("server", "session", "runtime", "pi",
                                        "server/tab", "pane"))
    out = project_supervision(service, flow("root"), room_id="room-1",
                              workspace_id="workspace-1", viewer_owner_id="u")
    assert out["jobs"][0]["agents"][0]["runtime"] == {
        "binding_status": "recorded_unconfirmed",
        "server_id": "server",
        "session_id": "session",
        "runtime_instance_id": "runtime",
        "adapter": "pi",
        "tab_id": "server/tab",
        "pane_id": "pane",
    }


def test_foreign_fabric_instance_cannot_overwrite_recorded_binding():
    service, _ = supervision()
    service.bind(HUMAN, "root", Binding("server", "recorded-session", "recorded-runtime",
                                        "pi", "server/tab", "recorded-pane"))
    room_flow = flow("root")
    room_flow["nodes"][0]["instance_id"] = "foreign-runtime"
    room_flow["nodes"][0]["fabric"] = {"session_id": "foreign-session",
                                         "status": "active", "host": "foreign-host"}
    out = project_supervision(service, room_flow, room_id="room-1",
                              workspace_id="workspace-1", viewer_owner_id="u")
    runtime = out["jobs"][0]["agents"][0]["runtime"]
    assert runtime["binding_status"] == "recorded_unconfirmed"
    assert runtime["runtime_instance_id"] == "recorded-runtime"
    assert runtime["session_id"] == "recorded-session"
    assert runtime["pane_id"] == "recorded-pane"
    assert "status" not in runtime and "host" not in runtime
    assert "foreign" not in repr(runtime)


def test_owner_visible_fabric_without_recorded_binding_remains_observation_only():
    service, _ = supervision()
    room_flow = flow("root")
    room_flow["nodes"][0]["instance_id"] = "observed-runtime"
    room_flow["nodes"][0]["fabric"] = {"session_id": "observed-session", "status": "active"}
    out = project_supervision(service, room_flow, room_id="room-1",
                              workspace_id="workspace-1", viewer_owner_id="u")
    assert out["jobs"][0]["agents"][0]["runtime"] == {
        "binding_status": "fabric_observed",
        "session_id": "observed-session",
        "status": "active",
    }


def test_runtime_is_hidden_from_other_authorized_room_viewers():
    service, _ = supervision()
    service.bind(HUMAN, "root", Binding("server", "session", "runtime", "pi", "server/tab", "pane"))
    room_flow = flow("root")
    room_flow["nodes"][0]["instance_id"] = "runtime"
    room_flow["nodes"][0]["fabric"] = {"session_id": "session", "status": "active"}
    room_flow["tasks"] = [{"task_id": "private-task", "from": "u:u", "to": "a:root",
                           "status": "accepted", "text": "private task summary"}]
    out = project_supervision(service, room_flow, room_id="room-1", workspace_id="workspace-1",
                              viewer_owner_id="different-user")
    node = out["jobs"][0]["agents"][0]
    assert "runtime" not in node
    assert node["responsibilities"] == [{"task_id": "private-task", "status": "accepted"}]
    assert node["checkpoint"] == {"progress": 0}
    assert "private task summary" not in repr(out)


def test_projection_survives_authoritative_replay_and_exposes_budget_checkpoint_evidence():
    service, sink = supervision()
    service.bind(HUMAN, "root", Binding("server", "s", "root-runtime", "pi", "server/tab", "root-pane"))
    service._now = lambda: 20
    service.checkpoint(ROOT, 2, "artifact://root-checkpoint")
    service.charge(ROOT, 25, "usage-1")
    restored = Supervision(sink, now=lambda: 20)
    restored.hydrate(sink.events)
    out = project_supervision(restored, flow("root", "child"), room_id="room-1",
                              workspace_id="workspace-1", viewer_owner_id="u")
    root = {n["agent_id"]: n for n in out["jobs"][0]["agents"]}["root"]
    assert root["budget"] == {"used_tokens": 25, "token_limit": 100}
    assert root["checkpoint"] == {"progress": 2, "evidence_ref": "artifact://root-checkpoint"}


def test_provider_interface_infers_linked_workspace_and_delegates_explicit_link():
    sink = Sink()
    service = Supervision(sink, now=lambda: 10)
    service.create_job(HUMAN, "job", "server/tab", "root", "task-root", POLICY)
    provider = SupervisionDashboardProvider(service)
    assert provider.link(HUMAN, "job", "room-1", "workspace-1") == {
        "job_id": "job", "room_id": "room-1", "workspace_id": "workspace-1"}
    out = provider.project("room-1", flow("root"), viewer_owner_id="u")
    assert out["workspace_id"] == "workspace-1" and out["jobs"][0]["job_id"] == "job"
    assert provider.agent_ids() == ["root"]
    assert provider.job_agent_ids("job") == ["root"]
    assert provider.links()[0]["agent_ids"] == ["root"]
