"""S3: the durable Pi activity mirror (write, hydrate, prune) only runs with work_center_v1; with the flag off nothing new
is persisted and the repo behaves as before the stack. Real repo and ingest."""
import copy
from datetime import UTC, datetime

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.mission_activity import ENTITY_TYPE, RETENTION_SECONDS
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


def snapshot(key, at):
    return {'entity_type': 'mission_activity', 'entity_id': f'pi:{key}', 'source': 'pi', 'event_type': 'mission_activity.snapshot',
            'payload': {'contract_version': 1, 'host_id': 'h', 'instance_id': 'i', 'session_id': 's', 'observed_at': stamp(at),
                        'snapshot_at': stamp(at), 'mission_revision': 1, 'connection': 'connected',
                        'mission': {'id': key, 'title': 'T', 'status': 'RUNNING'}, 'activity': {'health': 'active'}}}


def restart(sink, clock, **flags):
    streams, order = {}, []
    for row in sink.rows:
        streams.setdefault(f"{row['entity_type']}|{row['entity_id']}", []).append(row)
        order.append([row['entity_type'], row['entity_id'], row['sequence']])
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=ListSink(), **flags)
    repo.import_state({'events': streams, 'event_order': order})
    return repo


def test_flag_off_by_default_persists_and_prunes_nothing_but_serves_the_in_memory_projection():
    clock, sink = [NOW], ListSink()
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=sink)
    assert IngestService(repo).ingest(snapshot('MSN-1', NOW))['accepted'] is True
    clock[0] += 4000
    IngestService(repo).ingest(snapshot('MSN-2', clock[0]))
    assert sink.rows == [] and sink.pruned == []
    assert repo.mission_activity.get('pi:MSN-2') is not None


def test_flag_off_restart_ignores_leftover_rows_and_keeps_them_out_of_the_streams():
    clock, sink = [NOW], ListSink()
    IngestService(ControlPlaneRepo(now=lambda: clock[0], sink=sink, mission_activity_mirror=True)).ingest(snapshot('MSN-1', NOW))
    assert [r['entity_type'] for r in sink.rows] == [ENTITY_TYPE]
    off = restart(sink, clock)
    assert off.mission_activity.list_summaries() == []
    assert not any(k[0] == ENTITY_TYPE for k in off._events) and all(t[0] != ENTITY_TYPE for t in off._event_order)
    on = restart(sink, clock, mission_activity_mirror=True)
    assert [s['mission']['id'] for s in on.mission_activity.list_summaries()] == ['MSN-1']


def test_flag_on_still_writes_and_prunes():
    clock, sink = [NOW], ListSink()
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=sink, mission_activity_mirror=True)
    IngestService(repo).ingest(snapshot('MSN-1', NOW))
    assert len(sink.rows) == 1 and len(sink.pruned) == 1
    assert sink.pruned[0][0] == ENTITY_TYPE and RETENTION_SECONDS > 0
