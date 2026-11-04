# ruff: noqa: F811  (pytest fixtures are imported by name from wg_pg)
"""P05: the Postgres lease store answers the lease contract exactly as the in-memory one does, and across connections."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.team_task_leases_pg import PgLeaseStore
from herdr_engineering.control_plane.team_tasks import LeaseError, TaskSpec

from .wg_pg import Runner, wg_pg  # noqa: F401

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def task(task_id="t1", actions=("read", "post")):
    return TaskSpec(task_id=task_id, template_id="x", role="build", submitted_by="alice", room_id="r",
                    allowed_actions=frozenset(actions), max_runtime_s=600, deadline=T0 + timedelta(hours=1))


@pytest.fixture()
def env(wg_pg):
    r = Runner(wg_pg["dsn"])
    r.run(r.pool.execute("TRUNCATE team_task_leases"))
    clk = Clock()
    try:
        yield r, PgLeaseStore(r.pool, clock=clk), clk
    finally:
        r.close()


def refused(r, coro, match):
    with pytest.raises(LeaseError, match=match):
        r.run(coro)


def test_a_task_runs_once_across_a_retry_of_the_same_completion(env):
    r, s, _ = env
    lease = r.run(s.acquire(task(), "w1"))
    assert r.run(s.complete("t1", lease.token, {"pr": 7})) == {"pr": 7}
    assert r.run(s.complete("t1", lease.token, {"pr": 8})) == {"pr": 7}
    refused(r, s.acquire(task(), "w2"), "already_completed")


def test_after_a_crash_the_successor_wins_and_the_stale_worker_is_fenced_off(env):
    r, s, clk = env
    old = r.run(s.acquire(task(), "w1", ttl_s=30))
    refused(r, s.acquire(task(), "w2"), "held_by_another_worker")
    clk.now = T0 + timedelta(seconds=31)
    new = r.run(s.acquire(task(), "w2", ttl_s=30))
    assert new.token == old.token + 1
    assert not r.run(s.authorizes("t1", old.token, "post")) and r.run(s.authorizes("t1", new.token, "post"))
    refused(r, s.complete("t1", old.token, "late"), "stale_fencing_token")
    assert r.run(s.complete("t1", new.token, "ok")) == "ok"
    refused(r, s.complete("t1", old.token, "again"), "already_completed_by_another_token")


def test_an_aborted_execution_keeps_no_privileges(env):
    r, s, _ = env
    lease = r.run(s.acquire(task(), "w1"))
    assert r.run(s.authorizes("t1", lease.token, "post"))
    assert not r.run(s.authorizes("t1", lease.token, "execute"))
    r.run(s.abort("t1", lease.token))
    assert not r.run(s.authorizes("t1", lease.token, "post"))
    refused(r, s.complete("t1", lease.token, "after abort"), "lease_expired_or_revoked")
    assert r.run(s.acquire(task(), "w2")).token == lease.token + 1


def test_a_completed_lease_authorizes_nothing_and_an_expired_one_cannot_heartbeat(env):
    r, s, clk = env
    lease = r.run(s.acquire(task(), "w1", ttl_s=10))
    r.run(s.heartbeat("t1", lease.token, ttl_s=10))
    r.run(s.complete("t1", lease.token, "done"))
    assert not r.run(s.authorizes("t1", lease.token, "post"))
    l2 = r.run(s.acquire(task("t2"), "w1", ttl_s=10))
    clk.now = T0 + timedelta(seconds=11)
    refused(r, s.heartbeat("t2", l2.token), "lease_expired_or_revoked")


def test_concurrent_workers_on_separate_connections_produce_exactly_one_holder(env):
    r, s, _ = env

    async def race():
        out = await asyncio.gather(*(s.acquire(task(), f"w{i}") for i in range(12)), return_exceptions=True)
        return [x for x in out if not isinstance(x, Exception)], [x for x in out if isinstance(x, LeaseError)]
    won, lost = r.run(race())
    assert len(won) == 1 and len(lost) == 11 and won[0].token == 1
    assert all("held_by_another_worker" in str(e) for e in lost)
