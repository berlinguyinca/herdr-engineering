"""A real operator phone's bots get the default tools on first use (never send_email, never preauthorized)."""
import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    TOOL_CATALOG,
    AssistantService,
    assistant_config,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_rakazo_bridge import FAST, PW_A


def _acct(name, role):
    return {"email": f"{name}@example.com", "password_scrypt": rb.hash_password(PW_A, **FAST), "role": role}


def build(extra=None, single=True):
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    bridge = {"enabled": True, "accounts": [_acct("op", "operator"), _acct("fam", "family")], **(extra or {})}
    server = ControlPlaneServer(ControlPlaneRepo(), assistant_service=svc, rakazo_bridge=bridge, single_operator=single)
    return server, server.rakazo


def ctx_for(b, name):
    acct = next(a for a in b.accounts if a.email == f"{name}@example.com")
    return rb._Ctx(acct, b.svc.tenant_for(acct.principal_id), acct.principal_id, "", False)


def grants(b, ctx, bot):
    return b.svc.store.list_grants(tenant_id=ctx.tenant, user_id=ctx.user, assistant_id=bot)


def tools(b, ctx, bot):
    return {g.tool_id for g in grants(b, ctx, bot)}


def test_operator_first_use_gets_exactly_the_default_set_non_preauthorized():
    _, b = build()
    ctx = ctx_for(b, "op")
    bot = b.svc.create_assistant(tenant_id=ctx.tenant, user_id=ctx.user, name="x", instructions="", enabled=True)
    assert tools(b, ctx, bot.assistant_id) == set()
    b._assistant(ctx, bot.assistant_id)
    got = grants(b, ctx, bot.assistant_id)
    assert {g.tool_id for g in got} == set(rb.DEFAULT_TOOLS)
    assert "send_email" not in {g.tool_id for g in got}
    assert all(g.scope == "*" and g.constraints["preauthorized"] is False for g in got)


def test_create_grants_and_second_use_is_idempotent():
    _, b = build()
    ctx = ctx_for(b, "op")
    bot_id = b._bots_create(ctx, {"name": "Phone"})["id"]
    first = {g.grant_id for g in grants(b, ctx, bot_id)}
    assert len(first) == len(rb.DEFAULT_TOOLS)
    b._assistant(ctx, bot_id)
    b._assistant(ctx, bot_id)
    assert {g.grant_id for g in grants(b, ctx, bot_id)} == first


def test_revoked_grant_is_not_recreated():
    _, b = build()
    ctx = ctx_for(b, "op")
    bot_id = b._bots_create(ctx, {"name": "Phone"})["id"]
    victim = next(g for g in grants(b, ctx, bot_id) if g.tool_id == "search_email")
    b.svc.revoke_grant(victim.grant_id, tenant_id=ctx.tenant, user_id=ctx.user)
    b._assistant(ctx, bot_id)
    assert not any(g.tool_id == "search_email" and g.grant_id != victim.grant_id for g in grants(b, ctx, bot_id))


def test_family_and_cognito_accounts_get_nothing():
    _, b = build()
    fam = ctx_for(b, "fam")
    bot = b.svc.create_assistant(tenant_id=fam.tenant, user_id=fam.user, name="x", instructions="", enabled=True)
    b._assistant(fam, bot.assistant_id)
    assert tools(b, fam, bot.assistant_id) == set()
    op = ctx_for(b, "op")
    b._cognito_users[op.account.account_id] = "cog"
    bot2 = b.svc.create_assistant(tenant_id=op.tenant, user_id=op.user, name="y", instructions="", enabled=True)
    b._assistant(op, bot2.assistant_id)
    assert tools(b, op, bot2.assistant_id) == set()


def test_not_single_operator_mode_grants_nothing():
    _, b = build(single=False)
    ctx = ctx_for(b, "op")
    bot_id = b._bots_create(ctx, {"name": "Phone"})["id"]
    assert tools(b, ctx, bot_id) == set()


def test_config_override_is_respected_and_empty_disables():
    _, b = build({"default_tools": ["set_reminder", "read_email"]})
    ctx = ctx_for(b, "op")
    bot_id = b._bots_create(ctx, {"name": "Phone"})["id"]
    assert tools(b, ctx, bot_id) == {"set_reminder", "read_email"}
    _, b2 = build({"default_tools": []})
    ctx2 = ctx_for(b2, "op")
    bot2 = b2._bots_create(ctx2, {"name": "Phone"})["id"]
    assert tools(b2, ctx2, bot2) == set()


@pytest.mark.parametrize("bad", [["nope"], "set_reminder", ["send_email"], ["browse"], ["run_shell"], ["edit_shared"], [1]])
def test_bad_default_tools_are_refused_at_config_parse(bad):
    section = {"enabled": True, "accounts": [_acct("op", "operator")], "default_tools": bad}
    with pytest.raises(ValueError, match="default_tools"):
        rb.rakazo_bridge_config(section)


def test_default_constant_is_known_tools_without_send_email():
    assert "send_email" not in rb.DEFAULT_TOOLS
    assert set(rb.DEFAULT_TOOLS) <= set(TOOL_CATALOG)
    cfg = rb.rakazo_bridge_config({"enabled": True, "accounts": [_acct("op", "operator")]})
    assert cfg["default_tools"] == rb.DEFAULT_TOOLS


def test_grant_failure_never_breaks_chat(monkeypatch):
    _, b = build()
    ctx = ctx_for(b, "op")
    bot = b.svc.create_assistant(tenant_id=ctx.tenant, user_id=ctx.user, name="x", instructions="", enabled=True)
    monkeypatch.setattr(b.svc, "grant_tool", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert b._assistant(ctx, bot.assistant_id).assistant_id == bot.assistant_id


def test_concurrent_first_requests_grant_each_tool_once():
    import threading
    _, b = build()
    ctx = ctx_for(b, "op")
    bot = b.svc.create_assistant(tenant_id=ctx.tenant, user_id=ctx.user, name="x", instructions="", enabled=True)
    start = threading.Barrier(8)

    def hit():
        start.wait()
        b._assistant(ctx, bot.assistant_id)

    threads = [threading.Thread(target=hit) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ids = [g.tool_id for g in grants(b, ctx, bot.assistant_id)]
    assert sorted(ids) == sorted(rb.DEFAULT_TOOLS)
