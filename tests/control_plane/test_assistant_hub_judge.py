"""Judge question set and verdict: pure logic, plus a real-JevClient unreachable check and an optional integration test."""
from __future__ import annotations

import json
import math
import os
import socket
from pathlib import Path
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant_hub.canonical import RawAction
from herdr_engineering.control_plane.assistant_hub.jev import (
    JevClient,
    JudgeResult,
    JudgeUnavailable,
    NoulAnswer,
    ScoreAnswer,
    build_request,
)
from herdr_engineering.control_plane.assistant_hub.judge import (
    QUESTION_SET_VERSION,
    QUESTIONS,
    SEVERITY_LEVELS,
    Verdict,
    build_state,
    consult,
    evaluate,
)
from herdr_engineering.control_plane.assistant_hub.judge_version import judge_version
from herdr_engineering.control_plane.assistant_hub.verbs import (
    ActionKey,
    FlagClass,
    RiskClass,
    Verb,
)

THRESHOLDS = {"destructive": 0.5, "irreversible": 0.5, "severity_score": 2.0}
CFG: dict[str, Any] = {"model": "laya-auto", "thresholds": THRESHOLDS}
CANARY = "CANARY_CMD_rm_-rf_/secret"


def _key(target: str = "sha256:deadbeefdigest") -> ActionKey:
    return ActionKey("bash", Verb.GIT_PUSH, "org/repo", target, FlagClass.FORCE, RiskClass.REMOTE_REVERSIBLE)


def _res(d: float = 0.1, i: float = 0.1, s: float = 0.5, c: float = 0.9, model: str = "m") -> JudgeResult:
    return JudgeResult(
        model=model,
        answers={
            "destructive": NoulAnswer(d),
            "irreversible_or_external": NoulAnswer(i),
            "severity": ScoreAnswer(s, 0.9, {"0": 0.25, "1": 0.25, "2": 0.25, "3": 0.25}),
            "owner_approval_covers": NoulAnswer(c),
        },
        input_tokens=1,
        output_tokens=1,
        request_id="r",
    )


def test_version_reexport() -> None:
    assert QUESTION_SET_VERSION == 1
    assert SEVERITY_LEVELS == ("informational", "minor", "major", "critical")


def test_state_has_only_five_facts() -> None:
    state = build_state(_key(), {})
    assert set(state) == {"action", "owner_context"}
    assert state["action"] == {
        "tool": "bash", "verb": "git.push", "resource_scope": "org/repo",
        "flag_class": "force", "risk_class": "remote_reversible",
    }
    assert state["owner_context"] == {}


def test_request_body_excludes_target_command_hints_display() -> None:
    raw = RawAction(tool="bash", command=CANARY, hint={"display": "DISPLAY_CANARY", "x": "HINT_CANARY"})
    key = _key("TARGET_CANARY_digest")
    body = build_request("laya-auto", build_state(key, {}), QUESTIONS).decode()
    doc = json.loads(body)
    assert set(doc) == {"state", "model", "questions"}
    assert set(doc["state"]) == {"action", "owner_context"}
    assert set(doc["state"]["action"]) == {"tool", "verb", "resource_scope", "flag_class", "risk_class"}
    for secret in (CANARY, "DISPLAY_CANARY", "HINT_CANARY", "TARGET_CANARY_digest", key.key_id(), raw.command or ""):
        assert secret not in body
    assert "target" not in doc["state"]["action"]


def test_owner_context_allowlist() -> None:
    assert build_state(_key(), {"note": "ok"})["owner_context"] == {"note": "ok"}
    assert build_state(_key(), {"note": "x" * 500})["owner_context"]["note"] == "x" * 500
    with pytest.raises(ValueError):
        build_state(_key(), {"command": "rm"})
    with pytest.raises(ValueError):
        build_state(_key(), {"note": "x" * 501})
    with pytest.raises(ValueError):
        build_state(_key(), {"note": 5})  # type: ignore[dict-item]


def test_questions_shape() -> None:
    assert set(QUESTIONS) == {"destructive", "irreversible_or_external", "severity", "owner_approval_covers"}
    assert [QUESTIONS[n]["type"] for n in QUESTIONS] == ["noul", "noul", "score", "noul"]
    assert QUESTIONS["severity"]["criteria"] == list(SEVERITY_LEVELS)
    assert all(isinstance(q["instructions"], str) and q["instructions"] for q in QUESTIONS.values())


def test_no_escalation_when_all_below() -> None:
    v = evaluate(_res(), THRESHOLDS, "ver")
    assert isinstance(v, Verdict)
    assert not v.escalate and v.reasons == ()
    assert v.judge_version == "ver" and v.model == "m"


@pytest.mark.parametrize(
    ("field", "below", "at", "above", "reason"),
    [
        ("d", 0.49, 0.5, 0.51, "destructive"),
        ("i", 0.49, 0.5, 0.51, "irreversible"),
        ("s", 1.99, 2.0, 2.01, "severity"),
    ],
)
def test_threshold_boundaries(field: str, below: float, at: float, above: float, reason: str) -> None:
    assert not evaluate(_res(**{field: below}), THRESHOLDS, "v").escalate
    for val in (at, above):
        v = evaluate(_res(**{field: val}), THRESHOLDS, "v")
        assert v.escalate and len(v.reasons) == 1 and reason in v.reasons[0]


def test_covers_boundary() -> None:
    assert evaluate(_res(c=0.49), THRESHOLDS, "v").escalate
    assert not evaluate(_res(c=0.5), THRESHOLDS, "v").escalate
    assert not evaluate(_res(c=0.51), THRESHOLDS, "v").escalate


def test_reasons_are_static_and_multiple() -> None:
    v = evaluate(_res(d=0.9, i=0.9, s=3.0, c=0.0), THRESHOLDS, "v")
    assert len(v.reasons) == 4
    assert v.reasons == evaluate(_res(d=0.8, i=0.7, s=2.5, c=0.1), THRESHOLDS, "v").reasons


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_evaluate_refuses_non_finite(bad: float) -> None:
    for kw in ({"d": bad}, {"i": bad}, {"s": bad}, {"c": bad}):
        with pytest.raises(JudgeUnavailable):
            evaluate(_res(**kw), THRESHOLDS, "v")


def test_evaluate_missing_answer_or_wrong_type_fails_closed() -> None:
    r = _res()
    broken = JudgeResult("m", {k: v for k, v in r.answers.items() if k != "severity"}, 1, 1, "r")
    with pytest.raises(JudgeUnavailable):
        evaluate(broken, THRESHOLDS, "v")
    swapped = JudgeResult("m", {**r.answers, "severity": NoulAnswer(0.1)}, 1, 1, "r")
    with pytest.raises(JudgeUnavailable):
        evaluate(swapped, THRESHOLDS, "v")


def test_evaluate_empty_model_fails_closed() -> None:
    with pytest.raises(JudgeUnavailable):
        evaluate(_res(model=""), THRESHOLDS, "v")


def test_consult_unreachable_endpoint(tmp_path: Path) -> None:
    kf = tmp_path / "k"
    kf.write_text("sk-test-not-real\n")
    kf.chmod(0o600)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    client = JevClient(f"http://127.0.0.1:{port}", str(kf), "laya-auto", 1.0)
    with pytest.raises(JudgeUnavailable):
        consult(client, _key(), CFG)


_URL = os.environ.get("HERDR_JEV_TEST_URL", "")
_KEY = os.environ.get("HERDR_JEV_TEST_KEY_FILE", "")


@pytest.mark.integration
@pytest.mark.skipif(not (_URL and _KEY), reason="set HERDR_JEV_TEST_URL and HERDR_JEV_TEST_KEY_FILE to a real System One (mock backend ok)")
def test_integration_consult() -> None:
    client = JevClient(_URL, _KEY, os.environ.get("HERDR_JEV_TEST_MODEL", "laya-auto"), 30.0)
    v = consult(client, _key(), CFG)
    assert isinstance(v, Verdict) and v.model and v.judge_version == judge_version(CFG)
