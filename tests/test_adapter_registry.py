import pytest

from herdr_engineering.adapter_registry import AdapterRegistry, native_machine_launcher


class FakeScheduler:
    def __init__(self):
        self.released = []

    @staticmethod
    def request(body, principal):
        from herdr_engineering.scheduler import PlacementRequest
        return PlacementRequest(body["request_id"], principal, body["agent_family"],
                                body.get("placement_mode", "automatic"), body["resource_profile"],
                                parent_agent_id=body.get("parent_agent_id"))

    def reserve(self, body, principal):
        return {"state": "reserved", "reservation_id": "r1", "host_uid": "host-b"}

    def commit(self, reservation_id):
        return {"reservation_id": reservation_id, "host_uid": "host-b", "lease_id": "l1"}

    def release(self, reservation_id, state="released"):
        self.released.append((reservation_id, state))


def body(family="codex"):
    return {"request_id": "req-1", "agent_family": family, "placement_mode": "automatic",
            "parent_agent_id": "parent-1", "parent_host_uid": "mac",
            "resource_profile": {"class": "coding", "cpu_cores": 1}}


def test_registry_routes_all_native_families_through_one_admission_boundary():
    scheduler = FakeScheduler()
    registry = AdapterRegistry(scheduler)
    seen = []
    for family in ("pi", "claude-code", "codex", "agent"):
        registry.register(family, lambda host, request: seen.append((host, request.parent_agent_id)) or "started")
    result = registry.launch(body("codex"), "alice")
    assert result.host_uid == "host-b" and seen == [("host-b", "parent-1")]


def test_registry_releases_commit_when_native_launcher_fails():
    scheduler = FakeScheduler()
    registry = AdapterRegistry(scheduler)
    registry.register("pi", lambda *_: (_ for _ in ()).throw(RuntimeError("native failed")))
    with pytest.raises(RuntimeError, match="native failed"):
        registry.launch(body("pi"), "alice")
    assert scheduler.released == [("r1", "released")]


def test_native_launcher_resolves_saved_machine_and_forwards_safe_argv():
    class Machine:
        label = "beast"

    class Adapter:
        def resolve_machine(self, host_uid):
            return Machine() if host_uid == "uid-beast" else None

        def machine_targeted(self, label, command):
            return {"label": label, "command": command}

    launcher = native_machine_launcher(Adapter(), lambda request: ["agent", request.agent_family])
    assert launcher("uid-beast", registry_request()) == {"label": "beast", "command": ["agent", "codex"]}
    with pytest.raises(ValueError, match="control characters"):
        native_machine_launcher(Adapter(), lambda _: ["agent\nrun"])("uid-beast", registry_request())


def registry_request():
    from herdr_engineering.scheduler import PlacementRequest
    return PlacementRequest("native-1", "alice", "codex", "automatic", {"class": "coding"})

