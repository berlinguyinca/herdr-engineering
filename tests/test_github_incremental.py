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
        name = repository_id.removeprefix("github.com/")  # canonical ids are lower-case
        issues = next((v for k, v in self.repos.items() if k.lower() == name.lower()), {})
        rows = [dict(i) for i in issues.values() if i["state"] == state]
        return sorted(rows, key=lambda i: i["updatedAt"], reverse=True)

    def issues_page(self, repository_id, cursor=None):
        self.calls["issues"] += 1
        self.log.append(("issues", repository_id, cursor))
        return self._connection(self._sorted(repository_id, "OPEN"), cursor)

    def issue(self, repository_id, number):
        self.calls["detail"] += 1
        self.log.append(("detail", repository_id, number))
        name = repository_id.removeprefix("github.com/")
        issues = next((v for k, v in self.repos.items() if k.lower() == name.lower()), {})
        row = next(i for i in issues.values() if i["number"] == number)
        return dict(row, body=f"body of {number} at {row['updatedAt']}")

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

    def apply_repository(self, row, issues, removed, watermark):
        self._guard()
        return super().apply_repository(row, issues, removed, watermark)


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


# ---------------------------------------------------------------- G3


def test_closed_issue_is_removed_with_a_tombstone():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    revision = MemoryGitHubStore(identity=fake.credential_identity, shared=shared).revision()
    closed = [g for g, i in fake.repos["acme/app"].items() if i["number"] == 2][0]
    fake.tick(600)
    fake.edit("acme/app", closed, state="CLOSED")
    fake.log.clear()
    monitor.refresh()
    assert numbers(monitor, "github.com/acme/app") == [1, 3, 4, 5]
    assert [entry[:2] for entry in fake.log if entry[0] == "closed"] == [("closed", "github.com/acme/app")]
    assert len(issue_calls(fake, "github.com/acme/app")) == 1  # newest page only: no full read
    changes = MemoryGitHubStore(identity=fake.credential_identity, shared=shared).changes_since(revision)
    assert [(item["kind"], item["key"]) for item in changes["removed"]] == [("issue", closed)]


def test_deleted_issue_is_caught_by_the_count_check_and_one_repository_full_read():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    gone = [g for g, i in fake.repos["acme/app"].items() if i["number"] == 2][0]
    fake.delete("acme/app", gone)  # no closed event, no updatedAt bump anywhere
    fake.log.clear()
    monitor.refresh()
    assert numbers(monitor, "github.com/acme/app") == [1, 3, 4, 5]
    assert {entry[1] for entry in fake.log if entry[0] in {"issues", "closed"}} == {"github.com/acme/app"}
    # incremental read (newest page), then the full read from the first page
    assert [entry[2] for entry in issue_calls(fake)] == [None, None, "2"]
    restarted = monitor_for(fake, shared)
    assert numbers(restarted, "github.com/acme/app") == [1, 3, 4, 5]


def test_transferred_issue_disappears_from_the_source_repository():
    fake = seeded()
    monitor = monitor_for(fake, {})
    monitor.refresh()
    moved = [g for g, i in fake.repos["acme/app"].items() if i["number"] == 3][0]
    row = fake.repos["acme/app"].pop(moved)
    fake.repos["acme/lib"]["I_moved"] = dict(row, id="I_moved", number=42, updatedAt=_iso(fake.tick()))
    monitor.refresh()
    assert numbers(monitor, "github.com/acme/app") == [1, 2, 4, 5]
    assert numbers(monitor, "github.com/acme/lib") == [6, 7, 8, 42]


def test_scheduled_full_reconcile_touches_only_due_repositories_spread_across_passes():
    fake = FakeGitHub(page_size=100)
    for index in range(6):
        fake.add_issue(f"acme/r{index}")
    clock = [1_000_000.0]
    monitor = monitor_for(fake, {}, now=lambda: clock[0], full_reconcile_interval=3000, reconcile_per_pass=2)
    monitor.refresh()
    clock[0] += 300
    fake.log.clear()
    monitor.refresh()
    assert issue_calls(fake) == []  # nothing is due yet
    touched = []
    for _ in range(3):
        clock[0] += 3000
        fake.log.clear()
        monitor.refresh()
        touched.append({entry[1] for entry in issue_calls(fake)})
    assert [len(batch) for batch in touched] == [2, 2, 2]
    assert len(set().union(*touched)) == 6  # each pass took different repositories


def test_restart_spreads_reconcile_instead_of_making_every_repository_due():
    fake, shared = FakeGitHub(page_size=100), {}
    for index in range(12):
        fake.add_issue(f"acme/r{index}")
    clock = [1_000_000.0]
    monitor_for(fake, shared, now=lambda: clock[0], full_reconcile_interval=3000, reconcile_per_pass=12).refresh()
    clock[0] += 10
    restarted = monitor_for(fake, shared, now=lambda: clock[0], full_reconcile_interval=3000, reconcile_per_pass=12)
    reads, passes_with_reads = Counter(), 0
    for _ in range(10):  # one full interval in 300 s passes
        fake.log.clear()
        restarted.refresh()
        touched = {entry[1] for entry in issue_calls(fake)}
        reads.update(touched)
        passes_with_reads += bool(touched)
        clock[0] += 300
    assert sorted(reads.values()) == [1] * 12  # every stored repository reconciled exactly once
    assert passes_with_reads > 1  # spread over the interval, not all at the restart


# ---------------------------------------------------------------- G4


def scoped():
    fake = FakeGitHub(page_size=100)
    for _ in range(2):
        fake.add_issue("acme/app")
    fake.add_issue("Other/tool")
    fake.add_issue("third/lib")
    return fake


def test_allowlist_fetches_and_stores_only_listed_owners():
    fake, shared = scoped(), {}
    monitor = monitor_for(fake, shared, organizations=["ACME"])
    monitor.refresh()
    assert {entry[1] for entry in fake.log if entry[0] in {"issues", "closed"}} == {"github.com/acme/app"}
    view = monitor.repositories(start=False)
    assert [r["repository_id"] for r in view["repositories"]] == ["github.com/acme/app"]
    assert view["scope"] == {"owners": ["acme"], "excluded_repositories": 2, "restricted": True}
    assert numbers(monitor) == [1, 2]
    snapshot = MemoryGitHubStore(identity=fake.credential_identity, shared=shared).load()
    assert set(snapshot.repositories) == {"github.com/acme/app"}


def test_no_allowlist_means_every_owner_and_reports_them():
    fake = scoped()
    monitor = monitor_for(fake, {}, organizations=[])
    monitor.refresh()
    assert monitor.snapshot(start=False)["scope"] == {"owners": ["acme", "other", "third"],
                                                      "excluded_repositories": 0, "restricted": False}
    assert numbers(monitor) == [1, 2, 3, 4]


def test_removing_an_owner_purges_its_rows_and_tombstones_them():
    fake, shared, allow = scoped(), {}, [[]]
    monitor = monitor_for(fake, shared, organizations=lambda: allow[0])
    monitor.refresh()
    revision = MemoryGitHubStore(identity=fake.credential_identity, shared=shared).revision()
    allow[0] = ["acme", "third"]
    # out-of-scope rows stop being served at once, not only after the listing
    fake.log.clear()
    monitor.refresh()
    assert [entry for entry in fake.log if entry[0] != "repos"] == []  # nothing changed in scope
    assert numbers(monitor) == [1, 2, 4]
    changes = MemoryGitHubStore(identity=fake.credential_identity, shared=shared).changes_since(revision)
    assert sorted((item["kind"], item["repository_id"]) for item in changes["removed"]) == [
        ("issue", "github.com/other/tool"), ("repository", "github.com/other/tool")]
    assert monitor.snapshot(start=False)["scope"]["excluded_repositories"] == 1


def test_restart_with_a_narrower_allowlist_never_serves_unlisted_owners():
    fake, shared = scoped(), {}
    monitor_for(fake, shared, organizations=[]).refresh()
    fake.calls.clear()
    restarted = monitor_for(fake, shared, organizations=["acme"])
    assert numbers(restarted) == [1, 2]
    assert [r["repository_id"] for r in restarted.repositories(start=False)["repositories"]] == ["github.com/acme/app"]
    assert fake.calls == Counter()


def test_allowlist_comes_from_the_github_yaml_section_and_invalid_values_stop_the_pass(fake_host_config):
    fake = scoped()
    fake_host_config({"github": {"organizations": ["third"]}})
    monitor = monitor_for(fake, {})
    monitor.refresh()
    assert numbers(monitor) == [4]
    fake_host_config({"github": {"organizations": "third"}})
    fake.calls.clear()
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view["status"] == "invalid_config" and "github.organizations" in view["error"]
    assert fake.calls == Counter()


def test_fleet_file_github_section_carries_the_allowlist_list(tmp_path, fake_host_config):
    import hashlib

    import yaml

    from herdr_engineering.fleet_config import load_fleet_config
    path = tmp_path / "fleet-config.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "sections": {
        "github": {"roles": ["ci"], "values": {"token": "placeholder-token-value", "organizations": ["acme"]}}},
        "hosts": {"cp": {"roles": ["ci"], "token_sha256": hashlib.sha256(b"placeholder-host").hexdigest()}}}))
    path.chmod(0o600)
    fleet = load_fleet_config(path)
    fake_host_config({"github": fleet.section("github")})  # what the web process installs
    fake = scoped()
    monitor = monitor_for(fake, {})
    monitor.refresh()
    assert monitor.snapshot(start=False)["scope"]["owners"] == ["acme"]
    assert numbers(monitor) == [1, 2]


# ---------------------------------------------------------------- G5


def ladder(budget=None, **kwargs):
    """r1..r4 with 4, 3, 2 and 1 open issues (so a pass visits them in that order), synced once."""
    fake = FakeGitHub(page_size=100)
    for index, count in enumerate((4, 3, 2, 1), start=1):
        for _ in range(count):
            fake.add_issue(f"acme/r{index}")
    clock = [1_000_000.0]
    monitor = monitor_for(fake, {}, now=lambda: clock[0], point_budget=budget or 1000, **kwargs)
    monitor.refresh()
    for index in range(1, 5):
        gid = next(iter(fake.repos[f"acme/r{index}"]))
        fake.edit(f"acme/r{index}", gid, title=f"edited r{index}")
    return fake, monitor, clock


def titles(monitor, rid):
    return {row["title"] for row in monitor.issues(repository_id=rid, per_page=100, start=False)["issues"]}


def touched(fake):
    return [entry[1] for entry in fake.log if entry[0] in {"issues", "closed"}]


def test_point_budget_processes_only_what_fits_and_defers_the_rest():
    fake, monitor, clock = ladder()
    monitor._fixed_sync["point_budget"] = 5  # listing 1 + two changed repos at 2 points each
    fake.log.clear()
    fake.calls.clear()
    monitor.refresh()
    assert fake.calls == Counter({"repos": 1, "issues": 2, "closed": 2})
    assert set(touched(fake)) == {"github.com/acme/r1", "github.com/acme/r2"}
    view = monitor.snapshot(start=False)
    assert [row["repository_id"] for row in view["deferred_repos"]] == ["github.com/acme/r3", "github.com/acme/r4"]
    assert view["status"] == "partial" and view["stale"] is False  # deferred, but not for long yet
    assert "edited r1" in titles(monitor, "github.com/acme/r1")
    assert "edited r3" not in titles(monitor, "github.com/acme/r3")  # previous cache kept and served
    assert len(titles(monitor, "github.com/acme/r3")) == 2


def test_deferred_repositories_come_first_in_the_next_pass():
    fake, monitor, clock = ladder()
    monitor._fixed_sync["point_budget"] = 5
    monitor.refresh()
    monitor._fixed_sync["point_budget"] = 1000
    clock[0] += 300
    fake.log.clear()
    fake.calls.clear()
    monitor.refresh()
    assert fake.calls["repos"] == 0  # the deferred work resumes before anything is re-listed
    assert touched(fake)[0] == "github.com/acme/r3"
    assert set(touched(fake)) == {"github.com/acme/r3", "github.com/acme/r4"}
    view = monitor.snapshot(start=False)
    assert view["deferred_repos"] == [] and view["status"] == "ready"
    assert "edited r4" in titles(monitor, "github.com/acme/r4")


def test_repository_deferred_past_two_intervals_marks_the_catalog_stale():
    fake, monitor, clock = ladder()
    monitor._fixed_sync["point_budget"] = 5  # r3 and r4 deferred
    monitor.refresh()
    assert monitor.snapshot(start=False)["stale"] is False
    clock[0] += 300
    monitor._fixed_sync["point_budget"] = 2  # the resumed pass (no listing) fits r3 only
    monitor.refresh()
    clock[0] += 301
    view = monitor.snapshot(start=False)
    assert [row["repository_id"] for row in view["deferred_repos"]] == ["github.com/acme/r4"]
    assert view["stale"] is True  # r4 has waited longer than 2 x 300 s


def test_unchanged_repositories_are_never_deferred():
    fake = FakeGitHub(page_size=100)
    for name in ("acme/a", "acme/b", "acme/c"):
        fake.add_issue(name)
    monitor = monitor_for(fake, {}, point_budget=1)
    monitor.refresh()  # the listing is never cut; the first sync defers everything that needs a read
    assert len(monitor.snapshot(start=False)["deferred_repos"]) == 3
    for _ in range(3):
        monitor._fixed_sync["point_budget"] = 2
        monitor.refresh()
    assert monitor.snapshot(start=False)["deferred_repos"] == []
    fake.edit("acme/b", next(iter(fake.repos["acme/b"])), title="changed")
    monitor._fixed_sync["point_budget"] = 1
    monitor.refresh()  # the listing spends the whole budget; only acme/b needs a read
    assert [row["repository_id"] for row in monitor.snapshot(start=False)["deferred_repos"]] == ["github.com/acme/b"]


def test_rate_limited_mid_pass_defers_the_rest_the_same_way():
    from herdr_engineering.github import GitHubError
    fake, monitor, clock = ladder()
    original = fake.issues_page

    def limited(repository_id, cursor=None):
        if repository_id == "github.com/acme/r3":
            raise GitHubError("rate_limited", retry_at=clock[0] + 120)
        return original(repository_id, cursor)
    fake.issues_page = limited
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view["status"] == "rate_limited"
    assert [row["repository_id"] for row in view["deferred_repos"]] == ["github.com/acme/r3", "github.com/acme/r4"]
    assert "edited r3" not in titles(monitor, "github.com/acme/r3")
    fake.issues_page = original
    clock[0] += 300
    fake.log.clear()
    fake.calls.clear()
    monitor.refresh()
    assert fake.calls["repos"] == 0 and touched(fake)[0] == "github.com/acme/r3"
    assert monitor.snapshot(start=False)["deferred_repos"] == []


def test_a_rate_limit_defers_only_the_repositories_that_still_had_work():
    from herdr_engineering.github import GitHubError
    fake, monitor, clock = ladder()
    monitor.refresh()  # takes the ladder's edits: every repository is synced
    for name in ("acme/r1", "acme/r2"):
        fake.edit(name, next(iter(fake.repos[name])), title=f"again {name}")
    original = fake.issues_page

    def limited(repository_id, cursor=None):
        if repository_id == "github.com/acme/r1":
            raise GitHubError("rate_limited", retry_at=clock[0] + 120)
        return original(repository_id, cursor)
    fake.issues_page = limited
    clock[0] += 300
    fake.log.clear()
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view["status"] == "rate_limited"
    assert [row["repository_id"] for row in view["deferred_repos"]] == ["github.com/acme/r1", "github.com/acme/r2"]
    assert touched(fake) == []  # nothing more is read once GitHub said stop


def test_deferred_repos_in_the_view_are_capped_but_the_tag_sees_them_all(monkeypatch):
    from herdr_engineering import github
    monkeypatch.setattr(github, "DEFERRED_SHOWN", 2)
    fake, monitor, clock = ladder(budget=1)  # the listing spends it: all four wait
    view = monitor.snapshot(start=False)
    assert len(view["deferred_repos"]) == 2 and view["deferred_total"] == 4
    tag = monitor.list_tag(start=False)
    with monitor._lock:
        monitor._deferred["github.com/acme/r4"] += 1  # a change past the cap
    assert monitor.list_tag(start=False) != tag


def test_point_budget_is_read_from_yaml(fake_host_config):
    fake_host_config({"github": {"sync": {"point_budget": 5}}})
    fake, monitor, clock = ladder()
    monitor._fixed_sync["point_budget"] = None
    monitor.refresh()
    assert len(monitor.snapshot(start=False)["deferred_repos"]) == 2


def closed_backlog(budget):
    """acme/big with 10 issues closed since its last sync (5 closed pages of 2) plus one edit."""
    fake = FakeGitHub(page_size=2)
    keep = fake.add_issue("acme/big")
    gids = [fake.add_issue("acme/big") for _ in range(10)]
    fake.add_issue("acme/small")
    clock = [1_000_000.0]
    monitor = monitor_for(fake, {}, now=lambda: clock[0], point_budget=1000)
    monitor.refresh()
    for gid in gids:
        fake.edit("acme/big", gid, state="CLOSED")
    fake.edit("acme/big", keep, title="edited")
    monitor._fixed_sync["point_budget"] = budget
    return fake, monitor, clock, fake.repos["acme/big"][keep]["number"]


def drive(fake, monitor, clock, passes):
    """Run passes until nothing is deferred; return each pass's calls."""
    history = []
    for _ in range(passes):
        clock[0] += 300
        fake.log.clear()
        fake.calls.clear()
        monitor.refresh()
        history.append(list(fake.log))
        if not monitor.snapshot(start=False)["deferred_repos"]:
            break
    return history


def big_reads(log):
    return [entry for entry in log if entry[0] in {"issues", "closed"} and entry[1] == "github.com/acme/big"]


def test_a_closed_window_larger_than_the_budget_resumes_at_its_cursor_and_converges():
    fake, monitor, clock, keep = closed_backlog(3)
    first = drive(fake, monitor, clock, 1)
    assert monitor.snapshot(start=False)["deferred_repos"]  # the closed window does not fit one pass
    fake.add_repo("acme/new")       # only a fresh listing can show these two
    del fake.repos["acme/small"]
    history = first + drive(fake, monitor, clock, 12)
    view = monitor.snapshot(start=False)
    assert view["deferred_repos"] == [] and view["status"] == "ready"
    # every pass advanced acme/big; its closed window was never re-read from page 1
    assert all(big_reads(log) for log in history[:-1])
    closed_starts = [e for log in history for e in big_reads(log) if e[0] == "closed" and e[2] is None]
    assert len(closed_starts) == 1
    assert numbers(monitor, "github.com/acme/big") == [keep]
    clock[0] += 300
    monitor.refresh()  # the catalog is not frozen: the next pass lists again
    repos = {r["repository_id"] for r in monitor.repositories(start=False, per_page=100)["repositories"]}
    assert repos == {"github.com/acme/big", "github.com/acme/new"}


def test_a_repository_that_never_fits_the_budget_still_advances_and_the_listing_is_forced():
    fake, monitor, clock, keep = closed_backlog(1)  # the listing alone spends the budget
    drive(fake, monitor, clock, 1)
    fake.add_repo("acme/new")
    history = drive(fake, monitor, clock, 20)
    assert monitor.snapshot(start=False)["deferred_repos"] == []
    assert all(big_reads(log) for log in history[:-1])  # the longest-deferred repository always moves
    listed = [index for index, log in enumerate(history) if any(e[0] == "repos" for e in log)]
    assert listed and listed[0] <= 3  # resuming never freezes the listing for more than a few passes
    assert "github.com/acme/new" in {r["repository_id"] for r in
                                     monitor.repositories(start=False, per_page=100)["repositories"]}
    assert numbers(monitor, "github.com/acme/big") == [keep]


def test_a_relist_during_a_resumed_sync_never_lifts_the_watermark_past_an_unread_edit():
    fake, monitor, clock, keep = closed_backlog(1)
    drive(fake, monitor, clock, 1)
    drive(fake, monitor, clock, 1)  # acme/big's open read is done; its closed read is under way
    fake.edit("acme/big", next(g for g, i in fake.repos["acme/big"].items() if i["state"] == "OPEN"),
              title="edited after the open read")
    drive(fake, monitor, clock, 20)
    monitor._fixed_sync["point_budget"] = 1000
    clock[0] += 300
    monitor.refresh()
    assert titles(monitor, "github.com/acme/big") == {"edited after the open read"}


# ---------------------------------------------------------------- G6


def test_issue_detail_is_served_from_the_store_until_the_issue_changes():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    first = monitor.issue_detail("github.com/acme/app", 2)
    assert fake.calls["detail"] == 1 and first["issue"]["body"].startswith("body of 2")
    second = monitor.issue_detail("github.com/acme/app", 2)
    assert fake.calls["detail"] == 1  # unchanged updatedAt: zero calls, no TTL
    assert second["issue"]["body"] == first["issue"]["body"]
    restarted = monitor_for(fake, shared)
    assert restarted.issue_detail("github.com/acme/app", 2)["issue"]["body"] == first["issue"]["body"]
    assert fake.calls["detail"] == 1  # the body persisted with the catalog
    gid = next(g for g, i in fake.repos["acme/app"].items() if i["number"] == 2)
    fake.tick(600)
    fake.edit("acme/app", gid, title="edited")
    assert monitor.issue_detail("github.com/acme/app", 2)["issue"]["title"] == "Issue 2"
    assert fake.calls["detail"] == 1  # the pass has not seen the change yet
    monitor.refresh()
    third = monitor.issue_detail("github.com/acme/app", 2)
    assert fake.calls["detail"] == 2 and third["issue"]["title"] == "edited"
    monitor.issue_detail("github.com/acme/app", 2)
    assert fake.calls["detail"] == 2  # refetched once, then served again


def test_issue_detail_without_a_store_uses_the_same_rule():
    import time
    fake, clock = seeded(), [time.time()]
    monitor = GitHubMonitor(provider_factory=lambda: fake, now=lambda: clock[0])
    monitor.refresh()
    for _ in range(3):
        assert monitor.issue_detail("github.com/acme/lib", 6)["issue"]["number"] == 6
        clock[0] += 120  # well past the old 60 s TTL
    assert fake.calls["detail"] == 1


# ---------------------------------------------------------------- review fixes


def test_store_unavailable_at_viewer_bind_keeps_the_cache_and_rereads_nothing():
    from herdr_engineering.control_plane.github_store import GitHubStoreUnavailable
    fake, shared = seeded(), {}
    monitor_for(fake, shared).refresh()

    class BlipOnViewerLoad(MemoryGitHubStore):
        def load(self):
            if self._viewer is not None:
                raise GitHubStoreUnavailable("blip")
            return super().load()
    restarted = GitHubMonitor(provider_factory=lambda: fake, store=lambda identity, viewer=None: BlipOnViewerLoad(
        identity=identity, viewer=viewer, shared=shared))
    assert numbers(restarted) == list(range(1, 9))
    fake.calls.clear()
    restarted.refresh()
    assert fake.calls == Counter({"repos": 2})  # a transient error is not a purge: no issue re-read
    assert numbers(restarted) == list(range(1, 9))
    assert restarted.snapshot(start=False)["status"] == "ready"
    healthy = monitor_for(fake, shared)  # the store kept the catalog too
    fake.calls.clear()
    healthy.refresh()
    assert fake.calls == Counter({"repos": 2})


def test_a_failed_removal_never_leaves_the_watermark_past_an_unremoved_close():
    fake, shared = seeded(), {}
    monitor_for(fake, shared).refresh()
    rid = "github.com/acme/app"
    reader = MemoryGitHubStore(identity=fake.credential_identity, shared=shared)
    before = reader.watermark(rid)
    closed = next(g for g, i in fake.repos["acme/app"].items() if i["number"] == 2)
    fake.tick(600)
    fake.edit("acme/app", closed, state="CLOSED")
    fake.edit("acme/app", next(g for g, i in fake.repos["acme/app"].items() if i["number"] == 5), title="edit")

    class RemovalFails(MemoryGitHubStore):
        def remove_issues(self, repo_id, github_ids):
            raise RuntimeError("injected")

        def _drop_issues(self, *args, **kwargs):
            raise RuntimeError("injected")
    broken = GitHubMonitor(provider_factory=lambda: fake, store=lambda identity, viewer=None: RemovalFails(
        identity=identity, viewer=viewer, shared=shared))
    broken.refresh()
    stored = reader.load()
    # either the whole repository commit landed, or none of it did
    if closed in stored.issues[rid]:
        assert stored.watermarks[rid] == before
    else:
        assert stored.watermarks[rid] > before


def test_an_edit_on_a_closed_issue_never_lifts_the_watermark_past_an_unread_open_edit():
    fake = FakeGitHub(page_size=2)
    gids = [fake.add_issue("acme/a") for _ in range(6)]
    fake.edit("acme/a", gids[1], state="CLOSED")
    monitor = monitor_for(fake, {})
    monitor.refresh()
    fake.tick(600)
    fake.edit("acme/a", gids[-1], title="poke")  # the probe changes: an incremental pass
    original = fake.closed_issues_page

    def during_the_pass(repository_id, since, cursor=None):
        fake.closed_issues_page = original
        # after the open read: Y is edited, then a page of other edits, then a comment on closed W
        fake.edit("acme/a", gids[0], title="Y edited")
        fake.edit("acme/a", gids[2], title="Z1")
        fake.edit("acme/a", gids[3], title="Z2")
        fake.tick(30)
        fake.edit("acme/a", gids[1], title="comment on closed W")
        return original(repository_id, since, cursor)
    fake.closed_issues_page = during_the_pass
    monitor.refresh()
    fake.log.clear()
    monitor.refresh()
    # one incremental read (no count-mismatch re-read, no reconcile is due) must find Y
    assert [entry[2] for entry in issue_calls(fake)].count(None) == 1
    assert monitor._due_reconciles(monitor._repos) == set()
    titles = {row["github_id"]: row["title"] for row in monitor.issues(per_page=100, start=False)["issues"]}
    assert titles[gids[0]] == "Y edited"


def test_a_flush_on_a_superseded_store_cannot_purge_the_new_credentials_catalog():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    old_store = monitor._store
    fake.credential_identity = "fingerprint-two"  # rotation while a pass still holds old_store
    monitor.refresh()
    assert numbers(monitor) == list(range(1, 9))
    monitor._flush(old_store)  # the old pass reaching its flush late
    stored = MemoryGitHubStore(identity="fingerprint-two", shared=shared).load()
    assert stored is not None and len(stored.repositories) == 3
    assert monitor.revision() is not None  # a refused old store does not dirty the new catalog


def _revoke(fake):
    from herdr_engineering.github import GitHubError

    def rejected(*args, **kwargs):
        raise GitHubError("authentication")
    fake.repositories_page = fake.issues_page = fake.issue = rejected


def test_a_revoked_token_stops_serving_and_purges_its_private_catalog():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    _revoke(fake)  # same token, same fingerprint, but GitHub now answers 401
    monitor.refresh()
    view = monitor.repositories(start=False)
    assert view["status"] == "auth_error" and view["available"] is False and view["stale"] is False
    assert view["repositories"] == [] and numbers(monitor) == []
    restarted = monitor_for(fake, shared)  # a restart has nothing left to serve
    assert restarted.repositories(start=False)["repositories"] == [] and numbers(restarted) == []


def test_a_revoked_token_never_serves_a_cached_issue_body():
    from herdr_engineering.github import GitHubError
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared, now=lambda: 4_000_000_000.0)
    monitor.refresh()
    monitor.issue_detail("github.com/acme/app", 2)
    fake.tick(600)
    fake.edit("acme/app", next(g for g, i in fake.repos["acme/app"].items() if i["number"] == 2), title="new")
    monitor.refresh()  # the catalog now knows a newer updatedAt: the detail must be refetched
    _revoke(fake)
    try:
        monitor.issue_detail("github.com/acme/app", 2)
        raise AssertionError("served a cached body after a 401")
    except GitHubError as exc:
        assert exc.reason in {"authentication", "not_found"}
    assert numbers(monitor) == []
    assert MemoryGitHubStore(identity=fake.credential_identity, shared=shared).load().repositories == {}


def test_an_unreadable_viewer_store_is_retried_once_per_pass_not_per_page():
    from herdr_engineering.control_plane.github_store import GitHubStoreUnavailable
    fake, shared = seeded(), {}  # 3 repositories at 2 per page: two listing pages
    monitor_for(fake, shared).refresh()
    attempts = []

    class Unreadable(MemoryGitHubStore):
        def load(self):
            if self._viewer is not None:
                attempts.append(self._viewer)
                raise GitHubStoreUnavailable("down")
            return super().load()
    restarted = GitHubMonitor(provider_factory=lambda: fake, store=lambda identity, viewer=None: Unreadable(
        identity=identity, viewer=viewer, shared=shared))
    restarted.refresh()
    assert len(attempts) == 1
    restarted.refresh()
    assert len(attempts) == 2


def test_a_flush_on_a_superseded_store_leaves_the_current_store_dirty():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    old_store = monitor._store
    fake.credential_identity = "fingerprint-two"
    monitor.refresh()
    monitor._store_dirty = True  # the current store missed a write
    monitor._flush(old_store)
    assert monitor._store_dirty is True and monitor.revision() is None


def test_a_poll_during_a_purge_never_answers_an_empty_delta():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    since = monitor.revision()
    store, seen = monitor._store, []
    original = store.purge

    def purge():  # a client polls while memory is already cleared but the store not yet purged
        seen.append(monitor.changes(kind="issues", since=since, start=False))
        original()
    store.purge = purge
    _revoke(fake)
    monitor.refresh()
    assert seen and seen[0]["resync"] is True and seen[0]["issues"] == []
    after = monitor.changes(kind="issues", since=since, start=False)
    assert after["resync"] is True and after["issues"] == []


def test_a_revision_read_never_moves_back_or_comes_from_another_store():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    current, store = monitor.revision(), monitor._store
    monitor._remember_revision(store, current - 3)  # a read that raced a newer write
    assert monitor.revision() == current
    other = store_factory(shared)("fingerprint-one", "fixture-user")
    other.upsert_repositories([{"repository_id": "github.com/acme/zzz", "name_with_owner": "acme/zzz"}], None)
    monitor._remember_revision(other)  # not the monitor's store: never adopted
    assert monitor.revision() == current


def test_a_stored_detail_of_an_unlisted_issue_is_judged_by_when_it_was_fetched():
    fake, shared, clock = seeded(), {}, [1_000_000.0]
    monitor = monitor_for(fake, shared, now=lambda: clock[0])
    monitor.refresh()
    gid = fake.add_issue("acme/app")  # opened before any pass has seen it
    number = fake.repos["acme/app"][gid]["number"]
    monitor.issue_detail("github.com/acme/app", number)
    clock[0] += 120
    restarted = monitor_for(fake, shared, now=lambda: clock[0])
    restarted.issue_detail("github.com/acme/app", number)
    assert fake.calls["detail"] == 2  # fetched 120 s ago: refetched, not served as just checked
    clock[0] += 30
    restarted.issue_detail("github.com/acme/app", number)
    assert fake.calls["detail"] == 2  # within 60 s of that fetch: reused


def test_a_store_hit_is_kept_in_memory():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    monitor.issue_detail("github.com/acme/app", 2)
    restarted = monitor_for(fake, shared)
    restarted.snapshot(start=False)  # opens its store
    reads, original = [], restarted._store.stored_detail

    def counting(*args):
        reads.append(args)
        return original(*args)
    restarted._store.stored_detail = counting
    for _ in range(3):
        assert restarted.issue_detail("github.com/acme/app", 2)["issue"]["number"] == 2
    assert len(reads) == 1 and fake.calls["detail"] == 1


def test_dropping_an_owner_from_the_allowlist_counts_as_a_change_at_once():
    fake = scoped()
    allow = [["acme", "other"]]
    monitor = monitor_for(fake, {}, organizations=lambda: allow[0])
    monitor.refresh()
    allow[0] = ["acme"]
    marks, read_allowlist, list_page = {}, monitor._allowlist, fake.repositories_page

    def allowlist():
        value = read_allowlist()
        marks["after_allowlist"] = monitor._epoch
        return value

    def repositories_page(cursor=None):
        marks.setdefault("at_listing", monitor._epoch)
        return list_page(cursor)
    monitor._allowlist, fake.repositories_page = allowlist, repositories_page
    monitor.refresh()
    # the unlisted owner's rows left memory before the listing: no ETag may still describe them
    assert marks["at_listing"] > marks["after_allowlist"]


def test_an_unreadable_startup_load_reconciles_ghost_rows_on_the_first_pass():
    fake, shared = seeded(), {}
    seed = MemoryGitHubStore(identity=fake.credential_identity, viewer=fake.login, shared=shared)
    seed.upsert_repositories([{"repository_id": "github.com/acme/app", "name_with_owner": "acme/app"}], None)
    seed.upsert_issues("github.com/acme/app", [{"github_id": "I_ghost", "number": 99, "title": "gone"}], None)
    opened = []

    def factory(identity, viewer=None):
        store = MemoryGitHubStore(identity=identity, viewer=viewer, shared=shared)
        if not opened:  # the startup load fails (database briefly down)

            def unreadable():
                raise OSError("database down")
            store.load = unreadable
        opened.append(store)
        return store
    monitor = GitHubMonitor(provider_factory=lambda: fake, store=factory)
    monitor.snapshot(start=False)
    assert monitor.revision() is None  # memory knows nothing of what is stored: no revision to trust
    monitor.refresh()
    stored = seed.load().issues["github.com/acme/app"]
    assert "I_ghost" not in stored and len(stored) == 5
    assert monitor.revision() is not None


def test_every_call_is_charged_at_least_one_point():
    fake = FakeGitHub(page_size=100)
    for name in ("acme/a", "acme/b", "acme/c"):
        fake.add_issue(name)
    fake.rate_limit = {"remaining": 4000, "cost": 0, "resetAt": "2026-10-24T15:00:00Z"}
    monitor = monitor_for(fake, {}, point_budget=2)
    monitor.refresh()  # free calls would let a budget of 2 read every repository
    assert fake.calls["issues"] == 1
    assert len(monitor.snapshot(start=False)["deferred_repos"]) == 2


def test_a_call_without_a_reported_cost_is_charged_one_not_the_previous_cost():
    import json as _json

    from herdr_engineering.github import GitHubProvider
    listing = {"viewer": {"login": "me", "repositories": {"totalCount": 0, "nodes": [],
                                                           "pageInfo": {"hasNextPage": False}}}}
    replies = [dict(listing, rateLimit={"remaining": 10, "resetAt": "2026-10-24T15:00:00Z", "cost": 7}), listing]

    class Client:
        def open(self, request, **kwargs):
            import io
            return io.BytesIO(_json.dumps({"data": replies.pop(0)}).encode())
    provider = GitHubProvider(token="placeholder-token-value", client=Client())
    monitor = GitHubMonitor(provider_factory=lambda: provider)
    provider.repositories_page()
    monitor._spent = 0
    monitor._charge(provider)
    assert monitor._spent == 7
    provider.repositories_page()  # GitHub did not report this call's cost
    monitor._charge(provider)
    assert monitor._spent == 8


def test_a_pass_over_budget_with_errors_keeps_the_error_summary():
    from herdr_engineering.github import GitHubError
    fake, monitor, clock = ladder()
    before = monitor.snapshot(start=False)["last_success_at"]
    original = fake.issues_page

    def broken(repository_id, cursor=None):
        if repository_id == "github.com/acme/r1":
            raise GitHubError("unavailable")
        return original(repository_id, cursor)
    fake.issues_page = broken
    monitor._fixed_sync["point_budget"] = 5
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view["deferred_repos"] and view["status"] == "partial"
    assert "could not be refreshed" in view["error"] and "point budget" in view["error"]
    assert view["stale"] is True and view["last_success_at"] == before  # not a clean pass


def test_a_write_landing_during_a_purge_cannot_reinstate_the_old_revision():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    since = monitor.revision()
    store, seen = monitor._store, []
    original = store.purge

    def purge():  # a pass's write-through finishes and re-reads the pre-purge revision
        monitor._remember_revision(store)
        seen.append(monitor.changes(kind="issues", since=since, start=False))
        original()
    store.purge = purge
    _revoke(fake)
    monitor.refresh()
    assert seen[0]["resync"] is True and seen[0]["issues"] == []
    assert monitor.revision() is not None and monitor.revision() > since  # remembered after the purge


# ---------------------------------------------------------------- G9 Projects v2

STATUS = [{"id": "opt-todo", "name": "Todo"}, {"id": "opt-doing", "name": "In progress"}, {"id": "opt-done", "name": "Done"}]


class FakeProjects(FakeGitHub):
    """FakeGitHub plus Projects v2: owner -> projects -> items (position order), all with updatedAt."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.projects = {}   # owner -> {pid: project}
        self.fail_projects = None

    def add_project(self, owner, title="Board", *, status=True):
        pid = f"PVT_{owner}_{len(self.projects.get(owner, {})) + 1}"
        self.projects.setdefault(owner, {})[pid] = {
            "id": pid, "number": len(self.projects.get(owner, {})) + 1, "title": title,
            "url": f"https://github.com/orgs/{owner}/projects/1", "closed": False,
            "updatedAt": _iso(self.tick()), "items": {}, "status": status}
        return pid

    def _project(self, pid):
        return next(p[pid] for p in self.projects.values() if pid in p)

    def add_item(self, pid, status="Todo", title=None):
        project = self._project(pid)
        iid = f"PVTI_{pid}_{len(project['items']) + 1}"
        project["items"][iid] = {"id": iid, "type": "ISSUE", "isArchived": False, "updatedAt": _iso(self.tick()),
                                 "status": status, "content": {"title": title or f"Item {iid}", "number": 1,
                                                               "url": "https://github.com/acme/app/issues/1",
                                                               "issueState": "OPEN", "repository": {"nameWithOwner": "acme/app"}}}
        project["updatedAt"] = _iso(self.clock)
        return iid

    def move_item(self, pid, iid, status, *, bump_project=True):
        """``bump_project=False``: a move GitHub does not reflect in the project's updatedAt."""
        project = self._project(pid)
        project["items"][iid].update(status=status, updatedAt=_iso(self.tick()))
        if bump_project:
            project["updatedAt"] = _iso(self.clock)

    def remove_item(self, pid, iid):
        project = self._project(pid)
        del project["items"][iid]
        project["updatedAt"] = _iso(self.tick())

    def projects_page(self, login, cursor=None):
        self.calls["projects"] += 1
        self.log.append(("projects", login, cursor))
        if self.fail_projects:
            from herdr_engineering.github import GitHubError
            raise GitHubError(self.fail_projects)
        owned = self.projects.get(login, {})
        nodes = [{"id": p["id"], "number": p["number"], "title": p["title"], "url": p["url"], "closed": p["closed"],
                  "updatedAt": p["updatedAt"], "items": {"totalCount": len(p["items"])},
                  "field": {"id": "F_status", "options": STATUS} if p["status"] else None}
                 for p in sorted(owned.values(), key=lambda p: p["updatedAt"], reverse=True)]
        return self._connection(nodes, cursor)

    def project_items_page(self, project_id, cursor=None):
        self.calls["items"] += 1
        self.log.append(("items", project_id, cursor))
        project = self._project(project_id)
        options = {o["name"]: o["id"] for o in STATUS}
        nodes = [{"id": i["id"], "type": i["type"], "isArchived": i["isArchived"], "updatedAt": i["updatedAt"],
                  "status": {"name": i["status"], "optionId": options.get(i["status"])} if i["status"] else None,
                  "content": i["content"]} for i in project["items"].values()]
        return self._connection(nodes, cursor)


def item_calls(fake, project_id=None):
    return [entry for entry in fake.log if entry[0] == "items" and (project_id is None or entry[1] == project_id)]


def projects_seeded():
    fake = FakeProjects()
    fake.add_issue("acme/app")
    fake.add_issue("other/tool")
    p1 = fake.add_project("acme", "Roadmap")
    for status in ("Todo", "Todo", "Done"):
        fake.add_item(p1, status)
    p2 = fake.add_project("acme", "Bugs")
    fake.add_item(p2, "In progress")
    p3 = fake.add_project("other", "Theirs")
    fake.add_item(p3)
    return fake, p1, p2, p3


ON = {"enabled": True, "item_limit": 2000}


def test_projects_are_off_by_default_and_cost_nothing():
    fake, *_ = projects_seeded()
    monitor = monitor_for(fake, {})
    monitor.refresh()
    assert fake.calls["projects"] == 0 and fake.calls["items"] == 0
    view = monitor.projects(start=False)
    assert view["projects"] == [] and view["projects_sync"]["status"] == "disabled"


def test_projects_of_in_scope_owners_are_listed_with_items():
    fake, p1, p2, p3 = projects_seeded()
    monitor = monitor_for(fake, {}, projects=ON)
    monitor.refresh()
    listed = {p["project_id"]: p for p in monitor.projects(start=False)["projects"]}
    assert set(listed) == {p1, p2, p3}
    assert listed[p1]["item_count"] == 3 and [o["name"] for o in listed[p1]["status_options"]] == ["Todo", "In progress", "Done"]
    items = monitor.project_items(p1, start=False)["items"]
    assert sorted(i["status"] for i in items) == ["Done", "Todo", "Todo"]
    assert monitor.projects(start=False)["projects_sync"]["status"] == "ready"


def test_an_unchanged_project_costs_zero_item_calls():
    fake, *_ = projects_seeded()
    monitor = monitor_for(fake, {}, projects=ON)
    monitor.refresh()
    items_before, listings_before = fake.calls["items"], fake.calls["projects"]
    monitor.refresh()
    assert fake.calls["items"] == items_before, "no item reads for unchanged projects"
    # one listing per owner: acme, other and (no allowlist) the viewer's own account
    assert fake.calls["projects"] == listings_before + 3


def test_one_moved_item_fetches_only_its_project():
    fake, p1, p2, p3 = projects_seeded()
    monitor = monitor_for(fake, {}, projects=ON)
    monitor.refresh()
    since = monitor.revision()
    moved = next(iter(fake._project(p1)["items"]))
    fake.log.clear()
    fake.move_item(p1, moved, "Done")
    monitor.refresh()
    assert {entry[1] for entry in item_calls(fake)} == {p1}
    delta = monitor.changes(kind="project_items", since=since, project_id=p1, start=False)
    assert [(row["item_id"], row["status"]) for row in delta["upserted"]] == [(moved, "Done")]
    assert delta["removed"] == []


def test_a_removed_item_is_reported_removed():
    fake, p1, *_ = projects_seeded()
    monitor = monitor_for(fake, {}, projects=ON)
    monitor.refresh()
    since = monitor.revision()
    gone = next(iter(fake._project(p1)["items"]))
    fake.remove_item(p1, gone)
    monitor.refresh()
    delta = monitor.changes(kind="project_items", since=since, project_id=p1, start=False)
    assert delta["removed"] == [{"project_id": p1, "key": gone}]
    assert gone not in {i["item_id"] for i in monitor.project_items(p1, start=False)["items"]}


def test_out_of_scope_owners_make_zero_project_calls_and_their_rows_are_purged():
    fake, p1, p2, p3 = projects_seeded()
    shared, allow = {}, []
    monitor = monitor_for(fake, shared, projects=ON, organizations=lambda: allow)
    monitor.refresh()
    assert p3 in {p["project_id"] for p in monitor.projects(start=False)["projects"]}
    allow.append("acme")
    fake.log.clear()
    monitor.refresh()
    assert not [entry for entry in fake.log if entry[0] in {"projects", "items"} and "other" in str(entry[1])]
    assert {p["project_id"] for p in monitor.projects(start=False)["projects"]} == {p1, p2}
    stored = MemoryGitHubStore(identity="fingerprint-one", viewer="fixture-user", shared=shared).load()
    assert set(stored.projects) == {p1, p2} and set(stored.project_items) == {p1, p2}


def test_a_projects_failure_never_fails_the_issue_pass():
    fake, *_ = projects_seeded()
    fake.fail_projects = "permission"
    monitor = monitor_for(fake, {}, projects=ON)
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view["status"] == "ready" and view["error"] is None
    sync = monitor.projects(start=False)["projects_sync"]
    assert sync["status"] == "error" and "read:project" in sync["error"]


def test_a_spent_budget_defers_project_items_and_keeps_the_old_rows():
    fake, p1, *_ = projects_seeded()
    monitor = monitor_for(fake, {}, projects=ON, point_budget=1000)
    monitor.refresh()
    moved = next(iter(fake._project(p1)["items"]))
    fake.move_item(p1, moved, "Done")
    monitor._fixed_sync["point_budget"] = 1  # the listing alone spends it
    fake.log.clear()
    monitor.refresh()
    assert item_calls(fake) == []
    sync = monitor.projects(start=False)["projects_sync"]
    assert sync["status"] == "partial" and sync["deferred"] >= 1
    assert sorted(i["status"] for i in monitor.project_items(p1, start=False)["items"]) == ["Done", "Todo", "Todo"]
    monitor._fixed_sync["point_budget"] = 1000
    monitor.refresh()
    assert sorted(i["status"] for i in monitor.project_items(p1, start=False)["items"]) == ["Done", "Done", "Todo"]


def test_a_project_over_the_item_limit_keeps_its_old_items():
    fake, p1, *_ = projects_seeded()
    monitor = monitor_for(fake, {}, projects={"enabled": True, "item_limit": 3})
    monitor.refresh()
    fake.add_item(p1)
    monitor.refresh()
    assert len(monitor.project_items(p1, start=False)["items"]) == 3
    row = next(p for p in monitor.projects(start=False)["projects"] if p["project_id"] == p1)
    assert row["complete"] is False and "item_limit" in row["error"]


def test_a_restart_serves_stored_projects_with_zero_calls():
    fake, p1, *_ = projects_seeded()
    shared = {}
    monitor_for(fake, shared, projects=ON).refresh()
    fake.log.clear()
    fresh = monitor_for(fake, shared, projects=ON)
    assert {p["project_id"] for p in fresh.projects(start=False)["projects"]} >= {p1}
    assert len(fresh.project_items(p1, start=False)["items"]) == 3
    assert fake.log == []


def test_an_unknown_project_is_not_found():
    import pytest

    from herdr_engineering.github import GitHubError
    fake, *_ = projects_seeded()
    monitor = monitor_for(fake, {}, projects=ON)
    monitor.refresh()
    with pytest.raises(GitHubError):
        monitor.project_items("PVT_nope", start=False)


def test_a_restart_with_yaml_enabled_projects_serves_them_and_never_says_sync_is_off(monkeypatch):
    import herdr_engineering.github as github
    monkeypatch.setattr(github, "_configured_projects", lambda: {"enabled": True})
    fake, p1, *_ = projects_seeded()
    shared = {}
    monitor_for(fake, shared).refresh()
    fake.log.clear()
    fresh = monitor_for(fake, shared)
    view = fresh.projects(start=False)
    assert p1 in {p["project_id"] for p in view["projects"]}
    assert view["projects_sync"]["enabled"] is True and view["projects_sync"]["status"] == "stale"
    assert len(fresh.project_items(p1, start=False)["items"]) == 3
    assert fake.log == []
    monkeypatch.setattr(github, "_configured_projects", lambda: None)
    empty = monitor_for(FakeProjects(), {})
    assert empty.projects(start=False)["projects_sync"]["status"] == "disabled"


def test_projects_have_a_progress_floor_and_a_cut_short_read_resumes_at_its_cursor():
    fake, p1, p2, p3 = projects_seeded()  # p1 has 3 items: two pages at page_size 2
    monitor = monitor_for(fake, {}, projects=ON, point_budget=1)
    budget_spent_by_listings = []

    def items_this_pass():
        fake.log.clear()
        monitor._retry_at = 0
        monitor.refresh()
        budget_spent_by_listings.append(monitor._spent)
        return [(entry[1], entry[2]) for entry in item_calls(fake)]
    # pass 1: the repository and project listings spend the budget (listings count); nothing is deferred yet
    assert items_this_pass() == []
    assert budget_spent_by_listings[-1] >= 1 + 3, "one repository listing and three owner listings were charged"
    assert monitor.projects(start=False)["projects_sync"]["deferred"] == 3
    # pass 2: the longest-deferred project still makes one page (the floor), then stops at its cursor
    first = items_this_pass()
    assert len(first) == 1 and first[0][1] is None
    floor = first[0][0]
    # pass 3: it resumes at its cursor instead of starting again from page 1
    second = items_this_pass()
    assert second[0][0] == floor and second[0][1] is not None
    assert len(monitor.project_items(floor, start=False)["items"]) == len(fake._project(floor)["items"])
    # every changed project gets there, one floor read per pass
    for _ in range(6):
        items_this_pass()
    assert monitor.projects(start=False)["projects_sync"]["deferred"] == 0
    assert {len(monitor.project_items(p, start=False)["items"]) for p in (p1, p2, p3)} == {3, 1}


def test_a_resumed_project_that_changed_meanwhile_is_read_again_from_page_one():
    fake, p1, *_ = projects_seeded()
    monitor = monitor_for(fake, {}, projects=ON, point_budget=1)
    for _ in range(2):  # pass 1 defers everything; pass 2 reads one page of the floor project
        monitor._retry_at = 0
        monitor.refresh()
    floor = next(pid for pid, state in monitor._project_resume.items())
    fake.add_item(floor)  # the project changed: its probe moved
    fake.log.clear()
    monitor._retry_at = 0
    monitor.refresh()
    calls = [(entry[1], entry[2]) for entry in item_calls(fake)]
    assert calls and calls[0] == (floor, None), "a changed project restarts at page 1"


def test_a_move_that_does_not_bump_the_project_is_caught_by_the_daily_full_read():
    fake, p1, *_ = projects_seeded()
    clock = [1_000_000.0]
    monitor = monitor_for(fake, {}, projects=ON, now=lambda: clock[0], full_reconcile_interval=86400)
    monitor.refresh()
    moved = next(iter(fake._project(p1)["items"]))
    fake.move_item(p1, moved, "Done", bump_project=False)
    for hours in (1, 12, 23):
        clock[0] = 1_000_000.0 + hours * 3600
        fake.log.clear()
        monitor._retry_at = 0
        monitor.refresh()
        assert item_calls(fake, p1) == [], f"no item read {hours} h after the last full read"
    assert sorted(i["status"] for i in monitor.project_items(p1, start=False)["items"]) == ["Done", "Todo", "Todo"]
    clock[0] = 1_000_000.0 + 86400 + 60
    fake.log.clear()
    monitor._retry_at = 0
    monitor.refresh()
    assert {entry[1] for entry in item_calls(fake)} >= {p1}
    assert len([entry for entry in item_calls(fake, p1) if entry[2] is None]) == 1, "one full read of the project"
    assert sorted(i["status"] for i in monitor.project_items(p1, start=False)["items"]) == ["Done", "Done", "Todo"]
