import copy

import pytest

from herdr_engineering.pi_recovery import RecoveryStore
from herdr_engineering.pi_recovery_host import recovery_progress
from herdr_engineering.pi_recovery_verification import verify_recovery

IDENTITY = {
    "paneId": "w1:p1",
    "terminalId": "terminal",
    "sessionId": "session",
    "instanceId": "instance",
    "pid": 42,
    "birth": "birth",
    "uid": 1000,
}
MODEL = {"id": "working", "contextWindow": 262144, "reasoning": True, "input": ["text"]}
TARGET = {"host": "bender", "sshTarget": "bender", "port": 22, "local": True, "session": "default"}


def messages(stop="stop", *, tool_result=None):
    result = [
        {"type": "session", "id": "session"},
        {
            "type": "message",
            "id": "before",
            "parentId": None,
            "message": {"role": "assistant", "stopReason": "error"},
        },
        {
            "type": "message",
            "id": "request",
            "parentId": "before",
            "message": {"role": "user", "content": "PRIVATE REQUEST"},
        },
        {
            "type": "message",
            "id": "after",
            "parentId": "request",
            "message": {
                "role": "assistant",
                "stopReason": stop,
                "provider": "metabolomics",
                "model": "working",
                "errorMessage": "PRIVATE ERROR",
                "content": [{"type": "toolCall", "id": "call"}]
                if stop == "toolUse"
                else [{"type": "text", "text": "PRIVATE ANSWER"}],
            },
        },
    ]
    if tool_result is not None:
        result.append(
            {
                "type": "message",
                "id": "tool-result",
                "parentId": "after",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "call",
                    "isError": tool_result,
                    "content": "PRIVATE TOOL OUTPUT",
                },
            }
        )
    return result


class Observer:
    def __init__(self, entries=None):
        self.entries = entries or messages()
        self.current = {
            **IDENTITY,
            "provider": "metabolomics",
            "model": "working",
            "agentStatus": "working",
            "runtime": {},
            "missions": [],
            "requiredContext": 131072,
            "requiresVision": False,
            "requiresReasoning": True,
            "models": [MODEL],
        }
        self.observations = []
        self.status_reads = []

    def observe(self, target, pane, *, baseline=None):
        self.observations.append(pane)
        return {
            **copy.deepcopy(self.current),
            "progressEvidence": recovery_progress(self.entries, baseline),
        }

    def command(self, *args):
        raise AssertionError("verification must never prompt")

    def probe(self, *args):
        raise AssertionError("verification must never probe models")

    def maintenance(self, target, root, instance, request):
        assert request["action"] == "status"
        self.status_reads.append(request)
        return {
            "maintenance": {
                "action": "reload",
                "operationId": request["operationId"],
                "status": "succeeded",
                "result": {"commit": "b" * 40},
            }
        }


def setup(tmp_path):
    store = RecoveryStore(tmp_path / "state")
    plan = {
        "id": "a" * 32,
        "schema": 1,
        "status": "needs_verification",
        "hosts": {"bender": {"target": TARGET}},
        "sessions": [
            {
                "host": "bender",
                "paneId": "w1:p1",
                "identity": dict(IDENTITY),
                "baseline": "before",
                "selectedModel": "working",
                "requirements": {
                    "requiredContext": 131072,
                    "requiresVision": False,
                    "requiresReasoning": True,
                },
                "missionFence": [],
                "status": "accepted",
            }
        ],
    }
    store.write(plan)
    return store, plan


def verify(store, plan, observer, **kwargs):
    return verify_recovery(store, plan["id"], transport=observer, managed=True, **kwargs)


def test_new_completed_turn_is_verified_without_content_leakage(tmp_path):
    store, plan = setup(tmp_path)
    result = verify(store, plan, Observer())["sessions"][0]
    assert result["status"] == "progress_verified"
    assert result["verification"]["evidence"]["assistantId"] == "after"
    assert result["verification"]["selectedModelConfirmed"] is True
    assert "PRIVATE" not in str(store.read(plan["id"]))


def test_new_error_is_failed_even_when_outage_classifier_would_suppress_it(tmp_path):
    store, plan = setup(tmp_path)
    observer = Observer(messages("error"))
    observer.current["failure"] = None
    result = verify(store, plan, observer)["sessions"][0]
    assert result["status"] == "inference_failed"
    assert result["verification"]["evidence"]["stopReason"] == "error"


@pytest.mark.parametrize(
    "tool_error,expected",
    [(False, "progress_verified"), (True, "verification_waiting"), (None, "verification_waiting")],
)
def test_only_successful_new_tool_results_prove_progress(tmp_path, tool_error, expected):
    store, plan = setup(tmp_path)
    assert (
        verify(store, plan, Observer(messages("toolUse", tool_result=tool_error)))["sessions"][0][
            "status"
        ]
        == expected
    )


def test_working_badge_without_new_assistant_is_only_waiting(tmp_path):
    store, plan = setup(tmp_path)
    assert (
        verify(store, plan, Observer(messages()[:3]))["sessions"][0]["status"]
        == "verification_waiting"
    )


@pytest.mark.parametrize("field", ["pid", "birth", "instanceId", "sessionId", "terminalId"])
def test_changed_identity_never_proves_progress(tmp_path, field):
    store, plan = setup(tmp_path)
    observer = Observer()
    observer.current[field] = "replacement"
    assert verify(store, plan, observer)["sessions"][0]["status"] == "verification_unknown"


def test_baseline_must_exist_and_be_an_ancestor(tmp_path):
    store, plan = setup(tmp_path)
    entries = messages()
    entries[-1]["parentId"] = None
    assert verify(store, plan, Observer(entries))["sessions"][0]["status"] == "verification_unknown"
    assert recovery_progress(messages(), "missing")["baselinePresent"] is False


def test_compatible_native_fallback_is_reported_honestly(tmp_path):
    store, plan = setup(tmp_path)
    observer = Observer()
    observer.entries[-1]["message"]["model"] = "fallback"
    observer.current["models"].append({**MODEL, "id": "fallback"})
    result = verify(store, plan, observer)["sessions"][0]
    assert result["status"] == "progress_verified"
    assert result["verification"]["modelChanged"] is True
    assert result["verification"]["selectedModelConfirmed"] is False


def test_unconfigured_fallback_does_not_claim_verified_model(tmp_path):
    store, plan = setup(tmp_path)
    observer = Observer()
    observer.entries[-1]["message"]["model"] = "unknown-model"
    result = verify(store, plan, observer)["sessions"][0]
    assert result["status"] == "verification_unknown"
    assert result["verification"]["evidence"]["assistantId"] == "after"


def test_typed_mission_acceptance_is_not_interactive_progress(tmp_path):
    store, plan = setup(tmp_path)
    plan["sessions"][0]["request"] = {"action": "resume", "operationId": "operation"}
    store.write(plan)
    observer = Observer()
    result = verify(store, plan, observer)["sessions"][0]
    assert result["status"] == "accepted"
    assert result["verification"]["state"] == "manual_pending"
    assert not observer.observations


def test_running_reload_is_polled_only_after_proven_handler_handover(tmp_path):
    store, plan = setup(tmp_path)
    entry = plan["sessions"][0]
    entry["status"] = "unknown"
    entry["operations"] = {
        "reload": {
            "request": {"action": "reload", "operationId": "operation", "expectedCommit": "b" * 40},
            "receipt": {"status": "running"},
        }
    }
    store.write(plan)
    observer = Observer()
    observer.current.update(instanceId="new-handler", sourceRoot="/runtime")
    observer.current["runtime"] = {"commit": "b" * 40, "pending": False}
    result = verify(store, plan, observer)["sessions"][0]
    assert result["status"] == "unknown"  # Receipt success never clears dedupe or resumes work.
    assert result["verification"]["state"] == "operation_succeeded"
    assert observer.status_reads == [
        {"action": "status", "operationId": "operation", "expectedInstanceId": "new-handler"}
    ]


def test_active_recovery_lock_defers_live_verification(tmp_path):
    store, plan = setup(tmp_path)
    observer = Observer()
    with store.lock():
        result = verify(store, plan, observer)
    assert result["verificationDeferred"] == "recovery_in_progress"
    assert not observer.observations
    assert "verificationDeferred" not in store.read(plan["id"])


def test_verified_session_can_later_report_a_new_failure(tmp_path):
    store, plan = setup(tmp_path)
    observer = Observer()
    assert verify(store, plan, observer)["sessions"][0]["status"] == "progress_verified"
    observer.entries.append(
        {
            "type": "message",
            "id": "later-error",
            "parentId": "after",
            "message": {
                "role": "assistant",
                "provider": "metabolomics",
                "model": "working",
                "stopReason": "error",
            },
        }
    )
    assert verify(store, plan, observer)["sessions"][0]["status"] == "inference_failed"


def test_verification_bound_rotates_to_unchecked_sessions(tmp_path):
    store, plan = setup(tmp_path)
    second = copy.deepcopy(plan["sessions"][0])
    second["paneId"] = second["identity"]["paneId"] = "w1:p2"
    plan["sessions"].append(second)
    store.write(plan)

    class MultiObserver(Observer):
        def observe(self, target, pane, **kwargs):
            current = super().observe(target, pane, **kwargs)
            current["paneId"] = pane
            return current

    observer = MultiObserver()
    verify(store, plan, observer, max_sessions=1, now=lambda: 1000)
    result = verify(store, plan, observer, max_sessions=1, now=lambda: 1001)
    assert observer.observations == ["w1:p1", "w1:p2"]
    assert result["verificationSummary"]["progressVerified"] == 2


def test_status_cli_uses_read_only_verification(tmp_path, monkeypatch, capsys):
    from herdr_engineering import cli, pi_recovery, pi_recovery_verification

    store, plan = setup(tmp_path)
    observer = Observer()
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(pi_recovery, "RecoveryStore", lambda: store)
    monkeypatch.setattr(pi_recovery_verification, "RecoveryTransport", lambda: observer)
    args = cli._build_parser().parse_args(["fleet", "recover", "status", "--id", plan["id"]])
    assert args.func(args) == 0
    assert observer.observations == ["w1:p1"]
    assert "progress_verified" in capsys.readouterr().out


def test_unknown_operation_cannot_poll_reused_pid(tmp_path):
    store, plan = setup(tmp_path)
    entry = plan["sessions"][0]
    entry.update(
        status="unknown",
        operations={
            "reload": {
                "request": {"action": "reload", "operationId": "operation"},
                "receipt": {"status": "running"},
            }
        },
    )
    store.write(plan)
    observer = Observer()
    observer.current["birth"] = "reused"
    result = verify(store, plan, observer)["sessions"][0]
    assert (
        result["status"] == "unknown" and result["verification"]["state"] == "verification_unknown"
    )
    assert not observer.status_reads


def test_fallback_cannot_shrink_original_context_or_reasoning_requirements(tmp_path):
    store, plan = setup(tmp_path)
    plan["sessions"][0]["requirements"] = {
        "requiredContext": 131072,
        "requiresVision": False,
        "requiresReasoning": True,
    }
    store.write(plan)
    observer = Observer()
    observer.entries[-1]["message"]["model"] = "small"
    observer.current.update(
        requiredContext=32000,
        requiresReasoning=False,
        models=[{"id": "small", "contextWindow": 32000, "reasoning": False, "input": ["text"]}],
    )
    result = verify(store, plan, observer)["sessions"][0]
    assert result["status"] == "verification_unknown"


def test_legacy_candidate_envelope_can_prove_compatible_fallback(tmp_path):
    store, plan = setup(tmp_path)
    plan["sessions"][0].pop("requirements", None)
    plan["sessions"][0]["candidates"] = [MODEL]
    store.write(plan)
    observer = Observer()
    observer.entries[-1]["message"]["model"] = "fallback"
    observer.current["models"].append({**MODEL, "id": "fallback"})
    assert verify(store, plan, observer)["sessions"][0]["status"] == "progress_verified"


def test_fallback_without_original_requirement_evidence_stays_unverified(tmp_path):
    store, plan = setup(tmp_path)
    plan["sessions"][0].pop("requirements", None)
    plan["sessions"][0].pop("candidates", None)
    store.write(plan)
    observer = Observer()
    observer.entries[-1]["message"]["model"] = "fallback"
    observer.current["models"].append({**MODEL, "id": "fallback"})
    result = verify(store, plan, observer)["sessions"][0]
    assert result["status"] == "verification_unknown"
    assert result["verification"]["compatibilityBasis"] == "unavailable"


def test_tool_result_on_an_unrelated_branch_does_not_prove_progress():
    entries = messages("toolUse", tool_result=False)
    entries[-1]["parentId"] = "before"
    result = recovery_progress(entries, "before")
    assert result["assistant"]["id"] == "after"
    assert result["successfulToolResults"] == []


def test_legacy_unknown_input_capability_does_not_crash_verification(tmp_path):
    store, plan = setup(tmp_path)
    plan["sessions"][0].pop("requirements", None)
    plan["sessions"][0]["candidates"] = [{**MODEL, "input": None}]
    store.write(plan)
    observer = Observer()
    observer.entries[-1]["message"]["model"] = "fallback"
    observer.current["models"].append({**MODEL, "id": "fallback"})
    assert verify(store, plan, observer)["sessions"][0]["status"] == "verification_unknown"
