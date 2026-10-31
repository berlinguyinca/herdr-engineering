# ruff: noqa: F811
"""The same authorized collaboration identities back every supervision view."""
import json

import pytest

from herdr_engineering.control_plane.collab import CollabError, CollabService
from herdr_engineering.control_plane.tab_supervision import Actor, Policy
from tests.control_plane.test_collab_tasks import W, code, svc  # noqa: F401


@pytest.fixture
def w(svc):
    return W(svc)


def linked(w):
    _, root = w.agent("alice", name="lead")
    _, child = w.agent("alice", name="reviewer")
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["pi"], "allowed_models": ["a"]})
    provider = w.s.supervision_provider
    owner = Actor("alice", None, frozenset({"read"}), frozenset({"pi/a"}))
    agent = Actor("alice", root["agent_id"], owner.scopes, owner.models)
    policy = Policy(owner.scopes, owner.models, 100, w.s.clock() + 1000)
    with w.s._lock:
        provider.supervision.create_job(owner, "job", "tab", root["agent_id"], "task-root", policy)
        provider.supervision.delegate(agent, child["agent_id"], "task-review", policy)
    w.s.link_supervision_job(w.alice, w.room, {"job_id": "job", "workspace_id": "workspace"})
    return root, child


def test_endpoint_and_room_flow_share_collaboration_ids(w):
    root, child = linked(w)
    status, tree = w.s.dispatch("GET", ["rooms", w.room, "supervision"], {}, {},
                              "Bearer tok-alice-0123456789", None)
    assert status == 200
    assert tree == w.s.room_flow(w.alice, w.room)["supervision"]
    assert tree["workspace_id"] == "workspace"
    assert {a["agent_id"] for a in tree["jobs"][0]["agents"]} == {root["agent_id"], child["agent_id"]}
    assert w.s.room_supervision(w.alice, w.room2)["jobs"] == []


def test_read_requires_current_room_membership_and_hides_guests(w):
    linked(w)
    assert code(w.s.room_supervision, w.mallory, w.room) == 404
    assert code(w.s.room_supervision, w.dave, w.room) == 403
    assert w.s.room_supervision(w.carol, w.room)["jobs"]
    w.s.remove_member(w.alice, w.room, "carol")
    assert code(w.s.room_supervision, w.carol, w.room) == 404


def test_link_requires_room_owner_and_existing_owned_collaboration_agents(w):
    linked(w)
    body = {"job_id": "job", "workspace_id": "workspace"}
    assert code(w.s.link_supervision_job, w.bob, w.room, body) == 403
    assert code(w.s.link_supervision_job, w.alice, w.room, {**body, "grants": ["execute"]}) == 400
    assert code(w.s.link_supervision_job, w.alice, w.room2, body) in {400, 409}


def test_restart_restores_private_supervision_without_public_feed_payload(w):
    linked(w)
    rows = w.s._sink.load()
    assert any(r["event_type"] == "supervision_recorded" for r in rows)
    assert not any(e["event_type"] == "supervision_recorded"
                   for e in w.s.read_events(w.alice, w.room, 0, 200)["events"])
    restored = CollabService(w.s.auth, sink=w.s._sink, now=w.s.clock)
    restored.hydrate(rows)
    assert restored.room_supervision(w.alice, w.room) == w.s.room_supervision(w.alice, w.room)
    assert "scope" not in json.dumps(w.s.room_supervision(w.carol, w.room))


def test_supervision_changes_wake_the_existing_room_state_loop(w):
    root, _ = linked(w)
    before = w.s.room_state(w.alice, w.room, -1, 0, 0)["seq"]
    actor = Actor("alice", root["agent_id"], frozenset({"read"}), frozenset({"pi/a"}))
    with w.s._lock:
        w.s.supervision_provider.supervision.checkpoint(actor, 1, "verified-source")
    after = w.s.room_state(w.alice, w.room, before, 0, 0)
    assert after["changed"] is True
    assert after["flow"]["supervision"]["jobs"][0]["agents"][0]["checkpoint"]["progress"] == 1


def test_checkpoint_does_not_wake_another_linked_room(w):
    root, _ = linked(w)
    _, other = w.agent("alice", room=w.room2, name="other")
    owner = Actor("alice", None, frozenset({"read"}), frozenset({"pi/a"}))
    with w.s._lock:
        w.s.supervision_provider.supervision.create_job(
            owner, "other-job", "other-tab", other["agent_id"], "other-task",
            Policy(owner.scopes, owner.models, 100, w.s.clock() + 1000))
    w.s.set_room_profile(w.alice, w.room2, {"allowed_families": ["pi"], "allowed_models": ["a"]})
    w.s.link_supervision_job(w.alice, w.room2, {"job_id": "other-job", "workspace_id": "other-workspace"})
    before = w.s.room_state(w.alice, w.room2, -1, 0, 0)["seq"]
    with w.s._lock:
        w.s.supervision_provider.supervision.checkpoint(
            Actor("alice", root["agent_id"], owner.scopes, owner.models), 1, "progress")
    assert w.s.room_state(w.alice, w.room2, before, 0, 0)["changed"] is False


def test_embedded_supervision_requires_outer_serialized_operator_boundary(w):
    _, root = w.agent("alice", name="lead")
    owner = Actor("alice", None, frozenset({"read"}), frozenset({"pi/a"}))
    policy = Policy(owner.scopes, owner.models, 100, w.s.clock() + 1000)
    with pytest.raises(RuntimeError, match="serialized"):
        w.s.supervision_provider.supervision.create_job(owner, "job", "tab", root["agent_id"], "task", policy)
    assert w.s.supervision_provider.supervision.agents() == []


def test_real_http_route_and_dashboard_module_loading(w):
    import threading

    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    from tests.control_plane.test_collab import Api

    linked(w)
    server = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=w.s)
    worker = threading.Thread(target=server.serve, daemon=True)
    worker.start()
    server.wait_until_ready()
    try:
        api = Api(server.port)
        path = "/api/v1/collab/rooms/" + w.room + "/supervision"
        assert api.call("GET", path, user="alice")[0] == 200
        assert api.call("GET", path, user="mallory")[0] == 404
        assert api.call("GET", path)[0] == 401
        for module in ("supervision-tree.js", "supervision-catalog.js"):
            assert api.call("GET", "/" + module)[0] == 200
    finally:
        server.shutdown()
        worker.join(timeout=5)


def test_supervision_cannot_mint_an_identity_outside_collaboration(w):
    owner = Actor("alice", None, frozenset({"read"}), frozenset({"pi/a"}))
    with w.s.supervision_transaction() as supervision:
        with pytest.raises(RuntimeError, match="collaboration"):
            supervision.create_job(owner, "ghost", "tab", "invented-agent", "task",
                                   Policy(owner.scopes, owner.models, 100, w.s.clock() + 1000))
    assert w.s.supervision_provider.supervision.agents() == []


def test_revoked_supervisor_cannot_write_using_an_old_actor(w):
    root, child = linked(w)
    w.s.remove_agent_from_room(w.alice, w.room, root["agent_id"])
    actor = Actor("alice", child["agent_id"], frozenset({"read"}), frozenset({"pi/a"}))
    with w.s.supervision_transaction() as supervision:
        with pytest.raises(RuntimeError, match="grant"):
            supervision.checkpoint(actor, 1, "old-authority")
    assert w.s.room_supervision(w.alice, w.room)["jobs"] == []


def test_link_rejects_persisted_policy_beyond_current_collaboration_actions(w):
    _, root = w.agent("alice", actions=("read",), name="lead")
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["pi"], "allowed_models": ["a"]})
    owner = Actor("alice", None, frozenset({"read", "execute"}), frozenset({"pi/a"}))
    with w.s.supervision_transaction() as supervision:
        supervision.create_job(owner, "job", "tab", root["agent_id"], "task",
                               Policy(owner.scopes, owner.models, 100, w.s.clock() + 1000))
    with pytest.raises(CollabError, match="grant|policy"):
        w.s.link_supervision_job(w.alice, w.room, {"job_id": "job", "workspace_id": "workspace"})
    assert w.s.supervision_provider.links() == []


def test_current_model_policy_revocation_blocks_old_actor(w):
    root, _ = linked(w)
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["pi"], "allowed_models": ["other"]})
    actor = Actor("alice", root["agent_id"], frozenset({"read"}), frozenset({"pi/a"}))
    with w.s.supervision_transaction() as supervision:
        with pytest.raises(RuntimeError, match="model"):
            supervision.checkpoint(actor, 1, "old-model-approval")


def test_revoked_supervisor_cannot_verify_with_stale_actor(w):
    from herdr_engineering.control_plane.tab_supervision import Binding

    root, child = linked(w)
    owner = Actor("alice", None, frozenset({"read"}), frozenset({"pi/a"}))
    with w.s.supervision_transaction() as supervision:
        supervision.bind(owner, child["agent_id"], Binding("server", "session", "instance", "pi", "tab", "pane"))
    w.s.remove_agent_from_room(w.alice, w.room, root["agent_id"])
    actor = Actor("alice", root["agent_id"], owner.scopes, owner.models)
    with w.s.supervision_transaction() as supervision:
        with pytest.raises(RuntimeError, match="grant"):
            supervision.conclude(actor, child["agent_id"], "verified", "stale-review")
    assert w.s.supervision_provider.supervision.view(child["agent_id"])["status"] == "running"
