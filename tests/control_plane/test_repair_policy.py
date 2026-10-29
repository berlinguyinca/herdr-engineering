"""Negative-first policy fixtures (06-TEST-PLAN): 0 unauthorised merges, SHA revalidation, no model override."""
from dataclasses import replace

import pytest

from herdr_engineering.control_plane import repair_policy as rp
from herdr_engineering.review_gate.decision import MergeDecision

HEAD = "a" * 40
POLICY = rp.RepairPolicy(auto_merge_enabled=True, allowed_repositories=frozenset({"acme/app"}),
                         auto_merge_risks=frozenset({"low"}), require_human_approval=False)


def ev(**kw):
    base = dict(repository="acme/app", base_branch="main", head_sha=HEAD,
                change=rp.Change(paths=("docs/readme-fix.md",), additions=3), review_sha=HEAD, review_passed=True,
                reviewer_context="ctx-r", author_context="ctx-a", reviewer_model_family="fam-b",
                author_model_family="fam-a", check_shas={"ci": HEAD}, check_status={"ci": "pass"},
                required_checks=("ci",), changes_requested=False, human_approvals=0, provenance_signed=True)
    base.update(kw)
    return rp.Evidence(**base)


def test_docs_only_with_everything_green_is_the_only_allow_path():
    d = rp.evaluate(POLICY, ev())
    assert (d.outcome, d.risk, d.reasons) == ("allow", "low", ())


@pytest.mark.parametrize("paths,risk", [
    (("docs/a.md",), "low"),
    (("src/app/service.py",), "moderate"),
    (("herdr_engineering/control_plane/auth_guard.py",), "high"),          # sensitive one-line auth fix
    ((".woodpecker/ci.yml",), "high"), (("package-lock.json",), "high"), (("deploy/x.sh",), "high"),
    (("db/migrations/0001.sql",), "high"), (("a/scheduler.py",), "high"),
    (("keys/server.pem",), "critical"), (("config/.env.prod",), "critical"),
    (("docs/a.md", "src/auth.py"), "high"), ((), "high"),
])
def test_risk_is_by_path_not_size(paths, risk):
    assert rp.classify_risk(rp.Change(paths=paths, additions=1))[0] == risk
    assert rp.classify_risk(rp.Change(paths=paths, additions=5000))[0] == risk


def test_formatting_and_verified_fixtures_are_low_only_when_declared():
    assert rp.classify_risk(rp.Change(("a/b.py",), formatting_only=True))[0] == "low"
    assert rp.classify_risk(rp.Change(("a/b.py",)))[0] == "moderate"
    assert rp.classify_risk(rp.Change(("tests/fixtures/x.json",)))[0] == "high"   # fixtures are tests: unverified ones can manufacture green
    assert rp.classify_risk(rp.Change(("tests/fixtures/x.json",), fixtures_externally_verified=True))[0] == "low"


NEGATIVES = {
    "auto-merge disabled": (dict(auto_merge_enabled=False), {}),
    "repo not allowed": ({}, dict(repository="acme/other")),
    "branch not allowed": ({}, dict(base_branch="release")),
    "moderate risk": ({}, dict(change=rp.Change(("src/x.py",), 2))),
    "oversize": ({}, dict(change=rp.Change(("docs/a.md",), 900))),
    "conflicting incident": ({}, dict(conflicting_incident=True)),
    "science pending": ({}, dict(scientific_validation_pending=True)),
    "unsigned provenance": ({}, dict(provenance_signed=False)),
    "changes requested": ({}, dict(changes_requested=True)),
    "human approval absent": (dict(require_human_approval=True), {}),
    "review failed": ({}, dict(review_passed=False)),
    "stale review sha": ({}, dict(review_sha="b" * 40)),
    "same context reviewer": ({}, dict(reviewer_context="ctx-a")),
    "no reviewer context": ({}, dict(reviewer_context=None)),
    "same model family": ({}, dict(reviewer_model_family="fam-a")),
    "required check missing": ({}, dict(check_status={}, check_shas={})),
    "required check cancelled": ({}, dict(check_status={"ci": "cancelled"})),
    "required check pending": ({}, dict(check_status={"ci": "pending"})),
    "check on old sha": ({}, dict(check_shas={"ci": "c" * 40})),
    "no required checks configured": ({}, dict(required_checks=())),
}


@pytest.mark.parametrize("name", sorted(NEGATIVES))
def test_every_unmet_gate_blocks_the_merge(name):
    pol_kw, ev_kw = NEGATIVES[name]
    d = rp.evaluate(replace(POLICY, **pol_kw), ev(**ev_kw))
    assert d.outcome == "require_human" and d.reasons, name
    assert rp.combine(d, "merge") == "wait"


def test_deny_class_cannot_be_approved_by_a_human_flag_or_review():
    crit = ev(change=rp.Change(("keys/x.pem",), 1), human_approvals=5)
    assert rp.evaluate(replace(POLICY, require_human_approval=True), crit).outcome == "deny"
    assert rp.evaluate(replace(POLICY, kill_switch=True), ev()).outcome == "deny"
    assert rp.evaluate(POLICY, ev(attempt=4)).outcome == "deny"
    assert rp.evaluate(POLICY, ev(open_repair_prs=2)).outcome == "deny"


def test_combine_never_widens():
    allow = rp.PolicyDecision("allow", "low")
    assert rp.combine(allow, "merge") == "merge"
    assert rp.combine(allow, "wait") == "wait" and rp.combine(allow, "reject") == "reject"
    assert rp.combine(rp.PolicyDecision("require_human", "low", ("x",)), "merge") == "wait"
    assert rp.combine(rp.PolicyDecision("deny", "critical", ("x",)), "merge") == "reject"
    for gate in (MergeDecision("merge", ()), MergeDecision("wait", ("r",)), MergeDecision("reject", ("r",))):
        assert rp.combine(allow, gate.decision) == gate.decision  # allow defers to the gate, never overrides it


def test_evidence_has_no_model_authority_input():
    fields = set(rp.Evidence.__dataclass_fields__)
    assert not fields & {"confidence", "model_rationale", "verdict_text", "override", "approved_by_model"}


def test_incident_happy_path_and_pause_resume():
    inc = rp.Incident(rp.incident_key("p", "run-9", "v1"))
    for to in ("TRIAGED", "INVESTIGATING", "PROPOSED", "AWAITING_APPROVAL", "PROPOSED", "AUTHORIZED",
               "EXECUTING", "REVIEWING", "VERIFYING", "COMPLETED"):
        inc.transition(to, at="t", cause_event="e", policy_version="v1")
    assert inc.state == "COMPLETED" and len(inc.history) == 10


@pytest.mark.parametrize("start,to", [("DETECTED", "EXECUTING"), ("DETECTED", "COMPLETED"),
                                      ("TRIAGED", "AUTHORIZED"), ("DETECTED", "ROLLBACK_REQUESTED")])
def test_cannot_skip_authorisation_or_review(start, to):
    inc = rp.Incident("k")
    for s in ("TRIAGED",) if start == "TRIAGED" else ():
        inc.transition(s, at="t", cause_event="e", policy_version="v")
    with pytest.raises(rp.IllegalTransition):
        inc.transition(to, at="t", cause_event="e", policy_version="v")


def test_terminal_states_are_final_and_rollback_only_after_merge():
    inc = rp.Incident("k")
    inc.transition("FAILED", at="t", cause_event="e", policy_version="v")
    with pytest.raises(rp.IllegalTransition):
        inc.transition("TRIAGED", at="t", cause_event="e", policy_version="v")
    inc2 = rp.Incident("k2")
    for s in ("TRIAGED", "INVESTIGATING", "PROPOSED", "AUTHORIZED", "EXECUTING", "REVIEWING", "VERIFYING"):
        inc2.transition(s, at="t", cause_event="e", policy_version="v")
    inc2.transition("ROLLBACK_REQUESTED", at="t", cause_event="post-merge red", policy_version="v")


def test_duplicate_notifications_attach_and_key_is_per_policy_version():
    inc = rp.Incident(rp.incident_key("p", "run-9", "v1"))
    assert inc.attach("evt-1") and not inc.attach("evt-1")
    assert rp.incident_key("p", "run-9", "v1") != rp.incident_key("p", "run-9", "v2")


def test_circuit_breaker_needs_an_operator_to_close():
    cb = rp.CircuitBreaker(threshold=2)
    cb.record(False); cb.record(True); cb.record(False)
    assert not cb.open
    cb.record(False)
    assert cb.open
    cb.record(True)
    assert cb.open  # success does not self-heal
    cb.reset()
    assert not cb.open


# ---- independent-review findings (2026-10-28) ----------------------------------------------------------------
@pytest.mark.parametrize("path,risk", [
    ("docs/CODEOWNERS", "high"), ("src/README_loader.py", "moderate"), ("pkg/docs/hooks.py", "moderate"),
    ("x/CHANGELOG_x.py", "moderate"), ("skills/x/SKILL.md", "high"), ("packages/a/AGENTS.md", "high"),
    ("prompts/review-prompt.md", "high"), (".woodpecker.yml", "high"), ("Dockerfile", "high"), ("Makefile", "high"),
    ("scripts/run.sh", "high"), (".npmrc", "critical"), ("creds/id_rsa", "critical"), ("certs/a.p12", "critical"),
    ("a/KEY.PEM", "critical"), (".env", "critical"), ("cfg/.env.prod", "critical"), ("infra/main.tf", "high"),
    ("docs/guide/setup.md", "low"), ("README.md", "low"), ("./docs/a.md", "low"),
])
def test_path_classification_regressions(path, risk):
    assert rp.classify_risk(rp.Change((path,)))[0] == risk


def test_a_rename_into_docs_is_judged_by_its_old_path_when_the_caller_supplies_it():
    assert rp.classify_risk(rp.Change(("src/auth.py", "docs/auth.md")))[0] == "high"


def test_unknown_model_family_is_not_independence():
    for kw in ({"reviewer_model_family": None}, {"author_model_family": None}, {"reviewer_model_family": None, "author_model_family": None}):
        d = rp.evaluate(POLICY, ev(**kw))
        assert d.outcome == "require_human" and any("model families" in r for r in d.reasons)


def test_negative_sizes_and_bad_policy_are_refused():
    assert rp.evaluate(POLICY, ev(change=rp.Change(("docs/a.md",), additions=-5, deletions=1))).outcome == "require_human"
    with pytest.raises(ValueError):
        rp.RepairPolicy(auto_merge_risks=frozenset({"high"}))


def walk(inc, *states):
    for s in states:
        inc.transition(s, at="t", cause_event="e", policy_version="v")


def test_moving_between_pause_states_keeps_the_origin():
    inc = rp.Incident("k")
    walk(inc, "TRIAGED", "INVESTIGATING", "BLOCKED", "AWAITING_APPROVAL")
    assert inc.paused_from == "INVESTIGATING"
    walk(inc, "INVESTIGATING")                      # resumes where it was
    walk(inc, "PROPOSED")
    inc2 = rp.Incident("k2")
    walk(inc2, "TRIAGED", "INVESTIGATING", "AWAITING_APPROVAL", "BLOCKED", "AWAITING_APPROVAL")
    walk(inc2, "PROPOSED")                          # or its next step


def test_a_pause_cannot_skip_ahead():
    inc = rp.Incident("k")
    walk(inc, "TRIAGED", "BLOCKED")
    with pytest.raises(rp.IllegalTransition):
        walk(inc, "EXECUTING")
    walk(inc, "FAILED")


def test_rollback_after_completion_is_possible_but_then_final():
    inc = rp.Incident("k")
    walk(inc, "TRIAGED", "INVESTIGATING", "PROPOSED", "AUTHORIZED", "EXECUTING", "REVIEWING", "VERIFYING", "COMPLETED")
    with pytest.raises(rp.IllegalTransition):
        walk(inc, "TRIAGED")
    walk(inc, "ROLLBACK_REQUESTED")
    with pytest.raises(rp.IllegalTransition):
        walk(inc, "COMPLETED")


def test_audit_test_edits_spec_docs_and_odd_paths_are_high_risk():
    for path in ("tests/test_a.py", "pkg/tests/x.py", "src/foo.test.ts", "docs/specs/plan.md", "docs/../.github/workflows/ci.yml",
                 "docs\\..\\.github\\ci.yml"):
        assert rp.classify_risk(rp.Change((path,)))[0] == "high", path
    assert rp.classify_risk(rp.Change(("docs/guide.md",)))[0] == "low"


def test_audit_unknown_head_sha_requires_a_human():
    pol = rp.RepairPolicy(auto_merge_enabled=True, allowed_repositories=frozenset({"a/b"}), auto_merge_risks=frozenset({"low"}))
    ev = rp.Evidence(repository="a/b", base_branch="main", head_sha="", change=rp.Change(("docs/x.md",), 1), review_sha="",
                     review_passed=True, reviewer_context="r", author_context="a", reviewer_model_family="x", author_model_family="y",
                     check_shas={"ci": ""}, check_status={"ci": "pass"}, required_checks=("ci",), changes_requested=False,
                     human_approvals=1, provenance_signed=True)
    assert "head SHA is unknown or malformed" in rp.evaluate(pol, ev).reasons


def test_audit_blocked_incident_cannot_skip_to_authorized():
    inc = rp.Incident("k")
    for to in ("TRIAGED", "INVESTIGATING", "PROPOSED", "BLOCKED"):
        inc.transition(to, at="t", cause_event="e", policy_version="p")
    with pytest.raises(rp.IllegalTransition):
        inc.transition("AUTHORIZED", at="t", cause_event="e", policy_version="p")
    inc.transition("AWAITING_APPROVAL", at="t", cause_event="e", policy_version="p")
    inc.transition("AUTHORIZED", at="t", cause_event="e", policy_version="p")
