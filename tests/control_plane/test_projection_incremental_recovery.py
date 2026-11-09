"""Incremental recovery must equal a full replay, and fall back only on a huge divergence.

Real Postgres, no mocks. ``FULL`` forces the production full-replay fallback (tail budget of zero
row) so every test compares the two paths over the very same event log.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.projection_recovery import (
    CheckpointBudgets,
    checkpoint_budgets,
)
from tests.control_plane.test_projection_checkpoint_e2e import DSN, durable  # noqa: F401

pytestmark = pytest.mark.skipif(not DSN, reason="dedicated HERDR_PERF_PG_DSN required")

FULL = CheckpointBudgets(0, 1, 1)
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


async def put(conn, event_id, seq, kind, identity, event_type, payload, *, minute, actor=None):
    stamp = EPOCH + timedelta(minutes=minute)
    await conn.execute(
        "INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,actor,"
        "source_timestamp,ingest_timestamp) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$8)",
        event_id, seq, event_type, kind, identity, json.dumps(payload),
        json.dumps(actor) if actor else None, stamp)


async def token(conn, n, *, request=None, minute=None):
    request = request or f"req-{n}"
    await put(conn, f"tok-{n}", n, "token_usage", request, "token_usage.recorded",
              {"request_id": request, "model": "m", "input_tokens": n, "output_tokens": 2 * n,
               "mission_id": "mission-a"}, minute=n if minute is None else minute)


async def resource(conn, n, *, mission="mission-a", kind="cpu"):
    await put(conn, f"res-{n}", n, "resource_usage", f"{mission}:{kind}:{n}",
              "resource_usage.recorded", {"mission_id": mission, "kind": kind, "value": n},
              minute=n)


async def service(conn, n, status="healthy"):
    await put(conn, f"svc-{n}", n, "service", "svc-one", "ServiceRegistered",
              {"service_name": "App", "host_id": "h", "port": 80, "status": status}, minute=n)


async def probe(conn, n, kind, identity):
    await put(conn, f"{kind}-{n}", n, kind, identity, "ProbeObserved", {"v": n}, minute=n)


def projection(repo):
    """Everything a reader can observe from the recovered repository."""
    return json.dumps({"state": repo.export_state(include_history=False),
                       "ledger": repo.ledger.export_checkpoint(),
                       "tokens": repo._token_usage, "resources": {
                           "|".join(key): rows for key, rows in repo._resource_usage.items()}},
                      sort_keys=True, default=str)


def assert_equivalent(recover, budgets):
    incremental, source, _ = recover(budgets=budgets)
    assert source.recovery_info["mode"] == "checkpoint", source.recovery_info
    full, reference, _ = recover(budgets=FULL)
    assert reference.recovery_info["mode"] == "full_replay", reference.recovery_info
    assert projection(incremental) == projection(full)
    return incremental, source


async def seed_usage(conn):
    await service(conn, 0)
    for n in range(1, 9):
        await token(conn, n)
    for n in range(1, 4):
        await token(conn, 20 + n, request="req-shared")
        await resource(conn, 30 + n)


async def append_usage(conn):
    for n in range(100, 140):
        await token(conn, n, request=f"req-{n % 7}")  # new rows into old and new streams
        await resource(conn, n + 100, kind="mem")
    await service(conn, 300, "unavailable")


def test_budget_hard_cap_is_validated_and_defaults_above_the_soft_budget():
    assert checkpoint_budgets(None).hard_dirty_rows >= checkpoint_budgets(None).max_dirty_rows
    assert checkpoint_budgets({"max_dirty_rows": 7, "hard_dirty_rows": 9}) == (100_000, 7, 9)
    # A soft budget raised past the default hard cap drags the hard cap with it.
    assert checkpoint_budgets({"max_dirty_rows": 10_000_000}).hard_dirty_rows == 10_000_000
    with pytest.raises(ValueError, match="hard_dirty_rows"):
        checkpoint_budgets({"max_dirty_rows": 10, "hard_dirty_rows": 5})
    with pytest.raises(ValueError, match="hard_dirty_rows"):
        checkpoint_budgets({"hard_dirty_rows": 0})


def test_usage_appends_that_blow_the_old_budget_equal_a_full_replay(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed_usage(conn))
    recover(publish=True)
    run(append_usage(conn))
    # 40 token + 40 resource + 1 service rows; the pre-change budget (5 dirty rows) cannot hold them.
    _, source = assert_equivalent(recover, CheckpointBudgets(1000, 5, 5))
    assert source.recovery_info["folded_streams"] > 0
    assert source.recovery_info["event_rows_scanned"] <= 81


def test_retention_deletes_and_late_inserts_equal_a_full_replay(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed_usage(conn))
    recover(publish=True)

    async def churn():
        await conn.execute("DELETE FROM fabric_events WHERE event_id IN ('tok-1','tok-22','res-31')")
        await token(conn, 500, request="req-2", minute=0)   # backdated into an old stream
        await token(conn, 501, request="req-brand-new")
        await token(conn, 502, request="req-gone")
        await conn.execute("DELETE FROM fabric_events WHERE event_id='tok-502'")  # born and deleted
        await conn.execute("DELETE FROM fabric_events WHERE event_id='tok-3'")
        await token(conn, 3)                                # deleted, then re-inserted

    run(churn())
    _, source = assert_equivalent(recover, CheckpointBudgets(1000, 1, 1))
    assert source.recovery_info["folded_streams"] > 0


def test_a_fully_deleted_stream_disappears_like_in_a_full_replay(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed_usage(conn))
    recover(publish=True)
    run(conn.execute("DELETE FROM fabric_events WHERE entity_id='req-2'"))
    incremental, _ = assert_equivalent(recover, CheckpointBudgets(1000, 1, 1))
    assert ("token_usage", "req-2") not in incremental._events


def test_unfoldable_streams_over_budget_refold_under_the_hard_cap(durable, caplog):  # noqa: F811
    conn, run, recover = durable

    async def seed():
        for n in range(30):
            await probe(conn, n, "probe_kind", "p1")
            await probe(conn, 100 + n, "other_kind", "p2")

    run(seed())
    recover(publish=True)
    run(probe(conn, 500, "probe_kind", "p1"))
    with caplog.at_level("INFO"):
        _, source = assert_equivalent(recover, CheckpointBudgets(1000, 10, 100))
    assert source.recovery_info["refolded_rows"] == 31
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "over the dirty budget" in text and "refolding" in text, text


def test_a_huge_divergence_still_falls_back_to_full_replay(durable, caplog):  # noqa: F811
    conn, run, recover = durable

    async def seed():
        for n in range(30):
            await probe(conn, n, "probe_kind", "p1")

    run(seed())
    recover(publish=True)
    run(probe(conn, 500, "probe_kind", "p1"))
    with caplog.at_level("WARNING"):
        _, source, _ = recover(budgets=CheckpointBudgets(1000, 10, 20))
    assert source.recovery_info["mode"] == "full_replay"
    assert "hard cap" in " ".join(r.getMessage() for r in caplog.records)


def test_published_checkpoint_stays_fresh_without_a_full_replay(durable, caplog):  # noqa: F811
    conn, run, recover = durable
    run(seed_usage(conn))
    recover(publish=True)
    run(append_usage(conn))
    budgets = CheckpointBudgets(1000, 5, 5)
    with caplog.at_level("INFO"):
        _, publisher, _ = recover(publish=True, budgets=budgets)
    assert publisher.recovery_info["mode"] == "checkpoint"
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "projection recovery mode=checkpoint" in message and "rows_scanned=" in message
    # The next start finds nothing left to fold: the published checkpoint is current.
    _, restart, _ = recover(budgets=budgets)
    assert restart.recovery_info["mode"] == "checkpoint"
    assert restart.recovery_info["tail_events"] == 0
    assert restart.recovery_info["event_rows_scanned"] == 0
    assert restart.recovery_info["frontier"] == publisher.recovery_info["frontier"]
