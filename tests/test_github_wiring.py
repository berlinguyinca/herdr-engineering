"""The persistent GitHub catalog store is wired into control-plane-web.

``control_plane.pg_dsn`` set: the monitor gets a Postgres-backed store factory
(one asyncpg loop shared by every store it builds). Unset: an in-memory store
that lives as long as the process, as before. ``github.sync.*`` is read from
the fleet file's ``github`` section on every pass, like ``github.organizations``.
"""
from __future__ import annotations

from collections import Counter

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.github_store import GitHubStore, MemoryGitHubStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.github import GitHubMonitor, build_monitor


def _load_incremental():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("_github_incremental_fakes",
                                                  Path(__file__).with_name("test_github_incremental.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fakes = _load_incremental()
FakeGitHub, monitor_for, seeded = _fakes.FakeGitHub, _fakes.monitor_for, _fakes.seeded

DSN = "postgresql://herdr@127.0.0.1:1/never-connected"  # GitHubStore connects lazily


def test_without_pg_dsn_the_store_is_in_memory_and_survives_factory_calls():
    monitor = build_monitor(None)
    try:
        first = monitor._store_factory("fp", None)
        assert isinstance(first, MemoryGitHubStore)
        first.upsert_repositories([{"repository_id": "github.com/a/b", "name_with_owner": "a/b"}], None)
        second = monitor._store_factory("fp", "viewer")
        assert second.revision() == first.revision() == 1  # one process-lifetime catalog
    finally:
        monitor.close()


def test_with_pg_dsn_the_store_is_postgres_and_shares_one_loop():
    monitor = build_monitor(DSN)
    try:
        first, second = monitor._store_factory("fp", None), monitor._store_factory("fp", "viewer")
        assert isinstance(first, GitHubStore) and isinstance(second, GitHubStore)
        assert first._run == second._run  # one _LoopRunner thread, not one per store
        assert "never-connected" not in repr(first)
    finally:
        monitor.close()


def test_control_plane_server_builds_the_monitor_from_pg_dsn():
    with_db = ControlPlaneServer(ControlPlaneRepo(), github_pg_dsn=DSN)
    without = ControlPlaneServer(ControlPlaneRepo())
    try:
        assert isinstance(with_db.github_monitor._store_factory("fp", None), GitHubStore)
        assert isinstance(without.github_monitor._store_factory("fp", None), MemoryGitHubStore)
        assert with_db.github_monitor._thread is None  # constructing never starts polling
    finally:
        with_db.shutdown()
        without.shutdown()


class ClosingStore(MemoryGitHubStore):
    closed: list = []

    def close(self):
        self.closed.append(self)


def test_superseded_stores_are_closed():
    fake, shared = seeded(), {}
    made = []

    def factory(identity, viewer=None):
        made.append(ClosingStore(identity=identity, viewer=viewer, shared=shared))
        return made[-1]
    ClosingStore.closed = []
    monitor = GitHubMonitor(provider_factory=lambda: fake, store=factory)
    monitor.refresh()  # fingerprint store, then the viewer-tagged store
    assert len(made) == 2 and ClosingStore.closed == [made[0]]
    fake.credential_identity = "fingerprint-two"  # token rotation
    monitor.refresh()
    assert made[1] in ClosingStore.closed
    monitor.close()
    assert made[-1] in ClosingStore.closed


def test_full_reconcile_interval_is_read_from_yaml_each_pass(fake_host_config):
    fake = FakeGitHub(page_size=100)
    for index in range(4):
        fake.add_issue(f"acme/r{index}")
    clock = [1_000_000.0]
    fake_host_config({"github": {"sync": {"full_reconcile_interval": 100000}}})
    monitor = monitor_for(fake, {}, now=lambda: clock[0], reconcile_per_pass=4)
    monitor.refresh()
    clock[0] += 5000
    fake.log.clear()
    monitor.refresh()
    assert [e for e in fake.log if e[0] == "issues"] == []  # not due under 100000 s
    fake_host_config({"github": {"sync": {"full_reconcile_interval": 1000}}})
    fake.log.clear()
    monitor.refresh()
    assert len({e[1] for e in fake.log if e[0] == "issues"}) == 4  # the edit applied without a restart


def test_invalid_sync_settings_stop_the_pass_as_invalid_config(fake_host_config):
    fake = seeded()
    fake_host_config({"github": {"sync": {"full_reconcile_interval": "daily"}}})
    monitor = monitor_for(fake, {})
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view["status"] == "invalid_config"
    assert "github.sync.full_reconcile_interval" in view["error"]
    assert fake.calls == Counter()
