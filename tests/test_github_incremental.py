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


def test_point_budget_is_read_from_yaml(fake_host_config):
    fake_host_config({"github": {"sync": {"point_budget": 5}}})
    fake, monitor, clock = ladder()
    monitor._fixed_sync["point_budget"] = None
    monitor.refresh()
    assert len(monitor.snapshot(start=False)["deferred_repos"]) == 2


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
