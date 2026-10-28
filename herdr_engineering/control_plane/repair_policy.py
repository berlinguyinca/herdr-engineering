"""Deterministic risk policy, incident state machine and circuit breaker for autonomous CI repair
(spec 03-AUTONOMY-AND-CI-REPAIR; DECISIONS D6).

* ``evaluate`` is pure and sits IN FRONT of ``review_gate.decide_merge``: ``combine`` can only turn a
  "merge" into "wait/reject", never the reverse. No model output (verdict text, confidence, rationale) is an
  input here, so a model cannot argue its way past the gate; an AI review is evidence for a human or for
  ``decide_merge``, never the authorisation.
* Risk comes from paths and declared change facts, never from line count alone.
* Default for anything unclassified is ``require_human``; critical is ``deny``.
* The policy itself is data (``RepairPolicy``) loaded from YAML by the operator. Nothing here writes it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

RISKS = ("low", "moderate", "high", "critical")

def _glob(pattern: str) -> re.Pattern:
    """POSIX-style glob: ``*`` never crosses ``/``; ``**/`` is any depth (including none); ``**`` at the end is anything."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif pattern.startswith("**", i):
            out += ".*"; i += 2
        elif pattern[i] == "*":
            out += "[^/]*"; i += 1
        elif pattern[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(pattern[i]); i += 1
    return re.compile(out + r"\Z")


def _match(path: str, patterns) -> bool:
    p = path.lower().removeprefix("./").lstrip("/")
    return any(_glob(x.lower()).match(p) for x in patterns)


CRITICAL_PATHS = ("**/secrets/**", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx", "**/id_rsa*", "**/id_ed25519*", "**/.env*",
                  "**/billing/**", "**/iam/**", "deploy/**/prod*", "**/publication*", "**/artifact_acl*", "**/.npmrc", "**/.pypirc",
                  "**/credentials*")
HIGH_PATHS = (
    ".woodpecker*", ".woodpecker/**", ".github/**", "deploy/**", "ansible/**", "**/migrations/**", "**/*.sql", "**/dockerfile*",
    "**/makefile", "**/*.sh", "**/*.tf", "**/tox.ini", "**/conftest.py", "**/codeowners", "**/.gitmodules",
    "package.json", "package-lock.json", "pyproject.toml", "**/requirements*.txt", "**/cargo.toml", "**/cargo.lock",
    "**/go.mod", "**/go.sum", "**/*.lock",
    "**/auth*", "**/*_auth*", "**/*rbac*", "**/*acl*", "**/*secret*", "**/*token*", "**/scheduler*",
    "**/review_gate/**", "**/*policy*", "**/*benchmark*", "**/*scoring*", "**/api.py",
    # agent instructions, skills and prompt templates steer agents: never "docs"
    "**/skill.md", "**/agents.md", "**/claude.md", "**/skills/**", "**/*prompt*", "**/.claude/**", "**/.agents/**",
)
LOW_DOC_PATHS = ("docs/**/*.md", "docs/**/*.png", "docs/**/*.txt", "readme", "readme.md", "readme.rst", "changelog", "changelog.md",
                 "contributing.md", "license")
FIXTURE_PATHS = ("tests/fixtures/**", "**/fixtures/**", "**/testdata/**")


@dataclass(frozen=True)
class Change:
    paths: tuple[str, ...]       # callers MUST include the OLD path of every rename and deletion
    additions: int = 0
    deletions: int = 0
    formatting_only: bool = False          # declared by a deterministic formatter diff, not by a model
    fixtures_externally_verified: bool = False
    touches_tests_and_source: bool = False


def classify_risk(change: Change) -> tuple[str, tuple[str, ...]]:
    """Return ``(risk, reasons)``. A single high/critical path decides the whole change."""
    if not change.paths:
        return "high", ("empty change set cannot be classified",)
    reasons: list[str] = []
    worst = "low"
    for path in change.paths:
        if _match(path, CRITICAL_PATHS):
            return "critical", (f"critical path {path}",)
        if _match(path, HIGH_PATHS):
            worst = "high"
            reasons.append(f"protected/high-risk path {path}")
        elif _match(path, LOW_DOC_PATHS) or (change.formatting_only and path.endswith((".py", ".js", ".ts", ".rs"))):
            continue
        elif _match(path, FIXTURE_PATHS) and change.fixtures_externally_verified:
            continue
        elif worst == "low":
            worst = "moderate"
            reasons.append(f"behavioural path {path}")
    return worst, tuple(dict.fromkeys(reasons)) or ("docs/format/verified-fixture only",)


@dataclass(frozen=True)
class RepairPolicy:
    version: str = "p0"
    auto_merge_enabled: bool = False
    kill_switch: bool = False
    allowed_repositories: frozenset[str] = frozenset()
    allowed_branches: frozenset[str] = frozenset({"main"})
    auto_merge_risks: frozenset[str] = frozenset()      # subset of {"low"}; moderate needs explicit project approval
    max_patch_lines: int = 200
    require_human_approval: bool = True                 # a bot approval never satisfies this
    max_attempts: int = 3
    max_open_repair_prs: int = 2          # open repair PRs OTHER than the one being evaluated

    def __post_init__(self):
        if not self.auto_merge_risks <= {"low", "moderate"}:
            raise ValueError("auto_merge_risks may only contain low or moderate")


@dataclass(frozen=True)
class Evidence:
    repository: str
    base_branch: str
    head_sha: str
    change: Change
    review_sha: str | None
    review_passed: bool
    reviewer_context: str | None        # execution context id
    author_context: str | None
    reviewer_model_family: str | None
    author_model_family: str | None
    check_shas: dict[str, str]          # check name -> sha it ran on
    check_status: dict[str, str]        # check name -> pass|fail|pending|cancelled
    required_checks: tuple[str, ...]
    changes_requested: bool
    human_approvals: int
    provenance_signed: bool
    attempt: int = 1
    open_repair_prs: int = 0
    conflicting_incident: bool = False
    scientific_validation_pending: bool = False   # green CI is never scientific validity


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Literal["allow", "require_human", "deny"]
    risk: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


def evaluate(policy: RepairPolicy, ev: Evidence) -> PolicyDecision:
    risk, risk_reasons = classify_risk(ev.change)
    deny: list[str] = []
    human: list[str] = []

    if policy.kill_switch:
        deny.append("kill switch is on")
    if risk == "critical":
        deny.extend(risk_reasons)
    if ev.attempt > policy.max_attempts:
        deny.append(f"attempt {ev.attempt} exceeds budget {policy.max_attempts}")
    if ev.open_repair_prs >= policy.max_open_repair_prs:
        deny.append("too many open repair PRs (cascade breaker)")
    if deny:
        return PolicyDecision("deny", risk, tuple(deny))

    # Every merge precondition below turns into "human", never a silent allow.
    if not policy.auto_merge_enabled:
        human.append("auto-merge is not enabled for this project")
    if ev.repository not in policy.allowed_repositories:
        human.append(f"repository {ev.repository} not authorised for automation")
    if ev.base_branch not in policy.allowed_branches:
        human.append(f"branch {ev.base_branch} not authorised")
    if risk not in policy.auto_merge_risks:
        human.append(f"risk class {risk} requires a human ({'; '.join(risk_reasons)})")
    if ev.change.additions < 0 or ev.change.deletions < 0 or ev.change.additions + ev.change.deletions > policy.max_patch_lines:
        human.append("patch exceeds size budget")
    if ev.conflicting_incident:
        human.append("conflicting active incident")
    if ev.scientific_validation_pending:
        human.append("scientific validation pending; software CI is not validation")
    if not ev.provenance_signed:
        human.append("provenance is not signed")
    if ev.changes_requested:
        human.append("changes requested")
    if policy.require_human_approval and ev.human_approvals < 1:
        human.append("human approval required and absent")
    if not ev.review_passed:
        human.append("independent review did not pass")
    if ev.review_sha != ev.head_sha:
        human.append("review is not on the current head SHA")
    if not ev.reviewer_context or ev.reviewer_context == ev.author_context:
        human.append("reviewer is not an independent execution context")
    if not ev.reviewer_model_family or not ev.author_model_family:
        human.append("reviewer/author model families are not both known")
    elif ev.reviewer_model_family == ev.author_model_family:
        human.append("reviewer shares the author's model family")
    for name in ev.required_checks:
        if name not in ev.check_status:
            human.append(f"required check {name} missing")
        elif ev.check_status[name] != "pass":
            human.append(f"required check {name} is {ev.check_status[name]}")
        elif ev.check_shas.get(name) != ev.head_sha:
            human.append(f"required check {name} ran on a different SHA")
    if not ev.required_checks:
        human.append("no required checks are configured")
    return PolicyDecision("require_human" if human else "allow", risk, tuple(human))


def combine(policy_decision: PolicyDecision, gate_decision: str) -> str:
    """Final merge decision: the stricter of the policy and ``decide_merge``. Never widens."""
    if policy_decision.outcome == "deny" or gate_decision == "reject":
        return "reject"
    if policy_decision.outcome == "require_human" or gate_decision == "wait":
        return "wait"
    return "merge"


# ---- incident state machine -------------------------------------------------------------------
STATES = ("DETECTED", "TRIAGED", "INVESTIGATING", "PROPOSED", "AUTHORIZED", "EXECUTING", "REVIEWING",
          "VERIFYING", "COMPLETED", "BLOCKED", "AWAITING_APPROVAL", "FAILED", "CANCELLED", "ROLLBACK_REQUESTED")
_FORWARD = {"DETECTED": "TRIAGED", "TRIAGED": "INVESTIGATING", "INVESTIGATING": "PROPOSED",
            "PROPOSED": "AUTHORIZED", "AUTHORIZED": "EXECUTING", "EXECUTING": "REVIEWING",
            "REVIEWING": "VERIFYING", "VERIFYING": "COMPLETED"}
TERMINAL = frozenset({"FAILED", "CANCELLED", "ROLLBACK_REQUESTED"})   # COMPLETED may still request a rollback
_SIDE = frozenset({"BLOCKED", "AWAITING_APPROVAL", "FAILED", "CANCELLED", "ROLLBACK_REQUESTED"})
_PAUSES = ("BLOCKED", "AWAITING_APPROVAL")


class IllegalTransition(ValueError):
    pass


@dataclass(frozen=True)
class Transition:
    at: str
    from_state: str
    to_state: str
    cause_event: str
    policy_version: str
    agent_run_id: str | None = None


def incident_key(project: str, source_incident: str, policy_version: str) -> str:
    """One repair attempt per (project, source incident, policy version): duplicate CI notices attach."""
    return f"{project}|{source_incident}|{policy_version}"


@dataclass
class Incident:
    key: str
    state: str = "DETECTED"
    paused_from: str | None = None
    history: list[Transition] = field(default_factory=list)
    attached_events: set[str] = field(default_factory=set)

    def attach(self, event_id: str) -> bool:
        """A duplicate notification attaches to the same incident; returns False when already known."""
        if event_id in self.attached_events:
            return False
        self.attached_events.add(event_id)
        return True

    def transition(self, to: str, *, at: str, cause_event: str, policy_version: str, agent_run_id: str | None = None):
        cur = self.state
        if to not in STATES:
            raise IllegalTransition(f"unknown state {to}")
        if cur in TERMINAL:
            raise IllegalTransition(f"{cur} is terminal")
        if cur == "COMPLETED":
            if to != "ROLLBACK_REQUESTED":
                raise IllegalTransition("COMPLETED only allows a rollback request")
        elif to == "ROLLBACK_REQUESTED":
            if cur != "VERIFYING":
                raise IllegalTransition("rollback is only requested from post-merge verification or after completion")
        elif cur in _PAUSES:
            origin = self.paused_from
            # Leaving a pause returns to where it began (or its next step); moving between pauses keeps the origin.
            if not (to in _PAUSES and to != cur) and to not in (origin, _FORWARD.get(origin or ""), "FAILED", "CANCELLED"):
                raise IllegalTransition(f"{cur} -> {to} is not allowed")
        elif not (to == _FORWARD.get(cur) or (to in _SIDE and to != cur)):
            raise IllegalTransition(f"{cur} -> {to} is not allowed")
        if to in _PAUSES and cur not in _PAUSES:
            self.paused_from = cur
        elif cur in _PAUSES and to not in _PAUSES:
            self.paused_from = None
        self.history.append(Transition(at, cur, to, cause_event, policy_version, agent_run_id))
        self.state = to
        return self


@dataclass
class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; only an operator ``reset`` closes it (no self-healing)."""
    threshold: int = 3
    failures: int = 0
    open: bool = False

    def record(self, ok: bool) -> None:
        if ok:
            self.failures = 0
            return
        self.failures += 1
        if self.failures >= self.threshold:
            self.open = True

    def reset(self) -> None:
        self.failures, self.open = 0, False
