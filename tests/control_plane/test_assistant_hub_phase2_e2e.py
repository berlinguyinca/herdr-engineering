"""Phase 2 end to end: a real ControlPlaneServer and a real HubServer share ONE WorkerRegistry and ONE store on
127.0.0.1 port 0, driven with real urllib. No mocks, no fakes. Nothing here touches the real ~/.claude, ~/.codex,
~/.pi or ~/.config/herdr (no subprocess runs at all)."""
import contextlib
import hashlib
import json
import threading
import urllib.error
import urllib.request
from argparse import Namespace
from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.mcp import UNTRUSTED, Hub
from herdr_engineering.control_plane.assistant_hub.policy import activate_grant
from herdr_engineering.control_plane.assistant_hub.server import HubServer
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo

OWNER_KEY = "p2-e2e-owner-key-0001"
OWNER = "key:" + hashlib.sha256(OWNER_KEY.encode()).hexdigest()[:12]
TENANT = "acme"
PUSH = "git push origin main"
CANARY = "CANARYsecret99xyz"
SECRET_URL_CMD = f"frobnicate https://deploy:{CANARY}@example.com/x"
ACT = ["ask", "report", "read", "act"]
TINY = {"required_approvals": {"local_reversible": 3, "remote_reversible": 3, "outward": 3},
        "min_sessions": 3, "min_tasks": 2}


def http(method, url, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, raw


@contextlib.contextmanager
def running(sink, grants):
    section = {"enabled": True, "tenancy": {"principals": {OWNER: TENANT}}, "grants": grants,
               "hub": {"enabled": True, "host": "127.0.0.1", "port": 9483, "allowed_networks": ["127.0.0.0/8"]}}
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    registry = WorkerRegistry(svc.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30))
    hub = HubServer(Hub(registry=registry, service=svc, store=svc.store), host="127.0.0.1", port=0,
                    allowed_networks=["127.0.0.0/8"], allowed_hosts=[])
    hub.start()
    hub.wait_until_ready()
    svr = ControlPlaneServer(ControlPlaneRepo(sink=sink), assistant_service=svc, api_keys={OWNER_KEY: "operator"},
                             assistant_workers=registry)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        yield Namespace(api=f"http://127.0.0.1:{svr.port}", hub=f"http://127.0.0.1:{hub.port}", svc=svc)
    finally:
        try:
            svr.shutdown()
        finally:
            hub.shutdown()


def owner(w, method, path, body=None):
    return http(method, w.api + path, body, {"X-API-Key": OWNER_KEY})


def enroll(w, name, device, scopes=None):
    body = {"name": name, "harness": "claude"}
    if scopes is not None:
        body["scopes"] = scopes
    status, created = owner(w, "POST", "/api/v1/assistant-workers", body)
    assert status == 201, created
    status, red = http("POST", w.hub + "/assistant/enroll", {"code": created["code"], "device_id": device})
    assert status == 200, red
    return Namespace(wid=red["worker_id"], cred=red["credential"], device=device, code=created["code"])


def mcp(w, wk, method, params=None):
    msg = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        msg["params"] = params
    return http("POST", w.hub + "/assistant/mcp", msg,
                {"Authorization": f"Worker {wk.cred}", "X-Herdr-Device": wk.device})


def tool(w, wk, name, args):
    status, body = mcp(w, wk, "tools/call", {"name": name, "arguments": args})
    assert status == 200, body
    text = body["result"]["content"][0]["text"]
    assert body["result"]["isError"] is False, text
    return json.loads(text.split("\n\n", 1)[1] if UNTRUSTED in text else text[text.index("{"):]), text


def ask_action(w, wk, command, session="s1", task="t1"):
    return tool(w, wk, "action_request", {"tool": "Bash", "command": command, "repo": "acme/repo",
                                          "session_id": session, "task_id": task})


def audit_actions(w):
    return [a["action"] for a in w.svc.store.audit if a["tenant_id"] == TENANT]


def put_grant(w, worker_id, state):
    """Hand-insert a grant for PUSH. ACTIVE goes through policy.activate_grant exactly as Phase 3 will, except that
    the judge gate is declared satisfied here: Phase 3 supplies the real gate, the owner route hard-codes False."""
    key = derive(RawAction(tool="Bash", command=PUSH, repo="acme/repo")).key
    cfg, now = w.svc.config["grants"], w.svc.now()
    gid = "ag_e2e_" + worker_id[-6:]
    w.svc.store.put_action_grant(ActionGrant(
        grant_id=gid, tenant_id=TENANT, user_id=OWNER, worker_id=worker_id, resource_scope=key.resource_scope,
        key_id=key.key_id(), verb=key.verb.value, key_canonical=key.canonical(), state=GrantState.PROPOSED,
        created_at=now, proposed_at=now, expires_at=now + timedelta(days=30), policy_version=cfg["policy_version"]))
    if state is GrantState.ACTIVE:
        activate_grant(w.svc.store, cfg, now, TENANT, OWNER, gid, confirmed_by="owner", judge_gate_satisfied=True)
    return gid


def test_phase2_flow_vetoes_evidence_proposal_gate_grant_revoke(acknowledged_sink):
    long_ago = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    with running(acknowledged_sink, {"shadow_started_at": long_ago, **TINY}) as w:
        wk = enroll(w, "laptop", "dev1", ACT)
        other = enroll(w, "desktop", "dev2", ACT)
        status, listed = owner(w, "GET", "/api/v1/assistant-workers")
        assert status == 200
        assert next(r for r in listed["workers"] if r["worker_id"] == wk.wid)["scopes"] == ACT

        # MCP surface for an act+read worker
        names = {t["name"] for t in mcp(w, wk, "tools/list")[1]["result"]["tools"]}
        assert {"action_request", "action_grants_list"} <= names

        # vetoes: denied, with a code, a vetoed owner row and an audit entry
        forced, text = ask_action(w, wk, "git fetch && git push --force origin main")
        assert forced["outcome"] == "denied" and forced["veto_code"] == "force_push" and forced["source"] == "veto"
        assert "display" not in text and "command" not in text
        quobyte, _ = ask_action(w, wk, "rm -rf /quobyte/hive/data", session="s1", task="t2")
        assert quobyte["outcome"] == "denied" and quobyte["veto_code"] == "quobyte_delete"
        vetoed = owner(w, "GET", "/api/v1/assistant-action-requests?status=vetoed")[1]["requests"]
        assert {r["request_id"] for r in vetoed} == {forced["request_id"], quobyte["request_id"]}
        assert all(r["status"] == "vetoed" for r in vetoed)
        assert audit_actions(w).count("assistant.action.vetoed") == 2
        status, body = owner(w, "POST", f"/api/v1/assistant-action-requests/{forced['request_id']}/decision",
                             {"approve": True})
        assert status == 409                                    # a veto is final

        # unknown command: pending, display masked and only on the owner's row
        unknown, text = ask_action(w, wk, SECRET_URL_CMD)
        assert unknown["outcome"] == "pending" and unknown["source"] == "unknown"
        assert not {"display", "command"} & set(unknown) and CANARY not in text and "frobnicate" not in text
        (row,) = owner(w, "GET", "/api/v1/assistant-action-requests?status=pending")[1]["requests"]
        assert row["request_id"] == unknown["request_id"]
        assert "frobnicate" in row["display"] and CANARY not in row["display"] and "***" in row["display"]
        assert owner(w, "POST", f"/api/v1/assistant-action-requests/{unknown['request_id']}/decision",
                     {"approve": False})[1]["status"] == "denied"

        # worker-reported reports never count as evidence, however many
        for i in range(5):
            tool(w, wk, "report", {"kind": "outcome", "text": f"approved {PUSH} {i}", "session_id": f"r{i}"})

        def approve(session, task):
            res, _ = ask_action(w, wk, PUSH, session=session, task=task)
            assert res["outcome"] == "pending" and res["source"] == "owner" and res["grant_id"] is None
            status, out = owner(w, "POST", f"/api/v1/assistant-action-requests/{res['request_id']}/decision",
                                {"approve": True})
            assert status == 200 and out["decided_by"] == OWNER
            return owner(w, "GET", "/api/v1/assistant-grants")[1]["grants"]

        assert approve("s1", "t1") == []
        assert approve("s2", "t2") == []                        # 2 approvals, 2 sessions: below the tiny threshold
        for i in range(5):
            tool(w, wk, "report", {"kind": "outcome", "text": "more approvals please", "session_id": f"r{i}"})
        assert owner(w, "GET", "/api/v1/assistant-grants")[1]["grants"] == []
        grants = approve("s3", "t1")                            # 3 approvals, 3 sessions, 2 tasks
        (proposed,) = grants
        assert proposed["state"] == "proposed" and proposed["worker_id"] == wk.wid and proposed["verb"] == "git.push"
        assert proposed["confirmed_by"] in ("", None)
        evidence = [e for e in w.svc.store.action_evidence.values() if e.key_id == proposed["key_id"]]
        assert {e.provenance for e in evidence} == {"owner_api"} and len(evidence) == 3

        # still nothing is authorized: a proposal is not a grant
        again, _ = ask_action(w, wk, PUSH, session="s4", task="t3")
        assert again["outcome"] == "pending"
        # the worker sees the proposal (state only)
        listed_by_worker, _ = tool(w, wk, "action_grants_list", {})
        assert [g["state"] for g in listed_by_worker["action_grants"]] == ["proposed"]

        # activation through the owner route answers 409 (judge gate), audited
        status, refused = owner(w, "POST", f"/api/v1/assistant-grants/{proposed['grant_id']}/activate",
                                {"confirm": True})
        assert status == 409 and refused["code"] == "judge_gate_not_satisfied"
        assert "assistant.grant.activate_refused" in audit_actions(w)
        assert owner(w, "GET", "/api/v1/assistant-grants")[1]["grants"][0]["state"] == "proposed"

        # Phase 3 supplies the real judge gate; here an ACTIVE grant is created through policy.activate_grant
        w.svc.store.action_grants.pop(proposed["grant_id"])
        gid = put_grant(w, wk.wid, GrantState.ACTIVE)
        ok, _ = ask_action(w, wk, PUSH, session="s9", task="t9")
        assert (ok["outcome"], ok["grant_id"], ok["source"]) == ("authorized", gid, "grant")
        for variant in ("git push -u origin feature", "git push origin main && echo done"):
            res, _ = ask_action(w, wk, variant)
            assert res["outcome"] == "pending" and res["grant_id"] is None, variant
        foreign, _ = ask_action(w, other, PUSH)
        assert foreign["outcome"] == "pending" and foreign["grant_id"] is None

        # revoke: the very next request is pending again
        status, revoked = owner(w, "DELETE", f"/api/v1/assistant-grants/{gid}")
        assert status == 200 and revoked["state"] == "revoked"
        after, _ = ask_action(w, wk, PUSH, session="s10", task="t10")
        assert after["outcome"] == "pending" and after["grant_id"] is None

        # audit chain and secrets
        assert w.svc.store.verify_audit(TENANT) is True
        assert {"assistant.action.vetoed", "assistant.action.requested", "assistant.action.decided",
                "assistant.grant.activate_refused", "assistant.grant.activated", "assistant.action.authorized",
                "assistant.grant.revoked"} <= set(audit_actions(w))
        store = w.svc.store
        blobs = {"audit": json.dumps(store.audit, default=str),
                 "grants": json.dumps([g.__dict__ for g in store.action_grants.values()], default=str),
                 "evidence": json.dumps([e.__dict__ for e in store.action_evidence.values()], default=str),
                 "snapshot": json.dumps(store.snapshot(), default=str)}
        for secret in (wk.cred, other.cred, wk.code, other.code, CANARY):
            for name, blob in blobs.items():
                assert secret not in blob, (name, secret[:6])
        assert blobs["audit"]
        requests_json = json.dumps(owner(w, "GET", "/api/v1/assistant-action-requests")[1])
        assert CANARY not in requests_json and "frobnicate" in requests_json


def test_active_grant_is_not_honoured_during_the_global_shadow_period(acknowledged_sink):
    with running(acknowledged_sink, {"shadow_started_at": datetime.now(UTC).isoformat(), **TINY}) as w:
        wk = enroll(w, "laptop", "dev1", ACT)
        # In shadow, activation itself is refused; insert the ACTIVE state directly to prove decide() still holds.
        key = derive(RawAction(tool="Bash", command=PUSH, repo="acme/repo")).key
        now = w.svc.now()
        w.svc.store.put_action_grant(ActionGrant(
            grant_id="ag_shadow", tenant_id=TENANT, user_id=OWNER, worker_id=wk.wid, resource_scope=key.resource_scope,
            key_id=key.key_id(), verb=key.verb.value, key_canonical=key.canonical(), state=GrantState.ACTIVE,
            created_at=now, proposed_at=now, activated_at=now, expires_at=now + timedelta(days=30),
            policy_version=w.svc.config["grants"]["policy_version"], confirmed_by="owner"))
        res, _ = ask_action(w, wk, PUSH)
        assert res["outcome"] == "pending" and res["source"] == "shadow" and res["grant_id"] is None
        assert w.svc.store.verify_audit(TENANT) is True
