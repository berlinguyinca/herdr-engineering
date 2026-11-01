"""Rakazo sets a specialist up for the dashboard owner through the collaboration service, as that user and nothing more."""
import pytest

from herdr_engineering.assistant_self_setup import (
    GRANT,
    PI_TYPE,
    InProcessAssistantClient,
    SelfSetup,
    SelfSetupError,
)
from herdr_engineering.control_plane.collab import (
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from tests.control_plane.test_collab import TOKENS, Clock, _digest

OWNER = "alice"


@pytest.fixture
def world(tmp_path):
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in TOKENS.items()}),
                        sink=FileEventLog(str(tmp_path / "log")), now=Clock())
    pr = svc.authenticate(f"Bearer {TOKENS[OWNER]}", None)
    return svc, pr, SelfSetup(svc, OWNER)


def enroll(svc, pr, name=PI_TYPE):
    return svc.connect_agent(pr, {"name": name})["agent_id"]


def test_nothing_is_found_before_setup(world):
    _, _, setup = world
    assert setup.find(PI_TYPE) is None


def test_setup_needs_the_owners_own_enrolled_agent(world):
    _, _, setup = world
    with pytest.raises(SelfSetupError, match="no active agent named pi-secretary"):
        setup.provision(PI_TYPE)


def test_provision_creates_one_private_room_and_grants_only_read_and_execute(world):
    svc, pr, setup = world
    agent = enroll(svc, pr)
    out = setup.provision(PI_TYPE)
    assert out["agent_id"] == agent and out["created_room"] is True and out["actions"] == GRANT == ["execute", "read"]
    found = setup.find(PI_TYPE)
    assert found == {"room_id": out["room_id"], "agent_id": agent}
    granted = svc.list_my_agents(pr)["agents"][0]["rooms"][0]
    assert granted["room_id"] == out["room_id"] and sorted(granted["actions"]) == GRANT
    members = svc.get_room(pr, out["room_id"])
    assert [m for m in members.get("members", []) if m.get("user_id") != OWNER] == []      # private: only the owner


def test_provision_is_idempotent(world):
    svc, pr, setup = world
    enroll(svc, pr)
    first, second = setup.provision(PI_TYPE), setup.provision(PI_TYPE)
    assert second["room_id"] == first["room_id"] and second["created_room"] is False
    assert len([r for r in svc.list_rooms(pr)["rooms"] if r["name"].startswith("Assistant: ")]) == 1


def test_a_second_enrolled_agent_of_the_same_name_is_ambiguous_not_guessed(world):
    svc, pr, setup = world
    enroll(svc, pr), enroll(svc, pr)
    with pytest.raises(SelfSetupError):
        setup.provision(PI_TYPE)


def test_only_the_pi_type_has_a_setup(world):
    _, _, setup = world
    with pytest.raises(SelfSetupError, match="no setup is defined"):
        setup.provision("scout")


def test_resolver_serves_only_the_owner_and_only_after_setup(world):
    svc, pr, setup = world
    enroll(svc, pr)
    resolve = setup.resolver(lambda t, u: (t, u) == ("default", "open-listener"))
    assert resolve("default", "open-listener", PI_TYPE) is None                 # not set up yet
    setup.provision(PI_TYPE)
    assert resolve("default", "open-listener", PI_TYPE) is not None
    assert resolve("default", "someone-else", PI_TYPE) is None                  # not the owner
    assert resolve("default", "open-listener", "scout") is None                 # not this type


def test_resolver_falls_back_for_everything_it_does_not_serve(world):
    _, _, setup = world
    seen = []
    resolve = setup.resolver(lambda t, u: False, fallback=lambda *a: seen.append(a) or "fallback")
    assert resolve("t", "u", "x") == "fallback" and seen == [("t", "u", "x")]


def test_the_in_process_client_cannot_leave_the_collaboration_api(world):
    svc, _, _ = world
    with pytest.raises(ValueError):
        InProcessAssistantClient(svc, OWNER)._request("GET", "/api/v1/assistants")


def test_the_transport_creates_a_task_for_the_bound_agent_as_the_owner(world):
    from herdr_engineering.assistant_pi_delegate import HcnPiTaskTransport
    svc, pr, setup = world
    agent = enroll(svc, pr)
    out = setup.provision(PI_TYPE)
    task = HcnPiTaskTransport(setup.client, agent).create_task(
        room_id=out["room_id"], agent_id=agent, text="find a time", idempotency_key="k-1")
    assert task and svc.list_tasks(pr, out["room_id"])["tasks"][0]["target_agent_id"] == agent


def test_the_control_plane_wires_setup_only_with_collaboration_an_operator_single_operator_mode_and_persistence(world, tmp_path):
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.assistant import AssistantService, AssistantStore, assistant_config
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    svc, pr, _ = world

    def build(persist=True, **kw):
        store = AssistantStore(persist_path=str(tmp_path / f"a{len(kw)}{persist}.json")) if persist else None
        cfg = assistant_config({"personal_assistant": {"enabled": True}})
        return ControlPlaneServer(ControlPlaneRepo(), assistant_service=AssistantService(config=cfg, store=store),
                                  allow_open=True, **kw)

    on = build(collab=svc, operator_user=OWNER, single_operator=True)
    assert on.assistant.self_setup is not None
    owner = on.assistant.tenant_for("open-listener")
    enroll(svc, pr)
    on.assistant.self_setup.provision(PI_TYPE)
    assert on.assistant.delegation_adapter_resolver(owner, "open-listener", PI_TYPE) is not None
    assert on.assistant.delegation_adapter_resolver(owner, "someone-else", PI_TYPE) is None
    for kw in ({"collab": svc, "operator_user": OWNER}, {"collab": svc, "single_operator": True},
               {"operator_user": OWNER, "single_operator": True}):
        assert build(**kw).assistant.self_setup is None
    # Without acknowledged persistence a delegation could never be recorded, so nothing is wired and delegation is untouched.
    bare = build(persist=False, collab=svc, operator_user=OWNER, single_operator=True)
    assert bare.assistant.self_setup is None and bare.assistant.delegation_adapter_resolver is None


def test_a_duplicate_room_resolves_to_the_first_one_instead_of_unbinding(world):
    svc, pr, setup = world
    agent = enroll(svc, pr)
    first = setup.provision(PI_TYPE)["room_id"]
    pid = svc.list_rooms(pr)["rooms"][0]["project_id"]
    svc.create_room(pr, pid, {"name": "Assistant: pi-secretary"})            # what a lost race would have left behind
    assert setup.find(PI_TYPE) == {"room_id": first, "agent_id": agent} or setup.find(PI_TYPE)["agent_id"] == agent
    again = setup.provision(PI_TYPE)
    assert again["created_room"] is False and again["room_id"] == min(
        r["room_id"] for r in svc.list_rooms(pr)["rooms"] if r["name"] == "Assistant: pi-secretary")


def test_concurrent_setups_create_one_room(world):
    import threading
    svc, pr, setup = world
    enroll(svc, pr)
    results = []
    threads = [threading.Thread(target=lambda: results.append(setup.provision(PI_TYPE))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 6 and len({r["room_id"] for r in results}) == 1
    assert len([r for r in svc.list_rooms(pr)["rooms"] if r["name"] == "Assistant: pi-secretary"]) == 1


def test_two_agents_of_the_same_name_are_named_in_the_error_not_blamed_on_enrolment(world):
    svc, pr, setup = world
    enroll(svc, pr), enroll(svc, pr)
    with pytest.raises(SelfSetupError, match="2 active agents are named pi-secretary"):
        setup.provision(PI_TYPE)
