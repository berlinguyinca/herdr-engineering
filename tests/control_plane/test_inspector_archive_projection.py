"""An archive pass and the projection checkpoint (real PostgreSQL): the published checkpoint covers the deletes."""
import json

import pytest

from herdr_engineering.control_plane.inspector_archive import (
    InspectorArchiver,
    InspectorArchiveSettings,
)
from tests.control_plane.test_compaction_publishes_projection_checkpoint import publisher_for
from tests.control_plane.test_projection_checkpoint_e2e import DSN, durable  # noqa: F401

pytestmark = pytest.mark.skipif(not DSN, reason="dedicated HERDR_PERF_PG_DSN required")

SETTINGS = dict(enabled=True, dry_run=False, rebuild_checkpoint=False, chunk_pause_seconds=0)


async def seed(conn):
    for n in range(1, 41):
        await conn.execute(
            "INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,"
            "source_timestamp,ingest_timestamp) VALUES($1,$2,$3,'inspector','s1',$4,now()-interval '20 days',"
            "now()-interval '20 days')",
            f"i{n}", n, "inspector.tool" if n % 2 else "inspector.message",
            json.dumps({"kind": "tool" if n % 2 else "message", "call_id": f"c{n}", "tool": "bash", "role": "user",
                        "text": f"t{n}", "_received_at": 1.0, "_sequence": n}))
    await conn.execute("INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,"
                       "source_timestamp,ingest_timestamp) VALUES('svc1',1,'ServiceRegistered','service','svc-1',$1,now(),now())",
                       json.dumps({"service_name": "S", "host_id": "h", "port": 8000, "status": "healthy"}))
    await conn.execute("INSERT INTO inspector_snapshots (id, schema_version, fingerprint, watermark, event_count, body) "
                       "VALUES (1, 1, 'f', 1000, 0, '\\x00'::bytea)")


def test_the_publish_after_a_pass_lets_the_next_boot_use_a_checkpoint(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)                                       # the checkpoint a running fleet already has
    job = InspectorArchiver(InspectorArchiveSettings(**SETTINGS, publish_projection_checkpoint=True),
                            clock=iter(range(0, 10**6, 10**5)).__next__, projection_publisher=publisher_for(conn, run))
    outcome = run(job.tick(conn))
    assert outcome.startswith("archived 40 inspector rows") and outcome.endswith("projection checkpoint published"), outcome
    assert run(conn.fetchval("SELECT count(*) FROM fabric_events WHERE entity_type='inspector'")) == 0
    assert run(conn.fetchval("SELECT count(*) FROM fabric_events_archive WHERE entity_type='inspector'")) == 40
    _, source, _ = recover()
    assert source.recovery_info["mode"] == "checkpoint", source.recovery_info
    assert source.recovery_info["tail_events"] == 0


def test_without_the_publish_the_old_checkpoint_is_refused_and_a_full_replay_is_correct(durable):  # noqa: F811
    """This is why the publish defaults to on: an inspector delete in the change-log tail needs a complete replay."""
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    outcome = run(InspectorArchiver(InspectorArchiveSettings(**SETTINGS, publish_projection_checkpoint=False),
                                    clock=iter(range(0, 10**6, 10**5)).__next__).tick(conn))
    assert outcome == "archived 40 inspector rows"
    _, source, _ = recover()
    assert source.recovery_info["mode"] != "checkpoint", source.recovery_info
