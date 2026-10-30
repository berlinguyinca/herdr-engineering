# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Rakazo memory, search and usage over the assistant store."""
from tests.control_plane.test_rakazo_bridge import (  # noqa: F401
    alice,
    make_server,
    new_bot,
    send,
    srv,
)
from tests.control_plane.test_rakazo_contract import validate


def rpc(api, proc, inp, token):
    status, body, _ = api.rpc(proc, inp, token)
    return status


def test_memory_list_update_export_and_hide(srv, alice):
    svr, api = srv
    bot = new_bot(api, alice)
    store = svr.rakazo.svc.store
    assistant = store.assistants[bot["id"]]
    tenant, ctx_user = assistant.tenant_id, assistant.user_id
    m = svr.rakazo.svc.store_memory(tenant_id=tenant, user_id=ctx_user, assistant_id=bot["id"], content="likes tea")
    u = svr.rakazo.svc.store_memory(tenant_id=tenant, user_id=ctx_user, assistant_id=bot["id"], content="lives in Davis",
                                    namespace="shared")
    listing = api.ok("memory/list", {"botId": bot["id"]}, alice)
    validate("memory/list", listing)
    assert {r["content"] for r in listing} == {"likes tea", "lives in Davis"}
    assert [r["content"] for r in api.ok("memory/list", {"scope": "user"}, alice)] == ["lives in Davis"]
    before = m.revision
    out = api.ok("memory/update", {"documentId": m.memory_id, "content": "likes coffee", "expectedRevision": before}, alice)
    validate("memory/update", out)
    assert out["content"] == "likes coffee" and out["revision"] > before
    assert rpc(api, "memory/update", {"documentId": m.memory_id, "content": "x", "expectedRevision": 1}, alice) == 409
    md = api.ok("memory/exportMarkdown", {"botId": bot["id"]}, alice)
    validate("memory/exportMarkdown", md)
    assert "likes coffee" in md
    validate("memory/remove", api.ok("memory/remove", {"documentId": u.memory_id}, alice))
    assert [r["content"] for r in api.ok("memory/list", {}, alice)] == ["likes coffee"]
    assert u.memory_id in store.memory                       # hidden from the app, never deleted
    assert rpc(api, "memory/update", {"documentId": u.memory_id, "content": "x"}, alice) == 404


def test_memory_provider_is_the_servers_own(srv, alice):
    _, api = srv
    validate("memory/providerConfig", api.ok("memory/providerConfig", {}, alice))
    assert rpc(api, "memory/connectProvider", {"provider": "mem0", "settings": {}, "credentials": {"k": "v"},
                                               "defaultMemoryScope": "isolated"}, alice) == 400
    cfg = api.ok("memory/connectProvider", {"provider": "herdr", "settings": {}, "credentials": {},
                                            "defaultMemoryScope": "shared"}, alice)
    validate("memory/connectProvider", cfg)
    validate("memory/setDefaultScope", api.ok("memory/setDefaultScope", {"defaultMemoryScope": "isolated"}, alice))
    assert api.ok("memory/providerConfig", {}, alice)["defaultMemoryScope"] == "isolated"
    validate("memory/disconnectProvider", api.ok("memory/disconnectProvider", {}, alice))


def test_search_finds_bots_and_messages(srv, alice):
    _, api = srv
    bot = new_bot(api, alice, "Scout")
    send(api, alice, bot["id"], "remember the zebra crossing")
    found = api.ok("search/query", {"q": "zebra"}, alice)
    validate("search/query", found)
    assert any(h["kind"] == "message" and h["botId"] == bot["id"] for h in found["hits"])
    assert any(h["kind"] == "conversation" for h in api.ok("search/query", {"q": "scout"}, alice)["hits"])
    assert api.ok("search/query", {"q": ""}, alice)["hits"] == []


def test_usage_counts_runs_and_reports_unknown_tokens(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    send(api, alice, bot["id"], "hi")
    summary = api.ok("usage/summary", {}, alice)
    validate("usage/summary", summary)
    assert summary["runs"] == 1 and summary["inputTokens"] is None
    rows = api.ok("usage/list", {}, alice)
    validate("usage/list", rows)
    assert rows[0]["botId"] == bot["id"]
