"""owner_slack provenance and one-shot approvals (real AssistantStore, no doubles)."""
import threading
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.assistant import AssistantStore, _grants_config
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction
from herdr_engineering.control_plane.assistant_hub.policy import decide, record_owner_decision
from herdr_engineering.control_plane.assistant_hub.workers import Worker

T0 = datetime(2026, 10, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=1)          # still inside the shadow period: the one-shot approval must skip it
T, U = "acme", "owner"


def _cfg(**over):
    return _grants_config({"shadow_started_at": T0.isoformat(), "min_sessions": 2, "min_tasks": 2,
                           "required_approvals": {"local_reversible": 2, "remote_reversible": 2, "outward": 2},
                           **over})


def _worker(wid="w1"):
    return Worker(worker_id=wid, tenant_id=T, user_id=U, name="n", harness="claude", device_id="d",
                  secret_digest="x", status="active", created_at=T0, expires_at=T0 + timedelta(days=365))


def _push(cmd="git push origin main"):
    return RawAction(tool="Bash", command=cmd, repo="acme/repo-a")


def _ask(store, cfg, raw=None, wid="w1", now=NOW):
    return decide(store, cfg, _worker(wid), raw or _push(), "s1", "t1", now)


def _approve(store, cfg, request_id, now=NOW, provenance="owner_slack"):
    return record_owner_decision(store, cfg, now, T, U, request_id, True, "slack:U123", provenance=provenance)


def test_unknown_provenance_raises():
    store, cfg = AssistantStore(), _cfg()
    d = _ask(store, cfg)
    with pytest.raises(ValueError):
        record_owner_decision(store, cfg, NOW, T, U, d.request_id, True, "slack:U1", provenance="worker")


def test_slack_approval_counts_like_api():
    store, cfg = AssistantStore(), _cfg()
    late = T0 + timedelta(days=20)                    # grants are only proposed once the shadow period is over
    ids = [decide(store, cfg, _worker(), _push(), f"s{i}", f"t{i}", late).request_id for i in range(2)]
    for rid in ids:
        record_owner_decision(store, cfg, late, T, U, rid, True, "slack:U1", provenance="owner_slack")
    assert len(store.list_action_grants(T, U)) == 1


def test_approve_then_reask_authorizes_once():
    store, cfg = AssistantStore(), _cfg()
    d = _ask(store, cfg)
    assert d.outcome == "pending"
    _approve(store, cfg, d.request_id)
    again = _ask(store, cfg, now=NOW + timedelta(minutes=1))
    assert (again.outcome, again.source, again.request_id) == ("authorized", "owner", d.request_id)
    row = store.get_action_request(d.request_id, tenant_id=T, user_id=U)
    assert row.consumed_at is not None
    third = _ask(store, cfg, now=NOW + timedelta(minutes=2))
    assert third.outcome == "pending" and third.request_id != d.request_id
    authorized = [e for e in store.audit if e["action"] == "assistant.action.authorized"]
    assert len(authorized) == 1 and "git push" not in str(authorized[0])


def test_different_worker_or_key_does_not_consume():
    store, cfg = AssistantStore(), _cfg()
    d = _ask(store, cfg)
    _approve(store, cfg, d.request_id)
    assert _ask(store, cfg, wid="w2").outcome == "pending"
    assert _ask(store, cfg, raw=_push("git push origin dev")).outcome == "pending"
    assert store.get_action_request(d.request_id, tenant_id=T, user_id=U).consumed_at is None
    assert _ask(store, cfg).outcome == "authorized"


def test_expired_approval_does_not_authorize():
    store, cfg = AssistantStore(), _cfg()
    d = _ask(store, cfg)
    _approve(store, cfg, d.request_id)
    late = _ask(store, cfg, now=NOW + timedelta(minutes=16))
    assert late.outcome == "pending"


def test_ttl_is_configurable():
    store, cfg = AssistantStore(), _cfg(approval_ttl_minutes=60)
    d = _ask(store, cfg)
    _approve(store, cfg, d.request_id)
    assert _ask(store, cfg, now=NOW + timedelta(minutes=45)).outcome == "authorized"


def test_veto_wins_over_approved_row():
    store, cfg = AssistantStore(), _cfg()
    raw = _push("git push --force origin main")
    d = _ask(store, cfg, raw=raw)
    assert d.outcome == "denied"
    # even a hand-planted approved row for the same key cannot authorize a vetoed command
    row = store.get_action_request(d.request_id, tenant_id=T, user_id=U)
    from dataclasses import replace
    store.put_action_request(replace(row, status="approved", decided_at=NOW, decided_by="slack:U1"))
    assert _ask(store, cfg, raw=raw, now=NOW + timedelta(minutes=1)).outcome == "denied"


def test_denial_is_not_consumable():
    store, cfg = AssistantStore(), _cfg()
    d = _ask(store, cfg)
    record_owner_decision(store, cfg, NOW, T, U, d.request_id, False, "slack:U1", provenance="owner_slack")
    assert _ask(store, cfg, now=NOW + timedelta(minutes=1)).outcome == "pending"


def test_concurrent_reasks_consume_once():
    store, cfg = AssistantStore(), _cfg()
    d = _ask(store, cfg)
    _approve(store, cfg, d.request_id)
    out, gate = [], threading.Barrier(8)

    def go():
        gate.wait()
        out.append(_ask(store, cfg, now=NOW + timedelta(minutes=1)).outcome)
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count("authorized") == 1


def test_consumed_at_round_trips_and_old_snapshot_restores():
    store, cfg = AssistantStore(), _cfg()
    d = _ask(store, cfg)
    _approve(store, cfg, d.request_id)
    _ask(store, cfg, now=NOW + timedelta(minutes=1))
    snap = store.snapshot()
    other = AssistantStore()
    other.restore(snap)
    assert other.get_action_request(d.request_id, tenant_id=T, user_id=U).consumed_at is not None
    for r in snap["action_requests"]:
        r.pop("consumed_at")
    old = AssistantStore()
    old.restore(snap)
    assert old.get_action_request(d.request_id, tenant_id=T, user_id=U).consumed_at is None


@pytest.mark.parametrize("bad", [0, 121, True, "15", 1.5])
def test_ttl_validation(bad):
    with pytest.raises(ValueError):
        _grants_config({"approval_ttl_minutes": bad})


def test_ttl_default():
    assert _grants_config({})["approval_ttl_minutes"] == 15
