"""Code-review fixes for Phase 3: stale-judge replacement proposals, DNS under the deadline, a fresh clock after the
consult, the endpoint in the judge version, negative-wins label groups, regret only for active approvals, the
judge_status rate limit and a shared TLS context.  No mocks: real stores, real listeners, real functions."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering.control_plane.assistant import AssistantStore, _grants_config
from herdr_engineering.control_plane.assistant_hub import jev
from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.calibration import build_report
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.judge_version import judge_version
from herdr_engineering.control_plane.assistant_hub.policy import (
    activate_grant,
    decide,
    record_owner_decision,
    revoke_action_grant,
)
from herdr_engineering.control_plane.assistant_hub.shadow import JudgeRecord
from herdr_engineering.control_plane.assistant_hub.workers import Worker
from tests.control_plane.judge_support import (
    SAFE,
    WireListener,
    judge_cfg,
    labeled_records,
    make_runtime,
    success_body,
    write_key,
)
from tests.control_plane.test_assistant_hub_mcp import call, enrol, make_hub

T0 = datetime(2026, 10, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=20)
T, U, W = "acme", "owner", "w1"
PUSH = RawAction(tool="Bash", command="git push origin main", repo="acme/repo-a")


def _cfg() -> dict:
    return _grants_config({"shadow_started_at": T0.isoformat(), "min_sessions": 2, "min_tasks": 2,
                           "required_approvals": {"local_reversible": 2, "remote_reversible": 2, "outward": 2}})


def _worker() -> Worker:
    return Worker(worker_id=W, tenant_id=T, user_id=U, name="n", harness="claude", device_id="d", secret_digest="x",
                  status="active", created_at=T0, expires_at=T0 + timedelta(days=365))


def _grant(state: GrantState, *, judge_ver: str = "", at: datetime = NOW, ttl: timedelta = timedelta(days=30),
           policy: int = 1, gid: str = "ag1") -> ActionGrant:
    key = derive(PUSH).key
    assert key is not None
    active = state is GrantState.ACTIVE
    return ActionGrant(
        grant_id=gid, tenant_id=T, user_id=U, worker_id=W, resource_scope=key.resource_scope, key_id=key.key_id(),
        verb=key.verb.value, key_canonical=key.canonical(), state=state, created_at=at, proposed_at=at,
        activated_at=at if active else None, confirmed_by="owner" if active else "", expires_at=at + ttl,
        policy_version=policy, judge_version=judge_ver)


# ------------------------------------------------ (1) a judge-stale ACTIVE grant is replaced by a new proposal

def test_judge_stale_active_grant_is_replaced_and_can_be_activated(tmp_path: Path) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = make_runtime(store, judge_cfg("http://127.0.0.1:9", write_key(tmp_path), backend_confirmed=True), lambda: NOW)
    try:
        v1 = judge_version(rt.cfg_getter())
        store.put_action_grant(_grant(GrantState.ACTIVE, judge_ver=v1))
        jcfg = rt.cfg_getter()
        jcfg["model"] = "another-model"                       # the judge version moves: the old grant is now SHADOW
        v2 = judge_version(jcfg)
        assert v1 != v2
        later = NOW + timedelta(hours=1)
        asked = [decide(store, cfg, _worker(), PUSH, f"s{i}", f"t{i}", later) for i in range(2)]
        for d in asked:
            assert d.request_id is not None
            record_owner_decision(store, cfg, later, T, U, d.request_id, True, "owner", judge_version=v2)
        proposed = [g for g in store.list_action_grants(T, U) if g.state is GrantState.PROPOSED]
        assert len(proposed) == 1
        for r in labeled_records(jcfg, derive(PUSH).key.risk_class.value, tenant=T, user=U):
            store.add_judge_record(r)
        active = activate_grant(store, cfg, later, T, U, proposed[0].grant_id, "owner", rt)
        assert active.state is GrantState.ACTIVE and active.judge_version == v2
    finally:
        rt.recorder.stop()


# ------------------------------------------------ (2) name resolution runs under the deadline

def test_resolve_is_bounded_by_the_deadline() -> None:
    def slow(host, port, **kw):
        time.sleep(3)
        return []
    start = time.monotonic()
    with pytest.raises(jev.JudgeUnavailable) as exc:
        jev._resolve("example.invalid", 443, jev._Deadline(0.2), resolver=slow)
    assert exc.value.code == "timeout" and time.monotonic() - start < 1.5


def test_resolve_returns_the_resolver_answer_and_reports_failures_as_connect() -> None:
    fixed = [(2, 1, 6, "", ("127.0.0.1", 80))]
    assert jev._resolve("h", 80, jev._Deadline(2), resolver=lambda h, p, **kw: fixed) == fixed

    def boom(host, port, **kw):
        raise OSError("nope")
    with pytest.raises(OSError):
        jev._resolve("h", 80, jev._Deadline(2), resolver=boom)


# ------------------------------------------------ (3) the grant is re-checked against a fresh clock

def test_grant_that_expires_during_the_consult_is_not_authorized(tmp_path: Path) -> None:
    li = WireListener(success_body(SAFE), delay=1.0)
    store, cfg = AssistantStore(), _cfg()
    clock = lambda: datetime.now(UTC)  # noqa: E731 - a real clock
    rt = make_runtime(store, judge_cfg(f"http://127.0.0.1:{li.port}", write_key(tmp_path)), clock)
    try:
        cfg = _grants_config({"shadow_started_at": (clock() - timedelta(days=60)).isoformat(), "min_sessions": 2})
        start = clock()
        store.put_action_grant(_grant(GrantState.ACTIVE, judge_ver=judge_version(rt.cfg_getter()), at=start,
                                      ttl=timedelta(seconds=0.4)))
        d = decide(store, cfg, _worker(), PUSH, "s1", "t1", start, judge=rt)
        assert li.hits >= 1
        assert d.outcome == "pending" and d.grant_id is None
        assert not any(e["action"] == "assistant.action.authorized" for e in store._audit)
    finally:
        rt.recorder.stop()
        li.close()


# ------------------------------------------------ (4) the endpoint is part of the judge version

def test_endpoint_changes_the_judge_version_but_spelling_does_not() -> None:
    from herdr_engineering.control_plane.assistant import assistant_config

    def v(endpoint: str) -> str:
        cfg = assistant_config({"personal_assistant": {"judge": {"enabled": True, "endpoint": endpoint,
                                                                 "api_key_file": "/etc/k"}}})["judge"]
        return judge_version(cfg)
    base = v("https://jev.example.org/v1")
    assert v("https://other.example.org/v1") != base
    assert v("https://jev.example.org/v2") != base
    assert v("https://jev.example.org:8443/v1") != base
    assert v("HTTPS://Jev.Example.org/v1/") == base
    assert v("https://jev.example.org:443/v1") == base


# ------------------------------------------------ (5) a later denial is never hidden by an earlier approval

def _rec(n: int, key: str, *, decision: str = "approved", escalate: bool = False, regret: bool = False) -> JudgeRecord:
    cfg = judge_cfg("http://127.0.0.1:9", Path("/x"))
    return JudgeRecord(record_id=f"r{n}", request_id=f"rq{n}", tenant_id=T, user_id=U, worker_id=W, key_id=key, risk_class="remote_reversible",
                       judge_version=judge_version(cfg), status="ok", escalate=escalate, owner_decision=decision,  # type: ignore[arg-type]
                       regret=regret, created_at=T0 + timedelta(minutes=n))


def _report(records: list[JudgeRecord]):
    cfg = judge_cfg("http://127.0.0.1:9", Path("/x"))
    return build_report(records, cfg, NOW, T, U).classes["remote_reversible"]


@pytest.mark.parametrize("records", [
    [_rec(1, "k"), _rec(2, "k", decision="denied")],
    [_rec(1, "k", decision="denied"), _rec(2, "k")],
    [_rec(1, "k"), _rec(2, "k", regret=True)],
])
def test_negative_wins_inside_a_worker_key_group(records: list[JudgeRecord]) -> None:
    c = _report(records)
    assert (c.labeled, c.false_negatives, c.negatives) == (1, 1, 1)


def test_group_with_an_escalated_denial_is_negative_but_not_a_false_negative() -> None:
    c = _report([_rec(1, "k"), _rec(2, "k", decision="denied", escalate=True)])
    assert (c.labeled, c.false_negatives, c.negatives) == (1, 0, 1)


def test_clean_group_stays_one_label() -> None:
    c = _report([_rec(1, "k"), _rec(2, "k")])
    assert (c.labeled, c.false_negatives, c.negatives) == (1, 0, 0)


# ------------------------------------------------ (6) regret only for ACTIVE grants and approved records

def _regret_world(state: GrantState, **kw) -> tuple[AssistantStore, ActionGrant, list[JudgeRecord]]:
    store = AssistantStore()
    g = _grant(state, **kw)
    store.put_action_grant(g)
    base = derive(PUSH).key.key_id()                          # the same key id the grant carries
    recs = [replace(_rec(1, base), record_id="a"), replace(_rec(2, base, decision="denied"), record_id="d"),
            replace(_rec(3, base, decision=""), record_id="u")]
    for r in recs:
        store.add_judge_record(r)
    return store, g, recs


def _regret(store: AssistantStore) -> dict[str, bool]:
    return {r.record_id: r.regret for r in store.list_judge_records(T, U)}


def test_revoking_an_active_grant_flags_only_approved_records() -> None:
    store, g, _ = _regret_world(GrantState.ACTIVE)
    revoke_action_grant(store, NOW + timedelta(days=1), T, U, g.grant_id, "owner", policy_version=1)
    assert _regret(store) == {"a": True, "d": False, "u": False}


@pytest.mark.parametrize("state,kw,policy", [
    (GrantState.PROPOSED, {}, 1),
    (GrantState.ACTIVE, {"policy": 1}, 2),                    # policy bump: effective SHADOW
    (GrantState.ACTIVE, {"ttl": timedelta(hours=1)}, 1),      # expired by the time of the revoke
])
def test_revoking_anything_else_never_flags_regret(state: GrantState, kw: dict, policy: int) -> None:
    store, g, _ = _regret_world(state, **kw)
    revoke_action_grant(store, NOW + timedelta(days=1), T, U, g.grant_id, "owner", policy_version=policy)
    assert _regret(store) == {"a": False, "d": False, "u": False}


# ------------------------------------------------ (8) judge_status takes a rate-limit slot

def test_judge_status_is_rate_limited_like_the_other_tools() -> None:
    hub = make_hub()
    wk = enrol(hub, scopes=("ask", "report", "read"))
    for _ in range(30):
        assert call(hub, wk, "judge_status")["isError"] is False
    res = call(hub, wk, "judge_status")
    assert res["isError"] is True and json.loads(res["content"][0]["text"]) == {"error": "rate limited"}


# ------------------------------------------------ (10) one TLS context

def test_tls_context_is_built_once() -> None:
    assert jev._tls_context() is jev._tls_context()
