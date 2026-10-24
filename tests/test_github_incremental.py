"""Incremental GitHub sync from a persistent cache (Tasks G2-G4).

A fake GitHub models repositories and issues with ``updatedAt`` and counts every
GraphQL call by kind, so each test can assert exactly what a pass cost. The
store is the in-memory ``MemoryGitHubStore`` (same interface as Postgres,
which ``tests/test_github_store.py`` covers); a shared dict stands in for the
database across monitor restarts.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane.github_store import MemoryGitHubStore
from herdr_engineering.github import GitHubMonitor

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """A tiny GitHub: repos -> issues with state and updatedAt, paged newest first."""

    available = True
    credential_identity = "fingerprint-one"
    rate_limit = {"remaining": 4000, "cost": 1, "resetAt": "2026-10-24T15:00:00Z"}

    def __init__(self, *, page_size=2, login="fixture-user"):
        self.page_size = page_size
        self.login = login
        self.repos = {}           # "owner/name" -> {gid: issue dict}
        self.calls = Counter()    # kind -> count
        self.log = []             # (kind, repository_id, cursor)
        self.clock = T0
        self._next = 0

    # -- fixture helpers
    def tick(self, seconds=60):
        self.clock += timedelta(seconds=seconds)
        return self.clock

    def add_repo(self, name):
        self.repos.setdefault(name, {})

    def add_issue(self, name, *, state="OPEN", labels=()):
        self._next += 1
        gid = f"I_{self._next}"
        self.repos.setdefault(name, {})[gid] = {"id": gid, "number": self._next, "title": f"Issue {self._next}",
                                                "url": f"https://github.com/{name}/issues/{self._next}",
                                                "state": state, "updatedAt": _iso(self.tick()),
                                                "labels": {"nodes": [{"name": n} for n in labels],
                                                           "pageInfo": {"hasNextPage": False}},
                                                "assignees": {"nodes": []}}
        return gid

    def edit(self, name, gid, **fields):
        self.repos[name][gid].update(fields, updatedAt=_iso(self.tick()))

    def delete(self, name, gid):
        del self.repos[name][gid]

    # -- provider interface
    def _connection(self, nodes, cursor):
        start = int(cursor or 0)
        page = nodes[start:start + self.page_size]
        more = start + self.page_size < len(nodes)
        return {"nodes": page, "totalCount": len(nodes),
                "pageInfo": {"hasNextPage": more, "endCursor": str(start + self.page_size) if more else None}}

    def repositories_page(self, cursor=None):
        self.calls["repos"] += 1
        self.log.append(("repos", None, cursor))
        nodes = []
        for name in sorted(self.repos):
            issues = self.repos[name].values()
            newest = max((i["updatedAt"] for i in issues), default=None)
            nodes.append({"id": f"R_{name}", "nameWithOwner": name, "url": f"https://github.com/{name}",
                          "isPrivate": False, "isArchived": False, "hasIssuesEnabled": True,
                          "viewerPermission": "WRITE", "primaryLanguage": None,
                          "issues": {"totalCount": sum(i["state"] == "OPEN" for i in issues)},
                          "newest": {"nodes": [{"updatedAt": newest}] if newest else []}})
        return self.login, self._connection(nodes, cursor)

    def _sorted(self, repository_id, state):
        name = repository_id.removeprefix("github.com/")
        rows = [dict(i) for i in self.repos.get(name, {}).values() if i["state"] == state]
        return sorted(rows, key=lambda i: i["updatedAt"], reverse=True)

    def issues_page(self, repository_id, cursor=None):
        self.calls["issues"] += 1
        self.log.append(("issues", repository_id, cursor))
        return self._connection(self._sorted(repository_id, "OPEN"), cursor)

    def closed_issues_page(self, repository_id, since, cursor=None):
        self.calls["closed"] += 1
        self.log.append(("closed", repository_id, cursor))
        rows = [i for i in self._sorted(repository_id, "CLOSED") if i["updatedAt"] >= since]
        return self._connection(rows, cursor)


def store_factory(shared):
    return lambda identity, viewer=None: MemoryGitHubStore(identity=identity, viewer=viewer, shared=shared)


def monitor_for(fake, shared, **kwargs):
    return GitHubMonitor(provider_factory=lambda: fake, store=store_factory(shared), **kwargs)


def issue_calls(fake, repository_id=None):
    return [entry for entry in fake.log if entry[0] == "issues" and (repository_id is None or entry[1] == repository_id)]


def numbers(monitor, repository_id=None):
    return sorted(row["number"] for row in monitor.issues(repository_id=repository_id, per_page=100, start=False)["issues"])


def seeded():
    fake = FakeGitHub()
    for _ in range(5):
        fake.add_issue("acme/app")
    for _ in range(3):
        fake.add_issue("acme/lib")
    fake.add_repo("acme/empty")
    return fake


# ---------------------------------------------------------------- G2


def test_unchanged_catalog_costs_only_listing_calls():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    assert monitor.snapshot(start=False)["status"] == "ready"
    assert numbers(monitor) == [1, 2, 3, 4, 5, 6, 7, 8]
    revision = MemoryGitHubStore(identity=fake.credential_identity, shared=shared).revision()
    fake.calls.clear()
    monitor.refresh()
    assert fake.calls == Counter({"repos": 2})  # 3 repos at 2 per page
    assert MemoryGitHubStore(identity=fake.credential_identity, shared=shared).revision() == revision
    assert monitor.snapshot(start=False)["status"] == "ready"


def test_one_edited_issue_costs_only_that_repository_pages_up_to_the_watermark():
    fake = FakeGitHub()
    gids = [fake.add_issue("acme/app") for _ in range(7)]
    fake.add_issue("acme/lib")
    monitor = monitor_for(fake, {})
    monitor.refresh()
    fake.tick(3600)
    fake.edit("acme/app", gids[0], title="Edited title")
    fake.log.clear()
    monitor.refresh()
    # newest first, 2 per page: [edit, #7 (= watermark)], then [#6, #5] is below watermark - 10 s
    assert issue_calls(fake) == [("issues", "github.com/acme/app", None), ("issues", "github.com/acme/app", "2")]
    titles = {row["number"]: row["title"] for row in monitor.issues(per_page=100, start=False)["issues"]}
    assert titles[1] == "Edited title"
    assert numbers(monitor) == list(range(1, 9))


def test_watermark_overlap_rereads_issues_within_ten_seconds():
    fake = FakeGitHub(page_size=1)
    gids = [fake.add_issue("acme/app") for _ in range(4)]
    monitor = monitor_for(fake, {})
    monitor.refresh()
    # GitHub's updatedAt can trail an event by seconds, so edits stamped just
    # *before* the stored watermark must still be read: the read stops only
    # below watermark - 10 s, not at the first issue that is not newer.
    watermark = datetime.fromisoformat(fake.repos["acme/app"][gids[3]]["updatedAt"].replace("Z", "+00:00"))
    fake.repos["acme/app"][gids[2]].update(updatedAt=_iso(watermark - timedelta(seconds=1)))
    fake.repos["acme/app"][gids[1]].update(title="Late edit", updatedAt=_iso(watermark - timedelta(seconds=3)))
    fake.repos["acme/app"][gids[3]]["updatedAt"] = _iso(watermark + timedelta(seconds=1))
    fake.log.clear()
    monitor.refresh()
    titles = {row["number"]: row["title"] for row in monitor.issues(per_page=100, start=False)["issues"]}
    assert titles[2] == "Late edit"
    assert len(issue_calls(fake)) == 4  # #4, #3, #2 (overlap), #1 (below the overlap: stop)


def test_new_issue_appears_after_an_incremental_pass():
    fake = seeded()
    monitor = monitor_for(fake, {})
    monitor.refresh()
    fake.tick(600)
    fake.add_issue("acme/lib", labels=["auto-implement"])
    fake.log.clear()
    monitor.refresh()
    assert {entry[1] for entry in issue_calls(fake)} == {"github.com/acme/lib"}
    assert numbers(monitor, "github.com/acme/lib") == [6, 7, 8, 9]
    repo = [r for r in monitor.repositories(start=False)["repositories"] if r["repository_id"] == "github.com/acme/lib"][0]
    assert repo["open_issue_count"] == 4 and repo["issues_loaded"] == 4


def test_restart_serves_the_stored_catalog_as_stale_with_zero_calls():
    fake, shared = seeded(), {}
    first = monitor_for(fake, shared)
    first.refresh()
    expected = first.issues(per_page=100, start=False)["issues"]
    fake.calls.clear()
    second = monitor_for(fake, shared)
    view = second.issues(per_page=100, start=False)
    assert fake.calls == Counter()
    assert view["stale"] is True and view["status"] == "stale"
    assert [row["github_id"] for row in view["issues"]] == [row["github_id"] for row in expected]
    assert second.repositories(start=False)["pagination"]["total"] == 3
    second.refresh()
    assert fake.calls == Counter({"repos": 2})  # nothing changed: the restart re-reads no issues
    assert second.snapshot(start=False)["stale"] is False


def test_failed_issue_sync_does_not_mark_the_repository_synced():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    fake.tick(600)
    fake.add_issue("acme/app")
    original = fake.issues_page

    def broken(repository_id, cursor=None):
        from herdr_engineering.github import GitHubError
        raise GitHubError("unavailable")
    fake.issues_page = broken
    monitor.refresh()
    assert monitor.snapshot(start=False)["status"] == "partial"
    fake.issues_page = original
    restarted = monitor_for(fake, shared)
    restarted.refresh()
    assert 9 in numbers(restarted, "github.com/acme/app")


def test_viewer_change_purges_the_catalog_loaded_by_fingerprint():
    fake, shared = seeded(), {}
    monitor_for(fake, shared).refresh()
    fake.login = "someone-else"
    restarted = monitor_for(fake, shared)
    assert restarted.issues(start=False)["pagination"]["total"] == 8  # fingerprint alone matched
    fake.calls.clear()
    restarted.refresh()
    assert fake.calls["issues"] == 5  # the viewer-tagged store purged: every repo re-read (3 + 2 pages)
    assert numbers(restarted) == list(range(1, 9))


class FlakyStore(MemoryGitHubStore):
    """Writes fail while ``down`` is set (Postgres unreachable, or a roll before the migration)."""

    down = [False]

    def _guard(self):
        from herdr_engineering.control_plane.github_store import GitHubStoreUnavailable
        if self.down[0]:
            raise GitHubStoreUnavailable("down")

    def upsert_repositories(self, rows, scope):
        self._guard()
        return super().upsert_repositories(rows, scope)

    def upsert_issues(self, repo_id, rows, watermark):
        self._guard()
        return super().upsert_issues(repo_id, rows, watermark)

    def remove_issues(self, repo_id, github_ids):
        self._guard()
        return super().remove_issues(repo_id, github_ids)


def test_store_outage_keeps_serving_memory_and_flushes_after_recovery():
    fake, shared, down = seeded(), {}, [True]
    factory = lambda identity, viewer=None: type("S", (FlakyStore,), {"down": down})(  # noqa: E731
        identity=identity, viewer=viewer, shared=shared)
    monitor = GitHubMonitor(provider_factory=lambda: fake, store=factory)
    monitor.refresh()
    assert monitor.snapshot(start=False)["status"] == "ready"
    assert numbers(monitor) == list(range(1, 9))
    down[0] = False
    monitor.refresh()  # nothing changed on GitHub, but the store missed every write
    restarted = GitHubMonitor(provider_factory=lambda: fake, store=store_factory(shared))
    assert numbers(restarted) == list(range(1, 9))
    fake.calls.clear()
    restarted.refresh()
    assert fake.calls == Counter({"repos": 2})
