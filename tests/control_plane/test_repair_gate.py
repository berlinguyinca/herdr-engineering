from types import SimpleNamespace as NS

import pytest

from herdr_engineering.control_plane import repair_gate as rg
from herdr_engineering.review_gate.github_app import ReviewFile, ReviewMaterial

SHA = "a" * 40
PR = NS(number=7, head_sha=SHA, base_ref="main", head_ref="herdr-ci-fix/x")
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
        def pr_review_summary(self, *a):
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
    facts = rg.make_facts_fn(app=GApp(), repo=Repo(), registry=Reg(), families_fn=lambda: fams)(ENR, PR)
    assert facts == {"human_approvals": 1, "changes_requested": False, "provenance_signed": True, "author_context": "s-1",
                     "author_model_family": "anthropic", "reviewer_context": "gate-reviewer", "reviewer_model_family": "openai"}
    full = dict(FULL, require_human_approval=True)
    assert gate(full, App(DOC), facts) == ("allow", ())
    # an unlisted model has no family: independence cannot be shown, so the policy waits
    assert gate(full, App(DOC), rg.make_facts_fn(app=GApp(), repo=Repo(), registry=Reg(), families_fn=lambda: {})(ENR, PR))[0] == "wait"
