"""Pi package activation is private, attested and receipt-driven."""
from __future__ import annotations

import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import (
    CollabError,
    CollabService,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_assistant_packages import manifest
from tests.control_plane.test_collab import BASE, TOKENS, Api, Clock, _digest, invite, make_room


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
    thread.start(); server.wait_until_ready()
    yield type("Env", (), {"api": Api(server.port), "svc": service, "repo": repo,
                           "srv": server, "sink": acknowledged_sink, "clock": clock})
    server.shutdown(); thread.join(timeout=5)


def operator(env, method, parts, body=None, query=None):
    return env.svc.dispatch(method, parts, query or {}, body or {}, "", None, operator="mallory")


def package(env, room, *, runtime="pi", name="venue-coordinator"):
    status, out = env.api.call("POST", f"{BASE}/rooms/{room}/assistant-packages",
                               {"manifest": manifest(runtime=runtime, name=name)}, user="alice")
    assert status == 201
    return out["package"]


def attestation(env, record, *, host_id="local-linux", nonce="nonce-0123456789abcdef",
                models=("family:fast", "family:planner"), expires_delta=240,
                role_bindings=None):
    return {
        "schema_version": 1, "host_id": host_id, "profile": "task-restricted-v1",
        "profile_digest": "sha256:" + "a" * 64,
        "runtime": {"kind": "pi", "artifact_digest": record["manifest"]["runtime"]["artifact"]["digest"],
                    "entrypoint_id": "pi-fixed-v1"},
        "enforcement": {"bubblewrap": True, "cgroup_v2": True, "broker_only": True,
                        "task_only": True, "no_new_privileges": True,
                        "limits": record["manifest"]["sandbox"]["limits"]},
        "approved_models": list(models), "issued_at": env.clock.t,
        "expires_at": env.clock.t + expires_delta, "nonce": nonce,
        "role_bindings": (record["manifest"]["references"]["procedures"]
                          if role_bindings is None else role_bindings),
    }


def attest(env, record, **changes):
    body = attestation(env, record, **changes)
    status, out = operator(env, "POST", ["fleet", "assistant-runtime-hosts"], body)
    assert status == 201, out
    return body, out


def request_body(record, **changes):
    body = {"package_id": record["package_id"], "host_id": "local-linux",
            "name": "venue-agent", "idempotency_key": "activate-venue-v1"}
    body.update(changes)
    return body


def request_runtime(env, room, record, **changes):
    return env.api.call("POST", f"{BASE}/rooms/{room}/assistant-runtimes",
                        request_body(record, **changes), user="alice")


def claim(env, runtime_id, *, host_id="local-linux", nonce="nonce-0123456789abcdef"):
    return operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "claim"],
                    {"host_id": host_id, "attestation_nonce": nonce})


def checkpoint(sequence=1, phase="sandbox_started", output=None):
    return {"sequence": sequence, "phase": phase, "output_sha256": output}


def test_operator_attestation_is_strict_fresh_and_publicly_summarized(env):
    _, room = make_room(env.api); record = package(env, room); body = attestation(env, record)
    assert env.api.call("POST", f"{BASE}/fleet/assistant-runtime-hosts", body, user="alice")[0] == 404
    status, made = operator(env, "POST", ["fleet", "assistant-runtime-hosts"], body)
    assert status == 201 and "nonce" not in made["host"] and "artifact_digest" not in made["host"]
    status, hosts = env.api.call("GET", f"{BASE}/rooms/{room}/assistant-hosts", user="alice")
    assert status == 200
    assert hosts == {"hosts": [{"host_id": "local-linux", "label": "local-linux", "status": "ready",
        "attested_at": env.clock.t, "expires_at": env.clock.t + 240,
        "supported_runtime_kinds": ["pi"], "sandbox_profiles": ["task-restricted-v1"],
        "models": ["family:fast", "family:planner"],
        "role_bindings": record["manifest"]["references"]["procedures"],
        "capabilities": [], "resources": {}}]}                    # a runner that reports none is shown as none
    assert invite(env.api, room, "bob", role="contributor")[0] == 201
    assert env.api.call("GET", f"{BASE}/rooms/{room}/assistant-hosts", user="bob")[0] in (403, 404)
    for bad in [{**body, "command": "pi --dangerous"}, {**body, "expires_at": env.clock.t + 301},
                {**body, "issued_at": env.clock.t + 60},
                {**body, "enforcement": {**body["enforcement"], "broker_only": False}}]:
        assert operator(env, "POST", ["fleet", "assistant-runtime-hosts"], bad)[0] == 400


def test_owner_request_is_immutable_definition_only_and_model_scoped(env):
    _, room = make_room(env.api); record = package(env, room); attest(env, record)
    status, made = request_runtime(env, room, record)
    assert status == 202
    runtime = made["runtime"]
    assert runtime["status"] == "queued" and runtime["selected_model"] == "family:fast"
    assert runtime["access"] == ["execute", "read"]
    assert runtime["agent_id"] is None and runtime["launch_attestation"] is None
    assert "credential" not in str(runtime) and "claim_token" not in str(runtime)
    status, replay = request_runtime(env, room, record)
    assert status == 200 and replay == {"runtime": runtime, "replayed": True}
    assert request_runtime(env, room, record, name="different")[0] == 409
    assert request_runtime(env, room, record, selected_model="family:not-approved")[0] == 409
    assert request_runtime(env, room, record, command="pi")[0] == 400
    docker = package(env, room, runtime="docker", name="docker-agent")
    assert request_runtime(env, room, docker, idempotency_key="docker-attempt")[0] == 409
    mismatched_manifest = manifest(name="different-pi")
    mismatched_manifest["runtime"]["artifact"]["digest"] = "sha256:" + "9" * 64
    status, other = env.api.call("POST", f"{BASE}/rooms/{room}/assistant-packages",
                                 {"manifest": mismatched_manifest}, user="alice")
    assert status == 201
    assert request_runtime(env, room, other["package"], idempotency_key="digest-mismatch",
                           name="different-agent")[0] == 409


def test_idempotent_replay_survives_host_rotation_and_expiry(env):
    _, room = make_room(env.api); record = package(env, room); attest(env, record)
    status, first = request_runtime(env, room, record)
    assert status == 202
    attest(env, record, nonce="nonce-rotated-012345")
    status, replay = request_runtime(env, room, record)
    assert status == 200 and replay["replayed"] is True
    assert replay["runtime"]["runtime_id"] == first["runtime"]["runtime_id"]
    assert replay["runtime"]["status"] == "failed"
    assert replay["runtime"]["error_code"] == "host_attestation_expired"

    status, second = request_runtime(env, room, record, idempotency_key="expiry-replay",
                                     name="expiry-agent")
    assert status == 202
    env.clock.t += 301
    status, expired = request_runtime(env, room, record, idempotency_key="expiry-replay",
                                      name="expiry-agent")
    assert status == 200 and expired["replayed"] is True
    assert expired["runtime"]["runtime_id"] == second["runtime"]["runtime_id"]
    assert expired["runtime"]["status"] == "failed"


def test_claim_mints_one_private_credential_and_revalidates_host_policy(env):
    _, room = make_room(env.api); record = package(env, room); attested, _ = attest(env, record)
    runtime_id = request_runtime(env, room, record)[1]["runtime"]["runtime_id"]
    assert env.api.call(
        "POST", f"{BASE}/fleet/assistant-runtimes/{runtime_id}/claim",
        {"host_id": "local-linux", "attestation_nonce": "nonce-0123456789abcdef"},
        user="alice",
    )[0] == 404
    assert claim(env, runtime_id, nonce="wrong-nonce")[0] == 409
    status, claimed = claim(env, runtime_id)
    assert status == 200 and claimed["runtime"]["status"] == "claimed"
    assert claimed["credential"].startswith("hca_") and claimed["claim_token"] and claimed["device_id"]
    assert claimed["actions"] == ["execute", "read"] and claim(env, runtime_id)[0] == 409
    assert claimed["package"]["required_role_bindings"] == \
        record["manifest"]["references"]["procedures"]
    status, public = env.api.call("GET", f"{BASE}/rooms/{room}/assistant-runtimes/{runtime_id}", user="alice")
    assert status == 200 and public["runtime"]["agent_id"] == claimed["runtime"]["agent_id"]
    assert "credential" not in str(public) and "claim_token" not in str(public)
    agent = env.svc.authenticate(f"Agent {claimed['credential']}", claimed["device_id"])
    assert env.svc.agent_rooms(agent)["rooms"][0]["actions"] == ["execute", "read"]
    alice = env.svc.authenticate(f"Bearer {TOKENS['alice']}", None)
    task = env.svc.create_task(alice, room, {
        "target_agent_id": claimed["runtime"]["agent_id"], "text": "Prepare a local summary",
        "idempotency_key": "runtime-task-1",
    })
    assert env.svc.task_inbox(agent)["tasks"][0]["task_id"] == task["task_id"]
    assert env.svc.update_task(agent, task["task_id"], {"status": "accepted"})["status"] == "accepted"
    assert env.svc.update_task(agent, task["task_id"], {
        "status": "done", "result": "Summary ready",
    })["status"] == "done"
    env.svc.remove_agent_from_room(alice, room, claimed["runtime"]["agent_id"])
    assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "heartbeat"], {
        "claim_token": claimed["claim_token"], "attestation_nonce": attested["nonce"],
        "checkpoint": checkpoint(1, "grant_removed"),
    })[0] == 409


def test_claim_rechecks_changed_room_model_policy(env):
    _, room = make_room(env.api); record = package(env, room); attest(env, record)
    runtime_id = request_runtime(env, room, record)[1]["runtime"]["runtime_id"]
    alice = env.svc.authenticate(f"Bearer {TOKENS['alice']}", None)
    env.svc.set_room_profile(alice, room, {"allowed_models": ["family:planner"]})
    assert claim(env, runtime_id)[0] == 409
    status, viewed = env.api.call(
        "GET", f"{BASE}/rooms/{room}/assistant-runtimes/{runtime_id}", user="alice"
    )
    assert status == 200 and viewed["runtime"]["status"] == "failed"
    assert viewed["runtime"]["error_code"] == "runtime_policy_changed"


def test_heartbeat_rechecks_policy_and_lease_directly_caps_agent_authority(env):
    _, room = make_room(env.api); record = package(env, room); attested, _ = attest(env, record)
    runtime_id = request_runtime(env, room, record)[1]["runtime"]["runtime_id"]
    claimed = claim(env, runtime_id)[1]
    agent = env.svc.authenticate(f"Agent {claimed['credential']}", claimed["device_id"])
    alice = env.svc.authenticate(f"Bearer {TOKENS['alice']}", None)
    task = env.svc.create_task(alice, room, {"target_agent_id": claimed["runtime"]["agent_id"],
                                             "text": "Lease-bound task"})
    env.svc.update_task(agent, task["task_id"], {"status": "accepted"})
    env.clock.t += 121
    # No runtime status endpoint or sweep is called: `_effective` checks the grant deadline itself.
    assert env.svc.task_inbox(agent) == {"tasks": []}
    with pytest.raises(CollabError) as denied:
        env.svc.update_task(agent, task["task_id"], {"status": "done", "result": "too late"})
    assert denied.value.status in (403, 404)
    assert env.svc.get_assistant_runtime(alice, room, runtime_id)["runtime"]["status"] == "failed"
    assert env.svc._tasks[task["task_id"]]["status"] == "failed"

    # A new activation's active receipt rechecks current room model policy.
    env.clock.t -= 121
    record2 = package(env, room, name="policy-check")
    attest(env, record2, nonce="nonce-policy-0123456")
    second = request_runtime(env, room, record2, idempotency_key="policy-run",
                             name="policy-agent")[1]["runtime"]
    second_claim = claim(env, second["runtime_id"], nonce="nonce-policy-0123456")[1]
    env.svc.set_room_profile(alice, room, {"allowed_models": ["family:planner"]})
    status, _ = operator(env, "POST", ["fleet", "assistant-runtimes",
                                      second["runtime_id"], "heartbeat"], {
        "claim_token": second_claim["claim_token"],
        "attestation_nonce": "nonce-policy-0123456",
        "checkpoint": checkpoint(1, "policy_changed"),
    })
    assert status == 409
    status, viewed = env.api.call(
        "GET", f"{BASE}/rooms/{room}/assistant-runtimes/{second['runtime_id']}", user="alice"
    )
    assert status == 200 and viewed["runtime"]["status"] == "failed"


def test_runner_receipts_make_ready_and_checkpoints_are_monotonic(env):
    _, room = make_room(env.api); record = package(env, room); attested, _ = attest(env, record)
    runtime_id = request_runtime(env, room, record)[1]["runtime"]["runtime_id"]
    claimed = claim(env, runtime_id)[1]; token = claimed["claim_token"]
    starting = {"claim_token": token, "attestation_nonce": attested["nonce"],
                "status": "starting", "checkpoint": checkpoint()}
    assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], starting)[0] == 200
    assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "heartbeat"], {
        "claim_token": token, "attestation_nonce": attested["nonce"],
        "checkpoint": checkpoint(2, "pi_started", "sha256:" + "b" * 64)})[0] == 200
    assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "heartbeat"], {
        "claim_token": token, "attestation_nonce": attested["nonce"],
        "checkpoint": checkpoint(2, "duplicate")})[0] == 409
    launch = {"profile_digest": attested["profile_digest"],
              "runtime_artifact_digest": attested["runtime"]["artifact_digest"],
              "enforcement": attested["enforcement"],
              "role_bindings": record["manifest"]["references"]["procedures"]}
    ready = {"claim_token": token, "attestation_nonce": attested["nonce"], "status": "ready",
             "checkpoint": checkpoint(3, "ready"), "launch_attestation": launch,
             "instance_id": "pi-instance-1"}
    status, out = operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], ready)
    assert status == 200 and out["runtime"]["status"] == "ready"
    assert out["runtime"]["launch_attestation"] == launch and out["runtime"]["checkpoint"] == ready["checkpoint"]
    wrong = {**ready, "claim_token": "wrong-token", "checkpoint": checkpoint(4, "ready")}
    assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], wrong)[0] == 404


def test_secretary_role_must_be_host_configured_and_exactly_bound_at_ready(env):
    _, room = make_room(env.api)
    secretary = manifest(name="secretary")
    role = {"id": "procedure:secretary-system", "version": "1.0.0",
            "digest": "sha256:942e5b1002d74a9d52ae2fb7c2f8ef07f4f6b08aa1b9e08a36b8e0504ffab120"}
    secretary["references"]["procedures"] = [role]
    status, made = env.api.call("POST", f"{BASE}/rooms/{room}/assistant-packages",
                                {"manifest": secretary}, user="alice")
    assert status == 201
    record = made["package"]
    attest(env, record, role_bindings=[], nonce="nonce-no-role-01234567")
    assert request_runtime(env, room, record)[0] == 409

    attested, _ = attest(env, record, role_bindings=[role], nonce="nonce-secretary-012345")
    status, requested = request_runtime(env, room, record)
    assert status == 202
    runtime_id = requested["runtime"]["runtime_id"]
    claimed = claim(env, runtime_id, nonce=attested["nonce"])[1]
    assert claimed["package"]["required_role_bindings"] == [role]
    assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], {
        "claim_token": claimed["claim_token"], "attestation_nonce": attested["nonce"],
        "status": "starting", "checkpoint": checkpoint(1, "starting")})[0] == 200
    base_launch = {"profile_digest": attested["profile_digest"],
                   "runtime_artifact_digest": attested["runtime"]["artifact_digest"],
                   "enforcement": attested["enforcement"]}
    for roles in ([], [{**role, "digest": "sha256:" + "0" * 64}],
                  [role, {"id": "procedure:extra-system", "version": "1.0.0",
                          "digest": "sha256:" + "1" * 64}]):
        body = {"claim_token": claimed["claim_token"], "attestation_nonce": attested["nonce"],
                "status": "ready", "checkpoint": checkpoint(2, "ready"),
                "launch_attestation": {**base_launch, "role_bindings": roles},
                "instance_id": "secretary-instance"}
        assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], body)[0] == 409
    ready = {"claim_token": claimed["claim_token"], "attestation_nonce": attested["nonce"],
             "status": "ready", "checkpoint": checkpoint(2, "ready"),
             "launch_attestation": {**base_launch, "role_bindings": [role]},
             "instance_id": "secretary-instance"}
    status, result = operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], ready)
    assert status == 200 and result["runtime"]["launch_attestation"]["role_bindings"] == [role]
    status, public = env.api.call("GET", f"{BASE}/rooms/{room}/assistant-runtimes/{runtime_id}", user="alice")
    assert status == 200 and public["runtime"]["launch_attestation"]["role_bindings"] == [role]


def test_empty_role_package_remains_compatible(env):
    _, room = make_room(env.api)
    value = manifest(name="roleless")
    value["references"]["procedures"] = []
    status, made = env.api.call("POST", f"{BASE}/rooms/{room}/assistant-packages",
                                {"manifest": value}, user="alice")
    assert status == 201
    record = made["package"]
    attested, _ = attest(env, record, role_bindings=[])
    runtime_id = request_runtime(env, room, record)[1]["runtime"]["runtime_id"]
    claimed = claim(env, runtime_id)[1]
    assert claimed["package"]["required_role_bindings"] == []
    assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], {
        "claim_token": claimed["claim_token"], "attestation_nonce": attested["nonce"],
        "status": "ready", "checkpoint": checkpoint(1, "ready"),
        "launch_attestation": {"profile_digest": attested["profile_digest"],
            "runtime_artifact_digest": attested["runtime"]["artifact_digest"],
            "enforcement": attested["enforcement"], "role_bindings": []},
        "instance_id": "roleless-instance"})[0] == 200


def test_cancellation_revokes_execution_before_runner_receipt(env):
    _, room = make_room(env.api); record = package(env, room); attested, _ = attest(env, record)
    runtime_id = request_runtime(env, room, record)[1]["runtime"]["runtime_id"]
    claimed = claim(env, runtime_id)[1]; token = claimed["claim_token"]; aid = claimed["runtime"]["agent_id"]
    alice = env.svc.authenticate(f"Bearer {TOKENS['alice']}", None)
    agent = env.svc.authenticate(f"Agent {claimed['credential']}", claimed["device_id"])
    task = env.svc.create_task(alice, room, {"target_agent_id": aid, "text": "Long task"})
    env.svc.update_task(agent, task["task_id"], {"status": "accepted"})
    status, cancel = env.api.call("POST", f"{BASE}/rooms/{room}/assistant-runtimes/{runtime_id}/cancel", {}, user="alice")
    assert status == 200 and cancel["runtime"]["cancel_requested"] is True
    assert cancel["runtime"]["status"] == "claimed"
    assert aid not in [row["agent_id"] for row in env.svc.list_room_agents(alice, room)["agents"]]
    status, poll = operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "cancel-status"],
                            {"claim_token": token})
    assert status == 200 and poll == {"runtime_id": runtime_id, "cancel_requested": True,
                                      "reason": "owner_requested"}
    assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], {
        "claim_token": token, "attestation_nonce": attested["nonce"], "status": "starting",
        "checkpoint": checkpoint(1, "starting_after_cancel")})[0] == 409
    launch = {"profile_digest": attested["profile_digest"],
              "runtime_artifact_digest": attested["runtime"]["artifact_digest"],
              "enforcement": attested["enforcement"],
              "role_bindings": record["manifest"]["references"]["procedures"]}
    assert operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], {
        "claim_token": token, "attestation_nonce": attested["nonce"], "status": "ready",
        "checkpoint": checkpoint(1, "ready_after_cancel"), "launch_attestation": launch,
        "instance_id": "should-not-start"})[0] == 409
    status, done = operator(env, "POST", ["fleet", "assistant-runtimes", runtime_id, "report"], {
        "claim_token": token, "attestation_nonce": attested["nonce"], "status": "cancelled",
        "checkpoint": checkpoint(1, "cancelled")})
    assert status == 200 and done["runtime"]["status"] == "cancelled"
    assert env.svc._agents[aid]["status"] == "revoked"
    assert env.svc._tasks[task["task_id"]]["status"] == "failed"


def test_hydration_preserves_claim_token_hash_but_no_credentials(env):
    _, room = make_room(env.api); record = package(env, room); attested, _ = attest(env, record)
    runtime_id = request_runtime(env, room, record)[1]["runtime"]["runtime_id"]
    claimed = claim(env, runtime_id)[1]
    restored = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in TOKENS.items()}), now=env.clock)
    restored.hydrate(list(env.svc._log))
    status, heartbeat = restored.dispatch("POST", ["fleet", "assistant-runtimes", runtime_id, "heartbeat"], {}, {
        "claim_token": claimed["claim_token"], "attestation_nonce": attested["nonce"],
        "checkpoint": checkpoint(1, "restored")}, "", None, operator="mallory")
    assert status == 200 and heartbeat["runtime"]["checkpoint"]["phase"] == "restored"
    serialized = str(restored._log)
    assert claimed["claim_token"] not in serialized and claimed["credential"] not in serialized


def test_private_lifecycle_never_enters_room_feed_cursor_or_public_audit(env):
    project, room = make_room(env.api); record = package(env, room)
    assert invite(env.api, room, "bob", role="contributor")[0] == 201
    status, before = env.api.call("GET", f"{BASE}/rooms/{room}/state", user="bob")
    assert status == 200
    attest(env, record)
    made = request_runtime(env, room, record)[1]["runtime"]
    assert claim(env, made["runtime_id"])[0] == 200
    status, events = env.api.call("GET", f"{BASE}/rooms/{room}/events", user="bob")
    assert status == 200 and made["runtime_id"] not in str(events)
    status, unchanged = env.api.call(
        "GET", f"{BASE}/rooms/{room}/state?since={before['seq']}", user="bob"
    )
    assert status == 200 and unchanged == {"seq": before["seq"], "changed": False}
    status, audit = env.api.call("GET", f"{BASE}/audit?project_id={project}", user="alice")
    assert status == 200
    assert made["package_digest"] not in str(audit)
    assert "nonce-0123456789abcdef" not in str(audit)


def test_lease_expiry_and_owner_loss_fail_closed(env):
    _, room = make_room(env.api); record = package(env, room); attest(env, record)
    runtime_id = request_runtime(env, room, record)[1]["runtime"]["runtime_id"]
    claimed = claim(env, runtime_id)[1]; aid = claimed["runtime"]["agent_id"]
    env.clock.t += 121
    status, viewed = env.api.call("GET", f"{BASE}/rooms/{room}/assistant-runtimes/{runtime_id}", user="alice")
    assert status == 200 and viewed["runtime"]["status"] == "failed"
    assert viewed["runtime"]["error_code"] == "runtime_lease_expired"
    assert env.svc._agents[aid]["status"] == "revoked"
    record2 = package(env, room, name="second-package")
    attest(env, record2, nonce="nonce-second-01234567")
    second = request_runtime(env, room, record2, idempotency_key="second-run", name="second-agent")[1]["runtime"]
    env.svc.auth.disable("alice")
    assert claim(env, second["runtime_id"], nonce="nonce-second-01234567")[0] == 409
