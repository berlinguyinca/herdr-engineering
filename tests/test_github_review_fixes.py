"""PR #235 review fixes: store I/O off the monitor lock, bounded resync, point budget, 401/lock status."""
from __future__ import annotations

import logging
import threading

import pytest

from herdr_engineering import github as gh
from herdr_engineering.config import InvalidError, github_sync, load_config
from herdr_engineering.control_plane.github_store import MemoryGitHubStore
from herdr_engineering.github import GitHubError, GitHubMonitor
from tests.test_github_incremental import FakeGitHub, monitor_for, seeded


def _recording_factory(shared, monitor_ref, log):
    class Store(MemoryGitHubStore):
        def load(self, **kw):
            log.append(("load", kw.get("rows", True), self._viewer, monitor_ref[0]._lock._is_owned()))
            return super().load(**kw)

        def revision(self):
            log.append(("revision", None, self._viewer, monitor_ref[0]._lock._is_owned()))
            return super().revision()
    return lambda identity, viewer=None: Store(identity=identity, viewer=viewer, shared=shared)


def test_store_is_opened_and_read_outside_the_monitor_lock_and_loaded_once():
    fake, shared = seeded(), {}
    monitor_for(fake, shared).refresh()
    log, ref = [], [None]
    monitor = GitHubMonitor(provider_factory=lambda: fake, store=_recording_factory(shared, ref, log))
    ref[0] = monitor
    monitor.repositories(start=False)  # first use: opens the fingerprint store
    monitor.refresh()                  # the pass binds the viewer
    assert log and not any(entry[3] for entry in log), log  # never with the lock held
    full = [e for e in log if e[0] == "load" and e[1]]
    bare = [e for e in log if e[0] == "load" and not e[1]]
    assert len(full) == 1 and full[0][2] is None            # the catalog is read once ...
    assert len(bare) == 1 and bare[0][2] == "fixture-user"  # ... the viewer bind only claims it


def test_a_hung_store_load_does_not_stall_other_requests():
    fake, shared = seeded(), {}
    monitor_for(fake, shared).refresh()
    gate, entered = threading.Event(), threading.Event()

    class Hung(MemoryGitHubStore):
        def load(self, **kw):
            entered.set()
            gate.wait(10)
            return super().load(**kw)
    monitor = GitHubMonitor(provider_factory=lambda: fake, store=lambda identity, viewer=None: Hung(
        identity=identity, viewer=viewer, shared=shared))
    opener = threading.Thread(target=monitor._sync_credentials, daemon=True)
    opener.start()
    assert entered.wait(5)
    done = []
    reader = threading.Thread(target=lambda: done.append(monitor.revision()), daemon=True)
    reader.start()
    reader.join(2)
    try:
        assert done, "a request blocked on the monitor lock while the database was being read"
    finally:
        gate.set()
        opener.join(5)
    assert monitor.repositories(start=False)["repositories"]  # hydrated after the swap


def test_since_resync_is_a_bounded_first_page(monkeypatch):
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    monkeypatch.setattr(gh, "RESYNC_PAGE", 2)
    far = monitor.revision() + 10_000  # a since above the current revision always resyncs
    body = monitor.changes(kind="repositories", since=far, start=False)
    assert body["resync"] is True and len(body["repositories"]) == 2
    assert body["pagination"]["total"] == 3 and body["pagination"]["has_next"] is True
    issues = monitor.changes(kind="issues", since=far, start=False)
    assert issues["resync"] is True and len(issues["issues"]) == 2 and issues["pagination"]["total"] == 8


def test_since_resync_never_asks_the_store_for_rows():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    seen = []
    original = monitor._store.changes_since
    monitor._store.changes_since = lambda since, **kw: seen.append(kw) or original(since, **kw)
    monitor.changes(kind="issues", since=monitor.revision() + 5, start=False)
    assert seen == [{"full": False}]


def test_point_budget_per_hour_is_validated():
    assert github_sync(None)["point_budget"] == 400
    assert github_sync({"point_budget": 5000, "interval": 3600})["point_budget"] == 5000
    with pytest.raises(InvalidError, match="per hour"):
        github_sync({"point_budget": 1000, "interval": 300})  # 12000 points an hour
    with pytest.raises(InvalidError, match="point_budget"):
        github_sync({"point_budget": 400, "interval": 60})


def test_issue_detail_fetches_are_charged_to_the_point_budget():
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared, point_budget=2, now=lambda: 4_000_000_000.0)
    monitor.refresh()
    fake.rate_limit = {"cost": 1}
    monitor.issue_detail("github.com/acme/app", 1)
    monitor.issue_detail("github.com/acme/app", 2)
    assert monitor._detail_window[1] == 2
    with pytest.raises(GitHubError) as exc:
        monitor.issue_detail("github.com/acme/app", 3)  # budget spent for this interval
    assert exc.value.reason == "rate_limited" and fake.calls["detail"] == 2
    assert monitor.issue_detail("github.com/acme/app", 1)["issue"]  # a cached body is still served


def test_one_401_keeps_the_stored_catalog_and_a_second_purges_it():
    from tests.test_github_incremental import _revoke, numbers
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    _revoke(fake)
    monitor.refresh()
    assert MemoryGitHubStore(identity=fake.credential_identity, shared=shared).load().repositories
    assert numbers(monitor)  # nothing wiped yet
    monitor._retry_at = 0
    monitor.refresh()
    assert MemoryGitHubStore(identity=fake.credential_identity, shared=shared).load().repositories == {}


def test_a_success_resets_the_consecutive_401_count():
    from tests.test_github_incremental import _revoke
    fake, shared = seeded(), {}
    monitor = monitor_for(fake, shared)
    monitor.refresh()
    good = (fake.repositories_page, fake.issues_page, fake.issue)
    _revoke(fake)
    monitor.refresh()
    fake.repositories_page, fake.issues_page, fake.issue = good
    monitor._retry_at = 0
    monitor.refresh()
    assert monitor._auth_failures == 0


def test_a_pass_that_cannot_take_the_advisory_lock_says_so(caplog):
    import contextlib
    fake, shared = seeded(), {}

    class Locked(MemoryGitHubStore):
        @contextlib.contextmanager
        def advisory_lock(self):
            yield False
    monitor = GitHubMonitor(provider_factory=lambda: fake, store=lambda identity, viewer=None: Locked(
        identity=identity, viewer=viewer, shared=shared))
    with caplog.at_level(logging.WARNING):
        monitor.refresh()
    assert monitor.snapshot(start=False)["status"] == "advisory_locked"
    assert "advisory lock" in caplog.text and fake.calls["repos"] == 0


def test_local_github_section_warns_that_it_is_not_read(tmp_path, caplog):
    p = tmp_path / "c.yaml"
    p.write_text("version: 1\ngithub:\n  organizations: [acme]\n")
    with caplog.at_level(logging.WARNING):
        load_config([p])
    assert "not read from this file" in caplog.text
    caplog.clear()
    q = tmp_path / "d.yaml"
    q.write_text("version: 1\n")
    with caplog.at_level(logging.WARNING):
        load_config([q])
    assert "github" not in caplog.text
