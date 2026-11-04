"""Phase 3 end to end: a real ControlPlaneServer and a real HubServer share ONE WorkerRegistry and ONE store on
127.0.0.1 port 0, driven with real urllib.  The judge is the real ``JevClient`` talking to a raw-socket
``WireListener`` that serves the DOCUMENTED System One shape (not a recorded real response), or to a closed port.
No mocks, no fakes.  Nothing here touches the real ~/.claude, ~/.codex, ~/.pi or ~/.config/herdr.

Hand-built ``JudgeRecord`` rows stand in for a real judge when the test needs the label threshold met: reaching 200
genuinely judged and owner-labeled decisions is not something a test can do in seconds.

The ``integration`` tier at the bottom stays skipped unless HERDR_JEV_TEST_URL and HERDR_JEV_TEST_KEY_FILE point at a
real System One (mock backend ok).  It has NOT been run against one yet."""
import contextlib
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.mcp import UNTRUSTED, Hub
from herdr_engineering.control_plane.assistant_hub.server import HubServer
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.judge_support import (
    RISKY,
    SAFE,
    WireListener,
    closed_port,
    force_active_grant_for_tests,
    judge_cfg,
    make_runtime,
    satisfy_gate,
    success_body,
)

OWNER_KEY = "p3-e2e-owner-key-0001"
OWNER = "key:" + hashlib.sha256(OWNER_KEY.encode()).hexdigest()[:12]
TENANT = "acme"
PUSH = "git push origin main"
CANARY = "CANARYsecret99xyz"
SECRET_URL_CMD = f"frobnicate https://deploy:{CANARY}@example.com/x"
ACT = ["ask", "report", "read", "act"]
TINY = {"required_approvals": {"local_reversible": 3, "remote_reversible": 3, "outward": 3},
        "min_sessions": 3, "min_tasks": 2}
RISK = derive(RawAction(tool="Bash", command=PUSH, repo="acme/repo")).key.risk_class.value


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
def running(sink, tmp, *, endpoint, grants=None, backend_confirmed=False):
    key_path = tmp / "jev.key"
    key_text = "sk-" + secrets.token_hex(16)          # random per run: any leak of it is detectable
    key_path.write_text(key_text + "\n")
    key_path.chmod(0o600)
    long_ago = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    section = {"enabled": True, "tenancy": {"principals": {OWNER: TENANT}},
               "grants": {"shadow_started_at": long_ago, **TINY, **(grants or {})},
               "hub": {"enabled": True, "host": "127.0.0.1", "port": 9483, "allowed_networks": ["127.0.0.0/8"]}}
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    registry = WorkerRegistry(svc.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30))
    cfg = judge_cfg(endpoint, key_path, backend_confirmed=backend_confirmed)
    rt = make_runtime(svc.store, cfg, svc.now)
    hub = HubServer(Hub(registry=registry, service=svc, store=svc.store, judge=rt), host="127.0.0.1", port=0,
                    allowed_networks=["127.0.0.0/8"], allowed_hosts=[])
    hub.start()
    hub.wait_until_ready()
    svr = ControlPlaneServer(ControlPlaneRepo(sink=sink), assistant_service=svc, api_keys={OWNER_KEY: "operator"},
                             assistant_workers=registry, assistant_judge=rt)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        yield Namespace(api=f"http://127.0.0.1:{svr.port}", hub=f"http://127.0.0.1:{hub.port}", svc=svc, rt=rt,
                        cfg=cfg, key_text=key_text, key_path=str(key_path), outputs=[])
    finally:
        try:
            svr.shutdown()
        finally:
            hub.shutdown()
            rt.recorder.stop()


def owner(w, method, path, body=None):
    status, body = http(method, w.api + path, body, {"X-API-Key": OWNER_KEY})
    w.outputs.append(json.dumps(body, default=str) if not isinstance(body, bytes) else body.decode("utf-8", "replace"))
    return status, body


def enroll(w, name, device):
    status, created = owner(w, "POST", "/api/v1/assistant-workers", {"name": name, "harness": "claude", "scopes": ACT})
    assert status == 201, created
    status, red = http("POST", w.hub + "/assistant/enroll", {"code": created["code"], "device_id": device})
    assert status == 200, red
    return Namespace(wid=red["worker_id"], cred=red["credential"], device=device)


def tool(w, wk, name, args):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    status, body = http("POST", w.hub + "/assistant/mcp", msg,
                        {"Authorization": f"Worker {wk.cred}", "X-Herdr-Device": wk.device})
    assert status == 200, body
    text = body["result"]["content"][0]["text"]
    w.outputs.append(text)
    assert body["result"]["isError"] is False, text
    return json.loads(text.split("\n\n", 1)[1] if UNTRUSTED in text else text[text.index("{"):])


def ask_action(w, wk, command, session, task):
    return tool(w, wk, "action_request", {"tool": "Bash", "command": command, "repo": "acme/repo",
                                          "session_id": session, "task_id": task})


def wait_for(predicate, what, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def records(w):
    return w.svc.store.list_judge_records(TENANT, OWNER)


def audit_actions(w):
    return [a["action"] for a in w.svc.store.audit if a["tenant_id"] == TENANT]


def test_phase3_flow_shadow_labels_gate_judge_on_the_authorize_path(acknowledged_sink, tmp_path):
    safe, risky = WireListener(success_body(SAFE)), WireListener(success_body(RISKY))
    try:
        with running(acknowledged_sink, tmp_path, endpoint=f"http://127.0.0.1:{closed_port()}") as w:
            wk = enroll(w, "laptop", "dev1")

            # 1. judge configured but unreachable: every request is pending and shadow records say "unavailable"
            for i, task in enumerate(("t1", "t2", "t1"), start=1):
                res = ask_action(w, wk, PUSH, f"s{i}", task)
                assert res["outcome"] == "pending" and res["source"] == "owner" and res["grant_id"] is None
            wait_for(lambda: len(records(w)) == 3, "three shadow records")
            assert {r.status for r in records(w)} == {"unavailable"} and all(r.reason_code for r in records(w))

            # 2. owner approvals label those records (first label wins) and earn a PROPOSED grant
            for res in owner(w, "GET", "/api/v1/assistant-action-requests?status=pending")[1]["requests"]:
                status, out = owner(w, "POST", f"/api/v1/assistant-action-requests/{res['request_id']}/decision",
                                    {"approve": True})
                assert status == 200, out
            assert {r.owner_decision for r in records(w)} == {"approved"}
            (proposed,) = owner(w, "GET", "/api/v1/assistant-grants")[1]["grants"]
            gid = proposed["grant_id"]
            assert proposed["state"] == "proposed"

            # 3. status route: backend unconfirmed first, then (once confirmed) not enough labels
            status, st = owner(w, "GET", "/api/v1/assistant-judges")
            assert status == 200 and st["enabled"] is True and st["backend_confirmed"] is False
            assert st["gate"][RISK] == {"satisfied": False, "code": "backend_unconfirmed"}
            assert st["endpoint_host"] == "127.0.0.1" and len(st["key_fingerprint"]) == 8
            status, refused = owner(w, "POST", f"/api/v1/assistant-grants/{gid}/activate", {"confirm": True})
            assert status == 409 and refused["code"] == "backend_unconfirmed"

            w.cfg["backend_confirmed"] = True                       # the owner verified a real model is serving
            assert owner(w, "GET", "/api/v1/assistant-judges")[1]["gate"][RISK]["code"] == "insufficient_labels"
            status, refused = owner(w, "POST", f"/api/v1/assistant-grants/{gid}/activate", {"confirm": True})
            assert status == 409 and refused["code"] == "insufficient_labels"

            # 4. hand-built records stand in for a real judge: enough labels, keys and denied decisions
            satisfy_gate(w.svc.store, w.rt, RISK, TENANT, OWNER)
            assert owner(w, "GET", "/api/v1/assistant-judges")[1]["gate"][RISK] == {"satisfied": True, "code": ""}
            status, activated = owner(w, "POST", f"/api/v1/assistant-grants/{gid}/activate", {"confirm": True})
            assert status == 200 and activated["state"] == "active", activated

            # 5. an ACTIVE grant still needs the judge to answer: down => pending/judge_unavailable
            down = ask_action(w, wk, PUSH, "s10", "t10")
            assert (down["outcome"], down["source"], down["grant_id"]) == ("pending", "judge_unavailable", None)
            w.cfg["enabled"] = False                                # disabled judge is the same fail-closed answer
            off = ask_action(w, wk, PUSH, "s11", "t11")
            assert (off["outcome"], off["source"]) == ("pending", "judge_unavailable")
            w.cfg["enabled"] = True

            # 6. non-escalating answer => authorized; escalating answer => pending/judge
            w.cfg["endpoint"] = f"http://127.0.0.1:{safe.port}"
            ok = ask_action(w, wk, PUSH, "s12", "t12")
            assert (ok["outcome"], ok["source"], ok["grant_id"]) == ("authorized", "grant", gid)
            assert safe.hits >= 1
            w.cfg["endpoint"] = f"http://127.0.0.1:{risky.port}"
            flagged = ask_action(w, wk, PUSH, "s13", "t13")
            assert (flagged["outcome"], flagged["source"], flagged["grant_id"]) == ("pending", "judge", None)

            # 7. a different exact key never rides the grant, whatever the judge says
            other = ask_action(w, wk, "git push -u origin feature", "s14", "t14")
            assert other["outcome"] == "pending" and other["grant_id"] is None
            w.cfg["endpoint"] = f"http://127.0.0.1:{safe.port}"
            assert ask_action(w, wk, PUSH, "s15", "t15")["outcome"] == "authorized"

            # 8. changing the model changes the judge version: the grant shows SHADOW and authorizes nothing
            w.cfg["model"] = "some-other-model"
            shown = owner(w, "GET", "/api/v1/assistant-grants")[1]["grants"]
            assert [g["state"] for g in shown if g["grant_id"] == gid] == ["shadow"]
            hits = safe.hits
            moved = ask_action(w, wk, PUSH, "s16", "t16")
            assert moved["outcome"] == "pending" and moved["grant_id"] is None
            assert safe.hits >= hits        # the recorder may still record it; it just must not authorize

            # 9. a pending unknown command carrying a canary secret
            unknown = ask_action(w, wk, SECRET_URL_CMD, "s17", "t17")
            assert unknown["outcome"] == "pending" and unknown["source"] == "unknown"

            # 10. audit chain, and nothing secret anywhere
            wait_for(lambda: w.rt.recorder.queued() == 0, "recorder to drain")
            assert w.svc.store.verify_audit(TENANT) is True
            assert {"assistant.action.authorized", "assistant.grant.activate_refused",
                    "assistant.grant.activated"} <= set(audit_actions(w))
            tool(w, wk, "judge_status", {})
            store = w.svc.store
            blobs = {"audit": json.dumps(store.audit, default=str),
                     "records": json.dumps([r.__dict__ for r in records(w)], default=str),
                     "snapshot": json.dumps(store.snapshot(), default=str),
                     "outputs": "\n".join(w.outputs),
                     "to_judge": "\n".join(m.decode("utf-8", "replace").split("\r\n\r\n", 1)[-1]
                                           for m in safe.seen + risky.seen)}
            for secret in (w.key_text, w.key_path, CANARY, "frobnicate"):
                for name, blob in blobs.items():
                    if name == "snapshot" and secret == "frobnicate":
                        continue        # the owner's request row legitimately holds the masked display text
                    assert secret not in blob, (name, secret[:6])
            assert blobs["audit"] and blobs["records"]
            owner_rows = json.dumps(owner(w, "GET", "/api/v1/assistant-action-requests")[1])
            assert CANARY not in owner_rows
            assert "git push" not in blobs["to_judge"]          # command text never reaches the judge
    finally:
        safe.close()
        risky.close()


def test_active_grant_with_a_stale_judge_version_is_shadow_not_authorized(acknowledged_sink, tmp_path):
    li = WireListener(success_body(SAFE))
    try:
        with running(acknowledged_sink, tmp_path, endpoint=f"http://127.0.0.1:{li.port}", backend_confirmed=True) as w:
            wk = enroll(w, "laptop", "dev1")
            key = derive(RawAction(tool="Bash", command=PUSH, repo="acme/repo")).key
            now = w.svc.now()
            w.svc.store.put_action_grant(ActionGrant(
                grant_id="ag_p3", tenant_id=TENANT, user_id=OWNER, worker_id=wk.wid,
                resource_scope=key.resource_scope, key_id=key.key_id(), verb=key.verb.value,
                key_canonical=key.canonical(), state=GrantState.PROPOSED, created_at=now, proposed_at=now,
                expires_at=now + timedelta(days=30), policy_version=w.svc.config["grants"]["policy_version"]))
            force_active_grant_for_tests(w.svc.store, w.svc.config["grants"], "ag_p3", now, w.cfg)
            assert ask_action(w, wk, PUSH, "s1", "t1")["outcome"] == "authorized"
            w.cfg["thresholds"] = {**w.cfg["thresholds"], "destructive": 0.9}      # any version input resets it
            res = ask_action(w, wk, PUSH, "s2", "t2")
            assert res["outcome"] == "pending" and res["grant_id"] is None
            assert w.svc.store.verify_audit(TENANT) is True
    finally:
        li.close()


_URL = os.environ.get("HERDR_JEV_TEST_URL", "")
_KEY = os.environ.get("HERDR_JEV_TEST_KEY_FILE", "")


@pytest.mark.integration
@pytest.mark.skipif(not (_URL and _KEY), reason="set HERDR_JEV_TEST_URL and HERDR_JEV_TEST_KEY_FILE to a real System One (mock backend ok)")
def test_integration_active_grant_consults_the_real_system_one(acknowledged_sink, tmp_path):
    with running(acknowledged_sink, tmp_path, endpoint=_URL, backend_confirmed=True) as w:
        w.cfg["api_key_file"] = str(Path(_KEY))
        w.cfg["model"] = os.environ.get("HERDR_JEV_TEST_MODEL", "laya-auto")
        w.cfg["timeout_seconds"] = 30
        wk = enroll(w, "laptop", "dev1")
        key = derive(RawAction(tool="Bash", command=PUSH, repo="acme/repo")).key
        now = w.svc.now()
        w.svc.store.put_action_grant(ActionGrant(
            grant_id="ag_p3i", tenant_id=TENANT, user_id=OWNER, worker_id=wk.wid, resource_scope=key.resource_scope,
            key_id=key.key_id(), verb=key.verb.value, key_canonical=key.canonical(), state=GrantState.PROPOSED,
            created_at=now, proposed_at=now, expires_at=now + timedelta(days=30),
            policy_version=w.svc.config["grants"]["policy_version"]))
        force_active_grant_for_tests(w.svc.store, w.svc.config["grants"], "ag_p3i", now, w.cfg)
        res = ask_action(w, wk, PUSH, "s1", "t1")
        # Authorized only if the real judge answered and did not escalate; otherwise a fail-closed pending.
        assert (res["outcome"], res["source"]) in {("authorized", "grant"), ("pending", "judge"),
                                                   ("pending", "judge_unavailable")}
        assert w.svc.store.verify_audit(TENANT) is True
