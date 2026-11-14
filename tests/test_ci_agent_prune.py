"""Safety contract for the Woodpecker agent prune verdicts."""
import importlib.machinery
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("wpci_agent_prune", str(ROOT / "deploy/ci-agent-hygiene/wpci-agent-prune"))
_spec = importlib.util.spec_from_loader("wpci_agent_prune", _loader)
prune = importlib.util.module_from_spec(_spec)
_loader.exec_module(prune)

NOW = 1_000_000_000
HOUR, DAY = 3600, 86400


def agent(**kw):
    base = {"id": 1, "owner_id": -1, "org_id": -1, "created": NOW - 10 * DAY,
            "last_contact": 0, "last_work": 0}
    return {**base, **kw}


def verdict(a, busy=frozenset()):
    return prune.verdict(a, NOW, busy, HOUR, DAY)


def test_old_never_contacted_is_removed():
    assert verdict(agent()) == "never-contacted"


def test_fresh_never_contacted_is_kept():
    assert verdict(agent(created=NOW - 600)) is None


def test_stale_agent_is_removed():
    assert verdict(agent(last_contact=NOW - 2 * DAY, last_work=NOW - 3 * DAY)) == "stale"


def test_recent_contact_or_work_is_kept():
    assert verdict(agent(last_contact=NOW - 60)) is None
    assert verdict(agent(last_contact=NOW - 2 * DAY, last_work=NOW - HOUR)) is None


def test_agent_with_queued_workflow_is_never_removed():
    assert verdict(agent(), busy={1}) is None
    assert verdict(agent(last_contact=NOW - 9 * DAY), busy={1}) is None


def test_user_or_org_agents_are_kept():
    assert verdict(agent(owner_id=7)) is None
    assert verdict(agent(org_id=3)) is None
