# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Rakazo spaces on the mobile bridge: a space is a hard partition of one account's data (a derived assistant tenant).

The app selects the active space with the ``x-rakazo-space-id`` header on every request; no header means the default space,
which is the account's own tenant, so nothing that existed before spaces moved."""
import pytest

from herdr_engineering.control_plane import rakazo_spaces as sp
from tests.control_plane.test_rakazo_bridge import (  # noqa: F401
    ACCOUNTS,
    alice,
    make_server,
    new_bot,
    send,
    srv,
)
from tests.control_plane.test_rakazo_contract import validate

SPACE = "x-rakazo-space-id"


def rpc(api, proc, inp, token, space=None):
    return api.rpc(proc, inp, token, **({SPACE: space} if space else {}))


def ok(api, proc, inp, token, space=None):
    status, body, _ = rpc(api, proc, inp, token, space)
    assert status == 200, (proc, status, body)
    return body["json"]


def bot_in(api, token, space, name="Herdr"):
    return ok(api, "bots/create", {"name": name, "description": "", "instructions": "be brief", "notifyOnFinish": True,
                                   "computerMode": "team"}, token, space)


def names(api, token, space=None):
    return sorted(b["name"] for b in ok(api, "bots/list", {}, token, space))


# --------------------------------------------------------------------------------------------- listing and creating
def test_without_a_header_the_default_space_is_listed_alone_and_unchanged(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    listing = ok(api, "spaces/list", {}, alice)
    validate("spaces/list", listing)
    assert [s["id"] for s in listing["spaces"]] == [sp.DEFAULT_SPACE_ID]
    assert listing["current"]["id"] == sp.DEFAULT_SPACE_ID and listing["current"]["bots"][0]["id"] == bot["id"]
    only = listing["spaces"][0]
    assert only["isDefault"] is True and only["canRename"] is False and only["canDelete"] is False
    assert bot["spaceId"] == sp.DEFAULT_SPACE_ID and ok(api, "me", {}, alice)["spaceId"] == sp.DEFAULT_SPACE_ID


def test_create_a_space_and_see_it_in_the_list(srv, alice):
    _, api = srv
    created = ok(api, "spaces/create", {"name": "  Client   A "}, alice)
    validate("spaces/create", created)
    assert created["name"] == "Client A" and created["isDefault"] is False and created["canDelete"] is True
    assert created["id"].startswith("spc") and created["bots"] == [] and created["hasContent"] is False
    listing = ok(api, "spaces/list", {}, alice, created["id"])
    validate("spaces/list", listing)
    assert [s["id"] for s in listing["spaces"]] == [sp.DEFAULT_SPACE_ID, created["id"]]
    assert listing["current"]["id"] == created["id"]
    assert ok(api, "me", {}, alice, created["id"])["spaceId"] == created["id"]


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, 7, None, "bad\x07name"])
def test_a_space_name_is_validated(srv, alice, name):
    _, api = srv
    status, body, _ = rpc(api, "spaces/create", {"name": name}, alice)
    assert status == 400 and body["json"]["code"] == "BAD_REQUEST"


def test_names_are_unique_per_account_and_the_default_name_is_taken(srv, alice):
    _, api = srv
    ok(api, "spaces/create", {"name": "Home"}, alice)
    for name in ("home", "HOME", sp.DEFAULT_SPACE_NAME.lower()):
        assert rpc(api, "spaces/create", {"name": name}, alice)[0] == 400


def test_the_number_of_extra_spaces_is_bounded(srv, alice):
    _, api = srv
    for i in range(sp.MAX_SPACES):
        ok(api, "spaces/create", {"name": f"s{i}"}, alice)
    status, body, _ = rpc(api, "spaces/create", {"name": "one too many"}, alice)
    assert status == 400 and "at most" in body["json"]["message"]


# --------------------------------------------------------------------------------------------- the partition itself
def test_bots_are_invisible_across_spaces_in_both_directions(srv, alice):
    _, api = srv
    a, b = (ok(api, "spaces/create", {"name": n}, alice)["id"] for n in ("A", "B"))
    bot_in(api, alice, None, "in-default")
    bot_in(api, alice, a, "in-a")
    bot_in(api, alice, b, "in-b")
    assert names(api, alice) == ["in-default"]
    assert names(api, alice, a) == ["in-a"]
    assert names(api, alice, b) == ["in-b"]
    listing = ok(api, "spaces/list", {}, alice, a)
    by_id = {s["id"]: sorted(x["name"] for x in s["bots"]) for s in listing["spaces"]}
    assert by_id == {sp.DEFAULT_SPACE_ID: ["in-default"], a: ["in-a"], b: ["in-b"]}
    assert [x["name"] for x in listing["current"]["bots"]] == ["in-a"]
    validate("spaces/list", listing)


def test_a_bot_id_from_another_space_is_not_found_for_every_read_and_write(srv, alice):
    _, api = srv
    a = ok(api, "spaces/create", {"name": "A"}, alice)["id"]
    secret = bot_in(api, alice, a, "secret")
    for proc, inp in [("bots/get", {"botId": secret["id"]}), ("threads/get", {"botId": secret["id"]}),
                      ("threads/head", {"botId": secret["id"]}),
                      ("threads/send", {"botId": secret["id"], "text": "hi", "clientNonce": "n"}),
                      ("threads/messages", {"botId": secret["id"], "limit": 5}),
                      ("threads/stop", {"botId": secret["id"]}), ("threads/markRead", {"botId": secret["id"]})]:
        assert rpc(api, proc, inp, alice)[0] == 404, proc                 # default space
        assert ok(api, proc, inp, alice, a) is not None, proc            # its own space still works


def test_threads_and_runs_live_in_their_space(srv, alice):
    _, api = srv
    a = ok(api, "spaces/create", {"name": "A"}, alice)["id"]
    bot = bot_in(api, alice, a)
    assert api.rpc("threads/send", {"botId": bot["id"], "text": "in a", "clientNonce": "n1"}, alice,
                   **{SPACE: a})[0] == 200
    runs_a = ok(api, "runs/list", {"filter": "recent"}, alice, a)
    assert len(runs_a["runs"]) == 1 and runs_a["runs"][0]["botId"] == bot["id"]
    assert ok(api, "runs/list", {"filter": "recent"}, alice)["runs"] == []


def test_memory_and_audit_stay_in_the_tenant_of_the_space(srv, alice):
    svr, api = srv
    a = ok(api, "spaces/create", {"name": "A"}, alice)["id"]
    bot = bot_in(api, alice, a)
    account = svr.rakazo.accounts[0]
    base = svr.assistant.tenant_for(account.principal_id)
    inner = sp.space_tenant(base, a)
    assert inner != base and inner.startswith(base + "~")
    svr.assistant.store_memory(tenant_id=inner, user_id=account.principal_id, assistant_id=bot["id"],
                               content="the client codename is bluebird")
    found = svr.assistant.search_memory(tenant_id=inner, user_id=account.principal_id, assistant_id=bot["id"],
                                        query="bluebird")
    assert [m.content for m in found] == ["the client codename is bluebird"]
    # the account's own tenant (the default space) neither finds it nor can write to that bot
    assert svr.assistant.search_memory(tenant_id=base, user_id=account.principal_id, assistant_id=bot["id"],
                                       query="bluebird") == []
    with pytest.raises(KeyError):
        svr.assistant.store_memory(tenant_id=base, user_id=account.principal_id, assistant_id=bot["id"], content="x")
    # each space has its own audit chain
    assert svr.assistant.store.audit_head(inner) != svr.assistant.store.audit_head(base)


def test_an_unknown_or_foreign_or_malformed_space_id_is_not_found(srv, make_server):
    svr, api = srv
    alice_t, bob_t = api.login(), api.login("bob@example.com", "correct horse battery B")
    bobs = ok(api, "spaces/create", {"name": "Bobs"}, bob_t)["id"]
    for space in ("spcdeadbeef", bobs, "../../etc", "herdr2", "x" * 500):
        for proc in ("me", "bots/list", "spaces/list", "spaces/create"):
            assert rpc(api, proc, {"name": "n"} if proc == "spaces/create" else {}, alice_t, space)[0] == 404, \
                (proc, space)
    assert rpc(api, "bots/list", {}, bob_t, bobs)[0] == 200
    # and nothing leaked into a listing
    assert [s["id"] for s in ok(api, "spaces/list", {}, alice_t)["spaces"]] == [sp.DEFAULT_SPACE_ID]


def test_the_first_request_after_a_space_exists_is_not_confused_by_an_empty_header(srv, alice):
    _, api = srv
    ok(api, "spaces/create", {"name": "A"}, alice)
    assert rpc(api, "bots/list", {}, alice, " ")[0] == 200


# --------------------------------------------------------------------------------------------- rename, remove, overrides
def test_rename_and_the_default_space_is_fixed(srv, alice):
    _, api = srv
    a = ok(api, "spaces/create", {"name": "A"}, alice)["id"]
    renamed = ok(api, "spaces/rename", {"spaceId": a, "name": "Alpha"}, alice)
    validate("spaces/rename", renamed)
    assert renamed == {"id": a, "name": "Alpha"}
    assert rpc(api, "spaces/rename", {"spaceId": sp.DEFAULT_SPACE_ID, "name": "X"}, alice)[0] == 400
    assert rpc(api, "spaces/rename", {"spaceId": "spcnope", "name": "X"}, alice)[0] == 404
    ok(api, "spaces/create", {"name": "B"}, alice)
    assert rpc(api, "spaces/rename", {"spaceId": a, "name": "b"}, alice)[0] == 400


def test_removing_a_space_archives_its_bots_and_deletes_nothing(srv, alice):
    svr, api = srv
    a = ok(api, "spaces/create", {"name": "A"}, alice)["id"]
    keep = bot_in(api, alice, None, "keep")
    bot = bot_in(api, alice, a, "doomed")
    out = ok(api, "spaces/remove", {"spaceId": a}, alice)
    validate("spaces/remove", out)
    assert out == {"ok": True, "activeSpaceId": sp.DEFAULT_SPACE_ID}
    account = svr.rakazo.accounts[0]
    inner = sp.space_tenant(svr.assistant.tenant_for(account.principal_id), a)
    [kept] = svr.assistant.list_assistants(tenant_id=inner, user_id=account.principal_id)
    assert kept.assistant_id == bot["id"] and kept.enabled is False          # archived, still on record
    assert "assistant.archived" in [e["action"] for e in svr.assistant.store.audit]
    assert rpc(api, "bots/list", {}, alice, a)[0] == 404                      # the space is gone for the app
    assert names(api, alice) == ["keep"] and ok(api, "bots/get", {"botId": keep["id"]}, alice)
    assert [s["id"] for s in ok(api, "spaces/list", {}, alice)["spaces"]] == [sp.DEFAULT_SPACE_ID]
    assert rpc(api, "spaces/remove", {"spaceId": a}, alice)[0] == 404         # already gone


def test_the_default_space_cannot_be_removed(srv, alice):
    _, api = srv
    status, body, _ = rpc(api, "spaces/remove", {"spaceId": sp.DEFAULT_SPACE_ID}, alice)
    assert status == 400 and "default" in body["json"]["message"]


def test_a_removed_space_id_is_never_handed_out_again(srv, alice):
    _, api = srv
    seen = set()
    for _ in range(5):
        sid = ok(api, "spaces/create", {"name": "again"}, alice)["id"]
        assert sid not in seen
        seen.add(sid)
        ok(api, "spaces/remove", {"spaceId": sid}, alice)


def test_settings_overrides_round_trip_and_only_for_extra_spaces(srv, alice):
    _, api = srv
    a = ok(api, "spaces/create", {"name": "A"}, alice)["id"]
    assert ok(api, "spaceSettings/overrides", {}, alice, a) == []
    on = ok(api, "spaceSettings/setOverride", {"setting": "models", "enabled": True}, alice, a)
    validate("spaceSettings/setOverride", on)
    assert on == ["models"]
    assert ok(api, "spaceSettings/setOverride", {"setting": "voice", "enabled": True}, alice, a) == ["models", "voice"]
    off = ok(api, "spaceSettings/setOverride", {"setting": "models", "enabled": False}, alice, a)
    assert off == ["voice"] and ok(api, "spaceSettings/overrides", {}, alice, a) == ["voice"]
    validate("spaceSettings/overrides", ok(api, "spaceSettings/overrides", {}, alice, a))
    assert rpc(api, "spaceSettings/setOverride", {"setting": "bogus", "enabled": True}, alice, a)[0] == 400
    assert rpc(api, "spaceSettings/setOverride", {"setting": "models", "enabled": "yes"}, alice, a)[0] == 400
    assert rpc(api, "spaceSettings/setOverride", {"setting": "models", "enabled": True}, alice)[0] == 400   # default
    assert ok(api, "spaceSettings/overrides", {}, alice) == []


# --------------------------------------------------------------------------------------------- the registry itself
class FakeTable:
    def __init__(self):
        self.rows, self.fail = {}, False

    def load(self):
        return [dict(r) for r in self.rows.values()]

    def upsert(self, row):
        if self.fail:
            raise OSError("db down")
        self.rows[row["space_id"]] = dict(row)


def test_spaces_survive_a_restart_through_the_table():
    table = FakeTable()
    first = sp.SpaceRegistry(table)
    a = first.create("acct1", "A")
    b = first.create("acct1", "B")
    first.set_override("acct1", a.space_id, "voice", True)
    first.remove("acct1", b.space_id)
    again = sp.SpaceRegistry(table)
    assert [s.name for s in again.list("acct1")] == ["A"] and again.list("acct1")[0].overrides == {"voice"}
    assert again.get("acct1", b.space_id) is None and again.get("acct2", a.space_id) is None


def test_a_failed_table_write_changes_nothing_and_says_so():
    table = FakeTable()
    reg = sp.SpaceRegistry(table)
    a = reg.create("acct1", "A")
    table.fail = True
    for call in (lambda: reg.create("acct1", "B"), lambda: reg.rename("acct1", a.space_id, "Z"),
                 lambda: reg.remove("acct1", a.space_id), lambda: reg.set_override("acct1", a.space_id, "voice", True)):
        with pytest.raises(sp.SpaceError) as err:
            call()
        assert err.value.status == 503 and "db down" not in err.value.message
    assert [s.name for s in reg.list("acct1")] == ["A"] and reg.get("acct1", a.space_id).overrides == frozenset()


def test_an_unreadable_table_does_not_stop_the_bridge_starting():
    class Broken:
        def load(self):
            raise OSError("no table")

        def upsert(self, row):
            raise OSError("no table")

    assert sp.SpaceRegistry(Broken()).list("acct1") == []


def test_the_default_space_is_the_accounts_own_tenant():
    assert sp.space_tenant("default", sp.DEFAULT_SPACE_ID) == "default"
    assert sp.space_tenant("team-a", "spc0123") == "team-a~spc0123"
    assert sp.space_tenant("a", "spc1") != sp.space_tenant("a~spc1", "spc1")[:3]
