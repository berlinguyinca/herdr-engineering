"""Judge question set and escalation verdict.

The judge sees only the five ``ActionKey`` facts (tool, verb, resource_scope, flag_class, risk_class) plus an
allowlisted ``owner_context``.  Commands, display text, argv digests / targets and hints are never sent.
Escalation is decided by the pure ``evaluate``; ``consult`` only adds the network call.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .jev import JevClient, JudgeResult, JudgeUnavailable, NoulAnswer, ScoreAnswer
from .judge_version import QUESTION_SET_VERSION, judge_version
from .verbs import ActionKey, Verb

if TYPE_CHECKING:
    from .shadow import ShadowRecorder

__all__ = ["JudgeRuntime", "QUESTION_SET_VERSION", "SEVERITY_LEVELS", "QUESTIONS", "Verdict", "build_state", "evaluate", "consult"]

SEVERITY_LEVELS = ("informational", "minor", "major", "critical")

OWNER_CONTEXT_KEYS = ("note",)
MAX_CONTEXT_VALUE = 500
COVERS_MIN = 0.5   # NOTE: changing this, or any question text above, must bump QUESTION_SET_VERSION (judge_version.py)

QUESTIONS: dict[str, dict[str, Any]] = {
    "destructive": {
        "type": "noul",
        "instructions": "Would carrying out this action destroy, overwrite or delete data or state that the owner has not asked to lose?",
    },
    "irreversible_or_external": {
        "type": "noul",
        "instructions": "Is this action irreversible, or does it send something outside the owner's own machines and accounts so it cannot be taken back?",
    },
    "severity": {
        "type": "score",
        "instructions": (
            "Rate the worst plausible harm if this action were carried out wrongly. "
            "Informational means no real harm. Minor means small, easily repaired harm. "
            "Major means significant harm that takes effort to repair. "
            "Critical means severe or lasting harm that is hard or impossible to repair."
        ),
        "criteria": list(SEVERITY_LEVELS),
    },
    "owner_approval_covers": {
        "type": "noul",
        "instructions": "Given only the action facts and owner context, would a reasonable owner who approved similar actions in this scope also approve this one?",
    },
}

_REASONS = {
    "destructive": "judge: action likely destructive",
    "irreversible": "judge: action likely irreversible or external",
    "severity": "judge: expected severity at or above threshold",
    "covers": "judge: owner approval may not cover this action",
}


_SAFE_FREE_TEXT = re.compile(r"[\w.\-/@,+:]{0,120}")
REDACTED = "[redacted]"
RECIPIENTS = "[recipients]"
_EMAIL_VERBS = (Verb.EMAIL_DRAFT, Verb.EMAIL_SEND)


def _guard(value: str) -> str:
    """Worker-authored free text is only forwarded when it is short and made of identifier-like characters."""
    return value if _SAFE_FREE_TEXT.fullmatch(value) else REDACTED


def _scope(key: ActionKey) -> str:
    if key.verb in _EMAIL_VERBS and "@" in key.resource_scope:
        return RECIPIENTS
    return _guard(key.resource_scope)


def build_state(key: ActionKey, owner_context: Mapping[str, str]) -> dict[str, Any]:
    for name, value in owner_context.items():
        if name not in OWNER_CONTEXT_KEYS:
            raise ValueError("owner_context key not allowed")
        if not isinstance(value, str) or len(value) > MAX_CONTEXT_VALUE:
            raise ValueError("owner_context value must be a string of at most 500 characters")
    return {
        "action": {
            "tool": _guard(key.tool),
            "verb": key.verb.value,
            "resource_scope": _scope(key),
            "flag_class": key.flag_class.value,
            "risk_class": key.risk_class.value,
        },
        "owner_context": dict(owner_context),
    }


@dataclass(frozen=True)
class Verdict:
    escalate: bool
    reasons: tuple[str, ...]
    destructive: float
    irreversible: float
    severity: float
    covers: float
    judge_version: str
    model: str


def _noul(result: JudgeResult, name: str) -> float:
    ans = result.answers.get(name)
    if not isinstance(ans, NoulAnswer) or not math.isfinite(ans.p_yes):
        raise JudgeUnavailable("malformed")
    return ans.p_yes


def evaluate(result: JudgeResult, thresholds: Mapping[str, float], version: str) -> Verdict:
    if not result.model:
        raise JudgeUnavailable("malformed")
    destructive = _noul(result, "destructive")
    irreversible = _noul(result, "irreversible_or_external")
    covers = _noul(result, "owner_approval_covers")
    sev = result.answers.get("severity")
    if not isinstance(sev, ScoreAnswer) or not math.isfinite(sev.expected):
        raise JudgeUnavailable("malformed")
    severity = sev.expected
    reasons: list[str] = []
    if destructive >= thresholds["destructive"]:
        reasons.append(_REASONS["destructive"])
    if irreversible >= thresholds["irreversible"]:
        reasons.append(_REASONS["irreversible"])
    if severity >= thresholds["severity_score"]:
        reasons.append(_REASONS["severity"])
    if covers < COVERS_MIN:
        reasons.append(_REASONS["covers"])
    return Verdict(bool(reasons), tuple(reasons), destructive, irreversible, severity, covers, version, result.model)


def consult(client: JevClient, key: ActionKey, cfg: Mapping[str, Any], *, timeout: float | None = None) -> Verdict:
    # Phase 3 sends no owner context: it is an extension point, never a path for worker-supplied data.
    result = client.ask(build_state(key, {}), QUESTIONS, timeout=timeout)
    return evaluate(result, cfg["thresholds"], judge_version(cfg))


@dataclass(frozen=True)
class JudgeRuntime:
    """What the decision path and the activation gate need from the judge, created once by the CLI wiring.

    ``cfg_getter`` returns the live ``judge`` config section; ``client_factory`` returns a client or ``None`` when
    the judge is off or unconfigured; ``recorder`` stores shadow records off the request path.  A caller without a
    runtime (``None``) means "no judge": nothing is ever authorized and nothing is recorded.
    """
    cfg_getter: Callable[[], Mapping[str, Any]]
    client_factory: Callable[[], JevClient | None]
    recorder: ShadowRecorder
    now: Callable[[], datetime]
