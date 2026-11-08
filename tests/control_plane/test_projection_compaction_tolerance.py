"""Fabric compaction deletes interior service rows; a checkpoint must survive that."""
import json

import pytest

from tests.control_plane.test_projection_checkpoint_e2e import DSN, durable  # noqa: F401

pytestmark = pytest.mark.skipif(not DSN, reason="dedicated HERDR_PERF_PG_DSN required")

SERVICES = 6
OBSERVED = 8
PAYLOAD = {"service_name": "S", "host_id": "h", "port": 8000, "status": "healthy"}


async def put(conn, event_id, sequence, identity, event_type, second, entity_type="service"):
    await conn.execute(
        "INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,"
        "source_timestamp,ingest_timestamp) VALUES($1,$2,$3,$4,$5,$6,to_timestamp($7),to_timestamp($7))",
        event_id, sequence, event_type, entity_type, identity, json.dumps(PAYLOAD), float(second))


async def seed(conn):
    for s in range(SERVICES):
        await put(conn, f"s{s}-reg", 0, f"svc-{s}", "ServiceRegistered", 1000 + s)
        for n in range(1, OBSERVED):
            await put(conn, f"s{s}-{n}", n, f"svc-{s}", "ServiceListenerObserved", 1000 + n * 10 + s)


def compact_sql():
    # keep each service's first and last observation, like fabric_compaction does
    keep = ",".join(f"'s{s}-{n}'" for s in range(SERVICES) for n in (1, OBSERVED - 1))
    return ("DELETE FROM fabric_events WHERE entity_type='service' "
            f"AND event_type='ServiceListenerObserved' AND event_id NOT IN ({keep})")


def snapshot(repo):
    return {
        "services": sorted(repo.list_services(), key=lambda row: row["service_id"]),
        "streams": {f"svc-{n}": repo.stream("service", f"svc-{n}") for n in range(SERVICES)},
    }


def test_checkpoint_survives_service_compaction_and_equals_full_replay(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    deleted = run(conn.execute(compact_sql()))
    assert deleted == f"DELETE {SERVICES * (OBSERVED - 3)}"
    after, source, _ = recover()
    assert source.recovery_info["mode"] == "checkpoint", source.recovery_info
    run(conn.execute("DELETE FROM projection_checkpoints"))
    full, source, _ = recover()
    assert source.recovery_info["mode"] == "full_replay"
    assert snapshot(after) == snapshot(full)


def test_compaction_plus_appended_tail_equals_full_replay(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    run(conn.execute(compact_sql()))
    run(put(conn, "tail", OBSERVED, "svc-0", "ServiceListenerObserved", 9000))
    after, source, _ = recover()
    assert source.recovery_info["mode"] == "checkpoint", source.recovery_info
    run(conn.execute("DELETE FROM projection_checkpoints"))
    full, _, _ = recover()
    assert snapshot(after) == snapshot(full)


@pytest.mark.parametrize("statement", [
    "DELETE FROM fabric_events WHERE entity_type='mission'",
    "UPDATE fabric_events SET actor='{\"id\":\"x\"}' WHERE event_id='s0-1'",
    "TRUNCATE fabric_events",
])
def test_other_structural_mutations_still_reject(durable, statement):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    run(put(conn, "m-1", 0, "mission-1", "MissionCreated", 1100, entity_type="mission"))
    recover(publish=True)
    run(conn.execute(statement))
    _, source, _ = recover()
    assert source.recovery_info["mode"] == "full_replay"


def test_legacy_prefix_accounting_holds_for_unpositioned_deleted_rows(durable):  # noqa: F811
    conn, run, recover = durable

    async def legacy():
        await conn.execute("ALTER TABLE fabric_events DISABLE TRIGGER projection_position_before_write")
        await conn.execute("ALTER TABLE fabric_events DISABLE TRIGGER projection_change_after_write")
        await seed(conn)
        await conn.execute("ALTER TABLE fabric_events ENABLE TRIGGER projection_position_before_write")
        await conn.execute("ALTER TABLE fabric_events ENABLE TRIGGER projection_change_after_write")
        count = await conn.fetchval("SELECT count(*) FROM fabric_events WHERE projection_position IS NULL")
        await conn.execute("UPDATE projection_checkpoint_control SET legacy_rows=$1", count)
    run(legacy())
    recover(publish=True)
    run(conn.execute(compact_sql()))
    assert run(conn.fetchval("SELECT count(*) FROM projection_event_changes WHERE operation='D' "
                             "AND had_position IS NULL")) > 0
    after, source, _ = recover()
    assert source.recovery_info["mode"] == "checkpoint", source.recovery_info
    run(conn.execute("DELETE FROM projection_checkpoints"))
    full, _, _ = recover()
    assert snapshot(after) == snapshot(full)


def test_legacy_accounting_drift_still_rejects(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    run(conn.execute(compact_sql()))
    run(conn.execute("UPDATE projection_checkpoint_control SET legacy_rows=legacy_rows+1"))
    _, source, _ = recover()
    assert source.recovery_info["mode"] == "full_replay"
