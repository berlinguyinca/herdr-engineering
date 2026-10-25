# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""`#` issue references in rooms (Task C4): the grammar, the store's read-only search, the
`/github/resolve` route, server-derived `refs` on messages and the per-viewer view of them."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane import collab_refs
from herdr_engineering.control_plane.github_store import (
    MemoryGitHubStore,
    parse_issue_query,
    ref_insert,
)
from herdr_engineering.github import GitHubMonitor
from tests.control_plane.test_collab_tasks import USERS, W, code, svc  # noqa: F401
from tests.test_github_incremental import FakeGitHub, _iso
from tests.test_github_store import backend, issue, repo  # noqa: F401

# ---------------------------------------------------------------- grammar

def test_short_and_long_forms_are_references():
    text = "fixes #binview-44, see (acme/lib#3) and #my-repo-2-7."
    assert list(collab_refs.tokens(text)) == [(None, "binview", 44), ("acme", "lib", 3), (None, "my-repo-2", 7)]
    assert list(collab_refs.tokens("#BinView-44")) == [(None, "binview", 44)]
    assert list(collab_refs.tokens('"#app-1"')) == [(None, "app", 1)]


@pytest.mark.parametrize("text", [
    "`#binview-44`", "```\n#binview-44\n```", "~~~\n#binview-44\n~~~", "~~~py\n#binview-44",
    "abc#12", "abc#app-12", "&#39;", "&#app-39;", "# Title", "## app-1", "#app", "#44",
    "https://github.com/acme/app/issues#app-1", "see https://x.org/#app-1", "www.x.org/acme/app#4",
    "github.com/acme/app#4", "x/#app-1", "#app-1x", "#app-1.5", "acme/app#4x", "#app-1#2",
])
def test_code_urls_words_entities_and_headings_are_not_references(text):
    assert list(collab_refs.tokens(text)) == []


def test_the_reference_grammar_is_linear_on_adversarial_input():
    import time
    for text in ("#" + "a-" * 4000, "a/" * 4000 + "#1", "#" + "a" * 8000 + "-x", ("#a-1" * 2000)):
        started = time.perf_counter()
        list(collab_refs.tokens(text))
        assert time.perf_counter() - started < 1.0


def test_query_parsing_and_the_shortest_unambiguous_insert():
    assert parse_issue_query("#binv") == {"owner": None, "repo": "binv", "exact": False, "number": "", "short": None}
    assert parse_issue_query("binview-4")["short"] == ("binview", "4")
    assert parse_issue_query("acme/binview#4") == {"owner": "acme", "repo": "binview", "exact": True,
                                                   "number": "4", "short": None}
    assert parse_issue_query("a b") is None and parse_issue_query("x" * 200) is None
    assert ref_insert("acme", "app", 4, set()) == "#app-4"
    assert ref_insert("acme", "app", 4, {"app"}) == "acme/app#4"


# ---------------------------------------------------------------- store search (memory, and Postgres when set)

def _seed(store):
    store.load()
    store.upsert_repositories([repo("acme/app", private=True), repo("acme/lib"), repo("other/app"),
                               repo("excluded/tool")], None)
    store.upsert_issues("github.com/acme/app", [issue("a4", 4, state="OPEN", url="https://github.com/acme/app/issues/4"),
                                                issue("a44", 44, state="OPEN", updated="2026-10-24T05:00:00Z"),
                                                issue("a40", 40, state="OPEN", updated="2026-10-24T06:00:00Z")], None)
    store.upsert_issues("github.com/other/app", [issue("o4", 4, state="OPEN")], None)
    store.upsert_issues("github.com/acme/lib", [issue("l9", 9, state="OPEN",
                                                      url="https://github.com/acme/lib/pull/9")], None)
    store.upsert_issues("github.com/excluded/tool", [issue("t1", 1, state="OPEN")], None)


def test_search_is_read_only_scoped_bounded_and_ranked(backend):
    store = backend.make()
    _seed(store)
    revision = store.revision()
    found = store.search_issues("app-4", {"acme", "other"})
    assert [(r["owner"], r["repo"], r["number"]) for r in found][:2] in (
        [("acme", "app", 4), ("other", "app", 4)], [("other", "app", 4), ("acme", "app", 4)])
    assert [(r["repo"], r["number"]) for r in found[2:]] == [("app", 40), ("app", 44)]   # prefixes, newest first
    assert {r["insert"] for r in found} >= {"acme/app#4", "other/app#4"}                # a shared name needs its owner
    assert store.search_issues("app-4", {"acme"})[0]["insert"] == "#app-4"              # not shared within scope
    assert store.search_issues("tool", {"acme", "other"}) == []                          # excluded owner: never listed
    assert [r["number"] for r in store.search_issues("tool", None)] == [1]               # None = every owner
    lib = store.search_issues("acme/lib#9", {"acme"})
    assert lib[0]["kind"] == "pr" and lib[0]["insert"] == "#lib-9"
    assert set(lib[0]) == {"kind", "owner", "repo", "number", "title", "state", "url", "repository_id",
                           "private", "insert"}
    assert len(store.search_issues("", None, limit=500)) <= 20
    assert store.search_issues("a b", None) == []
    assert store.revision() == revision                                                  # reading moved nothing


def test_search_refuses_a_catalog_of_another_credential(backend):
    from herdr_engineering.control_plane.github_store import GitHubStoreIdentityMismatch
    _seed(backend.make())
    with pytest.raises(GitHubStoreIdentityMismatch):
        backend.make(identity="tok-b").search_issues("app", None)


# ---------------------------------------------------------------- the monitor and the room

class Fake(FakeGitHub):
    def __init__(self):
        super().__init__(page_size=50)
        self.private = set()

    def put(self, name, number, *, state="OPEN", title=None):
        gid = f"I_{name}_{number}"
        self.repos.setdefault(name, {})[gid] = {
            "id": gid, "number": number, "title": title or f"{name} {number}",
            "url": f"https://github.com/{name}/issues/{number}", "state": state, "updatedAt": _iso(self.tick()),
            "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}}, "assignees": {"nodes": []}}
        return gid

    def repositories_page(self, cursor=None):
        login, conn = super().repositories_page(cursor)
        for node in conn["nodes"]:
            node["isPrivate"] = node["nameWithOwner"] in self.private
        return login, conn


def monitor_for(fake, organizations=("acme", "other")):
    shared = {}
    monitor = GitHubMonitor(provider_factory=lambda: fake, organizations=list(organizations),
                            store=lambda identity, viewer=None: MemoryGitHubStore(identity=identity, viewer=viewer,
                                                                                  shared=shared))
    monitor.start = lambda: None  # the test drives passes itself
    return monitor


@pytest.fixture
def gh():
    fake = Fake()
    fake.put("acme/binview", 44, title="Fix the <b>chart</b>")
    fake.put("acme/secret", 7, title="Private plans")
    fake.private.add("acme/secret")
    fake.put("acme/app", 3)
    fake.put("other/app", 3)
    fake.put("excluded/binview", 45, title="out of scope")
    monitor = monitor_for(fake)
    monitor.refresh()
    yield fake, monitor
    monitor.close()


@pytest.fixture
def w(svc, gh):
    room = W(svc)
    svc.github = gh[1]
    return room


def say(w, who, text, **extra):
    status, out = w.s.post_message(who, w.room, {"text": text, "idempotency_key": f"k{abs(hash(text)) % 10**9}",
                                                 **extra})
    assert status == 201
    return out["event"]


def stored(w, ev):
    return w.s._log[ev["sequence"] - 1]["payload"].get("refs")


def test_an_in_scope_issue_resolves_and_its_chip_reads_title_and_state_now(w, gh):
    ev = say(w, w.bob, "fixes #binview-44 and acme/app#3")
    assert stored(w, ev) == [{"kind": "issue", "owner": "acme", "repo": "binview", "number": 44, "forms": ["short"]},
                             {"kind": "issue", "owner": "acme", "repo": "app", "number": 3, "forms": ["long"]}]
    assert ev["refs"][0] == {"kind": "issue", "owner": "acme", "repo": "binview", "number": 44, "forms": ["short"],
                             "title": "Fix the <b>chart</b>", "state": "open",
                             "url": "https://github.com/acme/binview/issues/44"}
    fake, monitor = gh
    fake.edit("acme/binview", "I_acme/binview_44", title="Renamed")          # title read at view time, not stored
    monitor.refresh()
    seen = [e for e in w.s.read_events(w.alice, w.room, 0, 50)["events"] if e["event_type"] == "message"][-1]
    assert seen["refs"][0]["title"] == "Renamed"
    assert "title" not in stored(w, ev)[0]


def test_an_excluded_owners_issue_is_neither_listed_nor_resolved(w, gh):
    ev = say(w, w.bob, "see #binview-45 and excluded/binview#45")
    assert stored(w, ev) is None and "refs" not in ev
    menu = w.s.search_refs(w.bob, w.room, "binview")
    assert [(i["owner"], i["number"]) for i in menu["items"]] == [("acme", 44)]
    assert gh[1].search_issues("excluded/binview#45")["items"] == []


def test_two_owners_with_one_repo_name_need_owner_slash_repo(w):
    menu = w.s.search_refs(w.bob, w.room, "app-3")
    assert {i["insert"] for i in menu["items"]} == {"acme/app#3", "other/app#3"}
    ev = say(w, w.bob, "ambiguous #app-3 but other/app#3 is clear")
    assert stored(w, ev) == [{"kind": "issue", "owner": "other", "repo": "app", "number": 3, "forms": ["long"]}]
    assert w.s.search_refs(w.bob, w.room, "binview-4")["items"][0]["insert"] == "#binview-44"


def test_a_closed_issue_shows_closed(w, gh):
    fake, monitor = gh
    ev = say(w, w.bob, "tracking #binview-44")
    fake.edit("acme/binview", "I_acme/binview_44", state="CLOSED")
    monitor.refresh()
    seen = [e for e in w.s.read_events(w.bob, w.room, 0, 50)["events"] if e["event_id"] == ev["event_id"]][0]
    assert seen["refs"][0]["state"] == "closed"
    assert stored(w, ev)[0] == {"kind": "issue", "owner": "acme", "repo": "binview", "number": 44, "forms": ["short"]}


def test_cache_not_ready_still_sends_and_the_menu_says_so(svc):
    room = W(svc)
    fake = Fake()
    fake.available = False                           # no token: nothing cached
    svc.github = monitor_for(fake)
    assert svc.search_refs(room.bob, room.room, "binview") == {"ready": False, "items": []}
    ev = say(room, room.bob, "fixes #binview-44")
    assert ev["text"] == "fixes #binview-44" and "refs" not in ev
    svc.github = None                                 # no GitHub at all
    assert svc.search_refs(room.bob, room.room, "x") == {"ready": False, "items": []}
    assert "refs" not in say(room, room.bob, "again #binview-44")


def test_a_client_cannot_forge_refs(w):
    ev = say(w, w.bob, "no refs here", refs=[{"kind": "issue", "owner": "evil", "repo": "x", "number": 1}])
    assert stored(w, ev) is None and "refs" not in ev
    assert w.s.stats["derived_fields_dropped"] >= 1


def test_refs_are_bounded_and_deduplicated(w, gh):
    fake, monitor = gh
    for n in range(100, 140):
        fake.put("acme/binview", n)
    monitor.refresh()
    ev = say(w, w.bob, " ".join(f"#binview-{n}" for n in range(100, 140)) + " #binview-44 #binview-44")
    assert len(stored(w, ev)) == collab_refs.MAX_REFS
    ev = say(w, w.bob, "#binview-44 acme/binview#44 (#binview-44)")
    assert len(stored(w, ev)) == 1 and stored(w, ev)[0]["forms"] == ["short", "long"]   # which text forms named it


def test_guests_and_external_agents_never_see_private_titles(w):
    from tests.control_plane.test_collab_external import enroll
    ext_pr, _, _ = enroll(w)
    say(w, w.bob, "#secret-7 and #binview-44")
    member = [e for e in w.s.read_events(w.carol, w.room, 0, 50)["events"] if e["event_type"] == "message"][-1]
    assert member["refs"][0]["title"] == "Private plans"
    for viewer in (w.dave, ext_pr):                                    # a guest and an external agent
        msg = [e for e in w.s.read_events(viewer, w.room, 0, 50)["events"] if e["event_type"] == "message"][-1]
        secret, public = msg["refs"]
        assert secret == {"kind": "issue", "owner": "acme", "repo": "secret", "number": 7, "forms": ["short"]}
        assert "Private plans" not in repr(msg["refs"])
        assert public["title"] == "Fix the <b>chart</b>" and public["state"] == "open"
    assert [i["repo"] for i in w.s.search_refs(w.carol, w.room, "secret")["items"]] == ["secret"]
    assert w.s.search_refs(w.dave, w.room, "secret")["items"] == []
    assert w.s.search_refs(w.dave, w.room, "binview")["items"][0]["title"] == "Fix the <b>chart</b>"


def test_the_menu_is_for_room_members_only(w):
    assert code(w.s.search_refs, w.mallory, w.room, "binview") == 404


def test_a_rotated_token_serves_nothing_cached_under_the_old_one(w, gh):
    fake, monitor = gh
    ev = say(w, w.bob, "#binview-44")
    fake.credential_identity = "fingerprint-two"
    seen = [e for e in w.s.read_events(w.bob, w.room, 0, 50)["events"] if e["event_id"] == ev["event_id"]][0]
    assert seen["refs"] == [{"kind": "issue", "owner": "acme", "repo": "binview", "number": 44, "forms": ["short"]}]
    assert "refs" not in say(w, w.bob, "now #binview-44 again")
    assert w.s.search_refs(w.bob, w.room, "binview") == {"ready": False, "items": []}


# ---------------------------------------------------------------- HTTP

def _get(port, path, headers=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null"), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null"), exc.headers


def test_resolve_route_lists_in_scope_candidates_with_an_etag(gh, tmp_path):
    import threading

    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"view-key": "viewer"}, github_monitor=gh[1])
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        status, body, _ = _get(server.port, "/api/v1/github/resolve?q=binview")
        assert status == 401                                              # auth as every GitHub route
        key = {"X-API-Key": "view-key"}
        status, body, headers = _get(server.port, "/api/v1/github/resolve?q=binview", key)
        assert status == 200 and body["ready"] is True
        assert [(i["owner"], i["number"], i["insert"]) for i in body["items"]] == [("acme", 44, "#binview-44")]
        status, _, _ = _get(server.port, "/api/v1/github/resolve?q=binview", {**key, "If-None-Match": headers["ETag"]})
        assert status == 304
        assert _get(server.port, "/api/v1/github/resolve?q=x&q=y", key)[0] == 400
        assert _get(server.port, "/api/v1/github/resolve?other=1", key)[0] == 400
        assert _get(server.port, "/api/v1/github/resolve?q=" + "a" * 161, key)[0] == 400
    finally:
        server.shutdown()
        thread.join(timeout=3)
