"""Durable coordinator exercises real files and a deterministic wire fixture."""

import copy
import json

import pytest

SHA = "a" * 40


def test_rollout_commands_are_registered():
    from herdr_engineering.cli import _build_parser

    args = _build_parser().parse_args(["fleet", "rollout", "plan", "--pi-revision", SHA])
    assert args.rollout_command == "plan"


class Wire:
    def __init__(self):
        self.offline = {"beast"}
        self.calls = []
        self.receipts = {}
        self.mutations = []
        self.progress = False
        self.corrupt = False
        self.rows = {h: self.row(h) for h in ("bender", "beast")}

    def row(self, host):
        return {
            "descriptor": {
                "instanceId": host + "-instance",
                "sessionId": host + "-conversation",
                "pid": 42,
            },
            "reachable": True,
            "reply": {
                "ok": True,
                "data": {
                    "runtime": {
                        "commit": "b" * 40,
                        "hostCommit": SHA,
                        "generation": 1,
                        "capabilities": [
                            "runtime.describe",
                            "runtime.prepare",
                            "runtime.reload",
                            "mission.resumeAfterMaintenance",
                        ],
                        "pending": False,
                    },
                    "missions": [
                        {
                            "id": "M1",
                            "status": "BLOCKED",
                            "revision": 2,
                            "resumptionGeneration": 0,
                            "operatorPaused": False,
                            "resumeEligible": True,
                            "lastMeaningfulProgressAt": None,
                        }
                    ],
                },
            },
        }

    def stage(self, revision, *, host, inventory):
        if host in self.offline:
            return {"ok": False, "error": "offline"}
        return {
            "ok": True,
            "revision": revision,
            "host": host,
            "sourceRoot": f"/home/alice/.local/share/herdr-engineering/pi/releases/{revision}",
        }

    def native(self, target):
        return [
            {
                "agent": "pi",
                "terminal_id": "terminal-1",
                "agent_session": {"value": target["host"] + "-conversation"},
                "session": "main",
            }
        ]

    def sessions(self, target, root):
        if self.corrupt:
            return {"unexpected": "object"}
        row = copy.deepcopy(self.rows[target["host"]])
        if self.progress:
            row["reply"]["data"]["missions"][0]["lastMeaningfulProgressAt"] = "2026-10-17T22:00:00Z"
        return [row]

    def maintenance(self, target, root, instance, request):
        self.calls.append(copy.deepcopy(request))
        key = (target["host"], request["operationId"])
        if request["action"] == "status":
            return self.receipts[key]
        if key in self.receipts:
            return self.receipts[key]
        self.mutations.append(key)
        row = self.rows[target["host"]]
        assert instance == row["descriptor"]["instanceId"]
        if request["action"] == "reload":
            row["reply"]["data"]["runtime"]["commit"] = SHA
            row["reply"]["data"]["runtime"]["generation"] += 1
            row["descriptor"]["instanceId"] += "-new"
        receipt = {
            "operationId": request["operationId"],
            "action": request["action"],
            "status": "succeeded",
            "result": {"status": "ready" if request["action"] == "prepare" else "resumed"},
        }
        result = {"maintenance": receipt}
        self.receipts[key] = result
        return result


def setup_rollout(tmp_path):
    from herdr_engineering.fleet_rollout import RolloutStore, create_rollout

    store = RolloutStore(tmp_path / "state")
    plan = create_rollout(
        store,
        SHA,
        [{"host": h, "sshTarget": h, "port": 22, "local": False} for h in ("bender", "beast")],
        inventory=tmp_path / "inventory.yml",
    )
    return store, plan


def apply(store, plan, wire):
    import shlex
    import subprocess
    from types import SimpleNamespace

    from herdr_engineering.fleet_rollout import PiTransport, apply_rollout

    class Adapter:
        def list_agents(self, machine=None, session=None):
            return [
                SimpleNamespace(to_dict=lambda row=row: row)
                for row in wire.native({"host": machine})
            ]

    def runner(argv, **kwargs):
        # Exercise production command construction and JSON decoding at the subprocess seam.
        host = argv[-2]
        remote = shlex.split(argv[-1])
        assert remote[0] == "node"
        root = remote[1].removesuffix("/scripts/pi-engineering.ts")
        if remote[3] == "list":
            reply = wire.sessions({"host": host}, root)
        else:
            assert remote[3] == "maintenance"
            reply = wire.maintenance({"host": host}, root, remote[4], json.loads(remote[6]))
        return subprocess.CompletedProcess(
            argv,
            getattr(wire, "returncode", 0) if remote[3] == "maintenance" else 0,
            json.dumps(reply),
            "",
        )

    from unittest.mock import patch

    with patch(
        "herdr_engineering.fleet_rollout.inventory_targets",
        side_effect=lambda inventory, host: [plan["hosts"][host]["target"]],
    ):
        return apply_rollout(
            store,
            plan["id"],
            transport=PiTransport(runner=runner, adapter=Adapter()),
            stage=wire.stage,
        )


def test_two_hosts_offline_restart_and_progress(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    first = apply(store, plan, wire)
    assert first["status"] == "pending"
    assert first["hosts"]["beast"]["status"] == "pending"
    assert (
        first["hosts"]["bender"]["sessions"]["bender-conversation"]["status"] == "awaiting_progress"
    )
    mutations = len(wire.mutations)
    from herdr_engineering.fleet_rollout import RolloutStore

    store = RolloutStore(store.root)
    apply(store, plan, wire)
    assert len(wire.mutations) == mutations
    wire.offline.clear()
    apply(store, plan, wire)
    wire.progress = True
    final = apply(store, plan, wire)
    assert final["status"] == "complete"
    assert final["revision"] == SHA
    assert len(wire.mutations) == len(set(wire.mutations))
    assert (store.root / (plan["id"] + ".json")).stat().st_mode & 0o777 == 0o600


def test_operator_pause_and_legacy_runtime_are_explicit(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    wire.offline.clear()
    wire.rows["bender"]["reply"]["data"]["missions"][0]["operatorPaused"] = True
    wire.rows["beast"]["reply"]["data"]["runtime"]["capabilities"] = []
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"] == "complete"
    assert (
        result["hosts"]["beast"]["sessions"]["beast-conversation"]["status"] == "bootstrap_required"
    )
    assert all(r["action"] != "resume" for r in wire.calls)
    assert result["status"] != "complete"


@pytest.mark.parametrize("bad", ["main", "../release", "-oProxyCommand=oops", "a" * 39])
def test_invalid_release_never_creates_state(tmp_path, bad):
    from herdr_engineering.fleet_rollout import RolloutStore, create_rollout

    with pytest.raises(ValueError):
        create_rollout(
            RolloutStore(tmp_path / "state"), bad, [{"host": "box"}], inventory=tmp_path / "inv"
        )


def test_missing_native_coverage_and_corrupt_reply_never_complete(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    wire.corrupt = True
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["status"] == "blocked"
    assert not wire.mutations
    wire.corrupt = False
    wire.native = lambda target: [{"agent": "pi", "terminal_id": "t", "agent_session": None}]
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["status"] == "blocked"
    assert not wire.mutations


def test_pid_replacement_and_ambiguous_conversation_fail_closed(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    apply(store, plan, wire)
    wire.rows["bender"]["descriptor"]["pid"] += 1
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["status"] == "blocked"
    assert "identity" in result["hosts"]["bender"]["error"]


def test_status_is_read_only_and_plan_is_immutable(tmp_path):
    store, plan = setup_rollout(tmp_path)
    path = store.root / (plan["id"] + ".json")
    before = path.stat().st_mtime_ns
    assert store.read(plan["id"])["revision"] == SHA
    assert path.stat().st_mtime_ns == before
    altered = copy.deepcopy(plan)
    altered["revision"] = "b" * 40
    with pytest.raises(ValueError, match="immutable"):
        store.write(altered)
    assert json.loads(path.read_text())["revision"] == SHA


def test_ssh_transport_quotes_json_and_rejects_bad_targets(tmp_path):
    import shlex
    import subprocess

    from herdr_engineering.fleet_rollout import PiTransport

    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        remote = shlex.split(argv[-1])
        assert remote[:3] == [
            "node",
            "/home/a b/releases/cli/scripts/pi-engineering.ts",
            "sessions",
        ]
        assert json.loads(remote[-1])["operationId"] == "op1"
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"ok": True, "data": {"maintenance": {"operationId": "op1"}}}), ""
        )

    transport = PiTransport(runner=runner)
    target = {"host": "box", "sshTarget": "alice@box", "port": 22, "local": False}
    reply = transport.maintenance(
        target, "/home/a b/releases/cli", "instance-1", {"operationId": "op1", "action": "status"}
    )
    assert reply["maintenance"]["operationId"] == "op1"
    assert calls[0][-2] == "alice@box"
    target["sshTarget"] = "-oProxyCommand=malicious"
    with pytest.raises(ValueError):
        transport.maintenance(target, "/tmp", "i", {"operationId": "op1"})
    assert len(calls) == 1


def test_uncertain_delivery_replays_same_receipt_after_controller_restart(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = wire.maintenance
    lost = False

    def lose_reply(target, root, instance, request):
        nonlocal lost
        reply = original(target, root, instance, request)
        if request["action"] == "resume" and not lost:
            lost = True
            raise ConnectionError("reply lost after mutation")
        return reply

    wire.maintenance = lose_reply
    first = apply(store, plan, wire)
    assert first["hosts"]["bender"]["status"] == "pending"
    apply(store, plan, wire)
    resumes = [r for r in wire.calls if r["action"] == "resume"]
    assert len(resumes) == 2
    assert resumes[0] == resumes[1]
    assert len(wire.mutations) == 3


def test_expired_named_wait_does_not_remain_verified(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    mission = wire.rows["bender"]["reply"]["data"]["missions"][0]
    mission["namedWait"] = {"action": "probe", "nextActionAt": "2099-01-01T00:00:00Z"}
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"] == "complete"
    mission["namedWait"]["nextActionAt"] = "2000-01-01T00:00:00Z"
    result = apply(store, plan, wire)
    assert (
        result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"]
        == "awaiting_progress"
    )


def test_busy_prepare_retries_with_new_id_without_reloading(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = wire.maintenance
    busy = True

    def prepare_busy(target, root, instance, request):
        if request["action"] == "prepare" and busy:
            return {
                "maintenance": {
                    "operationId": request["operationId"],
                    "action": "prepare",
                    "status": "skipped",
                    "result": {"status": "busy"},
                }
            }
        return original(target, root, instance, request)

    wire.maintenance = prepare_busy
    result = apply(store, plan, wire)
    assert not wire.mutations
    assert result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"] == "pending"
    busy = False
    result = apply(store, plan, wire)
    assert (
        result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"]
        == "awaiting_progress"
    )


def test_wrong_receipt_cannot_advance_session(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    wire.maintenance = lambda *args: {
        "maintenance": {"operationId": "wrong", "action": "prepare", "status": "succeeded"}
    }
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["status"] == "blocked"
    assert result["hosts"]["bender"]["sessions"]["bender-conversation"].get("prepared") is None


def test_reload_success_without_actual_activation_cannot_resume(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = wire.maintenance

    def ineffective_reload(target, root, instance, request):
        reply = original(target, root, instance, request)
        if request["action"] == "reload":
            wire.rows[target["host"]]["reply"]["data"]["runtime"]["commit"] = "b" * 40
        return reply

    wire.maintenance = ineffective_reload
    result = apply(store, plan, wire)
    assert (
        result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"]
        == "activation_unverified"
    )
    assert all(r["action"] != "resume" for r in wire.calls)


def test_duplicate_live_identity_blocks_mutation(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = wire.sessions
    wire.sessions = lambda *args: original(*args) * 2
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["status"] == "blocked"
    assert not wire.mutations


def test_changed_mission_revision_does_not_replace_uncertain_resume(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = wire.maintenance
    lost = False

    def interrupted(target, root, instance, request):
        nonlocal lost
        reply = original(target, root, instance, request)
        if request["action"] == "resume" and not lost:
            lost = True
            wire.rows[target["host"]]["reply"]["data"]["missions"][0]["revision"] += 1
            raise ConnectionError("lost reply")
        return reply

    wire.maintenance = interrupted
    apply(store, plan, wire)
    apply(store, plan, wire)
    resumes = [r for r in wire.calls if r["action"] == "resume"]
    assert len(resumes) == 2 and resumes[0] == resumes[1]


def test_private_state_rejects_symlink_and_concurrent_apply(tmp_path):
    from herdr_engineering.fleet_rollout import RolloutStore

    store, plan = setup_rollout(tmp_path)
    with store.lock(), pytest.raises(BlockingIOError), store.lock():
        pass
    link = tmp_path / "link"
    link.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(ValueError, match="private"):
        with RolloutStore(link).lock():
            pass


def test_progress_timestamp_regression_is_not_progress(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    mission = wire.rows["bender"]["reply"]["data"]["missions"][0]
    mission["lastMeaningfulProgressAt"] = "2026-10-17T22:00:00Z"
    apply(store, plan, wire)
    mission["lastMeaningfulProgressAt"] = "2026-10-17T21:00:00Z"
    result = apply(store, plan, wire)
    assert (
        result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"]
        == "awaiting_progress"
    )


def test_old_stable_host_is_bootstrap_required_even_when_runtime_updated(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    wire.rows["bender"]["reply"]["data"]["runtime"]["hostCommit"] = "b" * 40
    result = apply(store, plan, wire)
    state = result["hosts"]["bender"]["sessions"]["bender-conversation"]
    assert state["loaded"]["commit"] == SHA
    assert state["status"] == "bootstrap_required"
    assert all(r["action"] != "resume" for r in wire.calls)


def test_one_stale_session_does_not_starve_other_sessions_on_host(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    second = wire.row("bender")
    second["descriptor"]["sessionId"] = "second-conversation"
    second["descriptor"]["instanceId"] = "second-instance"
    second["descriptor"]["pid"] = 43
    second["reply"]["data"]["runtime"]["commit"] = SHA
    second["reply"]["data"]["missions"] = []
    old_native = wire.native
    wire.native = lambda t: (
        old_native(t)
        + [
            {
                "agent": "pi",
                "terminal_id": "terminal-2",
                "agent_session": {"value": "second-conversation"},
            }
        ]
    )
    old_sessions = wire.sessions
    wire.sessions = lambda *a: old_sessions(*a) + [copy.deepcopy(second)]
    old_maintenance = wire.maintenance

    def maintenance(t, root, instance, req):
        if instance == "second-instance":
            return {
                "maintenance": {
                    "operationId": req["operationId"],
                    "action": req["action"],
                    "status": "succeeded",
                    "result": {"status": "ready"},
                }
            }
        return old_maintenance(t, root, instance, req)

    wire.maintenance = maintenance
    apply(store, plan, wire)
    wire.rows["bender"]["descriptor"]["pid"] = 99
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["sessions"]["second-conversation"]["status"] == "complete"
    assert result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"] == "blocked"


def test_exit_one_failed_receipt_is_persisted_and_does_not_retry(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    wire.returncode = 1

    def failed_prepare(target, root, instance, request):
        wire.calls.append(copy.deepcopy(request))
        return {
            "maintenance": {
                "operationId": request["operationId"],
                "action": request["action"],
                "status": "failed",
                "error": "cannot checkpoint active task",
            }
        }

    wire.maintenance = failed_prepare
    result = apply(store, plan, wire)
    state = result["hosts"]["bender"]["sessions"]["bender-conversation"]
    assert state["operations"]["prepare"]["receipt"]["status"] == "failed"
    assert state["status"] == "blocked"
    apply(store, plan, wire)
    assert len(wire.calls) == 1


def test_unidentified_agent_does_not_block_identified_sibling(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    native = wire.native
    wire.native = lambda target: (
        [{"agent": "pi", "terminal_id": "unknown", "agent_session": None}] + native(target)
    )
    result = apply(store, plan, wire)
    host = result["hosts"]["bender"]
    assert host["sessions"]["bender-conversation"]["status"] == "awaiting_progress"
    assert host["status"] == "blocked"
    assert host["coverageErrors"][0]["terminalId"] == "unknown"
    assert "conversation" in host["coverageErrors"][0]["error"]


def test_complete_mission_is_preserved_without_progress_or_resume(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    mission = wire.rows["bender"]["reply"]["data"]["missions"][0]
    mission["status"] = "COMPLETE"
    mission["resumeEligible"] = False
    result = apply(store, plan, wire)
    state = result["hosts"]["bender"]["sessions"]["bender-conversation"]
    assert state["status"] == "complete"
    assert state["missions"]["M1"]["status"] == "preserved"
    assert not any(r["action"] == "resume" for r in wire.calls)


def test_skipped_resume_is_explicitly_blocked_without_new_operation(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = wire.maintenance

    def stale_resume(target, root, instance, request):
        if request["action"] == "resume":
            wire.calls.append(copy.deepcopy(request))
            return {
                "maintenance": {
                    "operationId": request["operationId"],
                    "action": "resume",
                    "status": "skipped",
                    "result": {"reason": "mission revision changed"},
                }
            }
        return original(target, root, instance, request)

    wire.maintenance = stale_resume
    result = apply(store, plan, wire)
    state = result["hosts"]["bender"]["sessions"]["bender-conversation"]
    assert state["status"] == "blocked"
    assert state["missions"]["M1"]["status"] == "blocked"
    assert "revision changed" in state["missions"]["M1"]["error"]
    wire.rows["bender"]["reply"]["data"]["missions"][0]["revision"] += 1
    apply(store, plan, wire)
    assert len([r for r in wire.calls if r["action"] == "resume"]) == 1
    wire.rows["bender"]["reply"]["data"]["missions"][0]["operatorPaused"] = True
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"] == "complete"


def test_inventory_includes_direct_and_deep_hosts_and_named_session(tmp_path, monkeypatch):
    import subprocess

    from herdr_engineering.fleet_rollout import inventory_targets

    inventory = tmp_path / "hosts.yml"
    inventory.write_text(
        "all:\n  children:\n    herdr_fleet:\n      hosts:\n        direct: {}\n      children:\n        nested:\n          children:\n            leaf:\n              hosts:\n                deep: {}\n"
    )
    resolved = {
        "herdr_fleet": {"hosts": ["direct"], "children": ["nested"]},
        "nested": {"children": ["leaf"]},
        "leaf": {"hosts": ["deep"]},
        "_meta": {"hostvars": {"deep": {"ansible_host": "deep.example", "herdr_session": "work"}}},
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, json.dumps(resolved), ""),
    )
    targets = inventory_targets(inventory)
    assert {t["host"] for t in targets} == {"direct", "deep"}
    assert next(t for t in targets if t["host"] == "deep")["session"] == "work"


def test_changed_inventory_destination_blocks_before_stage(tmp_path, monkeypatch):
    import herdr_engineering.fleet_rollout as module

    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    stages = []
    monkeypatch.setattr(
        module,
        "inventory_targets",
        lambda inventory, host: [{**plan["hosts"][host]["target"], "sshTarget": "different-host"}],
    )
    result = module.apply_rollout(
        store, plan["id"], transport=wire, stage=lambda *a, **kw: stages.append(kw)
    )
    assert not stages
    assert result["hosts"]["bender"]["status"] == "blocked"
    assert "destination changed" in result["hosts"]["bender"]["error"]


@pytest.mark.parametrize("action", ["resume", "reload"])
def test_busy_maintenance_retries_once_next_pass_with_new_operation(tmp_path, action):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = wire.maintenance
    busy = True
    attempts = []

    def sometimes_busy(target, root, instance, request):
        if request["action"] == action:
            attempts.append(request["operationId"])
            if busy:
                return {
                    "maintenance": {
                        "operationId": request["operationId"],
                        "action": action,
                        "status": "skipped",
                        "result": {"status": "busy", "reason": "active work"},
                    }
                }
        return original(target, root, instance, request)

    wire.maintenance = sometimes_busy
    first = apply(store, plan, wire)
    assert len(attempts) == 1
    assert first["hosts"]["bender"]["sessions"]["bender-conversation"]["status"] in (
        "pending",
        "awaiting_progress",
    )
    busy = False
    second = apply(store, plan, wire)
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert (
        second["hosts"]["bender"]["sessions"]["bender-conversation"]["status"]
        == "awaiting_progress"
    )


def test_stale_fence_code_retries_with_fresh_revision_and_generation(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = wire.maintenance
    requests = []

    def stale_once(target, root, instance, request):
        if request["action"] == "resume":
            requests.append(copy.deepcopy(request))
            if len(requests) == 1:
                return {
                    "maintenance": {
                        "operationId": request["operationId"],
                        "action": "resume",
                        "status": "skipped",
                        "result": {"status": "skipped", "code": "STALE_MISSION_FENCE"},
                    }
                }
        return original(target, root, instance, request)

    wire.maintenance = stale_once
    apply(store, plan, wire)
    mission = wire.rows["bender"]["reply"]["data"]["missions"][0]
    mission.update(revision=3, resumptionGeneration=1)
    result = apply(store, plan, wire)
    assert len(requests) == 2 and requests[0]["operationId"] != requests[1]["operationId"]
    assert requests[1]["expectedRevision"] == 3
    assert requests[1]["expectedResumptionGeneration"] == 1
    assert (
        result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"]
        == "awaiting_progress"
    )


@pytest.mark.parametrize(
    "code,output", [(1, "bad json"), (255, "{}"), (1, '{"maintenance":{"status":"succeeded"}}')]
)
def test_failed_process_without_valid_failed_receipt_remains_transport_error(code, output):
    import subprocess

    from herdr_engineering.fleet_rollout import PiTransport

    transport = PiTransport(
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, code, output, "failed")
    )
    target = {"host": "box", "sshTarget": "box", "port": 22, "local": False}
    with pytest.raises(ConnectionError):
        transport.maintenance(
            target, "/release", "instance", {"operationId": "op", "action": "prepare"}
        )


@pytest.mark.parametrize("deadline", ["2000-01-01T00:00:00Z", "2098-01-01T00:00:00Z", "invalid"])
def test_named_wait_requires_valid_future_deadline_covering_next_action(tmp_path, deadline):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    wire.rows["bender"]["reply"]["data"]["missions"][0]["namedWait"] = {
        "action": "probe",
        "nextActionAt": "2099-01-01T00:00:00Z",
        "deadline": deadline,
    }
    result = apply(store, plan, wire)
    assert (
        result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"]
        == "awaiting_progress"
    )


@pytest.mark.parametrize("deadline", [None, "2099-01-02T00:00:00Z"])
def test_named_wait_allows_no_deadline_or_covering_future_deadline(tmp_path, deadline):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    wire.rows["bender"]["reply"]["data"]["missions"][0]["namedWait"] = {
        "action": "probe",
        "nextActionAt": "2099-01-01T00:00:00Z",
        "deadline": deadline,
    }
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"] == "complete"


@pytest.mark.parametrize("mission_state", ["idle", "paused", "COMPLETE", "bootstrap"])
def test_already_current_runtime_is_never_drained(mission_state, tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    data = wire.rows["bender"]["reply"]["data"]
    data["runtime"]["commit"] = SHA
    if mission_state == "idle":
        data["missions"] = []
    elif mission_state == "paused":
        data["missions"][0]["operatorPaused"] = True
    elif mission_state == "bootstrap":
        data["runtime"]["hostCommit"] = "b" * 40
    else:
        data["missions"][0]["status"] = mission_state
    result = apply(store, plan, wire)
    assert not wire.calls
    assert result["hosts"]["bender"]["sessions"]["bender-conversation"]["status"] == (
        "bootstrap_required" if mission_state == "bootstrap" else "complete"
    )
