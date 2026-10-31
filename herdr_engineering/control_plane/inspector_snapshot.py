"""Keeps a replay snapshot of the inspector projection, so a boot does not replay every stored inspector event.

A boot used to re-run every durable inspector event through redaction and eviction (about nine minutes for ~780k
events). Here a shadow :class:`InspectorStore`, fed ONLY by ``hydrate``/``hydrate_delta``, is advanced periodically
and written to ``inspector_snapshots``. Feeding it only durable replay is what keeps the snapshot identical to a full
replay: a live store also holds kinds that are deliberately never persisted (reasoning).

The snapshot is an optimisation. The boot checks version, fingerprint, event count and ordering, and replays
everything on any doubt (:func:`apply_to_store`).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time

from .bridge import _rebuild_events
from .inspector import InspectorStore, SnapshotStale

log = logging.getLogger(__name__)

_PAGE = 20000
_COLUMNS = ("event_id, schema_version, source_timestamp, ingest_timestamp, entity_type, entity_id, mission_id, "
            "session_id, sequence, event_type, payload")
_VALID = "entity_type = 'inspector' AND entity_id IS NOT NULL AND entity_id <> ''"


def encode(state: dict) -> bytes:
    return gzip.compress(json.dumps(state, separators=(",", ":")).encode(), compresslevel=3)


def decode(body: bytes) -> dict:
    return json.loads(gzip.decompress(bytes(body)))


def apply_to_store(store: InspectorStore, row: dict | None, events: list[dict], backfill=None) -> str:
    """Rebuild ``store`` from a snapshot ``row`` plus the ``events`` that came after it, or from ``events`` alone.

    ``events`` are the inspector events the boot loaded: all of them, or, when the source verified the snapshot and
    skipped the rows below its watermark (``row["skipped_upto"]``), only the newer ones. ``backfill()`` then returns
    the skipped rows for the case where the snapshot is refused here. Returns "snapshot" or "full:<reason>"; the
    store is complete either way, and untouched by a refused snapshot."""
    if row is None:
        store.hydrate(events)
        return "full:no snapshot"
    try:
        watermark = int(row["watermark"])
        covered = row.get("covered_count")
        if covered is None:
            covered = sum(1 for e in events if isinstance(e.get("sequence"), int) and e["sequence"] <= watermark)
        if int(covered) != int(row["event_count"]):
            raise SnapshotStale("the event log below the watermark is not the one the snapshot consumed")
        state = decode(row["body"])
        if state.get("watermark") != watermark or state.get("count") != int(row["event_count"]):
            raise SnapshotStale("snapshot row and body disagree")
        store.load_state(state)
        delta = [e for e in events if not (isinstance(e.get("sequence"), int) and e["sequence"] <= watermark)]
        store.hydrate_delta(delta)
        return "snapshot"
    except SnapshotStale as exc:
        reason = str(exc)
    except (OSError, EOFError, ValueError, TypeError, KeyError) as exc:     # corrupt gzip/json, odd row
        reason = f"unreadable snapshot ({type(exc).__name__})"
    # load_state is all-or-nothing and hydrate_delta checks before it mutates, but a refusal after load_state
    # leaves the loaded snapshot in place: start over from an empty projection.
    store.reset_replay()
    if row.get("skipped_upto") is not None:
        if backfill is None:
            raise RuntimeError("inspector rows were skipped for a snapshot that was then refused, and there is no way "
                               "to load them; refusing to start with an incomplete inspector")
        events = [*backfill(), *events]
    store.hydrate(events)
    return f"full:{reason}"


class SnapshotWriter:
    """Run :meth:`tick` from the worker loop; it does nothing until ``interval`` seconds have passed."""

    def __init__(self, make_store, *, interval: float = 600.0, settle: float = 60.0, clock=time.monotonic):
        self._make_store = make_store
        self.interval = interval
        self.settle = settle
        self._clock = clock
        self._last: float | None = None
        self._shadow: InspectorStore | None = None
        self.last_outcome = "not run"

    async def tick(self, conn) -> str:
        now = self._clock()
        if self._last is not None and now - self._last < self.interval:
            return "waiting"
        self._last = now
        try:
            self.last_outcome = await self._advance(conn)
            log.info("inspector snapshot %s", self.last_outcome)
        except Exception as exc:        # never take the worker down; the boot falls back to a full replay
            log.exception("inspector snapshot failed: %s", exc)
            self._shadow = None
            self.last_outcome = f"failed: {type(exc).__name__}"
        return self.last_outcome

    async def _advance(self, conn) -> str:
        if self._shadow is None:
            self._shadow = await self._seed(conn)
        shadow = self._shadow
        events = await self._fetch(conn, shadow.replay_watermark)
        try:
            await asyncio.to_thread(shadow.hydrate_delta, events)
        except SnapshotStale as exc:
            log.warning("inspector snapshot restarts from the full log: %s", exc)
            return await self._rebuild(conn)
        covered = await conn.fetchval(f"SELECT count(*) FROM fabric_events WHERE {_VALID} AND sequence <= $1 "
                                      "AND ingest_timestamp < now() - make_interval(secs => $2)",
                                      shadow.replay_watermark, self.settle)
        if int(covered) != shadow.replay_count:
            log.warning("inspector snapshot missed or lost events (%s stored, %s consumed); rebuilding", covered, shadow.replay_count)
            return await self._rebuild(conn)
        return await self._write(conn, shadow, len(events))

    async def _seed(self, conn) -> InspectorStore:
        shadow = self._make_store()
        row = await conn.fetchrow("SELECT watermark, event_count, body FROM inspector_snapshots WHERE id = 1")
        if row is not None:
            try:
                shadow.load_state(decode(row["body"]))
                return shadow
            except (SnapshotStale, OSError, EOFError, ValueError, TypeError, KeyError) as exc:
                log.warning("stored inspector snapshot ignored: %s", exc)
        return self._make_store()

    async def _rebuild(self, conn) -> str:
        self._shadow = shadow = self._make_store()
        events = await self._fetch(conn, -1)
        await asyncio.to_thread(shadow.hydrate, events)
        covered = await conn.fetchval(f"SELECT count(*) FROM fabric_events WHERE {_VALID} AND sequence <= $1 "
                                      "AND ingest_timestamp < now() - make_interval(secs => $2)",
                                      shadow.replay_watermark, self.settle)
        if int(covered) != shadow.replay_count:
            self._shadow = None
            return "rebuild did not match the stored log; nothing written"
        return await self._write(conn, shadow, len(events))

    async def _fetch(self, conn, after: int) -> list[dict]:
        """Inspector events newer than ``after``, in the same shape a boot sees (via ``_rebuild_events``)."""
        rows: list[dict] = []
        last = after
        while True:
            page = await conn.fetch(
                f"SELECT {_COLUMNS} FROM fabric_events WHERE {_VALID} AND sequence > $1 "
                "AND ingest_timestamp < now() - make_interval(secs => $2) ORDER BY sequence LIMIT $3",
                last, self.settle, _PAGE)
            if not page:
                break
            rows.extend(dict(r) for r in page)
            last = int(page[-1]["sequence"])
            if len(page) < _PAGE:
                break
        streams, _ = _rebuild_events(rows)
        return [e for stream in streams.values() for e in stream]

    async def _write(self, conn, shadow: InspectorStore, fetched: int) -> str:
        if fetched == 0 and await conn.fetchval("SELECT 1 FROM inspector_snapshots WHERE id = 1") == 1:
            return "unchanged"
        state = await asyncio.to_thread(shadow.export_state)
        body = await asyncio.to_thread(encode, state)
        await conn.execute(
            "INSERT INTO inspector_snapshots (id, schema_version, fingerprint, watermark, event_count, body, created_at) "
            "VALUES (1, $1, $2, $3, $4, $5, now()) ON CONFLICT (id) DO UPDATE SET schema_version = EXCLUDED.schema_version, "
            "fingerprint = EXCLUDED.fingerprint, watermark = EXCLUDED.watermark, event_count = EXCLUDED.event_count, "
            "body = EXCLUDED.body, created_at = EXCLUDED.created_at",
            state["version"], state["fingerprint"], state["watermark"], state["count"], body)
        return f"written ({state['count']} events, {len(body) // 1024} KiB)"
