"""The phone (Rakazo bridge) and the dashboard must show the same assistants for the same person.

In single-operator mode a phone ``operator`` account is the dashboard operator (``open-listener``). Per-person DATA already
follows that rule (``canonical_person``); the assistants themselves must too, or the phone and the dashboard disagree.
"""
from herdr_engineering.control_plane import rakazo_bridge as rb
from tests.control_plane.test_operator_phone_person import OP, T, srv  # noqa: F401  (fixture)


def _ctx(server, who):
    account = server.rakazo._by_principal[server.p[who]]
    base = server.assistant.tenant_for(account.principal_id)
    return server.rakazo._ctx(account, base, "", False, base=base)


def _names(server, who):
    return sorted(b["name"] for b in server.rakazo._bots(_ctx(server, who)) if not b.get("isAgent"))


def test_phone_operator_sees_the_dashboard_operators_assistants(srv):  # noqa: F811
    srv.assistant.create_assistant(tenant_id=T, user_id=OP, name="Dashboard bot")
    assert "Dashboard bot" in _names(srv, "op")


def test_dashboard_operator_sees_assistants_created_on_the_phone(srv):  # noqa: F811
    ctx = _ctx(srv, "op")
    srv.assistant.create_assistant(tenant_id=ctx.tenant, user_id=ctx.user, name="Phone bot")
    assert "Phone bot" in sorted(a.name for a in srv.assistant.list_assistants(tenant_id=T, user_id=OP))


def test_assistants_owned_by_the_phone_principal_before_the_change_are_reported_not_moved(srv):  # noqa: F811
    old = srv.assistant.create_assistant(tenant_id=T, user_id=srv.p["op"], name="Old phone bot")
    assert [a["assistant_id"] for a in srv.rakazo.legacy_phone_assistants()] == [old.assistant_id]
    assert "Old phone bot" not in _names(srv, "op")


def test_mapped_operator_owns_as_dashboard_but_keeps_its_own_principal(srv):  # noqa: F811
    ctx = _ctx(srv, "op")
    assert (ctx.user, ctx.principal) == (OP, srv.p["op"])


def test_family_and_other_spaces_keep_the_phone_principal(srv):  # noqa: F811
    fam = _ctx(srv, "fam")
    assert fam.user == fam.principal == srv.p["fam"]
    account = srv.rakazo._by_principal[srv.p["op"]]
    in_space = srv.rakazo._ctx(account, "default~s1", "", False, "s1", "default")
    assert in_space.user == srv.p["op"]


def test_not_single_operator_nothing_maps(srv):  # noqa: F811
    srv.single_operator = False
    assert _ctx(srv, "op").user == srv.p["op"]


def test_a_family_account_never_sees_the_operators_assistants(srv):  # noqa: F811
    srv.assistant.create_assistant(tenant_id=T, user_id=OP, name="Dashboard bot")
    assert "Dashboard bot" not in _names(srv, "fam")


def test_crossing_spaces_recomputes_the_owner_so_each_space_lists_its_own_bots(srv):  # noqa: F811
    ctx = _ctx(srv, "op")
    account = srv.rakazo._by_principal[srv.p["op"]]
    other = srv.rakazo._in_space(ctx, "s1")
    assert other.user == srv.p["op"] and other.space == "s1" and other.tenant.startswith("default") and other.tenant != "default"
    back = srv.rakazo._in_space(other, rb.sp.DEFAULT_SPACE_ID)
    assert back.user == OP and back.tenant == "default" and back.principal == account.principal_id


def test_approvals_record_the_phone_principal_as_the_actor(srv):  # noqa: F811
    import inspect
    src = inspect.getsource(rb.RakazoBridge)
    assert src.count("actor=ctx.principal or ctx.user") == 2
    assert "actor=ctx.user)" not in src
