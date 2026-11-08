"""Checkpoint lock waiting and the logged reason for a rejected candidate.

Real PostgreSQL (HERDR_PERF_PG_DSN), like the other durable projection tests.
"""
import asyncio
import logging
import os
import time

import pytest

from herdr_engineering.control_plane.projection_recovery import MAX_REASON_CHARS, _rejection_detail
from herdr_engineering.control_plane.projection_store import CheckpointInvalid, ProjectionStore

from .test_projection_checkpoint_e2e import DSN, durable, insert  # noqa: F401

needs_pg = pytest.mark.skipif(not DSN, reason="dedicated HERDR_PERF_PG_DSN required")


def test_rejection_detail_is_bounded_printable_and_checkpoint_only():
    assert _rejection_detail(CheckpointInvalid('frontier is outside')) == ': frontier is outside'
    long = _rejection_detail(CheckpointInvalid('x' * 5000))
    assert len(long) == len(': ') + MAX_REASON_CHARS and long.endswith('...')
    assert '\n' not in _rejection_detail(CheckpointInvalid('a\nb\x1b[31m'))
    assert _rejection_detail(KeyError('postgres://u:secret@h/db')) == ''
    assert _rejection_detail(ValueError('row data')) == ''


@needs_pg
def test_publisher_wait_times_out_with_reason_then_succeeds_after_release(durable):  # noqa: F811
    import asyncpg
    conn, run, _ = durable

    async def scenario():
        other = await asyncpg.connect(DSN, server_settings={
            "search_path": await conn.fetchval("SELECT current_schema()")})
        try:
            holder = ProjectionStore(other).publisher()
            await holder.__aenter__()
            start = time.monotonic()
            with pytest.raises(CheckpointInvalid, match='another projection checkpoint materializer'):
                async with ProjectionStore(conn).publisher(wait=0.6, poll=0.05):
                    pass
            assert 0.5 <= time.monotonic() - start < 3
            # No wait: fails immediately (the worker's behaviour is unchanged).
            start = time.monotonic()
            with pytest.raises(CheckpointInvalid):
                async with ProjectionStore(conn).publisher():
                    pass
            assert time.monotonic() - start < 0.5
            # Released mid-wait: the waiter gets the lock.
            asyncio.get_running_loop().call_later(
                0.3, lambda: asyncio.ensure_future(holder.__aexit__(None, None, None)))
            async with ProjectionStore(conn).publisher(wait=5, poll=0.05):
                return True
        finally:
            await other.close()

    assert run(scenario()) is True


@needs_pg
def test_rejected_candidate_logs_the_reason(durable, caplog):  # noqa: F811
    conn, run, recover = durable
    run(insert(conn, "e1", 1, {"status": "running"}))
    recover(publish=True)
    # Break the capsule's frontier so the candidate fails validation for a stated reason.
    run(conn.execute("UPDATE projection_checkpoints SET frontier = frontier + 1"))
    with caplog.at_level(logging.WARNING):
        recover(publish=False)
    rejected = [r.getMessage() for r in caplog.records if 'candidate rejected' in r.getMessage()]
    assert rejected, caplog.text
    assert 'CheckpointInvalid: ' in rejected[0] and 'frontier' in rejected[0]
    assert os.environ.get("HERDR_PERF_PG_DSN") not in rejected[0]
