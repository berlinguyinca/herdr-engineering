"""S1 + N4: the durable mirror skips unchanged snapshots (with a long heartbeat), writes changes and connection flips at
once, and rate-limits the persist-failure warning. Real store, real repo, no mocks."""
import logging
from datetime import UTC, datetime

from herdr_engineering.control_plane.mission_activity import (
    PERSIST_HEARTBEAT_SECONDS,
    PERSIST_INTERVAL_SECONDS,
    MissionActivityStore,
)

NOW = 1_800_000_000


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def payload(at, *, revision=1, connection='connected', heartbeat=None, **activity):
    return {'contract_version': 1, 'host_id': 'h', 'instance_id': 'i', 'session_id': 's', 'observed_at': stamp(at),
            'snapshot_at': stamp(at), 'mission_revision': revision, 'connection': connection,
            'mission': {'id': 'MSN-1', 'title': 'T', 'status': 'RUNNING'},
            'activity': {'health': 'active', 'lastHeartbeatAt': stamp(heartbeat or at), **activity}}


def world():
    clock, rows = [NOW], []
    store = MissionActivityStore(now=lambda: clock[0], persist=rows.append)
    return clock, rows, store


def send(store, clock, **kwargs):
    store.ingest('pi:MSN-1', payload(clock[0], **kwargs), source='pi')


def test_unchanged_snapshots_are_not_rewritten_until_the_heartbeat():
    clock, rows, store = world()
    send(store, clock)
    for _ in range(5):
        clock[0] += PERSIST_INTERVAL_SECONDS + 1
        send(store, clock)
        if clock[0] - NOW >= PERSIST_HEARTBEAT_SECONDS:
            break
    assert len(rows) == 1, 'only timestamps moved; nothing should have been written yet'
    clock[0] = NOW + PERSIST_HEARTBEAT_SECONDS
    send(store, clock)
    assert len(rows) == 2, 'the heartbeat write keeps restart freshness'
    clock[0] += PERSIST_INTERVAL_SECONDS + 1
    send(store, clock)
    assert len(rows) == 2


def test_a_changed_snapshot_is_written_after_the_throttle_interval():
    clock, rows, store = world()
    send(store, clock)
    clock[0] += 5
    send(store, clock, nextAction='Run tests')
    assert len(rows) == 1, 'still inside the throttle interval'
    clock[0] += PERSIST_INTERVAL_SECONDS
    send(store, clock, nextAction='Run tests')
    assert len(rows) == 2 and rows[1]['payload']['value']['activity']['nextAction'] == 'Run tests'


def test_a_connection_change_is_written_immediately_even_if_otherwise_unchanged():
    clock, rows, store = world()
    send(store, clock)
    clock[0] += 1
    store.ingest('pi:MSN-1', payload(clock[0], connection='disconnected'), source='pi')
    assert [r['payload']['value']['connection'] for r in rows] == ['connected', 'disconnected']


def test_a_heartbeat_only_difference_counts_as_unchanged():
    clock, rows, store = world()
    send(store, clock)
    clock[0] += PERSIST_INTERVAL_SECONDS + 1
    send(store, clock, heartbeat=clock[0])
    assert len(rows) == 1


def test_restart_hydrate_remembers_what_was_written():
    clock, rows, store = world()
    send(store, clock)
    fresh_rows = []
    fresh = MissionActivityStore(now=lambda: clock[0], persist=fresh_rows.append)
    fresh.hydrate(rows)
    clock[0] += PERSIST_INTERVAL_SECONDS + 1
    fresh.ingest('pi:MSN-1', payload(clock[0]), source='pi')
    assert fresh_rows == []


def test_bookkeeping_stays_bounded_with_many_missions():
    clock, rows, store = world()
    for index in range(40):
        body = payload(clock[0])
        body['mission']['id'] = f'MSN-{index}'
        store.ingest(f'pi:MSN-{index}', body, source='pi')
    assert len(store._persisted) <= 4 * store._max_missions


def test_persist_failures_warn_once_per_window_and_are_counted(caplog):
    clock = [NOW]

    def broken(_row):
        raise RuntimeError('db down')
    store = MissionActivityStore(now=lambda: clock[0], persist=broken)
    caplog.set_level(logging.WARNING)
    for _ in range(6):
        store.ingest('pi:MSN-1', payload(clock[0], connection='connected', revision=int(clock[0] - NOW) + 1), source='pi')
        clock[0] += 1
        store.ingest('pi:MSN-1', payload(clock[0], connection='updating', revision=int(clock[0] - NOW) + 1), source='pi')
        clock[0] += 1
    warnings = [r for r in caplog.records if 'persistence failed' in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].exc_info is not None
    assert store.persist_failures == 12
    clock[0] += 400
    store.ingest('pi:MSN-1', payload(clock[0], connection='connected', revision=99), source='pi')
    warnings = [r for r in caplog.records if 'persistence failed' in r.getMessage()]
    assert len(warnings) == 2 and '11 earlier' in warnings[1].getMessage()
