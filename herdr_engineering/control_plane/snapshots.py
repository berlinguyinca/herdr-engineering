"""State snapshots + replay-after (Spec §55)."""
from __future__ import annotations

from typing import Any


class SnapshotStore:
    def __init__(self, conn: Any):
        self._conn = conn

    async def save(self, entity_kind: str, entity_id: str,
                   state: dict[str, Any], sequence: int) -> None:
        await self._conn.execute(
            "INSERT INTO state_snapshots (entity_kind, entity_id, sequence, state) "
            "VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (entity_kind, entity_id, sequence) "
            "DO UPDATE SET state=EXCLUDED.state, created_at=now()",
            entity_kind, entity_id, sequence, state)

    async def load_latest(self, entity_kind: str, entity_id: str):
        row = await self._conn.fetchrow(
            "SELECT state, sequence FROM state_snapshots "
            "WHERE entity_kind=$1 AND entity_id=$2 "
            "ORDER BY sequence DESC LIMIT 1", entity_kind, entity_id)
        if row is None:
            return None
        return (row["state"], row["sequence"])

    async def replay(self, entity_kind: str, entity_id: str,
                     event_store: Any, after: int = 0):
        return await event_store.read_stream(entity_kind, entity_id, after=after)


async def recover(entity_kind: str, entity_id: str,
                  snapshot_store: SnapshotStore, event_store: Any) -> dict[str, Any]:
    """Load latest snapshot, replay later events, fold payloads over state."""
    loaded = await snapshot_store.load_latest(entity_kind, entity_id)
    state: dict[str, Any] = {}
    after = 0
    if loaded is not None:
        state, after = loaded
    for event in await snapshot_store.replay(entity_kind, entity_id,
                                             event_store, after=after):
        for key, value in event.payload.items():
            state[key] = value
    return state
