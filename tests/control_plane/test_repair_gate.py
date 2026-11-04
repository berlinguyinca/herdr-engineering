from types import SimpleNamespace as NS

import pytest

from herdr_engineering.control_plane import repair_gate as rg
from herdr_engineering.review_gate.github_app import ReviewFile, ReviewMaterial

SHA = "a" * 40
PR = NS(number=7, head_sha=SHA, base_ref="main", head_ref="herdr-ci-fix/x", author_login="herdr-bot")
ENR = NS(repo="o/r")
CHECKS = [NS(name="ci:pull_request", status="pass", sha=SHA)]
FULL = {"enabled": True, "auto_merge_enabled": True, "allowed_repositories": ["o/r"], "auto_merge_risks": ["low"],
        "require_human_approval": False}
GOOD_FACTS = {"reviewer_context": "rc", "author_context": "ac", "reviewer_model_family": "a", "author_model_family": "b",
              "provenance_signed": True}


class App:
    def __init__(self, files, truncated=False):
        self.m = None if files is None else ReviewMaterial("t", "b", "main", SHA, tuple(files), truncated)

    def review_material(self, *a, **k):
        return self.m


def gate(raw, app, facts=None):
    return rg.make_repair_gate(app=app, policy_fn=lambda: rg.policy_from_section(raw), facts_fn=lambda e, p: facts or {})(ENR, PR, CHECKS)


DOC = [ReviewFile("docs/guide.md", "modified", "+hello\n-bye")]


def test_inert_unless_enabled():
    assert gate({}, App(DOC))[0] == "allow"
    assert gate({"enabled": False, "kill_switch": True}, App(DOC))[0] == "allow"
    assert gate(None, App(DOC))[0] == "allow"


def test_kill_switch_rejects_when_enabled():
    assert gate({"enabled": True, "kill_switch": True}, App(DOC))[0] == "reject"


def test_bare_enabled_deployment_never_auto_merges():
    verdict, why = gate(FULL, App(DOC))     # no facts: no reviewer independence, no provenance
    assert verdict == "wait" and any("independent" in w or "provenance" in w for w in why)


def test_all_facts_present_allows_low_risk_docs():
    assert gate(FULL, App(DOC), GOOD_FACTS) == ("allow", ())


@pytest.mark.parametrize("files,truncated", [
    ([ReviewFile("a.py", "renamed", "")], False),
    (DOC, True),
    (None, False)])
def test_unreadable_or_unclassifiable_change_waits(files, truncated):
    assert gate(FULL, App(files, truncated), GOOD_FACTS)[0] == "wait"


def test_protected_path_never_allowed_even_with_every_fact():
    verdict, _ = gate(FULL, App([ReviewFile(".woodpecker/ci.yaml", "modified", "+x")]), GOOD_FACTS)
    assert verdict in ("wait", "reject")


def test_same_model_family_blocks():
    assert gate(FULL, App(DOC), dict(GOOD_FACTS, reviewer_model_family="b"))[0] == "wait"


def test_bad_config_raises():
    with pytest.raises(ValueError):
        rg.policy_from_section({"enabled": True, "typo": 1})
    with pytest.raises(ValueError):
        rg.policy_from_section({"enabled": True, "allowed_repositories": "o/r"})
    with pytest.raises(ValueError):
        rg.policy_from_section({"enabled": True, "max_patch_lines": True})


def test_unreadable_central_config_after_enable_rejects(tmp_path, monkeypatch):
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.fleet_config import FleetConfigError

    class Fleet:
        broken = False

        def current(self):
            if self.broken:
                raise FleetConfigError("unreadable")
            return self

        def section(self, name):
            return {"project_intelligence": {"repair": {"enabled": True, "kill_switch": False}}}

    fleet = Fleet()
    server = ControlPlaneServer.__new__(ControlPlaneServer)
    gate = ControlPlaneServer._build_repair_gate(server, App(DOC), fleet)
    assert gate(ENR, PR, CHECKS)[0] in ("wait", "allow")       # enabled and evaluated
    fleet.broken = True
    assert gate(ENR, PR, CHECKS)[0] == "reject"
    fresh = ControlPlaneServer._build_repair_gate(server, App(DOC), fleet)
    assert fresh(ENR, PR, CHECKS)[0] == "allow"                 # never enabled in this process: unchanged behaviour


def test_facts_fn_builds_independence_from_systems_of_record():
    class GApp:
        def pr_review_summary(self, *a, **k):
            return 1, False

        def pr_commits_verified(self, *a):
            return True

    class Reg:
        def author_of(self, key, ref):
            return NS(kind="pi", model="claude-opus-5", session_id="s-1")

    class Repo:
        def stream(self, entity, eid):
            return [{"event_type": "verdict", "payload": {"verdict": "PASS", "sha": SHA, "reviewer_model": "gpt-6"}}]

    fams = {"claude": "anthropic", "gpt": "openai"}
    facts = rg.make_facts_fn(app=GApp(), repo=Repo(), registry=Reg(), families_fn=lambda: fams, board_lookup=lambda k: ("p", "o/r#7"))(ENR, PR)
    assert facts == {"human_approvals": 1, "changes_requested": False, "provenance_signed": True, "author_context": "s-1",
                     "author_model_family": "anthropic", "reviewer_context": "gate-reviewer", "reviewer_model_family": "openai"}
    full = dict(FULL, require_human_approval=True)
    assert gate(full, App(DOC), facts) == ("allow", ())
    # an unlisted model has no family: independence cannot be shown, so the policy waits
    assert gate(full, App(DOC), rg.make_facts_fn(app=GApp(), repo=Repo(), registry=Reg(), families_fn=lambda: {})(ENR, PR))[0] == "wait"


def test_untracked_incident_waits_and_counters_enforce_the_budgets():
    # an enabled gate whose facts say the PR is not bound to a tracked incident never auto-merges
    out = rg.make_repair_gate(app=App(DOC), policy_fn=lambda: rg.policy_from_section(FULL),
                              facts_fn=lambda e, p: dict(GOOD_FACTS, incident_unbound=True))(ENR, PR, CHECKS)
    assert out[0] == "wait" and "not tied to a tracked incident" in out[1][0]
    # attempt and cascade counters reach the policy (previously defaulted to 1 and 0)
    over = rg.make_repair_gate(app=App(DOC), policy_fn=lambda: rg.policy_from_section(dict(FULL, max_attempts=2, max_open_repair_prs=1)),
                               facts_fn=lambda e, p: GOOD_FACTS, attempt_fn=lambda e, p: 3)
    assert over(ENR, PR, CHECKS)[0] == "reject"
    cascade = rg.make_repair_gate(app=App(DOC), policy_fn=lambda: rg.policy_from_section(dict(FULL, max_open_repair_prs=1)),
                                  facts_fn=lambda e, p: GOOD_FACTS, open_repair_prs_fn=lambda e, p=None: 1)
    assert cascade(ENR, PR, CHECKS)[0] == "reject"


def test_make_counters_counts_runs_and_other_open_repair_prs():
    class A:
        def open_prs(self, repo):
            return [NS(number=7, head_ref="herdr-ci-fix/a"), NS(number=8, head_ref="herdr-ci-fix/b"), NS(number=9, head_ref="feat/x")]

    class R:
        def entity_ids(self, e):
            return ["f1", "f2", "f3"]

        def stream(self, e, i):
            run = {"f1": 7, "f2": 7, "f3": 9}[i]
            return [{"payload": {"repository": "o/r", "run": run}}]

    attempt, open_prs = rg.make_counters(app=A(), repo=R(), board_lookup=lambda k: ("p", "o/r#7") if k == "o/r#7" else None)
    assert attempt(ENR, NS(number=7)) == 2          # two repair requests for run 7
    assert attempt(ENR, NS(number=99)) == 1          # unbound PR: the gate's incident_unbound check handles it
    assert open_prs(ENR, NS(number=7)) == 1          # PR 8 only; itself and non-repair PRs excluded


def test_unknown_pr_author_means_no_approval_counts():
    class GApp:
        def pr_review_summary(self, *a, **k):
            return 2, False

        def pr_commits_verified(self, *a):
            return True

    class Reg:
        def author_of(self, key, ref):
            return NS(kind="unknown")

    class Repo:
        def stream(self, entity, eid):
            return []
    pr = NS(number=7, head_sha=SHA, base_ref="main", head_ref="herdr-ci-fix/x")      # no author_login
    facts = rg.make_facts_fn(app=GApp(), repo=Repo(), registry=Reg(), families_fn=dict)(ENR, pr)
    assert facts["human_approvals"] == 0
