"""Compactable (service) entities must fold appended events incrementally."""
import json

import pytest

from herdr_engineering.control_plane import projection_recovery as recovery
from tests.control_plane.test_projection_checkpoint_e2e import DSN, durable  # noqa: F401

pytestmark = pytest.mark.skipif(not DSN, reason="dedicated HERDR_PERF_PG_DSN required")

SERVICES = 12
PER_SERVICE = 30


async def put(conn, event_id, sequence, identity, payload, second):
    await conn.execute(
        "INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,"
        "source_timestamp,ingest_timestamp) VALUES($1,$2,'ServiceRegistered','service',$3,$4,"
        "to_timestamp($5),to_timestamp($5))",
        event_id, sequence, identity, json.dumps(payload), float(second))


async def seed(conn):
    for s in range(SERVICES):
        for n in range(PER_SERVICE):
            await put(conn, f"s{s}-{n}", n, f"svc-{s}",
                      {"service_name": f"S{s}", "host_id": "h", "port": 8000 + s,
                       "status": "healthy", "description": f"v{n}"}, 1000 + n * 10 + s)


def test_appended_service_events_do_not_reload_whole_streams(durable, monkeypatch):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    _, first, counts = recover(publish=True)
    assert first.recovery_info["mode"] == "full_replay"
    assert counts["events"] == SERVICES * PER_SERVICE

    async def tail():
        for s in range(SERVICES):
            await put(conn, f"s{s}-new", PER_SERVICE, f"svc-{s}",
                      {"status": "unavailable"}, 5000 + s)
    run(tail())
    # Loading even one whole service stream would exceed this budget.
    monkeypatch.setattr(recovery, "MAX_DIRTY_ROWS", PER_SERVICE - 1)
    fast, second, _ = recover()
    assert second.recovery_info["mode"] == "checkpoint", second.recovery_info
    assert second.recovery_info["event_rows_scanned"] == SERVICES
    assert fast.get_service("svc-0")["status"] == "unavailable"


def test_chained_publish_keeps_appending_incrementally(durable, monkeypatch):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    monkeypatch.setattr(recovery, "MAX_DIRTY_ROWS", PER_SERVICE - 1)
    for rnd in range(3):
        async def tail(rnd=rnd):
            for s in range(SERVICES):
                await put(conn, f"s{s}-r{rnd}", PER_SERVICE + rnd, f"svc-{s}",
                          {"status": "unavailable" if rnd % 2 == 0 else "healthy"},
                          5000 + rnd * 100 + s)
        run(tail())
        _, source, _ = recover(publish=True)
        assert source.recovery_info["mode"] == "checkpoint", (rnd, source.recovery_info)
        assert source.recovery_info["event_rows_scanned"] == SERVICES


def snapshot(repo):
    return {
        "services": sorted(repo.list_services(), key=lambda row: row["service_id"]),
        "streams": {f"svc-{n}": repo.stream("service", f"svc-{n}") for n in range(SERVICES)},
    }


def test_incremental_equals_full_replay_across_chained_checkpoints(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    for rnd in range(3):
        async def tail(rnd=rnd):
            for s in range(0, SERVICES, 2):  # half the services stay quiet
                await put(conn, f"s{s}-q{rnd}", PER_SERVICE + rnd, f"svc-{s}",
                          {"status": ["unavailable", "healthy", "unavailable"][rnd],
                           "description": f"tail {rnd}"}, 6000 + rnd * 100 + s)
        run(tail())
        incremental, source, _ = recover(publish=True)
        assert source.recovery_info["mode"] == "checkpoint"
        assert source.recovery_info["event_rows_scanned"] == SERVICES // 2
    run(conn.execute("DELETE FROM projection_checkpoints"))
    full, source, _ = recover()
    assert source.recovery_info["mode"] == "full_replay"
    assert snapshot(incremental) == snapshot(full)


@pytest.mark.parametrize("second", [1285, 1290])
def test_stream_not_after_cursor_still_requires_authoritative_replay(durable, monkeypatch, second):  # noqa: F811
    """Backdated or tied tails must never be folded onto a newer prefix (guard kept)."""
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    run(put(conn, "late", PER_SERVICE, "svc-0", {"status": "unavailable"}, second))
    monkeypatch.setattr(recovery, "MAX_DIRTY_ROWS", PER_SERVICE - 1)
    _, source, _ = recover()
    assert source.recovery_info["mode"] == "full_replay"


def row(op, ts=None):
    return {"operation": op, "ingest_timestamp": ts}


def test_append_blocker_reasons_are_named():
    from datetime import UTC, datetime
    at = lambda sec: datetime.fromtimestamp(sec, tz=UTC)  # noqa: E731
    micro = 1_000_000
    assert recovery._append_blocker([row("I")], [row("I", at(11))], 10 * micro, True) is None
    assert recovery._append_blocker([row("I")], [row("I", at(10))], 10 * micro, True) == "not_after_cursor"
    assert recovery._append_blocker([row("I")], [row("I", at(10))], None, True) == "no_cursor"
    assert recovery._append_blocker([row("I")], [row("I", at(10))], None, False) is None
    assert recovery._append_blocker([row("I"), row("D")], [row("I", at(1))], None, False) == "non_insert"
    assert recovery._append_blocker([row("I"), row("I")], [row("I", at(1))], None, False) == "tail_rows_missing"


def test_budget_failure_names_dirty_types_and_blockers():
    summary = recovery._budget_summary(
        [("service", "a"), ("service", "b"), ("host", "h")],
        {("service", "not_after_cursor"): 2})
    assert "'service': 2" in summary and "service:not_after_cursor': 2" in summary
