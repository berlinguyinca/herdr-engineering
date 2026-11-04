# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""The optional Hindsight link: app edits follow into the shared fact bank, and search adds semantic recall."""
import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane import rakazo_hindsight as hs
from tests.control_plane.test_rakazo_bridge import (  # noqa: F401
    alice,
    bridge_section,
    make_server,
    new_bot,
    srv,
)
from tests.control_plane.test_rakazo_contract import validate


class FakeClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def retain(self, content, *, kind, document_id=None, explicit=False, evidence=()):
        self.calls.append(("retain", document_id, content, kind, explicit))
        if self.fail:
            raise OSError("down")

    def forget(self, document_id, *, kind="fact"):
        self.calls.append(("forget", document_id, kind))

    def recall(self, query, *, kind="fact"):
        if self.fail:
            raise OSError("down")
        return {"offline": False, "data": {"results": [{"text": "Alice prefers tea"}, {"content": "lives in Davis"}, {}]}}


def seed(svr, bot):
    a = svr.rakazo.svc.store.assistants[bot["id"]]
    return svr.rakazo.svc.store_memory(tenant_id=a.tenant_id, user_id=a.user_id, assistant_id=bot["id"], content="likes tea")


def test_edits_and_removals_follow_into_the_fact_bank(srv, alice):
    svr, api = srv
    client = FakeClient()
    svr.rakazo.hindsight = hs.Mirror(client)
    bot = new_bot(api, alice)
    m = seed(svr, bot)
    api.ok("memory/update", {"documentId": m.memory_id, "content": "likes coffee"}, alice)
    api.ok("memory/remove", {"documentId": m.memory_id}, alice)
    svr.rakazo.hindsight.wait()
    assert client.calls == [("retain", m.memory_id, "likes coffee", "fact", True), ("forget", m.memory_id, "fact")]


def test_search_adds_semantic_recall_and_survives_an_outage(srv, alice):
    svr, api = srv
    svr.rakazo.hindsight = hs.Mirror(FakeClient())
    out = api.ok("search/query", {"q": "tea"}, alice)
    validate("search/query", out)
    assert [h["snippet"] for h in out["hits"]] == ["Alice prefers tea", "lives in Davis"]
    svr.rakazo.hindsight = hs.Mirror(FakeClient(fail=True))
    assert api.ok("search/query", {"q": "tea"}, alice)["hits"] == []
    bot = new_bot(api, alice)
    m = seed(svr, bot)
    api.ok("memory/update", {"documentId": m.memory_id, "content": "x"}, alice)      # a failing write never fails the app
    svr.rakazo.hindsight.wait()


def test_without_the_link_nothing_changes(srv, alice):
    assert srv[0].rakazo.hindsight is None
    assert srv[1].ok("search/query", {"q": "tea"}, alice) == {"hits": []}


def test_the_configuration_is_validated():
    section = bridge_section()
    with pytest.raises(ValueError, match="hindsight"):
        rb.rakazo_bridge_config({**section, "hindsight": {"url": "http://x"}})
    good = {"url": "http://x", "token_file": "/t", "project": "p", "cache_dir": "/c"}
    assert rb.rakazo_bridge_config({**section, "hindsight": good})["hindsight"] == good
