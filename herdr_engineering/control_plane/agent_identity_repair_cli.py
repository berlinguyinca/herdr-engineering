"""Explicit private identity reconciliation: plan first, append atomically, never delete."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import UTC, datetime

from .agent_identity import AGENT_IDENTITY_LOCK, AgentIdentityRegistry
from .agent_identity_repair import plan_identity_repair, prepare_identity_replay
from .bridge import _rebuild_events
from .events import EventStore
from .sink import _dict_to_event


async def read_history(conn):
    rows = await conn.fetch("""
        SELECT * FROM fabric_events
        WHERE entity_type IN ('agent_identity', 'agent_identity_reconciliation')
        ORDER BY ingest_timestamp, sequence, event_id
    """)
    streams, _ = _rebuild_events([dict(row) for row in rows])
    return [event for stream in streams.values() for event in stream]


async def create_plan(conn):
    return plan_identity_repair(await read_history(conn))


def correction_events(plan, *, sequence, stamp):
    """Create the exact private events used both by dry-run replay and application."""
    events = []
    for record in plan['corrections']:
        suffix = hashlib.sha256(record['identity'].encode()).hexdigest()
        events.append({
            'event_id': f"{plan['repair_id']}_{suffix}", 'schema_version': 1,
            'entity_type': 'agent_identity', 'entity_id': record['identity'],
            'event_type': 'agent_identity.updated', 'sequence': sequence + len(events),
            'source_timestamp': stamp, 'ingest_timestamp': stamp,
            'mission_id': None, 'session_id': None, 'payload': copy.deepcopy(record),
        })
    events.append({
        'event_id': f"{plan['repair_id']}_marker", 'schema_version': 1,
        'entity_type': 'agent_identity_reconciliation', 'entity_id': plan['repair_id'],
        'event_type': 'agent_identity.reconciled', 'sequence': sequence + len(events),
        'source_timestamp': stamp, 'ingest_timestamp': stamp, 'mission_id': None, 'session_id': None,
        'payload': {'plan': copy.deepcopy(plan), 'correction_event_ids': [event['event_id'] for event in events]},
    })
    return events


def _summary(plan, status):
    return {'status': status, 'repair_id': plan['repair_id'],
            'covered_identity_events': len(plan['covered']),
            'corrected_identities': len(plan['corrections']),
            'display_names_changed': sum(change['before']['display_name'] != change['after']['display_name']
                                         for change in plan['changes']),
            'blocked_names': len(plan['blocked_names']), 'blocked_handles': len(plan['blocked_handles'])}


async def apply_plan(conn, plan):
    """Fence naming writers, verify exact input, then append corrections + marker."""
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='5s'")
        await conn.execute('SELECT pg_advisory_xact_lock($1)', AGENT_IDENTITY_LOCK)
        await conn.execute('LOCK TABLE fabric_events IN SHARE ROW EXCLUSIVE MODE')
        history = await read_history(conn)
        existing = [event for event in history if event['entity_type'] == 'agent_identity_reconciliation'
                    and event['entity_id'] == plan.get('repair_id')]
        if existing:
            if any(event['payload'].get('plan') != plan for event in existing):
                raise ValueError('stored repair differs from requested plan')
            replay, names, handles = prepare_identity_replay(history)
            AgentIdentityRegistry(blocked_names=names, blocked_handles=handles).hydrate(replay)
            return _summary(plan, 'already_applied')
        if plan_identity_repair(history) != plan:
            raise ValueError('stale or tampered identity reconciliation plan')
        sequence = max(int(time.time() * 1_000_000),
                       max((int(event.get('sequence', 0)) + 1 for event in history), default=0))
        additions = correction_events(plan, sequence=sequence, stamp=datetime.now(UTC).isoformat())
        replay, names, handles = prepare_identity_replay(history + additions)
        AgentIdentityRegistry(blocked_names=names, blocked_handles=handles).hydrate(replay)
        store = EventStore(conn)
        for event in additions:
            if not await store.append(_dict_to_event(event)):
                raise ValueError('repair event identity already exists without its marker')
        return _summary(plan, 'applied')


def main(argv=None):
    import argparse
    import asyncio
    import os
    import stat
    import sys
    from pathlib import Path
    from urllib.parse import urlsplit, urlunsplit

    import asyncpg

    from herdr_engineering.fleet_config import load_fleet_config

    parser = argparse.ArgumentParser(description='Plan or explicitly apply private agent identity reconciliation')
    parser.add_argument('--fleet-config', type=Path,
                        default=Path.home() / '.config/herdr-engineering/fleet/fleet-config.yaml')
    parser.add_argument('--local-port', type=int)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('plan').add_argument('--out', type=Path, required=True)
    actions.add_parser('apply').add_argument('--plan', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.local_port is not None and not 1 <= args.local_port <= 65535:
        parser.error('local port must be between 1 and 65535')

    async def run():
        parts = urlsplit(load_fleet_config(args.fleet_config).section('control_plane')['pg_dsn'])
        dsn = urlunsplit(parts)
        if args.local_port is not None:
            credentials = parts.netloc.rpartition('@')[0] + '@' if '@' in parts.netloc else ''
            dsn = urlunsplit((parts.scheme, f'{credentials}127.0.0.1:{args.local_port}',
                              parts.path, parts.query, parts.fragment))
        settings = {'default_transaction_read_only': 'on'} if args.action == 'plan' else {}
        conn = await asyncpg.connect(dsn, command_timeout=30, server_settings=settings)
        try:
            if args.action == 'plan':
                plan = await create_plan(conn)
                descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as output:
                    json.dump(plan, output, indent=2, sort_keys=True)
                return _summary(plan, 'planned')
            if stat.S_IMODE(args.plan.stat().st_mode) & 0o077:
                raise ValueError('identity repair plan must be private')
            return await apply_plan(conn, json.loads(args.plan.read_text()))
        finally:
            await conn.close()

    try:
        print(json.dumps(asyncio.run(run()), sort_keys=True))
    except Exception as error:
        print(f'Identity reconciliation failed ({type(error).__name__}); no partial repair committed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
