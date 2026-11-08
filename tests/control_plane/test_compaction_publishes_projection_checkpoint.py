"""A drained compaction pass publishes a projection checkpoint that already covers its deletes (real PostgreSQL)."""
import logging
from urllib.parse import urlencode

import pytest

from herdr_engineering.control_plane.fabric_compaction import CompactionSettings, FabricCompactor
from herdr_engineering.control_plane.projection_refresh import refresh_checkpoint
from herdr_engineering.control_plane.projection_store import ProjectionStore
from tests.control_plane.test_projection_checkpoint_e2e import DSN, durable  # noqa: F401
from tests.control_plane.test_projection_compaction_tolerance import seed

pytestmark = pytest.mark.skipif(not DSN, reason="dedicated HERDR_PERF_PG_DSN required")

SETTINGS = dict(enabled=True, dry_run=False, rebuild_checkpoint=False, min_age_days=1, chunk_pause_seconds=0)


def publisher_for(conn, run):
    """The production publish path (refresh_checkpoint) aimed at the test schema."""
    schema = run(conn.fetchval("SELECT current_schema()"))
    joiner = "&" if "?" in DSN else "?"
    dsn = f"{DSN}{joiner}{urlencode({'search_path': schema})}"
    return lambda wait: refresh_checkpoint(dsn, lock_wait=wait)


def compactor(conn, run, **overrides):
    settings = CompactionSettings(**{**SETTINGS, **overrides})
    return FabricCompactor(settings, clock=iter(range(0, 10**6, 10**5)).__next__,
                           projection_publisher=publisher_for(conn, run))


def checkpoints(conn, run):
    return [tuple(r) for r in run(conn.fetch("SELECT checkpoint_id, frontier FROM projection_checkpoints ORDER BY 1"))]


def last_delete_position(conn, run):
    return run(conn.fetchval("SELECT max(position) FROM projection_event_changes WHERE operation='D'"))


def test_drained_pass_publishes_a_checkpoint_covering_the_deletes(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    before = checkpoints(conn, run)
    job = compactor(conn, run, publish_projection_checkpoint=True)
    outcome = run(job.tick(conn))
    assert outcome.startswith("archived") and "projection checkpoint published" in outcome, outcome
    after = checkpoints(conn, run)
    assert len(after) == len(before) + 1
    assert after[-1][1] >= last_delete_position(conn, run)
    _, source, _ = recover()
    assert source.recovery_info["mode"] == "checkpoint", source.recovery_info
    assert source.recovery_info["tail_events"] == 0
    assert job._projection_pending is False


def test_setting_off_publishes_nothing(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    before = checkpoints(conn, run)
    outcome = run(compactor(conn, run).tick(conn))
    assert outcome.startswith("archived") and "projection" not in outcome
    assert checkpoints(conn, run) == before


def test_a_pass_that_moved_nothing_publishes_nothing(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    job = compactor(conn, run, publish_projection_checkpoint=True)
    run(job.tick(conn))                                  # moves the interior rows and publishes
    marker = checkpoints(conn, run)
    job._last = None
    outcome = run(job.tick(conn))
    assert outcome.startswith("archived 0 rows") and "projection" not in outcome
    assert checkpoints(conn, run) == marker


def test_a_held_lock_keeps_the_pass_successful_and_the_publish_pending(durable, caplog):  # noqa: F811
    import asyncpg
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    before = checkpoints(conn, run)
    schema = run(conn.fetchval("SELECT current_schema()"))

    async def hold():
        other = await asyncpg.connect(DSN, server_settings={"search_path": schema})
        holder = ProjectionStore(other).publisher()
        await holder.__aenter__()
        return other, holder

    other, holder = run(hold())
    job = compactor(conn, run, publish_projection_checkpoint=True, projection_lock_wait_seconds=0.3)
    try:
        with caplog.at_level(logging.WARNING):
            outcome = run(job.tick(conn))
        assert outcome.startswith("archived") and "projection checkpoint deferred" in outcome, outcome
        assert any("projection checkpoint" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert DSN not in caplog.text
        assert job._projection_pending is True
        assert checkpoints(conn, run) == before
    finally:
        run(holder.__aexit__(None, None, None))
        run(other.close())
    job._last = None                                     # the next pass moves nothing but retries the pending publish
    outcome = run(job.tick(conn))
    assert "projection checkpoint published" in outcome, outcome
    assert job._projection_pending is False
    assert len(checkpoints(conn, run)) == len(before) + 1
