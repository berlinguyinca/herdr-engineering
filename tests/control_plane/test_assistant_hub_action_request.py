"""MCP action_request / action_grants_list over a real hub, store and policy (no doubles)."""
import json
from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane.assistant import (
    AssistantService,
    AssistantStore,
    assistant_config,
)
from herdr_engineering.control_plane.assistant_hub import mcp
from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.events import list_events
from herdr_engineering.control_plane.assistant_hub.mcp import Hub
from herdr_engineering.control_plane.assistant_hub.policy import activate_grant
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from tests.control_plane.test_assistant_hub_mcp import body_of, call, enrol, rpc

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
T0 = NOW - timedelta(days=30)
ACT = ("ask", "report", "read", "act")
CANARY = "hunter2-CANARY-9f3a"
PUSH = "git push origin main"


def make_hub(shadow_started_at=T0, enabled=True):
    cfg = assistant_config({"personal_assistant": {"enabled": enabled, "actions": {"allow_self_approval": True},
                                                   "grants": {"shadow_started_at": shadow_started_at.isoformat()}}})
    svc = AssistantService(config=cfg, now=lambda: NOW)
    reg = WorkerRegistry(svc.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30),
                         now=lambda: NOW)
    return Hub(registry=reg, service=svc, store=svc.store)


def act(hub, auth, **over):
    args = {"tool": "Bash", "command": PUSH, "repo": "acme/repo", "session_id": "s1", "task_id": "t1", **over}
    return call(hub, auth, "action_request", args)


def grant_for(hub, worker, cmd=PUSH, state=GrantState.PROPOSED, activate=True):
    key = derive(RawAction(tool="Bash", command=cmd, repo="acme/repo")).key
    gid = "ag_" + key.key_id()[:8] + worker.worker_id[-4:]
    cfg = hub.service.config["grants"]
    hub.store.put_action_grant(ActionGrant(
        grant_id=gid, tenant_id=worker.tenant_id, user_id=worker.user_id, worker_id=worker.worker_id,
        resource_scope=key.resource_scope, key_id=key.key_id(), verb=key.verb.value, key_canonical=key.canonical(),
        state=state, created_at=NOW, proposed_at=NOW, expires_at=NOW + timedelta(days=30),
        policy_version=cfg["policy_version"]))
    if activate:
        activate_grant(hub.store, cfg, NOW, worker.tenant_id, worker.user_id, gid, confirmed_by="owner",
                       judge_gate_satisfied=True)
    return gid


def test_action_request_requires_act_scope():
    hub = make_hub()
    auth = enrol(hub, scopes=("ask", "report", "read"))
    out = act(hub, auth)
    assert out["isError"] and "scope" in body_of(out)["error"]
    assert hub.store.list_action_requests("t1", "u1", "pending") == []
    _, body = rpc(hub, auth, "tools/list")
    assert "action_request" not in {t["name"] for t in body["result"]["tools"]}


def test_action_request_veto_returns_denied_with_code():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    out = act(hub, auth, command="git push --force origin main")
    assert out["isError"] is False
    body = body_of(out)
    assert (body["outcome"], body["veto_code"], body["source"]) == ("denied", "force_push", "veto")
    row = hub.store.get_action_request(body["request_id"], tenant_id="t1", user_id="u1")
    assert row.status == "vetoed"


def test_action_request_unknown_is_pending_and_creates_row():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    body = body_of(act(hub, auth, command="frobnicate --all"))
    assert (body["outcome"], body["source"]) == ("pending", "unknown")
    assert hub.store.get_action_request(body["request_id"], tenant_id="t1", user_id="u1").status == "pending"
    assert set(body) == {"outcome", "reason", "request_id", "grant_id", "veto_code", "source"}


def test_action_request_exact_active_grant_authorizes():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    gid = grant_for(hub, auth[0])
    body = body_of(act(hub, auth))
    assert (body["outcome"], body["grant_id"], body["source"]) == ("authorized", gid, "grant")
    other = body_of(act(hub, auth, command="git push origin dev"))
    assert other["outcome"] == "pending"


def test_action_request_grant_in_shadow_is_pending():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    grant_for(hub, auth[0])
    hub.service.config["grants"]["shadow_started_at"] = (NOW - timedelta(days=1)).isoformat()
    body = body_of(act(hub, auth))
    assert (body["outcome"], body["source"]) == ("pending", "shadow")


def test_action_request_cannot_set_tenant_user_or_worker_via_args():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    for extra in ("tenant_id", "user_id", "worker_id"):
        out = act(hub, auth, **{extra: "evil"})
        assert out["isError"] and body_of(out)["error"] == "unknown argument"
    assert hub.store.list_action_requests("t1", "u1", "pending") == []


def test_action_request_response_and_event_never_leak_command_display_or_canary():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    cmd = f"deploy-tool --password {CANARY} --target prod"
    out = act(hub, auth, command=cmd)
    text = out["content"][0]["text"]
    assert CANARY not in text and "deploy-tool" not in text and "display" not in text
    assert "key_canonical" not in text
    events = list_events(hub.store, tenant_id="t1", user_id="u1", kind="action_request")
    assert len(events) == 1 and events[0].provenance == "worker_reported"
    assert set(events[0].payload) == {"outcome", "source", "verb", "key_id"}
    key = derive(RawAction(tool="Bash", command=cmd, repo="acme/repo")).key
    expect_verb, expect_id = (key.verb.value, key.key_id()) if key else ("unknown", "")
    body = body_of(out)
    assert events[0].payload == {"outcome": body["outcome"], "source": body["source"], "verb": expect_verb,
                                 "key_id": expect_id}
    assert CANARY not in json.dumps(events[0].payload) and "deploy-tool" not in json.dumps(events[0].payload)


def test_event_payload_for_a_known_action_carries_its_verb_and_key_id():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    body = body_of(act(hub, auth))
    key = derive(RawAction(tool="Bash", command=PUSH, repo="acme/repo")).key
    ev = list_events(hub.store, tenant_id="t1", user_id="u1", kind="action_request")[0]
    assert ev.payload == {"outcome": body["outcome"], "source": body["source"], "verb": key.verb.value,
                          "key_id": key.key_id()}
    assert key.verb.value == "git.push"


def test_hint_over_cap_rejected():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    bad_hints = [{f"k{i}": "v" for i in range(17)}, {"k" * 65: "v"}, {"cwd": "x" * 513}, {"cwd": 5}, "cwd"]
    for hint in bad_hints:
        out = act(hub, auth, hint=hint)
        assert out["isError"] and "hint" in body_of(out)["error"]
    ok = act(hub, auth, hint={"cwd": "/tmp", "repo": "acme/repo", "ignored": "x"})
    assert ok["isError"] is False


def test_command_and_recipient_caps():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    assert act(hub, auth, command="é" * 4097)["isError"]                    # 8194 bytes
    assert not act(hub, auth, command="echo " + "a" * 8187)["isError"]      # exactly 8192 bytes
    assert act(hub, auth, recipients=["a@b.c"] * 21)["isError"]
    assert act(hub, auth, recipients=["a" * 255])["isError"]
    assert act(hub, auth, session_id="s" * 129)["isError"]
    assert act(hub, auth, task_id="")["isError"]


def test_rate_limit_31st_and_one_slot_per_call():
    hub = make_hub()
    auth = enrol(hub, scopes=ACT)
    for i in range(30):
        out = act(hub, auth, task_id=f"t{i}")
        assert out["isError"] is False, i
    out = act(hub, auth, task_id="t31")
    assert out["isError"] and body_of(out)["error"] == "rate limited"
    assert len(list_events(hub.store, tenant_id="t1", user_id="u1", kind="action_request", limit=100)) == 30


def test_disabled_assistant_refuses():
    hub = make_hub(enabled=False)
    auth = enrol(hub, scopes=ACT)
    out = act(hub, auth)
    assert out["isError"] and body_of(out)["error"] == "assistant is not enabled"


class _ExplodingGrants(AssistantStore):
    def list_action_grants(self, tenant_id, user_id):
        raise RuntimeError(f"internal {CANARY}")


def test_generic_exception_still_returns_tool_failed():
    cfg = assistant_config({"personal_assistant": {"enabled": True, "grants": {"shadow_started_at": T0.isoformat()}}})
    store = _ExplodingGrants()
    svc = AssistantService(config=cfg, store=store, now=lambda: NOW)
    reg = WorkerRegistry(store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30), now=lambda: NOW)
    hub = Hub(registry=reg, service=svc, store=store)
    auth = enrol(hub, scopes=ACT)
    for out in (act(hub, auth), call(hub, auth, "action_grants_list")):
        assert out["isError"] and body_of(out) == {"error": "tool failed"}
        assert CANARY not in out["content"][0]["text"]


def test_action_grants_list_only_own_tenant_and_user_and_no_canonical():
    hub = make_hub()
    mine = enrol(hub, scopes=ACT)
    other_user = enrol(hub, user="u2", device="d2", scopes=ACT)
    other_tenant = enrol(hub, tenant="t2", device="d3", scopes=ACT)
    gid = grant_for(hub, mine[0])
    grant_for(hub, other_user[0])
    grant_for(hub, other_tenant[0])
    out = call(hub, mine, "action_grants_list")
    assert mcp.UNTRUSTED in out["content"][0]["text"]
    rows = body_of(out)["action_grants"]
    assert [r["grant_id"] for r in rows] == [gid]
    assert set(rows[0]) == {"grant_id", "worker_id", "verb", "resource_scope", "key_id", "state", "expires_at",
                            "proposed_at", "activated_at", "revoked_at"}
    assert rows[0]["state"] == "active"
    assert "key_canonical" not in out["content"][0]["text"] and "confirmed_by" not in out["content"][0]["text"]
    assert call(hub, mine, "action_grants_list", {"limit": 0})["isError"]


def test_action_grants_list_requires_read_scope():
    hub = make_hub()
    auth = enrol(hub, scopes=("ask", "act"))
    assert call(hub, auth, "action_grants_list")["isError"]
