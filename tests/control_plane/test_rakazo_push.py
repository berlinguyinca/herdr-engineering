"""Phone push through Expo: tokens the app registers, one notification per new proposal, nothing leaks."""
import json
import time
import urllib.error
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane.rakazo_push import ExpoSender, PushNotifier, PushRegistry
from tests.control_plane import test_rakazo_bridge_proposals as _proposals
from tests.control_plane.test_rakazo_bridge import ACCOUNTS, bridge_section, new_bot

RERUN = _proposals.RERUN
make_server_p = _proposals.make_server_p      # the same server fixture, re-exported for pytest

TOKEN = "ExponentPushToken[abcdefghijklmnopqrstuv]"


class FakeSender:
    def __init__(self, tickets=None):
        self.sent, self.tickets = [], tickets

    def send(self, messages):
        self.sent.append(messages)
        return self.tickets if self.tickets is not None else [{"status": "ok", "id": "t"} for _ in messages]


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------------------------------------ registry and sender
def test_registry_keeps_a_few_tokens_per_account_and_forgets_a_sign_out():
    r = PushRegistry()
    for i in range(5):
        r.add("a1", f"ExponentPushToken[device{i:08d}]", "s1" if i < 4 else "s2")
    assert r.tokens("a1") == [f"ExponentPushToken[device{i:08d}]" for i in (2, 3, 4)]
    assert r.remove_session("a1", "s1") == 2 and len(r.tokens("a1")) == 1
    assert r.tokens("other") == []


def test_registry_survives_a_broken_database():
    class Broken:
        def load(self):
            raise RuntimeError("down")

        def save(self, *a):
            raise RuntimeError("down")

        def remove(self, *a):
            raise RuntimeError("down")
    r = PushRegistry(Broken())
    r.add("a1", TOKEN, "s")
    assert r.tokens("a1") == [TOKEN]


def test_expo_sender_posts_json_reads_tickets_and_never_echoes_the_access_token():
    seen = {}

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.body

    def opener(req, timeout):
        seen["auth"], seen["body"] = req.get_header("Authorization"), json.loads(req.data)
        return Resp(json.dumps({"data": [{"status": "ok", "id": "x"}]}).encode())
    tickets = ExpoSender("secret-access", opener).send([{"to": TOKEN, "title": "t", "body": "b"}])
    assert tickets == [{"status": "ok", "id": "x"}] and seen["auth"] == "Bearer secret-access"
    assert seen["body"][0]["to"] == TOKEN

    def failing(req, timeout):
        raise urllib.error.URLError("secret-access in a message")
    with pytest.raises(OSError) as exc:
        ExpoSender("secret-access", failing).send([])
    assert "secret-access" not in str(exc.value)


# ------------------------------------------------------------------------------------------ notifier
def notifier(sender, **kw):
    reg = PushRegistry()
    reg.add("a1", TOKEN, "s")
    n = PushNotifier(reg, sender, lambda account: {"botId": "bot1", "spaceId": "herdr"}, enabled=True, **kw)
    n.start()
    return n


def test_a_key_is_notified_once_with_a_target_that_opens_the_bot():
    s = FakeSender()
    n = notifier(s)
    assert n.notify("a1", "proposal:1", "Approval needed", "Re-run x") is True
    assert n.notify("a1", "proposal:1", "Approval needed", "Re-run x") is False
    assert wait_for(lambda: s.sent)
    [msg] = s.sent[0]
    assert msg["to"] == TOKEN and msg["data"]["botId"] == "bot1" and msg["data"]["spaceId"] == "herdr"
    assert msg["data"]["kind"] == "help" and msg["data"]["deliveryId"]
    n.close()


def test_off_or_without_a_device_nothing_is_queued():
    s = FakeSender()
    n = notifier(s)
    assert n.notify("nobody", "k", "t", "b") is False
    n.enabled = False
    assert n.notify("a1", "k2", "t", "b") is False and s.sent == []
    n.close()


def test_quiet_hours_hold_normal_notices_but_not_urgent_ones():
    s = FakeSender()
    n = notifier(s, quiet_hours=(22, 7), clock=lambda: datetime(2026, 10, 30, 23, tzinfo=UTC))
    assert n.notify("a1", "n1", "t", "b", priority="normal") is False
    assert n.notify("a1", "u1", "t", "b", priority="urgent") is True
    n.close()


def test_a_device_expo_reports_gone_is_forgotten_and_a_transport_error_is_survived():
    s = FakeSender([{"status": "error", "details": {"error": "DeviceNotRegistered"}}])
    n = notifier(s)
    n.notify("a1", "k", "t", "b")
    assert wait_for(lambda: n.registry.tokens("a1") == [])
    assert n.counters["unregistered"] == 1

    class Down:
        def send(self, messages):
            raise OSError("expo push unreachable")
    n2 = notifier(Down())
    n2.notify("a1", "k", "t", "b")
    assert wait_for(lambda: n2.counters["failed"] == 1)
    n.close()
    n2.close()


# ------------------------------------------------------------------------------------------ config and bridge
@pytest.mark.parametrize("push,bad", [
    ({"enabled": True, "typo": 1}, "push"),
    ({"enabled": True, "quiet_hours": {"start": 25, "end": 1}}, "quiet_hours"),
    ({"enabled": True, "quiet_hours": {"start": 22}}, "quiet_hours"),
    ({"enabled": True, "expo_access_token_file": 5}, "expo_access_token_file"),
    ("yes", "push")])
def test_push_config_is_validated(push, bad):
    with pytest.raises(ValueError, match=bad):
        rb.rakazo_bridge_config(bridge_section(push=push))


def test_push_is_off_by_default_and_parses_quiet_hours():
    assert rb.rakazo_bridge_config(bridge_section())["push"] == {"enabled": False}
    cfg = rb.rakazo_bridge_config(bridge_section(push={"enabled": True, "quiet_hours": {"start": 22, "end": 7}}))
    assert cfg["push"]["enabled"] is True and cfg["push"]["quiet_hours"] == (22, 7)


def rig(make_server_p, push=True):
    svr, api = make_server_p(bridge_section(push={"enabled": push}))
    sender = FakeSender()
    svr.rakazo.push._sender = sender
    token = api.login()
    bot = new_bot(api, token)
    user = svr.rakazo.accounts[0].principal_id
    tenant = svr.assistant.tenant_for(user)
    section = {"enabled": True, "suggest": True, "execute": False,
               "assistant_viewers": {f"{tenant}/{user}": {"org_id": "org1", "projects": "*"}}}
    svr.intelligence._section = lambda: section
    svr.intelligence._store = pi.IntelligenceStore()
    svr.intelligence.single_operator_fn = lambda: True
    return svr, api, token, bot, sender


def test_a_new_board_proposal_pushes_to_a_registered_phone_once(make_server_p):
    svr, api, token, bot, sender = rig(make_server_p)
    assert api.ok("notifications/registerPush", {"token": TOKEN}, token) == {"ok": True}
    p = svr.intelligence.board.request(org_id="org1", requester="x", action="ci_rerun", payload=dict(RERUN),
                                       resource_version=1, idempotency_key="k1")
    svr.intelligence.board.request(org_id="org1", requester="x", action="ci_rerun", payload=dict(RERUN),
                                   resource_version=1, idempotency_key="k1")          # a repeat is not a new proposal
    assert wait_for(lambda: sender.sent)
    time.sleep(0.1)
    assert len(sender.sent) == 1
    [msg] = sender.sent[0]
    assert msg["to"] == TOKEN and msg["title"] == "Approval needed" and "Re-run" in msg["body"]
    assert msg["data"]["botId"] == bot["id"] and p.id not in json.dumps(msg)


def test_unregister_stops_the_pushes_and_other_orgs_proposals_are_not_pushed(make_server_p):
    svr, api, token, bot, sender = rig(make_server_p)
    api.ok("notifications/registerPush", {"token": TOKEN}, token)
    api.ok("notifications/unregisterPush", {}, token)
    svr.intelligence.board.request(org_id="org1", requester="x", action="ci_rerun", payload=dict(RERUN), resource_version=1)
    time.sleep(0.2)
    assert sender.sent == []
    api.ok("notifications/registerPush", {"token": TOKEN}, token)
    svr.intelligence.board.request(org_id="other-org", requester="x", action="ci_rerun", payload=dict(RERUN), resource_version=1)
    time.sleep(0.2)
    assert sender.sent == []


def test_a_malformed_token_is_refused_and_push_off_drops_even_a_good_one(make_server_p):
    svr, api, token, bot, sender = rig(make_server_p, push=False)
    assert api.rpc("notifications/registerPush", {"token": "has space in it"}, token)[0] == 400
    api.ok("notifications/registerPush", {"token": TOKEN}, token)
    assert svr.rakazo.push.registry.tokens(svr.rakazo.accounts[0].account_id) == []
    svr.intelligence.board.request(org_id="org1", requester="x", action="ci_rerun", payload=dict(RERUN), resource_version=1)
    time.sleep(0.2)
    assert sender.sent == []


def test_a_family_account_is_never_pushed_a_proposal(make_server_p):
    svr, api = make_server_p(bridge_section(accounts=[{**ACCOUNTS[0], "role": "family"}], push={"enabled": True}))
    sender = FakeSender()
    svr.rakazo.push._sender = sender
    token = api.login()
    new_bot(api, token)
    user = svr.rakazo.accounts[0].principal_id
    tenant = svr.assistant.tenant_for(user)
    section = {"enabled": True, "suggest": True, "assistant_viewers": {f"{tenant}/{user}": {"org_id": "org1", "projects": "*"}}}
    svr.intelligence._section = lambda: section
    svr.intelligence._store = pi.IntelligenceStore()
    svr.intelligence.single_operator_fn = lambda: True
    api.ok("notifications/registerPush", {"token": TOKEN}, token)
    svr.intelligence.board.request(org_id="org1", requester="x", action="ci_rerun", payload=dict(RERUN), resource_version=1)
    time.sleep(0.2)
    assert sender.sent == []
