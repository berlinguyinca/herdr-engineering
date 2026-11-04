"""The judge on the decision path and the real activation gate (Phase 3, Task 6).

No fake judge anywhere: the always-run tier uses a closed port (real connect failure) and the pure ``_apply_verdict``
helper; the wire tier uses a raw TCP listener that serves the DOCUMENTED System One success shape so the real
``JevClient`` + ``consult`` + ``decide`` path runs end to end.  ACTIVE grants for decide-path tests come from the
tests-only ``force_active_grant_for_tests``; the gate tests populate a store with real ``JudgeRecord`` rows.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    AssistantService,
    AssistantStore,
    _grants_config,
    assistant_config,
)
from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.jev import JudgeResult, NoulAnswer, ScoreAnswer
from herdr_engineering.control_plane.assistant_hub.judge import JudgeRuntime, Verdict, evaluate
from herdr_engineering.control_plane.assistant_hub.judge_version import judge_version
from herdr_engineering.control_plane.assistant_hub.policy import (
    ActivationRefused,
    _apply_verdict,
    activate_grant,
    decide,
    record_owner_decision,
    revoke_action_grant,
)
from herdr_engineering.control_plane.assistant_hub.workers import Worker
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.judge_support import (
    RISKY,
    SAFE,
    WireListener,
    closed_port,
    force_active_grant_for_tests,
    judge_cfg,
    labeled_records,
    make_runtime,
    success_body,
    write_key,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=20)
T, U, W = "acme", "owner", "w1"
PUSH_CMD = "git push origin main"


def _cfg(**over: Any) -> dict[str, Any]:
    return _grants_config({"shadow_started_at": T0.isoformat(), "min_sessions": 2, "min_tasks": 2,
                           "required_approvals": {"local_reversible": 2, "remote_reversible": 2, "outward": 2}, **over})


def _worker(worker_id: str = W, tenant: str = T, user: str = U) -> Worker:
    return Worker(worker_id=worker_id, tenant_id=tenant, user_id=user, name="n", harness="claude", device_id="d",
                  secret_digest="x", status="active", created_at=T0, expires_at=T0 + timedelta(days=365))


def _push(cmd: str = PUSH_CMD) -> RawAction:
    return RawAction(tool="Bash", command=cmd, repo="acme/repo-a")


def _proposed(store: AssistantStore, cfg: dict[str, Any], raw: RawAction, worker_id: str = W, tenant: str = T,
              user: str = U) -> ActionGrant:
    key = derive(raw).key
    assert key is not None
    g = ActionGrant(
        grant_id=f"ag_{worker_id}_{key.key_id()[:8]}", tenant_id=tenant, user_id=user, worker_id=worker_id,
        resource_scope=key.resource_scope, key_id=key.key_id(), verb=key.verb.value, key_canonical=key.canonical(),
        state=GrantState.PROPOSED, created_at=NOW, proposed_at=NOW, expires_at=NOW + timedelta(days=30),
        policy_version=cfg["policy_version"])
    store.put_action_grant(g)
    return g


def _wait(cond: Callable[[], bool], seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture()
def key_file(tmp_path: Path) -> Path:
    return write_key(tmp_path)


@pytest.fixture()
def listener_safe():
    li = WireListener(success_body(SAFE))
    yield li
    li.close()


@pytest.fixture()
def listener_risky():
    li = WireListener(success_body(RISKY))
    yield li
    li.close()


def _runtime(store: AssistantStore, li: WireListener | None, key_file: Path, **kw: Any) -> JudgeRuntime:
    port = li.port if li is not None else closed_port()
    return make_runtime(store, judge_cfg(f"http://127.0.0.1:{port}", key_file, **kw), lambda: NOW)


def _stop(rt: JudgeRuntime) -> None:
    rt.recorder.stop()


def _active(store: AssistantStore, cfg: dict[str, Any], rt: JudgeRuntime | None, raw: RawAction | None = None) -> ActionGrant:
    g = _proposed(store, cfg, raw or _push())
    return force_active_grant_for_tests(store, cfg, g.grant_id, NOW, rt.cfg_getter() if rt else None)


# ----------------------------------------------------------------- _apply_verdict (pure)


def _verdict(**over: Any) -> Verdict:
    base: dict[str, Any] = dict(escalate=False, reasons=(), destructive=0.1, irreversible=0.1, severity=0.5, covers=0.9,
                                judge_version="v", model="m")
    base.update(over)
    return Verdict(**base)


def test_apply_verdict_non_escalating_authorizes() -> None:
    assert _apply_verdict(_verdict()) == ("authorized", "grant")


def test_apply_verdict_escalating_is_pending_by_judge_with_static_reason() -> None:
    outcome, source = _apply_verdict(_verdict(escalate=True, reasons=("judge: whatever the model said",)))
    assert (outcome, source) == ("pending", "judge")


def test_apply_verdict_follows_real_evaluate() -> None:
    res = JudgeResult("m", {"destructive": NoulAnswer(0.9), "irreversible_or_external": NoulAnswer(0.0),
                            "owner_approval_covers": NoulAnswer(0.9),
                            "severity": ScoreAnswer(0.0, 0.9, {"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0})}, 1, 1, "r")
    v = evaluate(res, {"destructive": 0.3, "irreversible": 0.3, "severity_score": 2.0}, "ver")
    assert _apply_verdict(v)[0] == "pending"


# ----------------------------------------------------------------- fail closed


def test_no_judge_runtime_never_authorizes_and_never_records() -> None:
    store, cfg = AssistantStore(), _cfg()
    g = _active(store, cfg, None)
    d = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW)
    assert (d.outcome, d.source, d.grant_id) == ("pending", "judge_unavailable", None)
    assert d.request_id is not None
    assert g.effective_state(NOW, cfg["policy_version"]) is GrantState.ACTIVE
    assert store.list_judge_records(T, U) == []


def test_unreachable_judge_is_pending_judge_unavailable(key_file: Path) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, None, key_file)
    _active(store, cfg, rt)
    d = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)
    _stop(rt)
    assert (d.outcome, d.source) == ("pending", "judge_unavailable")
    assert not any(e["action"] == "assistant.action.authorized" for e in store._audit)


def test_disabled_judge_config_is_pending_judge_unavailable(key_file: Path) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, None, key_file, enabled=False)
    _active(store, cfg, rt)
    d = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)
    assert (d.outcome, d.source) == ("pending", "judge_unavailable")
    assert store.list_judge_records(T, U) == [] and not rt.recorder.is_running()   # a disabled judge records nothing


# ----------------------------------------------------------------- wire tier (documented-shape listener)


def test_wire_non_escalating_verdict_authorizes_with_judge_audit(key_file: Path, listener_safe: WireListener) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, listener_safe, key_file)
    g = _active(store, cfg, rt)
    d = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)
    assert (d.outcome, d.source, d.grant_id) == ("authorized", "grant", g.grant_id)
    assert listener_safe.hits == 1
    audit = [e for e in store._audit if e["action"] == "assistant.action.authorized"]
    assert len(audit) == 1
    detail = audit[0]["detail"]
    assert f"judge_version={judge_version(rt.cfg_getter())}" in detail
    for name in ("destructive", "irreversible", "severity", "covers"):
        assert re.search(rf"\b{name}=\d+\.\d{{2}}%", detail), detail
    assert "destructive=5.00%" in detail and "covers=90.00%" in detail
    blob = json.dumps(store._audit, default=str)
    for secret in ("sk-test", "127.0.0.1", str(listener_safe.port), "origin", "push origin"):
        assert secret not in blob
    assert rt.recorder.submitted == []                       # authorized: no pending row, nothing recorded
    request = listener_safe.seen[0].decode()
    assert "Authorization: Bearer" in request and "origin main" not in request   # command text never leaves
    _stop(rt)


def test_wire_escalating_verdict_is_pending_judge_and_recorded_once(key_file: Path, listener_risky: WireListener) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, listener_risky, key_file)
    _active(store, cfg, rt)
    first = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)
    assert (first.outcome, first.source) == ("pending", "judge")
    assert first.request_id is not None
    assert _wait(lambda: len(store.list_judge_records(T, U)) == 1)
    again = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)      # dedupe: same row
    assert again.request_id == first.request_id
    assert len(rt.recorder.submitted) == 1                  # the repeat did not resubmit
    audit = [e for e in store._audit if e["action"] == "assistant.action.requested"]
    assert len(audit) == 1 and "source=judge" in audit[0]["detail"]
    assert re.search(r"destructive=90\.00%", audit[0]["detail"]) and "judge_version=" in audit[0]["detail"]
    assert not any(e["action"] == "assistant.action.authorized" for e in store._audit)
    _stop(rt)


def test_owner_pending_rows_submit_once_and_vetoes_never_reach_judge(key_file: Path, listener_safe: WireListener) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, listener_safe, key_file)
    a = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)              # no grant: owner decides
    assert (a.outcome, a.source) == ("pending", "owner")
    decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)
    assert len(rt.recorder.submitted) == 1
    v = decide(store, cfg, _worker(), _push("git push --force origin main"), "s1", "t1", NOW, judge=rt)
    assert v.outcome == "denied" and v.source == "veto"
    assert len(rt.recorder.submitted) == 1                                             # vetoed: never submitted
    u = decide(store, cfg, _worker(), RawAction(tool="Bash", command="frobnicate --all"), "s1", "t1", NOW, judge=rt)
    assert u.source == "unknown" and len(rt.recorder.submitted) == 1                   # unclassifiable: nothing to judge
    assert _wait(lambda: len(store.list_judge_records(T, U)) == 1)
    _stop(rt)


# ----------------------------------------------------------------- labels and regret


def test_owner_decision_labels_record_and_revoke_marks_regret(key_file: Path, listener_safe: WireListener) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, listener_safe, key_file)
    d = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)
    assert d.request_id is not None
    assert _wait(lambda: len(store.list_judge_records(T, U)) == 1)
    record_owner_decision(store, cfg, NOW, T, U, d.request_id, True, "owner")
    rec = store.list_judge_records(T, U)[0]
    assert rec.owner_decision == "approved" and not rec.regret
    g = _proposed(store, cfg, _push())
    revoke_action_grant(store, NOW, T, U, g.grant_id, "owner")
    assert store.list_judge_records(T, U)[0].regret is True
    _stop(rt)


def test_owner_decision_without_a_record_still_works() -> None:
    store, cfg = AssistantStore(), _cfg()
    d = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW)
    assert d.request_id is not None
    assert record_owner_decision(store, cfg, NOW, T, U, d.request_id, False, "owner").status == "denied"


# ----------------------------------------------------------------- the real gate


_labeled_records = labeled_records


def _gate_world(key_file: Path, **records: Any) -> tuple[AssistantStore, dict[str, Any], JudgeRuntime, ActionGrant]:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, None, key_file)
    g = _proposed(store, cfg, _push())
    risk = derive(_push()).key.risk_class.value                     # type: ignore[union-attr]
    for r in _labeled_records(rt.cfg_getter(), risk, **records):
        store.add_judge_record(r)
    return store, cfg, rt, g


def _activate(store: AssistantStore, cfg: dict[str, Any], g: ActionGrant, rt: JudgeRuntime | None) -> ActionGrant:
    return activate_grant(store, cfg, NOW, T, U, g.grant_id, "owner", rt)


def test_activation_succeeds_only_with_populated_calibrated_store(key_file: Path) -> None:
    store, cfg, rt, g = _gate_world(key_file)
    active = _activate(store, cfg, g, rt)
    assert active.state is GrantState.ACTIVE
    assert active.judge_version == judge_version(rt.cfg_getter())
    assert store.get_action_grant(g.grant_id, tenant_id=T, user_id=U).judge_version == active.judge_version   # type: ignore[union-attr]
    _stop(rt)


@pytest.mark.parametrize("name,records,cfg_over,code", [
    ("no_runtime", {}, None, "judge_disabled"),
    ("disabled", {}, {"enabled": False}, "judge_disabled"),
    ("unconfirmed", {}, {"backend_confirmed": False}, "backend_unconfirmed"),
    ("few_labels", {"n_safe": 50, "n_flagged": 5}, {}, "insufficient_labels"),
    ("one_key", {"keys": 1}, {}, "insufficient_diversity"),
    ("no_negatives", {"n_flagged": 0}, {}, "no_discrimination"),
    ("fn_too_high", {"n_fn": 15}, {}, "fn_bound_exceeded"),
])
def test_activation_gate_refusal_codes(key_file: Path, name: str, records: dict, cfg_over: dict | None, code: str) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = None if cfg_over is None else _runtime(store, None, key_file, **{k: v for k, v in cfg_over.items() if k != "enabled"})
    if cfg_over is not None and cfg_over.get("enabled") is False:
        rt = _runtime(store, None, key_file, enabled=False)
    g = _proposed(store, cfg, _push())
    if rt is not None:
        risk = derive(_push()).key.risk_class.value                 # type: ignore[union-attr]
        for r in _labeled_records(rt.cfg_getter(), risk, **records):
            store.add_judge_record(r)
    with pytest.raises(ActivationRefused) as err:
        _activate(store, cfg, g, rt)
    assert err.value.code == code
    assert store.get_action_grant(g.grant_id, tenant_id=T, user_id=U).state is GrantState.PROPOSED   # type: ignore[union-attr]
    assert any(e["action"] == "assistant.grant.activate_refused" and code in e["detail"] for e in store._audit)
    if rt is not None:
        _stop(rt)


def test_labels_of_another_judge_version_or_risk_class_or_user_do_not_count(key_file: Path) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, None, key_file)
    g = _proposed(store, cfg, _push())
    for r in _labeled_records(rt.cfg_getter(), "local_reversible"):
        store.add_judge_record(r)
    for r in _labeled_records(rt.cfg_getter(), "remote_reversible", version="oldversion0000000"):
        store.add_judge_record(replace(r, record_id="x" + r.record_id, request_id="x" + r.request_id))
    with pytest.raises(ActivationRefused) as err:
        _activate(store, cfg, g, rt)
    assert err.value.code == "insufficient_labels"


def test_gate_precedence_before_judge_codes(key_file: Path) -> None:
    store, cfg = AssistantStore(), _cfg()
    with pytest.raises(ActivationRefused) as e1:
        _activate(store, cfg, ActionGrant(grant_id="nope"), None)
    assert e1.value.code == "not_found"
    g = _proposed(store, cfg, _push())
    store.put_action_grant(replace(g, state=GrantState.SHADOW))
    with pytest.raises(ActivationRefused) as e2:
        _activate(store, cfg, g, None)
    assert e2.value.code == "not_proposed"
    store.put_action_grant(g)
    young = _cfg(shadow_started_at=(NOW - timedelta(days=1)).isoformat())
    with pytest.raises(ActivationRefused) as e3:
        _activate(store, young, g, None)
    assert e3.value.code == "still_in_shadow"
    with pytest.raises(ActivationRefused) as e4:
        activate_grant(store, cfg, NOW, T, U, g.grant_id, "  ", None)
    assert e4.value.code == "confirmation_missing"
    with pytest.raises(ActivationRefused) as e5:
        _activate(store, cfg, g, None)
    assert e5.value.code == "judge_disabled"


# ----------------------------------------------------------------- judge version change resets trust


def test_judge_version_change_after_activation_makes_grant_shadow_and_next_decide_pending(
        key_file: Path, listener_safe: WireListener) -> None:
    store, cfg, rt, g = _gate_world(key_file)
    # rebuild the runtime against the live listener so the pre-change decide is authorized end to end
    live = make_runtime(store, judge_cfg(f"http://127.0.0.1:{listener_safe.port}", key_file), lambda: NOW)
    active = _activate(store, cfg, g, live)
    assert decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=live).outcome == "authorized"
    changed = make_runtime(store, judge_cfg(f"http://127.0.0.1:{listener_safe.port}", key_file, model="other-model"),
                           lambda: NOW)
    new_version = judge_version(changed.cfg_getter())
    assert new_version != active.judge_version
    assert active.effective_state(NOW, cfg["policy_version"], new_version) is GrantState.SHADOW
    hits = listener_safe.hits
    d = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=changed)
    assert (d.outcome, d.source) == ("pending", "owner")
    assert listener_safe.hits == hits                       # no active grant any more, so no judge consult
    _stop(rt)
    _stop(live)
    _stop(changed)


# ----------------------------------------------------------------- owner route, real server


def _pid(key: str) -> str:
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:12]


OWNER_KEY = "t6-route-owner-key"


def _call(base: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-API-Key": OWNER_KEY})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


class _Route:
    def __init__(self, acknowledged_sink: Any, key_file: Path, with_runtime: bool, **judge_over: Any) -> None:
        long_ago = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        self.user = _pid(OWNER_KEY)
        section = {"enabled": True, "tenancy": {"principals": {self.user: T}}, "grants": {"shadow_started_at": long_ago}}
        self.svc = AssistantService(config=assistant_config({"personal_assistant": section}))
        from datetime import timedelta as td

        from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
        registry = WorkerRegistry(self.svc.store, enrollment_ttl=td(minutes=10), worker_ttl=td(days=30))
        self.rt = (make_runtime(self.svc.store, judge_cfg(f"http://127.0.0.1:{closed_port()}", key_file, **judge_over),
                                self.svc.now) if with_runtime else None)
        self.svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=self.svc,
                                      api_keys={OWNER_KEY: "operator"}, assistant_workers=registry,
                                      assistant_judge=self.rt)
        threading.Thread(target=self.svr.serve, daemon=True).start()
        self.svr.wait_until_ready()
        self.base = f"http://127.0.0.1:{self.svr.port}"
        self.cfg = self.svc.config["grants"]

    def propose(self) -> ActionGrant:
        raw = _push()
        key = derive(raw).key
        assert key is not None
        g = ActionGrant(grant_id="ag_route", tenant_id=T, user_id=self.user, worker_id=W, resource_scope=key.resource_scope,
                        key_id=key.key_id(), verb=key.verb.value, key_canonical=key.canonical(),
                        state=GrantState.PROPOSED, created_at=self.svc.now(), proposed_at=self.svc.now(),
                        expires_at=self.svc.now() + timedelta(days=30), policy_version=self.cfg["policy_version"])
        self.svc.store.put_action_grant(g)
        return g

    def fill(self, **records: Any) -> None:
        assert self.rt is not None
        risk = derive(_push()).key.risk_class.value                 # type: ignore[union-attr]
        for r in _labeled_records(self.rt.cfg_getter(), risk, tenant=T, user=self.user, **records):
            self.svc.store.add_judge_record(r)

    def close(self) -> None:
        self.svr.shutdown()
        if self.rt is not None:
            self.rt.recorder.stop()


@pytest.mark.parametrize("with_runtime,over,records,code", [
    (False, {}, {}, "judge_disabled"),
    (True, {"enabled": False}, {}, "judge_disabled"),
    (True, {"backend_confirmed": False}, {}, "backend_unconfirmed"),
    (True, {}, {"n_safe": 30, "n_flagged": 5}, "insufficient_labels"),
    (True, {}, {"keys": 2}, "insufficient_diversity"),
    (True, {}, {"n_flagged": 0}, "no_discrimination"),
    (True, {}, {"n_fn": 15}, "fn_bound_exceeded"),
])
def test_route_maps_each_gate_code_to_409(acknowledged_sink: Any, key_file: Path, with_runtime: bool, over: dict,
                                          records: dict, code: str) -> None:
    w = _Route(acknowledged_sink, key_file, with_runtime, **over)
    try:
        g = w.propose()
        if with_runtime:
            w.fill(**records)
        status, body = _call(w.base, "POST", f"/api/v1/assistant-grants/{g.grant_id}/activate", {"confirm": True})
        assert (status, body["code"]) == (409, code)
        assert w.svc.store.get_action_grant(g.grant_id, tenant_id=T, user_id=w.user).state is GrantState.PROPOSED   # type: ignore[union-attr]
    finally:
        w.close()


def test_route_earlier_codes_and_success_and_shadow_after_version_change(acknowledged_sink: Any, key_file: Path) -> None:
    w = _Route(acknowledged_sink, key_file, True)
    try:
        base = f"{w.base}"
        assert _call(base, "POST", "/api/v1/assistant-grants/missing/activate", {"confirm": True})[0] == 404
        g = w.propose()
        w.fill()
        status, body = _call(base, "POST", f"/api/v1/assistant-grants/{g.grant_id}/activate", {"confirm": True})
        assert status == 200 and body["state"] == "active" and body["judge_version"] == judge_version(w.rt.cfg_getter())   # type: ignore[union-attr]
        assert _call(base, "POST", f"/api/v1/assistant-grants/{g.grant_id}/activate", {"confirm": True})[1]["code"] == "not_proposed"
        w.rt.cfg_getter()["model"] = "swapped-model"                                 # type: ignore[union-attr]
        _, listing = _call(base, "GET", "/api/v1/assistant-grants")
        assert listing["grants"][0]["state"] == "shadow"
    finally:
        w.close()


def test_mcp_grants_list_and_action_request_follow_the_judge_version() -> None:
    from tests.control_plane.test_assistant_hub_action_request import (
        ACT,
        act,
        body_of,
        call,
        enrol,
        grant_for,
        make_hub,
    )
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    gid = grant_for(hub, auth[0])
    assert body_of(act(hub, auth))["outcome"] == "authorized"
    assert body_of(call(hub, auth, "action_grants_list"))["action_grants"][0]["state"] == "active"
    assert hub.judge is not None
    hub.judge.cfg_getter()["model"] = "swapped-model"                  # the judge version changes after activation
    rows = body_of(call(hub, auth, "action_grants_list"))["action_grants"]
    assert rows[0]["grant_id"] == gid and rows[0]["state"] == "shadow"
    after = body_of(act(hub, auth, session_id="s2"))
    assert (after["outcome"], after["source"]) == ("pending", "owner")
    hub.judge.recorder.stop()


def test_mcp_hub_without_a_judge_never_authorizes() -> None:
    from tests.control_plane.test_assistant_hub_action_request import (
        ACT,
        act,
        body_of,
        enrol,
        grant_for,
        make_hub,
    )
    with_judge = make_hub()
    auth = enrol(with_judge, scopes=ACT)
    grant_for(with_judge, auth[0])
    hub = replace(with_judge, judge=None)                              # same store and grant, no judge wired
    out = body_of(act(hub, auth))
    assert (out["outcome"], out["source"]) == ("pending", "judge_unavailable")


def test_cli_wiring_shares_one_judge_runtime_between_hub_and_owner_api(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from herdr_engineering.cli import _wire_assistant_hub
    from tests.control_plane.judge_support import closed_port
    port = closed_port()
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "hub": {"enabled": True, "host": "127.0.0.1", "port": port, "allowed_networks": ["127.0.0.0/8"]}}}))
    svr = SimpleNamespace(assistant=svc, assistant_workers=None, assistant_judge="unset")
    hub_server = _wire_assistant_hub(svr)
    try:
        assert hub_server is not None
        assert svr.assistant_judge is hub_server.hub.judge and svr.assistant_judge is not None
        assert svr.assistant_judge.client_factory() is None            # judge disabled by default: no client, no calls
    finally:
        hub_server.shutdown()
    off = SimpleNamespace(assistant=AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}})),
                          assistant_workers="x", assistant_judge="unset")
    assert _wire_assistant_hub(off) is None and off.assistant_judge is None


def test_active_grant_without_judge_version_never_authorizes_even_with_safe_judge(
        key_file: Path, listener_safe: WireListener) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, listener_safe, key_file)
    g = _proposed(store, cfg, _push())
    force_active_grant_for_tests(store, cfg, g.grant_id, NOW, None)           # stored judge_version == ""
    assert store.get_action_grant(g.grant_id, tenant_id=T, user_id=U).judge_version == ""   # type: ignore[union-attr]
    d = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)
    assert d.outcome == "pending" and d.grant_id is None
    assert not any(e["action"] == "assistant.action.authorized" for e in store._audit)
    _stop(rt)


def test_revoke_during_the_judge_call_is_not_authorized(key_file: Path) -> None:
    li = WireListener(success_body(SAFE), delay=1.0)
    try:
        store, cfg = AssistantStore(), _cfg()
        rt = _runtime(store, li, key_file)
        g = _active(store, cfg, rt)
        out: list[Any] = []
        t = threading.Thread(target=lambda: out.append(decide(store, cfg, _worker(), _push(), "s1", "t1", NOW, judge=rt)))
        t.start()
        assert _wait(lambda: li.hits >= 1)                  # the consult is in flight
        revoke_action_grant(store, NOW, T, U, g.grant_id, "owner")
        t.join(10)
        assert out and out[0].outcome == "pending" and out[0].grant_id is None
        assert not any(e["action"] == "assistant.action.authorized" for e in store._audit)
        _stop(rt)
    finally:
        li.close()


def test_owner_decision_before_the_record_exists_is_copied_by_the_recorder(key_file: Path, listener_safe: WireListener) -> None:
    store, cfg = AssistantStore(), _cfg()
    rt = _runtime(store, listener_safe, key_file)
    d = decide(store, cfg, _worker(), _push(), "s1", "t1", NOW)                # no runtime: nothing submitted
    assert d.request_id is not None
    record_owner_decision(store, cfg, NOW, T, U, d.request_id, False, "owner")  # owner is faster than the recorder
    assert store.list_judge_records(T, U) == []
    key = derive(_push()).key
    assert key is not None
    from herdr_engineering.control_plane.assistant_hub.shadow import ShadowJob, label_decision
    rt.recorder._process(ShadowJob(T, U, W, "s1", "t1", d.request_id, key))
    rec = store.list_judge_records(T, U)[0]
    assert rec.owner_decision == "denied" and rec.status == "ok"
    assert label_decision(store, T, U, d.request_id, True, NOW) is False     # first label still wins
    assert store.list_judge_records(T, U)[0].owner_decision == "denied"
    _stop(rt)


@pytest.mark.parametrize("break_it", ["cfg_getter_raises", "report_raises"])
def test_gate_errors_become_judge_error(key_file: Path, break_it: str) -> None:
    store, cfg = AssistantStore(), _cfg()
    good = _runtime(store, None, key_file)
    g = _proposed(store, cfg, _push())

    def boom() -> dict[str, Any]:
        raise RuntimeError("config exploded")
    getter = boom if break_it == "cfg_getter_raises" else (lambda: {})
    bad = JudgeRuntime(cfg_getter=getter, client_factory=good.client_factory, recorder=good.recorder)
    with pytest.raises(ActivationRefused) as err:
        _activate(store, cfg, g, bad)
    assert err.value.code == "judge_error" and "exploded" not in str(err.value)
    assert store.get_action_grant(g.grant_id, tenant_id=T, user_id=U).state is GrantState.PROPOSED   # type: ignore[union-attr]
    _stop(good)


def test_route_judge_error_is_409(acknowledged_sink: Any, key_file: Path) -> None:
    w = _Route(acknowledged_sink, key_file, True)
    try:
        g = w.propose()
        assert w.rt is not None

        def boom() -> dict[str, Any]:
            raise RuntimeError("x")
        w.svr.assistant_judge = JudgeRuntime(cfg_getter=boom, client_factory=w.rt.client_factory,
                                             recorder=w.rt.recorder)
        status, body = _call(w.base, "POST", f"/api/v1/assistant-grants/{g.grant_id}/activate", {"confirm": True})
        assert (status, body["code"]) == (409, "judge_error")
    finally:
        w.close()
