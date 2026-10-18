"""Durable coordinator exercises real files and a deterministic wire fixture."""

import copy
import json

import pytest

SHA = "a" * 40


def test_rollout_commands_are_registered():
    from herdr_engineering.cli import _build_parser

    args = _build_parser().parse_args(["fleet", "rollout", "plan", "--pi-revision", SHA])
    assert args.rollout_command == "plan"


def _remote_argv(command):
    import shlex

    prefix = 'PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" exec '
    assert command.startswith(prefix)
    return shlex.split(command[len(prefix) :])


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
        if self.progress and row["reply"]["data"]["missions"]:
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
    import subprocess

    from herdr_engineering.fleet_rollout import PiTransport, apply_rollout

    def runner(argv, **kwargs):
        # Exercise production command construction and JSON decoding at the subprocess seam.
        host = argv[-2]
        remote = _remote_argv(argv[-1])
        if remote[0] == "herdr":
            if "process-info" in remote:
                reply = {"result": {"process_info": wire.process_info({"host": host}, remote[-1])}}
            else:
                assert remote[-2:] == ["api", "snapshot"]
                reply = {"result": {"snapshot": {"agents": wire.native({"host": host})}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")
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
            transport=PiTransport(runner=runner),
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
    import subprocess

    from herdr_engineering.fleet_rollout import PiTransport

    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        remote = _remote_argv(argv[-1])
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


class RunClock:
    def __init__(self):
        from datetime import UTC, datetime

        self.value = datetime(2026, 10, 18, tzinfo=UTC)
        self.delays = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        from datetime import timedelta

        self.delays.append(seconds)
        self.value += timedelta(seconds=seconds)


def run_with_wire(store, plan, wire, clock, **kwargs):
    from herdr_engineering.fleet_rollout import run_rollout

    return run_rollout(
        store,
        plan["id"],
        reconcile=lambda store, rollout_id: apply(store, plan, wire),
        now=clock.now,
        sleep=clock.sleep,
        **kwargs,
    )


def test_run_cli_supports_bounded_unattended_mode():
    from herdr_engineering.cli import _build_parser

    args = _build_parser().parse_args(
        [
            "fleet",
            "rollout",
            "run",
            "--id",
            "a" * 32,
            "--interval-seconds",
            "30",
            "--max-passes",
            "5",
        ]
    )
    assert args.rollout_command == "run" and args.max_passes == 5


def test_run_reconnects_offline_host_on_second_pass(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire, clock = Wire(), RunClock()
    for row in wire.rows.values():
        row["reply"]["data"]["missions"] = []
    old_sleep = clock.sleep

    def reconnect(seconds):
        old_sleep(seconds)
        wire.offline.clear()

    clock.sleep = reconnect
    result = run_with_wire(store, plan, wire, clock, interval_seconds=30, max_passes=3)
    assert result["status"] == "complete"
    assert result["runner"]["status"] == "complete" and result["runner"]["passes"] == 2
    assert clock.delays == [30]
    assert result["revision"] == SHA


def test_run_restart_preserves_operation_ids_and_backoff(tmp_path):
    from herdr_engineering.fleet_rollout import RolloutStore

    store, plan = setup_rollout(tmp_path)
    wire, clock = Wire(), RunClock()
    first = run_with_wire(store, plan, wire, clock, interval_seconds=30, max_passes=1)
    assert first["runner"]["status"] == "max_passes"
    operation = first["hosts"]["bender"]["sessions"]["bender-conversation"]["operations"][
        "resume:M1"
    ]["request"]
    mutation_count = len(wire.mutations)
    wire.progress = True
    wire.offline.clear()
    wire.rows["beast"]["reply"]["data"]["missions"] = []
    result = run_with_wire(
        RolloutStore(store.root), plan, wire, clock, interval_seconds=30, max_passes=1
    )
    assert result["status"] == "complete"
    assert result["runner"]["passes"] == 2
    assert (
        result["hosts"]["bender"]["sessions"]["bender-conversation"]["operations"]["resume:M1"][
            "request"
        ]
        == operation
    )
    assert len(wire.mutations) == mutation_count + 2
    assert clock.delays == [30]


def test_run_backoff_caps_without_treating_attempt_counts_as_progress(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire, clock = Wire(), RunClock()
    wire.offline.add("bender")
    result = run_with_wire(store, plan, wire, clock, interval_seconds=30, max_passes=6)
    assert result["runner"]["status"] == "max_passes"
    assert clock.delays == [30, 60, 120, 240, 300]
    assert result["runner"]["nextAttemptAt"]


@pytest.mark.parametrize("blocker", ["bootstrap", "unknown"])
def test_run_stops_on_operator_attention_without_sleep(tmp_path, blocker):
    store, plan = setup_rollout(tmp_path)
    wire, clock = Wire(), RunClock()
    if blocker == "bootstrap":
        wire.rows["bender"]["reply"]["data"]["runtime"]["capabilities"] = []
    else:
        wire.stage = lambda *a, **k: {"ok": False, "error": "dependency install failed"}
    result = run_with_wire(store, plan, wire, clock, interval_seconds=30, max_passes=10)
    assert result["runner"]["status"] == "blocked"
    assert result["runner"]["reason"]
    assert not clock.delays


def test_run_interruption_is_durable_and_releases_exclusive_runner(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire, clock = Wire(), RunClock()
    wire.offline.add("bender")

    def interrupted(seconds):
        raise KeyboardInterrupt

    clock.sleep = interrupted
    result = run_with_wire(store, plan, wire, clock, interval_seconds=30, max_passes=3)
    assert result["runner"]["status"] == "interrupted"
    assert store.read(plan["id"])["runner"]["status"] == "interrupted"
    clock.sleep = RunClock.sleep.__get__(clock)
    result = run_with_wire(store, plan, wire, clock, interval_seconds=30, max_passes=1)
    assert result["runner"]["status"] == "max_passes"


def test_second_runner_is_rejected_even_between_passes(tmp_path):
    from herdr_engineering.fleet_rollout import run_rollout

    store, plan = setup_rollout(tmp_path)
    wire, clock = Wire(), RunClock()
    wire.offline.add("bender")
    old_sleep = clock.sleep

    def competing(seconds):
        with pytest.raises(BlockingIOError):
            run_rollout(store, plan["id"], max_passes=1)
        old_sleep(seconds)

    clock.sleep = competing
    run_with_wire(store, plan, wire, clock, interval_seconds=30, max_passes=2)


@pytest.mark.parametrize(
    "interval,max_passes", [(0, 1), (3601, 1), (float("nan"), 1), (30, 0), (30, -1)]
)
def test_run_rejects_invalid_bounds_before_contact(tmp_path, interval, max_passes):
    from herdr_engineering.fleet_rollout import run_rollout

    store, plan = setup_rollout(tmp_path)
    with pytest.raises(ValueError):
        run_rollout(store, plan["id"], interval_seconds=interval, max_passes=max_passes)


@pytest.mark.parametrize("local", [False, True])
def test_native_inventory_uses_frozen_host_transport_without_central_registration(
    monkeypatch, local
):
    import subprocess

    import herdr_engineering.fleet_rollout as module

    class UnregisteredController:
        def list_agents(self, **kwargs):
            raise AssertionError("controller only knows beast; must query actual target")

    monkeypatch.setattr(
        module, "NativeHerdrAdapter", lambda: UnregisteredController(), raising=False
    )
    target = {
        "host": "bender",
        "sshTarget": "alice@bender.example",
        "port": 2222,
        "local": local,
        "session": "engineering",
    }
    raw = {
        "agent": "pi",
        "terminal_id": "terminal-7",
        "pane_id": "pane-2",
        "revision": 12,
        "agent_session": {"kind": "path", "source": "pi", "value": "/sessions/exact.jsonl"},
        "agent_status": "working",
        "future_identity_field": {"owner": "retained"},
    }
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        native = argv if local else _remote_argv(argv[-1])
        assert native == ["herdr", "--session", "engineering", "api", "snapshot"]
        if not local:
            assert argv[-2] == "alice@bender.example"
            assert argv[argv.index("-p") + 1] == "2222"
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"result": {"snapshot": {"agents": [raw]}}}), ""
        )

    agents = module.PiTransport(runner=runner).native(target)
    assert len(calls) == 1
    assert all(agents[0][key] == value for key, value in raw.items())
    assert agents[0]["machine"] == "bender" and agents[0]["session"] == "engineering"


@pytest.mark.parametrize(
    "reply", [{"ok": False, "error": "missing session"}, {"result": {"snapshot": {}}}]
)
def test_native_host_errors_cannot_be_reported_as_empty_inventory(monkeypatch, reply):
    import subprocess

    import herdr_engineering.fleet_rollout as module

    class UnregisteredController:
        def list_agents(self, **kwargs):
            raise AssertionError("central machine routing forbidden")

    monkeypatch.setattr(
        module, "NativeHerdrAdapter", lambda: UnregisteredController(), raising=False
    )
    transport = module.PiTransport(
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")
    )
    with pytest.raises(ValueError, match="native"):
        transport.native({"host": "bender", "sshTarget": "bender", "port": 22, "local": False})


@pytest.mark.parametrize("executable", ["herdr", "node"])
def test_remote_transport_resolves_user_bins_without_evaluating_argv(tmp_path, executable):
    import os
    import subprocess

    from herdr_engineering.fleet_rollout import PiTransport

    user_bin = tmp_path / ".local/bin"
    user_bin.mkdir(parents=True)
    executable_path = user_bin / executable
    executable_path.write_text(
        '#!/usr/bin/python3\nimport json, sys\nprint(json.dumps({"argv": sys.argv[1:]}))\n'
    )
    executable_path.chmod(0o700)
    marker = tmp_path / "shell-injection-ran"
    payload = [
        f"$( : > {marker})",
        f"`: > {marker}`",
        "literal $HOME",
        "apostrophe' and spaces",
        "; exit 9",
    ]
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        # Execute only the generated remote shell command in an isolated test HOME.
        # No ssh invocation, network access, or actual HerdR/Pi processes.
        env = {**os.environ, "HOME": str(tmp_path), "PATH": str(tmp_path / "empty-path")}
        return subprocess.run(["/bin/sh", "-c", argv[-1]], env=env, **kwargs)

    transport = PiTransport(runner=runner)
    target = {"host": "beast", "sshTarget": "alice@beast", "port": 22, "local": False}
    reply = transport._json(target, [executable, *payload])
    assert reply["argv"] == payload
    assert not marker.exists()
    assert commands[0][-1].startswith(
        'PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" exec '
    )


def test_transport_stderr_diagnostics_are_bounded_and_do_not_expose_secrets():
    import subprocess

    from herdr_engineering.fleet_rollout import PiTransport

    secret = "sk-private-token-123456"
    stderr = f"/bin/sh: 1: herdr: not found\nAuthorization: Bearer {secret}\n" + "x" * 10000
    transport = PiTransport(
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 127, "", stderr)
    )
    with pytest.raises(ConnectionError) as error:
        transport.native({"host": "beast", "sshTarget": "beast", "port": 22, "local": False})
    assert "herdr" in str(error.value) and "not found" in str(error.value)
    assert secret not in str(error.value) and "Authorization" not in str(error.value)
    assert len(str(error.value)) < 300


def foreground_fixture(wire):
    native = {
        "agent": "pi",
        "pane_id": "pane-1",
        "terminal_id": "terminal-1",
        "revision": 8,
        "state_change_seq": 3,
        "agent_session": None,
    }
    wire.native = lambda target: [copy.deepcopy(native)]
    row = wire.rows["bender"]
    row["reply"]["data"].update(
        pid=row["descriptor"]["pid"],
        instanceId=row["descriptor"]["instanceId"],
        sessionId=row["descriptor"]["sessionId"],
    )
    wire.process_info = lambda target, pane: {
        "pane_id": pane,
        "foreground_process_group_id": 42,
        "foreground_processes": [{"pid": 42, "name": "pi"}],
        "shell_pid": 40,
    }
    return native


def test_missing_native_reference_uses_verified_foreground_pid_without_mutating_native(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = foreground_fixture(wire)
    result = apply(store, plan, wire)
    state = result["hosts"]["bender"]["sessions"]["bender-conversation"]
    assert state["status"] == "awaiting_progress"
    assert state["native"]["agent_session"] is None
    assert original["agent_session"] is None
    assert state["identityProof"]["source"] == "verified_foreground_pid"
    assert state["identityProof"]["pid"] == 42
    assert state["identityProof"]["paneId"] == "pane-1"


@pytest.mark.parametrize(
    "failure",
    [
        "foreign_worker",
        "multiple",
        "changed_terminal",
        "changed_revision",
        "unreachable",
        "stale_control",
        "missing_revision",
        "changed_state_seq",
    ],
)
def test_foreground_pid_fallback_rejects_unproved_identity(tmp_path, failure):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = foreground_fixture(wire)
    if failure == "foreign_worker":
        wire.process_info = lambda *a: {
            "pane_id": "pane-1",
            "foreground_processes": [{"pid": 999, "name": "worker"}],
        }
    elif failure == "multiple":
        wire.process_info = lambda *a: {
            "pane_id": "pane-1",
            "foreground_processes": [{"pid": 42}, {"pid": 43}],
        }
        sessions = wire.sessions
        second = copy.deepcopy(wire.rows["bender"])
        second["descriptor"].update(
            pid=43, instanceId="another-instance", sessionId="another-conversation"
        )
        wire.sessions = lambda *a: sessions(*a) + [second]
    elif failure == "missing_revision":
        original.pop("revision")
        original.pop("state_change_seq")
    elif failure in ("changed_terminal", "changed_revision", "changed_state_seq"):
        calls = 0

        def native(target):
            nonlocal calls
            calls += 1
            row = copy.deepcopy(original)
            if calls > 1:
                key = {
                    "changed_terminal": "terminal_id",
                    "changed_revision": "revision",
                    "changed_state_seq": "state_change_seq",
                }[failure]
                row[key] = "replacement" if failure == "changed_terminal" else 9
            return [row]

        wire.native = native
    elif failure == "unreachable":
        wire.rows["bender"]["reachable"] = False
    else:
        wire.rows["bender"]["reply"]["data"]["pid"] = 999
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["status"] == "blocked"
    assert result["hosts"]["bender"]["coverageErrors"]
    assert not wire.mutations


def test_foreground_process_query_uses_frozen_host_and_named_session():
    import subprocess

    from herdr_engineering.fleet_rollout import PiTransport

    target = {
        "host": "bender",
        "sshTarget": "alice@bender",
        "port": 22,
        "local": False,
        "session": "engineering",
    }

    def runner(argv, **kwargs):
        assert argv[-2] == "alice@bender"
        assert _remote_argv(argv[-1]) == [
            "herdr",
            "--session",
            "engineering",
            "pane",
            "process-info",
            "--pane",
            "pane-1",
        ]
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "result": {
                        "process_info": {"pane_id": "pane-1", "foreground_processes": [{"pid": 42}]}
                    }
                }
            ),
            "",
        )

    assert PiTransport(runner=runner).process_info(target, "pane-1")["foreground_processes"] == [
        {"pid": 42}
    ]


@pytest.mark.parametrize(
    "value", ["/sessions/reporting-rollout-20261013.jsonl", "reporting-rollout-20261013.jsonl"]
)
def test_custom_transcript_reference_uses_stable_foreground_proof(tmp_path, value):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    native = foreground_fixture(wire)
    native["agent_session"] = {"kind": "path", "source": "pi", "value": value}
    result = apply(store, plan, wire)
    state = result["hosts"]["bender"]["sessions"]["bender-conversation"]
    assert state["status"] == "awaiting_progress"
    assert state["identityProof"]["source"] == "verified_foreground_pid"
    assert state["native"]["agent_session"] == native["agent_session"]


def test_changed_custom_reference_rejects_otherwise_valid_foreground_proof(tmp_path):
    store, plan = setup_rollout(tmp_path)
    wire = Wire()
    original = foreground_fixture(wire)
    original["agent_session"] = {"kind": "path", "value": "/sessions/custom.jsonl"}
    calls = 0

    def native(target):
        nonlocal calls
        calls += 1
        row = copy.deepcopy(original)
        if calls > 1:
            row["agent_session"]["value"] = "/sessions/replacement.jsonl"
        return [row]

    wire.native = native
    result = apply(store, plan, wire)
    assert result["hosts"]["bender"]["status"] == "blocked"
    assert not wire.mutations
