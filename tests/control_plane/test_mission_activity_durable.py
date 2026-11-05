"""G8-C: the Pi activity projection survives a restart. Real store and repo; the sink is an in-memory list of the very rows the
durable sink would write, replayed through import_state exactly as the Postgres recovery path does."""
import copy
import json
from datetime import UTC, datetime

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.mission_activity import (
    ENTITY_TYPE,
    MAX_MISSIONS,
    MAX_PROJECTION_BYTES,
    PERSIST_INTERVAL_SECONDS,
    RETENTION_SECONDS,
    MissionActivityStore,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1_800_000_000


class ListSink:
    def __init__(self):
        self.rows, self.pruned = [], []

    def record(self, event):
        self.rows.append({'event_id': f'e{len(self.rows)}', 'sequence': len(self.rows), 'schema_version': 1,
                          'source_timestamp': datetime.fromtimestamp(NOW, UTC).isoformat(), **copy.deepcopy(event)})

    def prune_events_before(self, entity_type, cutoff):
        self.pruned.append((entity_type, cutoff))


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def snapshot(key, at, **mission):
    return {'entity_type': 'mission_activity', 'entity_id': f'pi:{key}', 'source': 'pi', 'event_type': 'mission_activity.snapshot',
            'payload': {'contract_version': 1, 'host_id': 'h', 'instance_id': 'i', 'session_id': 's', 'observed_at': stamp(at),
                        'snapshot_at': stamp(at), 'mission_revision': 1, 'connection': 'connected',
                        'mission': {'id': key, 'title': f'Title {key}', 'status': 'RUNNING', **mission},
                        'activity': {'health': 'active', 'nextAction': 'Run tests'}}}


def restart(sink, clock):
    """A new repo that sees only the durable rows, as after a control-plane restart."""
    streams, order = {}, []
    for row in sink.rows:
        streams.setdefault(f"{row['entity_type']}|{row['entity_id']}", []).append(row)
        order.append([row['entity_type'], row['entity_id'], row['sequence']])
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=ListSink())
    repo.import_state({'events': streams, 'event_order': order})
    return repo


def test_projection_survives_restart_and_stays_out_of_the_shared_streams():
    clock, sink = [NOW], ListSink()
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=sink)
    IngestService(repo).ingest(snapshot('MSN-1', NOW, created_at=stamp(NOW - 60),
                                        links=[{'kind': 'issue', 'repository': 'acme/app', 'number': 3}]))
    before = repo.mission_activity.get('pi:MSN-1')
    assert [r['entity_type'] for r in sink.rows] == [ENTITY_TYPE]
    clock[0] += 10
    again = restart(sink, clock)
    after = again.mission_activity.get('pi:MSN-1')
    assert after['mission'] == before['mission'] and after['activity'] == before['activity']
    assert after['mission']['links'] == [{'kind': 'issue', 'repository': 'acme/app', 'number': 3}]
    assert after['freshness']['age_seconds'] == 10.0
    assert [s['mission']['id'] for s in again.mission_activity.list_summaries()] == ['MSN-1']
    assert not any(k[0] == ENTITY_TYPE for k in again._events) and all(t[0] != ENTITY_TYPE for t in again._event_order)


def test_restored_store_keeps_ordering_rules_and_keeps_persisting():
    clock, sink = [NOW], ListSink()
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=sink)
    IngestService(repo).ingest(snapshot('MSN-1', NOW))
    again = restart(sink, clock)
    old = IngestService(again).ingest(snapshot('MSN-1', NOW - 5))
    assert again.mission_activity.get('pi:MSN-1')['observed_at'] == stamp(NOW)
    assert old['accepted'] is False    # out-of-order rules survive the restart
    clock[0] += PERSIST_INTERVAL_SECONDS + 1
    IngestService(again).ingest(snapshot('MSN-1', clock[0]))
    assert [r['entity_type'] for r in again._sink.rows] == [ENTITY_TYPE]


def test_writes_are_throttled_per_mission_but_a_connection_change_is_written():
    clock, sink = [NOW], ListSink()
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=sink)
    service = IngestService(repo)
    service.ingest(snapshot('MSN-1', NOW))
    clock[0] += 5
    service.ingest(snapshot('MSN-1', clock[0]))
    assert len(sink.rows) == 1
    clock[0] += 5
    changed = snapshot('MSN-1', clock[0])
    changed['payload']['connection'] = 'disconnected'
    service.ingest(changed)
    assert len(sink.rows) == 2
    clock[0] += PERSIST_INTERVAL_SECONDS
    service.ingest(snapshot('MSN-1', clock[0]))
    assert len(sink.rows) == 3
    service.ingest(snapshot('MSN-2', clock[0]))
    assert len(sink.rows) == 4


def test_a_failing_sink_never_breaks_ingest():
    class Broken(ListSink):
        def record(self, event):
            raise RuntimeError('db down')
    repo = ControlPlaneRepo(now=lambda: NOW, sink=Broken())
    assert IngestService(repo).ingest(snapshot('MSN-1', NOW))['accepted'] is True
    assert repo.mission_activity.get('pi:MSN-1') is not None


def test_retention_and_count_caps_apply_on_restore():
    clock, sink = [NOW], ListSink()
    store = MissionActivityStore(now=lambda: clock[0], persist=lambda ev: sink.record(ev))
    payload = snapshot('MSN-OLD', NOW)['payload']
    store.ingest('pi:MSN-OLD', payload, source='pi')
    clock[0] += RETENTION_SECONDS - 50
    for index in range(MAX_MISSIONS + 20):
        key = f'MSN-{index}'
        store.ingest(f'pi:{key}', snapshot(key, clock[0])['payload'], source='pi')
        clock[0] += 0.001
    assert len(sink.rows) == MAX_MISSIONS + 21
    clock[0] += 100        # MSN-OLD is now past 24 h; the rest are not
    fresh = MissionActivityStore(now=lambda: clock[0])
    counts = fresh.hydrate(sink.rows)
    assert counts == {'loaded': MAX_MISSIONS, 'skipped': 1}
    keys = [s['mission']['id'] for s in fresh.list_summaries()]
    assert len(keys) == MAX_MISSIONS and 'MSN-OLD' not in keys
    assert keys[0] == f'MSN-{MAX_MISSIONS + 19}', 'newest first; the oldest of the surplus were dropped'


def test_newest_row_wins_for_one_mission():
    clock, sink = [NOW], ListSink()
    store = MissionActivityStore(now=lambda: clock[0], persist=sink.record)
    store.ingest('pi:MSN-1', snapshot('MSN-1', NOW)['payload'], source='pi')
    clock[0] += PERSIST_INTERVAL_SECONDS + 1
    newer = snapshot('MSN-1', clock[0])['payload']
    newer['mission']['title'] = 'Newer'
    store.ingest('pi:MSN-1', newer, source='pi')
    fresh = MissionActivityStore(now=lambda: clock[0])
    fresh.hydrate(list(reversed(sink.rows)))
    assert fresh.get('pi:MSN-1')['mission']['title'] == 'Newer'


def good_row(sink_rows=None):
    clock = [NOW]
    rows = ListSink()
    MissionActivityStore(now=lambda: clock[0], persist=rows.record).ingest('pi:MSN-1', snapshot('MSN-1', NOW)['payload'], source='pi')
    return rows.rows[0]


def corrupt(row, how):
    row = copy.deepcopy(row)
    if how == 'no_payload':
        row['payload'] = None
    elif how == 'string_garbage':
        row['payload'] = '{not json'
    elif how == 'wrong_key':
        row['entity_id'] = 'pi:MSN-2'
    elif how == 'bad_key':
        row['entity_id'] = 'not-a-pi-key'
    elif how == 'unknown_event':
        row['event_type'] = 'Other'
    elif how == 'bad_epoch':
        row['payload']['received_epoch'] = 'yesterday'
    elif how == 'bool_epoch':
        row['payload']['observed_epoch'] = True
    elif how == 'nan_epoch':
        row['payload']['received_epoch'] = float('nan')
    elif how == 'future':
        row['payload']['received_epoch'] = NOW + 10_000
    elif how == 'no_value':
        del row['payload']['value']
    elif how == 'value_not_dict':
        row['payload']['value'] = 'x'
    elif how == 'bad_connection':
        row['payload']['value']['connection'] = 'weird'
    elif how == 'source':
        row['payload']['value']['source'] = 'herdr'
    elif how == 'oversize':
        row['payload']['value']['activity'] = {'blob': 'x' * (MAX_PROJECTION_BYTES + 1)}
    elif how == 'mission_id':
        row['payload']['value']['mission']['id'] = 'MSN-9'
    return row


def test_corrupt_unknown_and_foreign_rows_are_skipped_not_fatal():
    good = good_row()
    modes = ['no_payload', 'string_garbage', 'wrong_key', 'bad_key', 'unknown_event', 'bad_epoch', 'bool_epoch', 'nan_epoch',
             'future', 'no_value', 'value_not_dict', 'bad_connection', 'source', 'oversize', 'mission_id']
    bad = [corrupt(good, mode) for mode in modes] + ['not a row', None, {}, {'entity_type': ENTITY_TYPE}]
    store = MissionActivityStore(now=lambda: NOW)
    counts = store.hydrate(bad + [good])
    assert counts == {'loaded': 1, 'skipped': len(bad)}
    assert store.get('pi:MSN-1')['mission']['title'] == 'Title MSN-1'
    only_bad = MissionActivityStore(now=lambda: NOW)
    assert only_bad.hydrate(bad) == {'loaded': 0, 'skipped': len(bad)} and only_bad.list_summaries() == []


def test_json_text_payload_from_the_database_is_accepted_and_bad_links_are_dropped_on_restore():
    row = good_row()
    row['payload']['value']['mission']['links'] = [{'kind': 'commit', 'repository': 'x/y', 'number': 1},
                                                    {'kind': 'issue', 'repository': 'acme/app', 'number': 2}]
    row['payload'] = json.dumps(row['payload'])
    store = MissionActivityStore(now=lambda: NOW)
    assert store.hydrate([row]) == {'loaded': 1, 'skipped': 0}
    assert store.get('pi:MSN-1')['mission']['links'] == [{'kind': 'issue', 'repository': 'acme/app', 'number': 2}]


def test_durable_prune_runs_hourly_with_the_retention_cutoff():
    clock, sink = [NOW], ListSink()
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=sink)
    service = IngestService(repo)
    service.ingest(snapshot('MSN-1', NOW))
    assert [(t, c.timestamp()) for t, c in sink.pruned] == [(ENTITY_TYPE, NOW - RETENTION_SECONDS)]
    clock[0] += 60
    service.ingest(snapshot('MSN-2', clock[0]))
    assert len(sink.pruned) == 1
    clock[0] += 3600
    service.ingest(snapshot('MSN-3', clock[0]))
    assert len(sink.pruned) == 2


def test_no_sink_means_no_persistence_and_unchanged_behaviour():
    repo = ControlPlaneRepo(now=lambda: NOW)
    assert IngestService(repo).ingest(snapshot('MSN-1', NOW))['accepted'] is True
    assert repo.mission_activity.get('pi:MSN-1') is not None
