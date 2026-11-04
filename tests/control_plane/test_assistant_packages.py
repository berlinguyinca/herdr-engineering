"""Private immutable assistant package definitions; registration never grants authority."""
from __future__ import annotations

import copy
import threading

import pytest

import herdr_engineering.control_plane.collab as collab_module
from herdr_engineering.assistant_packages import agent_package_record
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab import (
    BASE,
    TOKENS,
    Api,
    Clock,
    _digest,
    enroll,
    invite,
    make_room,
)


@pytest.fixture
def env(acknowledged_sink):
    clock = Clock()
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    auth = StaticTokenAuthenticator({user: _digest(token) for user, token in TOKENS.items()})
    service = CollabService(auth, sink=acknowledged_sink, now=clock,
                            stream_poll_seconds=0.05)
    server = ControlPlaneServer(repo, host="127.0.0.1", port=0, collab=service,
                                api_keys={"op-key": "operator", "view-key": "viewer"})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    yield type("Env", (), {"api": Api(server.port), "svc": service, "repo": repo,
                           "srv": server, "sink": acknowledged_sink, "clock": clock})
    server.shutdown()
    thread.join(timeout=5)


def manifest(*, name="venue-coordinator", version="1.0.0", runtime="pi"):
    artifact = {
        "pi": {"type": "trusted-pi-runtime", "digest": "sha256:" + "1" * 64},
        "docker": {"type": "oci-image",
                   "reference": "registry.example/agent@sha256:" + "2" * 64},
        "apptainer": {"type": "apptainer-sif",
                      "reference": "oras://registry.example/agent@sha256:" + "3" * 64,
                      "digest": "sha256:" + "3" * 64},
    }[runtime]
    return {
        "schema_version": 1,
        "name": name,
        "version": version,
        "charter": {
            "purpose": "Coordinate one user's venue research.",
            "scope": ["event.research"],
            "rules": ["Never contact a venue without a current task grant."],
            "delegation_requests": ["web.read"],
        },
        "capabilities": ["event.coordinate.v1"],
        "runtime": {"kind": runtime, "artifact": artifact},
        "sandbox": {
            "profile": "task-restricted-v1",
            "network": "broker-only",
            "filesystem": "task-only",
            "limits": {"cpu_quota_percent": 100, "memory_bytes": 1_073_741_824, "process_count": 64,
                       "wall_time_seconds": 900},
        },
        "model_policy": {"allowed": ["family:fast", "family:planner"],
                         "default": "family:fast"},
        "references": {
            "procedures": [{"id": "procedure:event-v1", "version": "1.0.0",
                            "digest": "sha256:" + "4" * 64}],
            "knowledge": [{"id": "knowledge:venue-baseline", "version": "1.0.0",
                           "digest": "sha256:" + "5" * 64}],
        },
    }


def path(room, suffix=""):
    return f"{BASE}/rooms/{room}/assistant-packages{suffix}"


def test_owner_registers_definition_only_package_and_reads_it(env):
    _, room = make_room(env.api)
    body = {"manifest": manifest()}
    status, made = env.api.call("POST", path(room), body, user="alice")
    assert status == 201
    expected = agent_package_record(body["manifest"])
    package = made["package"]
    assert package["manifest"] == expected["manifest"]
    assert package["digest"] == expected["digest"]
    assert package["runtime_launch_supported"] is False
    assert package["capabilities_are_requests"] is True
    assert package["status"] == "definition-only"
    assert "owner" not in package and "authority" not in package

    status, listed = env.api.call("GET", path(room), user="alice")
    assert status == 200 and listed["packages"] == [package]
    status, inspected = env.api.call(
        "GET", path(room, "/" + package["package_id"]), user="alice"
    )
    assert status == 200 and inspected["package"] == package


def test_same_content_replays_and_changed_same_version_conflicts(env):
    _, room = make_room(env.api)
    original = manifest()
    status, first = env.api.call("POST", path(room), {"manifest": original}, user="alice")
    assert status == 201
    status, replay = env.api.call("POST", path(room), {"manifest": original}, user="alice")
    assert status == 200 and replay["replayed"] is True
    assert replay["package"] == first["package"]

    changed = copy.deepcopy(original)
    changed["charter"]["purpose"] = "A different immutable definition."
    status, error = env.api.call("POST", path(room), {"manifest": changed}, user="alice")
    assert status == 409 and "version" in error["error"]


def test_owner_only_and_same_room_members_never_see_another_users_definition(env):
    _, room = make_room(env.api)
    assert invite(env.api, room, "bob", role="contributor")[0] == 201
    package = env.api.call("POST", path(room), {"manifest": manifest()}, user="alice")[1]

    for method, suffix, body in [
        ("GET", "", None),
        ("GET", "/" + package["package"]["package_id"], None),
        ("POST", "", {"manifest": manifest(name="bob-attempt")}),
    ]:
        status, _ = env.api.call(method, path(room, suffix), body, user="bob")
        assert status in (403, 404)

    agent = enroll(env.api, "alice")
    status, _ = env.api.call("GET", path(room), agent=agent)
    assert status in (403, 404)


def test_private_registration_does_not_touch_shared_room_feed_or_cursor(env):
    project, room = make_room(env.api)
    assert invite(env.api, room, "bob", role="contributor")[0] == 201
    status, before = env.api.call("GET", f"{BASE}/rooms/{room}/state", user="bob")
    assert status == 200
    secret = "private-package-detail"
    value = manifest()
    value["charter"]["purpose"] = secret
    status, made = env.api.call("POST", path(room), {"manifest": value}, user="alice")
    assert status == 201

    status, feed = env.api.call("GET", f"{BASE}/rooms/{room}/events", user="bob")
    assert status == 200 and secret not in str(feed)
    status, unchanged = env.api.call(
        "GET", f"{BASE}/rooms/{room}/state?since={before['seq']}", user="bob"
    )
    assert status == 200 and unchanged == {"seq": before["seq"], "changed": False}
    status, audit = env.api.call("GET", f"{BASE}/audit?project_id={project}", user="alice")
    assert status == 200 and secret not in str(audit)
    assert made["package"]["digest"] not in str(audit)
    assert made["package"]["package_id"] not in str(audit)


def test_hydration_restores_owner_private_records_and_replay(env):
    _, room = make_room(env.api)
    value = manifest(runtime="docker")
    status, made = env.api.call("POST", path(room), {"manifest": value}, user="alice")
    assert status == 201

    restored = CollabService(
        StaticTokenAuthenticator({user: _digest(token) for user, token in TOKENS.items()}),
        now=env.clock,
    )
    restored.hydrate(list(env.svc._log))
    alice = restored.authenticate(f"Bearer {TOKENS['alice']}", None)
    bob = restored.authenticate(f"Bearer {TOKENS['bob']}", None)
    assert restored.list_assistant_packages(alice, room)["packages"] == [made["package"]]
    assert restored.create_assistant_package(alice, room, {"manifest": value}) == (
        200, {"package": made["package"], "replayed": True}
    )
    with pytest.raises(Exception) as denied:
        restored.list_assistant_packages(bob, room)
    assert denied.value.status in (403, 404)


def test_unknown_scope_and_secret_shaped_fields_are_rejected_without_echo(env):
    _, room = make_room(env.api)
    secret = "never-echo-this-credential"
    bad = manifest()
    bad["credential"] = secret
    cases = [
        {"manifest": manifest(), "owner_user_id": "bob"},
        {"manifest": manifest(), "authority": {"launch": True}},
        {"manifest": bad},
        {"manifest": manifest(), "extra": secret},
    ]
    for body in cases:
        status, result = env.api.call("POST", path(room), body, user="alice")
        assert status == 400
        assert secret not in str(result)


def test_limits_and_current_owner_access_are_rechecked(env, monkeypatch):
    _, room = make_room(env.api)
    monkeypatch.setattr(collab_module, "MAX_ASSISTANT_PACKAGES_PER_OWNER_ROOM", 1)
    monkeypatch.setattr(collab_module, "ASSISTANT_PACKAGE_RATE", 100)
    assert env.api.call("POST", path(room), {"manifest": manifest()}, user="alice")[0] == 201
    assert env.api.call(
        "POST", path(room), {"manifest": manifest(name="second")}, user="alice"
    )[0] == 429

    principal = env.svc.authenticate(f"Bearer {TOKENS['alice']}", None)
    env.svc.auth.disable("alice")
    with pytest.raises(Exception) as denied:
        env.svc.list_assistant_packages(principal, room)
    assert denied.value.status == 404
