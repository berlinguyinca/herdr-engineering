import copy

import pytest

from herdr_engineering.pi_recovery import RecoveryStore, apply_recovery, create_plan


@pytest.fixture(autouse=True)
def managed_test_context(monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")


TARGET = {"host": "bender", "sshTarget": "bender", "port": 22, "local": True, "session": "default"}
MODEL = {"id": "working", "contextWindow": 262144, "input": ["text"], "reasoning": True}


class Clock:
    def __init__(self):
        self.value = 1000

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Wire:
    def __init__(self):
        self.events = []
        self.clock = Clock()
        self.state = {
            "paneId": "w1:p1",
            "terminalId": "term1",
            "sessionId": "session1",
            "instanceId": "instance1",
            "pid": 42,
            "birth": "1234",
            "uid": 1000,
            "agentStatus": "idle",
            "currentTool": None,
            "sourceRoot": "/release",
            "runtime": {"commit": "a" * 40, "pending": False},
            "missions": [],
            "failure": "inference",
            "model": "old",
            "requiredContext": 131072,
            "requiresVision": False,
            "requiresReasoning": True,
            "models": [MODEL],
            "lastAssistantId": "error1",
            "modelAck": None,
            "promptReady": True,
        }
        self.change_on_probe = None
        self.fail_prompt = False

    def native(self, target):
        return [{"agent": "pi", "pane_id": "w1:p1"}]

    def observe(self, target, pane):
        return copy.deepcopy(self.state)

    def probe(self, target, model):
        if self.change_on_probe:
            self.state.update(self.change_on_probe)
        return True

    def command(self, target, pane, text):
        self.events.append((self.clock.now(), text))
        self.clock.value += 10
        if text.startswith("/model "):
            self.state["modelAck"] = text.removeprefix("/model ")
            self.state["model"] = "working"
            self.state["provider"] = "metabolomics"
        elif self.fail_prompt:
            raise TimeoutError("reply lost")
        else:
            self.state["agentStatus"] = "working"


def setup(tmp_path):
    wire = Wire()
    store = RecoveryStore(tmp_path / "state")
    plan = create_plan(store, [TARGET], transport=wire)
    return wire, store, plan


def apply(wire, store, plan):
    return apply_recovery(
        store, plan["id"], transport=wire, now=wire.clock.now, sleep=wire.clock.sleep, managed=True
    )


def test_plan_is_read_only(tmp_path):
    wire, _, plan = setup(tmp_path)
    assert not wire.events
    assert plan["sessions"][0]["status"] == "eligible"


@pytest.mark.parametrize(
    "update",
    [
        {"agentStatus": "working"},
        {"agentStatus": "blocked"},
        {"failure": None},
        {"promptReady": False},
        {"missions": [{"operatorPaused": True}]},
        {"missions": [{"status": "DONE"}]},
        {"currentTool": "bash"},
    ],
)
def test_excludes_unsafe_sessions(tmp_path, update):
    wire, store, _ = setup(tmp_path)
    wire.state.update(update)
    plan = create_plan(store, [TARGET], transport=wire)
    apply(wire, store, plan)
    assert wire.events == []


@pytest.mark.parametrize(
    "update",
    [
        {"pid": 99},
        {"birth": "reused"},
        {"sessionId": "other"},
        {"terminalId": "new"},
        {"uid": 2000},
        {"agentStatus": "working"},
    ],
)
def test_revalidates_after_model_probe(tmp_path, update):
    wire, store, plan = setup(tmp_path)
    wire.change_on_probe = update
    report = apply(wire, store, plan)
    assert wire.events == []
    assert report["sessions"][0]["status"] == "blocked"


def test_accepted_is_not_verified_progress(tmp_path):
    wire, store, plan = setup(tmp_path)
    report = apply(wire, store, plan)
    assert len(wire.events) == 2
    assert report["sessions"][0]["status"] == "accepted"
    apply(wire, store, plan)
    assert len(wire.events) == 2


def test_lost_response_never_replays_prompt(tmp_path):
    wire, store, plan = setup(tmp_path)
    wire.fail_prompt = True
    assert apply(wire, store, plan)["sessions"][0]["status"] == "unknown"
    apply(wire, store, plan)
    assert len(wire.events) == 2


def test_global_dedupe_across_plan_ids(tmp_path):
    wire, store, plan = setup(tmp_path)
    other = create_plan(store, [TARGET], transport=wire)
    apply(wire, store, plan)
    wire.state["agentStatus"] = "idle"
    apply(wire, store, other)
    assert len(wire.events) == 2


def test_cooldown_persists_after_attempt_completion(tmp_path):
    wire, store, plan = setup(tmp_path)
    apply(wire, store, plan)
    finish = wire.clock.now()
    wire.state.update(sessionId="session2", instanceId="instance2", agentStatus="idle")
    other = create_plan(store, [TARGET], transport=wire)
    apply(wire, RecoveryStore(store.root), other)
    assert wire.events[2][0] >= finish + 60


def test_global_lock_rejects_concurrent_apply(tmp_path):
    wire, store, plan = setup(tmp_path)
    with store.lock():
        with pytest.raises(BlockingIOError):
            apply(wire, store, plan)
    assert not wire.events


def test_model_picker_stops_without_continuation(tmp_path):
    wire, store, plan = setup(tmp_path)
    original = wire.command

    def command(*args):
        original(*args)
        wire.state.update(promptReady=False, modelAck=None)

    wire.command = command
    apply(wire, store, plan)
    assert len(wire.events) == 1


def test_requires_compatible_model(tmp_path):
    wire, store, _ = setup(tmp_path)
    wire.state["requiresVision"] = True
    plan = create_plan(store, [TARGET], transport=wire)
    apply(wire, store, plan)
    assert not wire.events


def test_requires_managed_caller(tmp_path):
    wire, store, plan = setup(tmp_path)
    with pytest.raises(ValueError, match="Herdr"):
        apply_recovery(store, plan["id"], transport=wire, managed=False)
    assert not wire.events


def test_rejects_changed_failure_after_planning(tmp_path):
    wire, store, plan = setup(tmp_path)
    wire.change_on_probe = {"lastAssistantId": "new-user-work-failed"}
    apply(wire, store, plan)
    assert not wire.events


def test_crash_marker_enforces_fresh_cooldown(tmp_path):
    wire, store, plan = setup(tmp_path)
    journal = store.journal()
    journal.update(inFlight="lost-controller", nextEligibleAt=500)
    store.save_journal(journal)
    before = wire.clock.now()
    apply(wire, RecoveryStore(store.root), plan)
    assert wire.events[0][0] >= before + 60


def test_probe_requires_finished_tool_call(tmp_path):
    import json

    from herdr_engineering.pi_recovery_host import probe

    models = tmp_path / ".pi/agent/models.json"
    models.parent.mkdir(parents=True)
    models.write_text(
        json.dumps(
            {
                "providers": {
                    "metabolomics": {
                        "baseUrl": "https://example.org/v1",
                        "apiKey": "secret",
                        "models": [{"id": "working"}],
                    }
                }
            }
        )
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, _limit):
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "recovery_health",
                                            "arguments": '{"ok":true}',
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                }
            ).encode()

    assert probe(tmp_path, "working", opener=lambda *a, **kw: Response())["healthy"] is False


def test_read_only_default_cli():
    from herdr_engineering.cli import _build_parser

    args = _build_parser().parse_args(["fleet", "recover"])
    assert args.recovery_command == "plan"


def test_mission_without_exact_model_capability_never_changes_model(tmp_path):
    wire, store, _ = setup(tmp_path)
    wire.state["missions"] = [
        {"id": "mission", "status": "BLOCKED", "resumeEligible": True, "operatorPaused": False}
    ]
    plan = create_plan(store, [TARGET], transport=wire)
    apply(wire, store, plan)
    assert wire.events == []


@pytest.mark.parametrize(
    "tool_results",
    [
        [],
        [
            {
                "type": "message",
                "id": "result",
                "parentId": "failed",
                "message": {"role": "toolResult", "toolCallId": "call1"},
            }
        ],
    ],
)
def test_error_turn_tool_calls_require_reconciliation(tool_results):
    from herdr_engineering.pi_recovery_host import transcript_metadata

    entries = [
        {"type": "session", "id": "s"},
        {"type": "model_change", "id": "model", "modelId": "working", "provider": "metabolomics"},
        {
            "type": "message",
            "id": "failed",
            "parentId": "model",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "stream error",
                "content": [{"type": "toolCall", "id": "call1"}],
            },
        },
    ] + tool_results
    config = {"providers": {"metabolomics": {"models": [MODEL]}}}
    assert transcript_metadata(entries, config)["failure"] is None


def test_forked_transcript_is_not_inferred_from_tail():
    from herdr_engineering.pi_recovery_host import transcript_metadata

    entries = [
        {"type": "session", "id": "s"},
        {"type": "model_change", "id": "model", "modelId": "working", "provider": "metabolomics"},
        {
            "type": "message",
            "id": "first",
            "parentId": "model",
            "message": {"role": "assistant", "stopReason": "stop"},
        },
        {
            "type": "message",
            "id": "branch",
            "parentId": "model",
            "message": {"role": "assistant", "stopReason": "error", "errorMessage": "stream error"},
        },
    ]
    assert (
        transcript_metadata(entries, {"providers": {"metabolomics": {"models": [MODEL]}}})[
            "failure"
        ]
        is None
    )


def test_exact_model_mission_resume_uses_typed_fence_without_prompt(tmp_path):
    wire, store, _ = setup(tmp_path)
    wire.state["runtime"]["capabilities"] = ["mission.resume.selected-model.v1"]
    wire.state["runtime"]["interactiveIdle"] = True
    wire.state["missions"] = [
        {
            "id": "mission",
            "status": "BLOCKED",
            "resumeEligible": True,
            "operatorPaused": False,
            "revision": 12,
            "resumptionGeneration": 2,
        }
    ]
    requests = []

    def maintenance(target, root, instance, request):
        requests.append(request)
        return {
            "maintenance": {
                "operationId": request["operationId"],
                "action": "resume",
                "status": "succeeded",
                "result": {
                    "status": "resumed",
                    "missionId": "mission",
                    "selectedModel": request["selectedModel"],
                },
            }
        }

    wire.maintenance = maintenance
    plan = create_plan(store, [TARGET], transport=wire)
    assert apply(wire, store, plan)["sessions"][0]["status"] == "accepted"
    assert not wire.events
    assert requests[0]["selectedModel"] == {"provider": "metabolomics", "id": "working"}
    assert requests[0]["expectedRevision"] == 12


def test_short_native_model_ack_requires_provider_proof(tmp_path):
    wire, store, plan = setup(tmp_path)
    original = wire.command

    def command(*args):
        original(*args)
        wire.state.update(modelAck="working", provider="metabolomics")

    wire.command = command
    assert apply(wire, store, plan)["sessions"][0]["status"] == "accepted"


@pytest.mark.parametrize(
    "text,ready,picker",
    [
        ("", False, False),
        ("ordinary output", False, False),
        ("────────────\n\n────────────", True, False),
        ("────────────\nuser draft\n────────────", False, False),
        ("tab scope (all/scoped)\n────────────\n\n────────────", False, True),
    ],
)
def test_screen_requires_positive_empty_composer(monkeypatch, text, ready, picker):
    import subprocess

    from herdr_engineering import pi_recovery_host

    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, text, "")
    )
    observed = pi_recovery_host.screen("default", "w1:p1")
    assert observed["promptReady"] is ready
    assert observed["modelPicker"] is picker


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"ok": True},
        {"error": {"code": "no"}},
        {"result": {"type": "agent_prompted", "agent": {"pane_id": "other"}}},
    ],
)
def test_native_rejection_never_counts_as_delivery(reply):
    import json
    import subprocess

    from herdr_engineering.pi_recovery import RecoveryTransport

    transport = RecoveryTransport(
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(reply), "")
    )
    with pytest.raises(ValueError):
        transport.command(TARGET, "w1:p1", "continue")


def test_native_receipt_without_top_level_ok_is_valid():
    import json
    import subprocess

    from herdr_engineering.pi_recovery import RecoveryTransport

    reply = {
        "id": "cli:agent:prompt",
        "result": {"type": "agent_prompted", "agent": {"pane_id": "w1:p1"}},
    }
    transport = RecoveryTransport(
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(reply), "")
    )
    assert transport.command(TARGET, "w1:p1", "continue")["type"] == "agent_prompted"


def test_model_picker_refreshes_in_place_once_then_continues(tmp_path):
    wire, store, plan = setup(tmp_path)
    original = wire.command
    refreshed = False

    def keys(target, pane, key):
        wire.events.append((wire.clock.now(), key))
        wire.state.update(modelPicker=False, promptReady=True)

    def command(*args):
        nonlocal refreshed
        if args[-1] == "/reload":
            wire.events.append((wire.clock.now(), "/reload"))
            refreshed = True
            wire.state.update(reloadAck=True, instanceId="reloaded-instance")
        else:
            original(*args)
            if args[-1].startswith("/model ") and not refreshed:
                wire.state.update(modelPicker=True, promptReady=False, modelAck=None)

    wire.keys = keys
    wire.command = command
    report = apply(wire, store, plan)
    assert report["sessions"][0]["status"] == "accepted"
    assert [e[1] for e in wire.events][:4] == [
        "/model metabolomics/working",
        "esc",
        "/reload",
        "/model metabolomics/working",
    ]


def test_reload_cannot_adopt_replaced_process(tmp_path):
    wire, store, plan = setup(tmp_path)
    original = wire.command

    def keys(*args):
        wire.state.update(modelPicker=False, promptReady=True)

    def command(*args):
        original(*args)
        if args[-1].startswith("/model "):
            wire.state.update(modelPicker=True, promptReady=False, modelAck=None)
        elif args[-1] == "/reload":
            wire.state.update(reloadAck=True, pid=99, birth="reused")

    wire.keys = keys
    wire.command = command
    report = apply(wire, store, plan)
    assert report["sessions"][0]["status"] == "unknown"
    assert not any("Continue the previously" in text for _, text in wire.events)


def test_stale_idle_metadata_with_working_ui_is_excluded(tmp_path):
    wire, store, _ = setup(tmp_path)
    wire.state["busyUI"] = True
    plan = create_plan(store, [TARGET], transport=wire)
    apply(wire, store, plan)
    assert not wire.events


def test_working_ui_does_not_count_as_empty_idle_editor(monkeypatch):
    import subprocess

    from herdr_engineering import pi_recovery_host

    text = "Working · Waiting for model ·39s\n────────────\n\n────────────"
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, text, "")
    )
    assert pi_recovery_host.screen("default", "w1:p1")["promptReady"] is False


def test_owned_mission_outage_evidence_does_not_need_interactive_error(tmp_path):
    wire, store, _ = setup(tmp_path)
    wire.state["failure"] = None
    wire.state["runtime"]["capabilities"] = ["mission.resume.selected-model.v1"]
    wire.state["runtime"]["interactiveIdle"] = True
    wire.state["missions"] = [
        {
            "id": "m",
            "status": "BLOCKED",
            "resumeEligible": True,
            "operatorPaused": False,
            "lastError": "inference provider unavailable",
        }
    ]
    plan = create_plan(store, [TARGET], transport=wire)
    assert plan["sessions"][0]["status"] == "eligible"


def test_read_only_plan_requires_managed_context(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    with pytest.raises(ValueError, match="Herdr"):
        create_plan(RecoveryStore(tmp_path / "state"), [TARGET], transport=Wire())


def test_rendered_operator_pause_overrides_empty_owned_inventory(tmp_path):
    wire, store, _ = setup(tmp_path)
    wire.state["operatorPausedUI"] = True
    plan = create_plan(store, [TARGET], transport=wire)
    apply(wire, store, plan)
    assert not wire.events


def test_preflight_exception_never_calls_mutation(tmp_path):
    wire, store, plan = setup(tmp_path)
    wire.observe = lambda *args: (_ for _ in ()).throw(ValueError("proof unavailable"))
    apply(wire, store, plan)
    assert not wire.events


def test_historical_thinking_text_is_not_current_busy_evidence(monkeypatch):
    import subprocess

    from herdr_engineering import pi_recovery_host

    text = "Thinking...\nStream error after retries\n────────────\n\n────────────"
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, text, "")
    )
    assert pi_recovery_host.screen("default", "w1:p1")["promptReady"] is True


def test_plan_batches_status_reads_and_apply_adapter_stays_fresh(tmp_path):
    import json
    import subprocess

    from herdr_engineering.pi_recovery import RecoveryTransport

    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps([]), "")

    class NativeWire(RecoveryTransport):
        def native(self, target):
            return [{"agent": "pi", "pane_id": "w1:p1"}, {"agent": "pi", "pane_id": "w1:p2"}]

        def observe(self, target, pane):
            self.sessions(target, "/release")
            state = Wire().state
            state["paneId"] = pane
            return state

    wire = NativeWire(runner=runner)
    events = []
    create_plan(RecoveryStore(tmp_path / "state"), [TARGET], transport=wire, progress=events.append)
    assert len(calls) == 1
    wire.sessions(TARGET, "/release")
    wire.sessions(TARGET, "/release")
    assert len(calls) == 3
    assert any("w1:p2" in event for event in events)


def test_final_sdk_busy_observation_blocks_continuation(tmp_path):
    wire, store, plan = setup(tmp_path)
    observe = wire.observe
    after_model = 0

    def racing(*args):
        nonlocal after_model
        if wire.events:
            after_model += 1
            if after_model >= 2:
                wire.state["runtime"]["interactiveIdle"] = False
        return observe(*args)

    wire.observe = racing
    apply(wire, store, plan)
    assert len(wire.events) == 1


def test_upgrade_never_overrides_completed_or_paused_evidence(tmp_path):
    wire, store, _ = setup(tmp_path)
    wire.state.update(installedCommit="b" * 40, failure=None)
    plan = create_plan(store, [TARGET], transport=wire)
    assert plan["sessions"][0]["reason"] == "no current inference-outage failure evidence"
    wire.state["missions"] = [{"operatorPaused": True}]
    plan = create_plan(store, [TARGET], transport=wire)
    assert plan["sessions"][0]["reason"] == "operator-paused or terminal mission preserved"


@pytest.mark.parametrize("async_reply", [False, True, "gap", "transport_gap"])
def test_opted_in_upgrade_is_fenced_before_exact_mission_resume(tmp_path, monkeypatch, async_reply):
    from herdr_engineering import pi_recovery

    wire, store, _ = setup(tmp_path)
    revision = "b" * 40
    root = "/home/me/.local/share/herdr-engineering/pi/releases/" + revision
    wire.state["missions"] = [
        {
            "id": "mission",
            "status": "BLOCKED",
            "resumeEligible": True,
            "operatorPaused": False,
            "revision": 12,
            "resumptionGeneration": 2,
        }
    ]
    wire.host = lambda *args, **kwargs: {"root": root}
    requests = []
    receipts = {}
    gap_pending = False
    status_failed = False
    original_observe = wire.observe

    def observe(*args):
        nonlocal gap_pending
        if gap_pending:
            gap_pending = False
            raise ValueError("control descriptor handover")
        return original_observe(*args)

    wire.observe = observe

    def maintenance(target, source, instance, request):
        nonlocal gap_pending, status_failed
        requests.append(request)
        if request["action"] == "status":
            assert request["expectedInstanceId"] == "reloaded-instance"
            if (
                async_reply == "transport_gap"
                and receipts[request["operationId"]]["maintenance"]["action"] == "reload"
                and not status_failed
            ):
                status_failed = True
                raise ConnectionError("control socket changed after observation")
            return receipts[request["operationId"]]
        if request["action"] == "reload":
            wire.state["runtime"].update(
                commit=revision,
                capabilities=["mission.resume.selected-model.v1"],
                interactiveIdle=True,
            )
            wire.state.update(installedCommit=revision, instanceId="reloaded-instance")
            gap_pending = async_reply == "gap"
        result = {"status": "ready" if request["action"] == "prepare" else "resumed"}
        if request["action"] == "resume":
            result.update(missionId="mission", selectedModel=request["selectedModel"])
        receipt = {
            "maintenance": {
                "operationId": request["operationId"],
                "action": request["action"],
                "status": "succeeded",
                "result": result,
            }
        }
        receipts[request["operationId"]] = receipt
        if async_reply and request["action"] in ("reload", "resume"):
            return {
                "maintenance": {
                    "operationId": request["operationId"],
                    "action": request["action"],
                    "status": "running",
                }
            }
        return receipt

    wire.maintenance = maintenance
    monkeypatch.setattr(pi_recovery, "inventory_targets", lambda *args: [TARGET])
    plan = create_plan(
        store, [TARGET], transport=wire, revision=revision, inventory=tmp_path / "inventory"
    )
    assert plan["sessions"][0]["status"] == "eligible"
    report = apply(wire, store, plan)
    assert report["sessions"][0]["status"] == "accepted"
    mutations = [r for r in requests if r["action"] != "status"]
    assert [r["action"] for r in mutations] == ["prepare", "reload", "resume"]
    assert mutations[1]["sourceRoot"] == root
    assert mutations[2]["expectedInstanceId"] == "reloaded-instance"
    assert not wire.events


def test_new_owned_mission_after_planning_blocks_even_upgrade(tmp_path):
    wire, store, plan = setup(tmp_path)
    wire.state["missions"] = [
        {
            "id": "new",
            "status": "BLOCKED",
            "resumeEligible": True,
            "operatorPaused": False,
            "lastError": "inference timeout",
            "revision": 1,
            "resumptionGeneration": 0,
        }
    ]

    def unexpected(*args):
        raise AssertionError("new mission must not receive maintenance")

    wire.maintenance = unexpected
    wire.state["runtime"].update(
        capabilities=["mission.resume.selected-model.v1"], interactiveIdle=True
    )
    apply(wire, store, plan)
    assert not wire.events


@pytest.mark.parametrize("missions", [None, [None], ["not-a-mission"], {}])
def test_unavailable_or_malformed_mission_inventory_is_excluded(tmp_path, missions):
    wire, store, _ = setup(tmp_path)
    wire.state["missions"] = missions
    plan = create_plan(store, [TARGET], transport=wire)
    assert plan["sessions"][0]["status"] == "excluded"
    assert not wire.events


def test_explicit_run_combines_plan_and_apply(tmp_path, monkeypatch, capsys):
    from herdr_engineering import cli, fleet_rollout, pi_recovery

    wire = Wire()
    store = RecoveryStore(tmp_path / "state")
    actual_apply = pi_recovery.apply_recovery
    monkeypatch.setattr(pi_recovery, "RecoveryStore", lambda: store)
    # Supply the deterministic native fixture at both public command seams.
    actual_plan = pi_recovery.create_plan
    monkeypatch.setattr(
        pi_recovery,
        "create_plan",
        lambda store, targets, **kw: actual_plan(store, targets, transport=wire, **kw),
    )
    monkeypatch.setattr(
        pi_recovery,
        "apply_recovery",
        lambda store, run_id, **kw: actual_apply(
            store, run_id, transport=wire, now=wire.clock.now, sleep=wire.clock.sleep, **kw
        ),
    )
    monkeypatch.setattr(fleet_rollout, "inventory_targets", lambda *args: [TARGET])
    args = cli._build_parser().parse_args(
        ["fleet", "recover", "run", "--inventory", str(tmp_path / "inventory")]
    )
    assert args.func(args) == 2  # Accepted submission still needs progress verification.
    assert len(wire.events) == 2
    assert "Recovery plan:" in capsys.readouterr().err


def test_prior_vision_model_without_image_evidence_allows_text_model():
    from herdr_engineering.pi_recovery_host import transcript_metadata

    model = {**MODEL, "input": ["text", "image"]}
    entries = [
        {"type": "session", "id": "s"},
        {"type": "model_change", "id": "m", "provider": "metabolomics", "modelId": "working"},
        {
            "type": "message",
            "id": "e",
            "parentId": "m",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "inference timeout",
            },
        },
    ]
    metadata = transcript_metadata(entries, {"providers": {"metabolomics": {"models": [model]}}})
    assert metadata["requiresVision"] is False
    entries[-1]["message"]["content"] = [{"type": "image"}]
    assert (
        transcript_metadata(entries, {"providers": {"metabolomics": {"models": [model]}}})[
            "requiresVision"
        ]
        is True
    )


def test_side_panel_preserves_empty_editor_but_not_draft(monkeypatch):
    import subprocess

    from herdr_engineering import pi_recovery_host

    text = "────────────│ Mission\n            │ sidebar\n────────────│ status"
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kw: subprocess.CompletedProcess(args, 0, text, "")
    )
    assert pi_recovery_host.screen("default", "w1:p1")["promptReady"] is True
    text = "────────────│ Mission\nuser draft  │ sidebar\n────────────│ status"
    assert pi_recovery_host.screen("default", "w1:p1")["promptReady"] is False


def test_capacity_probe_reports_safe_http_status(tmp_path):
    import json
    import urllib.error

    from herdr_engineering.pi_recovery_host import probe

    path = tmp_path / ".pi/agent/models.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "metabolomics": {
                        "baseUrl": "https://example.org/v1",
                        "apiKey": "private-key",
                        "models": [{"id": "working"}],
                    }
                }
            }
        )
    )

    def saturated(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://example.org/v1", 429, "private upstream detail", {}, None
        )

    result = probe(tmp_path, "working", opener=saturated)
    assert result["evidence"] == "http_429"
    assert "private" not in json.dumps(result)


def test_preflight_error_code_is_specific_without_dumping_private_data(tmp_path):
    wire, store, plan = setup(tmp_path)
    wire.state["birth"] = "new"
    result = apply(wire, store, plan)["sessions"][0]
    assert result["errorCode"] == "session_identity_changed"


@pytest.mark.parametrize("planning", [True, False])
def test_native_observation_refreshes_only_target_control(planning):
    from herdr_engineering.pi_recovery import RecoveryTransport

    state = Wire().state
    descriptor = {"pid": 42, "sessionId": "session1", "instanceId": "instance1"}
    live = {
        "descriptor": descriptor,
        "reachable": True,
        "reply": {
            "ok": True,
            "data": {
                **descriptor,
                "currentTool": None,
                "missionSource": "runtime",
                "missions": [],
                "runtime": {},
            },
        },
    }

    class NativeWire(RecoveryTransport):
        def __init__(self):
            super().__init__()
            self.target_reads = 0
            self._plan_cache = {} if planning else None

        def host(self, target, action, **fields):
            if action == "root":
                return {"root": "/release"}
            if action == "screen":
                return {"promptReady": True}
            return {**state, "owned": True}

        def native(self, target):
            return [
                {
                    "agent": "pi",
                    "pane_id": "w1:p1",
                    "terminal_id": "term1",
                    "revision": 1,
                    "state_change_seq": 1,
                    "agent_status": "idle",
                }
            ]

        def process_info(self, target, pane):
            return {"pane_id": pane, "foreground_processes": [{"pid": 42}], "shell_pid": 10}

        def sessions(self, target, root):
            assert planning, "apply must not refresh unrelated sessions"
            return [{"descriptor": descriptor, "reachable": False}]

        def target_sessions(self, target, root, pids):
            assert pids == [42]
            self.target_reads += 1
            return [live]

    wire = NativeWire()
    assert wire.observe(TARGET, "w1:p1")["sessionId"] == "session1"
    assert wire.target_reads == 1


def test_native_transcript_path_disambiguates_only_with_matching_header(tmp_path, monkeypatch):
    import json

    from herdr_engineering import pi_recovery_host

    session = "00000000-0000-0000-0000-000000000001"
    root = tmp_path / ".pi/agent/sessions"
    for directory in ("one", "two"):
        folder = root / directory
        folder.mkdir(parents=True)
        (folder / ("timestamp_" + session + ".jsonl")).write_text(
            json.dumps({"type": "session", "id": session, "cwd": "/repo"}) + "\n"
        )
    config = tmp_path / ".pi/agent/models.json"
    config.write_text('{"providers":{}}')
    monkeypatch.setattr(pi_recovery_host, "process_birth", lambda pid: "same-birth")
    chosen = root / "one" / ("timestamp_" + session + ".jsonl")
    with pytest.raises(ValueError, match="ambiguous"):
        pi_recovery_host.inspect(tmp_path, 42, session)
    assert pi_recovery_host.inspect(tmp_path, 42, session, native_path=str(chosen))[
        "sessionPath"
    ] == str(chosen)
    chosen.write_text(json.dumps({"type": "session", "id": "wrong"}) + "\n")
    with pytest.raises(ValueError, match="header"):
        pi_recovery_host.inspect(tmp_path, 42, session, native_path=str(chosen))


def test_wrapped_acknowledgements_match_only_observed_model(monkeypatch):
    import subprocess

    from herdr_engineering import pi_recovery_host

    text = "Model: work\ning\nReloaded keybindings, extensions, skills,\nprompts, themes, and context files\n────────────\n\n────────────"
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kw: subprocess.CompletedProcess(args, 0, text, "")
    )
    result = pi_recovery_host.screen("default", "w1:p1", expected_model="working")
    assert result["modelAck"] == "working" and result["reloadAck"] is True
    assert (
        pi_recovery_host.screen("default", "w1:p1", expected_model="other")["modelAck"] != "other"
    )


@pytest.mark.parametrize(
    "error_text",
    ["no capacity on this deployment; gateway advises an alternate model", "capacity_unavailable"],
)
def test_capacity_errors_are_recognized_without_bypassing_tool_guards(error_text):
    from herdr_engineering.pi_recovery_host import transcript_metadata

    entries = [
        {"type": "session", "id": "s"},
        {"type": "model_change", "id": "m", "provider": "metabolomics", "modelId": "working"},
        {
            "type": "message",
            "id": "e",
            "parentId": "m",
            "message": {"role": "assistant", "stopReason": "error", "errorMessage": error_text},
        },
    ]
    config = {"providers": {"metabolomics": {"models": [MODEL]}}}
    assert transcript_metadata(entries, config)["failure"] == "inference"
    entries[-1]["message"]["content"] = [{"type": "toolCall", "id": "unresolved"}]
    result = transcript_metadata(entries, config)
    assert result["failure"] is None and result["failureEvidence"] == "unreconciled_tool_calls"


def test_scoped_picker_stops_after_verified_reload_without_guessing(tmp_path):
    wire, store, plan = setup(tmp_path)
    original = wire.command

    def keys(target, pane, key):
        wire.events.append((wire.clock.now(), key))
        wire.state.update(modelPicker=False, promptReady=True)

    def command(*args):
        if args[-1] == "/reload":
            wire.events.append((wire.clock.now(), "/reload"))
            wire.state["reloadAck"] = True
        else:
            original(*args)
            wire.state.update(
                modelPicker=True, modelScopeBlocked=True, promptReady=False, modelAck=None
            )

    wire.keys = keys
    wire.command = command
    result = apply(wire, store, plan)["sessions"][0]
    assert result["errorCode"] == "model_scope_blocked"
    assert [text for _, text in wire.events] == [
        "/model metabolomics/working",
        "esc",
        "/reload",
        "/model metabolomics/working",
    ]


def _node_imports_typescript() -> bool:
    """Plain `node` imports .ts files only with type stripping (22.18+ by default); older hosts cannot run this transport."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if not shutil.which("node"):
        return False
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "m.ts").write_text("export const x: number = 1;\n")
        probe = "import {pathToFileURL} from 'node:url'; await import(pathToFileURL(process.argv[1]).href);"
        return subprocess.run(["node", "--input-type=module", "-e", probe, str(Path(d) / "m.ts")],
                              capture_output=True, timeout=30).returncode == 0


@pytest.mark.skipif(not _node_imports_typescript(), reason="this host's node cannot import .ts files (needs type stripping)")
def test_target_lookup_preserves_native_default_legacy_root_discovery(tmp_path):
    from herdr_engineering.pi_recovery import RecoveryTransport

    module = tmp_path / "src/sessionControl/SessionControl.ts"
    module.parent.mkdir(parents=True)
    module.write_text(
        "export function sessionControlRoot(){return '/short-root';} export async function discoverSessions(...args){if(args.length)throw Error('default discovery bypassed');return [{pid:42,instanceId:'legacy',sessionId:'session'}];} export async function requestSession(){return {ok:true,data:{instanceId:'legacy'}};}"
    )
    result = RecoveryTransport().target_sessions(TARGET, str(tmp_path), [42])
    assert result[0]["reachable"] is True and result[0]["descriptor"]["instanceId"] == "legacy"


def test_companion_sets_its_own_stage_repository_from_any_cwd(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    helper = tmp_path / "python"
    helper.write_text(
        "#!"
        + sys.executable
        + "\nimport json,os,subprocess,sys\nif sys.argv[1]=='-c':sys.exit(subprocess.call([sys.executable,*sys.argv[1:]]))\nprint(json.dumps({'repo':os.environ.get('HERDR_ENGINEERING_REPO')}))\n"
    )
    helper.chmod(0o700)
    env = {
        **os.environ,
        "HERDR_PI_RECOVERY_PYTHON": str(helper),
        "HERDR_ENGINEERING_REPO": "/wrong-repo",
    }
    result = subprocess.run(
        [str(root / "scripts/herdr-pi-recover"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["repo"] == str(root)


@pytest.mark.parametrize(
    "sidebar", ["esc to interrupt", "approve this tool", "tab scope (all/scoped)"]
)
def test_sidebar_cannot_hide_busy_or_approval_evidence(monkeypatch, sidebar):
    import subprocess

    from herdr_engineering import pi_recovery_host

    text = "────────────│ Mission\n            │ " + sidebar + "\n────────────│ status"
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kw: subprocess.CompletedProcess(args, 0, text, "")
    )
    assert pi_recovery_host.screen("default", "w1:p1")["promptReady"] is False


@pytest.mark.parametrize(
    "error_text",
    [
        "Stream ended without finish_reason",
        "Error: stream ended without finish reason",
        "STREAM ENDED WITHOUT A FINISH_REASON",
    ],
)
def test_incomplete_stream_termination_is_recoverable_only_without_unreconciled_tools(error_text):
    from herdr_engineering.pi_recovery_host import transcript_metadata

    entries = [
        {"type": "session", "id": "s"},
        {"type": "model_change", "id": "m", "provider": "metabolomics", "modelId": "working"},
        {
            "type": "message",
            "id": "e",
            "parentId": "m",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": error_text,
            },
        },
    ]
    config = {"providers": {"metabolomics": {"models": [MODEL]}}}
    result = transcript_metadata(entries, config)
    assert result["failure"] == "inference"
    assert result["failureEvidence"] == "current_inference_error"
    entries[-1]["message"]["stopReason"] = "stop"
    assert transcript_metadata(entries, config)["failure"] is None
    entries[-1]["message"].update(
        stopReason="error", content=[{"type": "toolCall", "id": "unresolved"}]
    )
    result = transcript_metadata(entries, config)
    assert result["failure"] is None
    assert result["failureEvidence"] == "unreconciled_tool_calls"
