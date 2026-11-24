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
_KEYS = {"enabled", "interval_seconds"}
MIN_INTERVAL_SECONDS = 60
DEFAULT_INTERVAL_SECONDS = 600.0
_COLUMNS = ("event_id, schema_version, source_timestamp, ingest_timestamp, entity_type, entity_id, mission_id, "
            "session_id, sequence, event_type, payload")
_VALID = "entity_type = 'inspector' AND entity_id IS NOT NULL AND entity_id <> ''"
_LIVE = "fabric_events"
# Rows the archive job moved out of fabric_events still count toward a snapshot's event_count, so a full rebuild reads both.
_WITH_ARCHIVE = f"(SELECT {_COLUMNS} FROM fabric_events UNION ALL SELECT {_COLUMNS} FROM fabric_events_archive) AS fe"


async def _source(conn, archived: bool) -> str:
    """The FROM expression a full rebuild reads: the live journal, plus the archive when it exists."""
    if archived and await conn.fetchval("SELECT to_regclass('fabric_events_archive') IS NOT NULL"):
        return _WITH_ARCHIVE
    return _LIVE


# Keyset condition on (sequence, event_id) with ``$1``/``$2``: the sequence is a millisecond clock, so rows tie on it and
# paging on it alone would skip the rest of a tie that straddles a page boundary. ``sequence >= $1`` keeps the partial
# sequence index usable.
_AFTER = "sequence >= $1 AND (sequence, event_id) > ($1, $2)"


def parse_inspector_snapshots(raw) -> dict:
    """Validate ``control_plane.inspector_snapshots`` (YAML only) and return ``{"enabled", "interval_seconds"}``.

    Absent or empty means off with the default interval. Anything malformed raises ``ValueError`` rather than being
    guessed at, so a typo cannot silently leave the optimisation in an unintended state."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("inspector_snapshots must be a mapping")
    unknown = sorted(set(raw) - _KEYS, key=str)
    if unknown:
        raise ValueError(f"inspector_snapshots: unknown key(s) {unknown}; allowed: {sorted(_KEYS)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("inspector_snapshots.enabled must be a boolean")
    interval = raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < MIN_INTERVAL_SECONDS:
        raise ValueError(f"inspector_snapshots.interval_seconds must be a number >= {MIN_INTERVAL_SECONDS}")
    return {"enabled": enabled, "interval_seconds": float(interval)}


async def archived_inspector_count(conn, watermark: int) -> int:
    """Inspector rows the archive job moved out of ``fabric_events`` at or below ``watermark``. The snapshot consumed them,
    so they count toward its ``event_count``; 0 when the archive table does not exist."""
    if not await conn.fetchval("SELECT to_regclass('fabric_events_archive') IS NOT NULL"):
        return 0
    return int(await conn.fetchval(f"SELECT count(*) FROM fabric_events_archive WHERE {_VALID} AND sequence <= $1",
                                   int(watermark)))


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
        if int(covered) + int(row.get("archived_count") or 0) != int(row["event_count"]):
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
        covered = int(covered) + await archived_inspector_count(conn, shadow.replay_watermark)
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
        """Rebuild the shadow from the whole log, one page at a time so memory stays bounded by ``_PAGE``.

        ``hydrate`` replays in (receive time, per-session sequence) order, not fabric-sequence order. Pages are in
        fabric-sequence order, so feeding them one by one (``hydrate_delta``) is exactly a full replay only while each
        page's order keys do not precede what earlier pages consumed. The first page that does (``SnapshotStale``)
        abandons the streaming pass for :meth:`_replay_in_order`."""
        self._shadow = shadow = self._make_store()
        fetched = 0
        try:
            async for page in self._pages(conn, -1, archived=True):
                await asyncio.to_thread(shadow.hydrate_delta, page)
                fetched += len(page)
        except SnapshotStale as exc:
            log.warning("inspector snapshot rebuild is out of replay order across pages (%s); replaying the log in one pass", exc)
            self._shadow = shadow = self._make_store()
            fetched = await self._replay_in_order(conn, shadow)
        covered = await conn.fetchval(f"SELECT count(*) FROM fabric_events WHERE {_VALID} AND sequence <= $1 "
                                      "AND ingest_timestamp < now() - make_interval(secs => $2)",
                                      shadow.replay_watermark, self.settle)
        covered = int(covered) + await archived_inspector_count(conn, shadow.replay_watermark)
        if int(covered) != shadow.replay_count:
            self._shadow = None
            return "rebuild did not match the stored log; nothing written"
        return await self._write(conn, shadow, fetched)

    async def _replay_in_order(self, conn, shadow: InspectorStore) -> int:
        """Replay the whole log in exact replay order with bounded memory: one pass that reads only each row's order key
        (about 80 bytes a row, instead of the row), a sort of those keys, then the rows fetched ``_PAGE`` at a time in
        key order. Ties on the key keep fabric-sequence order, as in the one-shot hydrate. Returns the rows replayed."""
        keys: list[tuple] = []
        last, last_id = 0, ""
        source = await _source(conn, True)
        while True:
            page = await conn.fetch(
                "SELECT sequence, event_id, source_timestamp, ingest_timestamp, "
                "CASE WHEN jsonb_typeof(payload->'_received_at') = 'number' THEN payload->>'_received_at' END AS ra, "
                "CASE WHEN jsonb_typeof(payload->'_sequence') = 'number' THEN payload->>'_sequence' END AS sq "
                f"FROM {source} WHERE {_VALID} AND {_AFTER} "
                "AND ingest_timestamp < now() - make_interval(secs => $3) ORDER BY sequence, event_id LIMIT $4",
                last, last_id, self.settle, _PAGE)
            for r in page:
                probe = {"sequence": int(r["sequence"]), "source_timestamp": r["source_timestamp"],
                         "ingest_timestamp": r["ingest_timestamp"],
                         "payload": {"_received_at": float(r["ra"]) if r["ra"] is not None else None,
                                     "_sequence": float(r["sq"]) if r["sq"] is not None else None}}
                recv, seq = shadow._replay_order(probe)
                keys.append((recv, seq, probe["sequence"], r["event_id"]))
            if len(page) < _PAGE:
                break
            last, last_id = int(page[-1]["sequence"]), page[-1]["event_id"]
        keys.sort()
        replayed = 0
        for lo in range(0, len(keys), _PAGE):
            wanted = [(k[2], k[3]) for k in keys[lo:lo + _PAGE]]
            rows = await conn.fetch(f"SELECT {_COLUMNS} FROM {source} WHERE {_VALID} AND (sequence, event_id) IN "
                                    "(SELECT * FROM unnest($1::bigint[], $2::text[]))",
                                    [w[0] for w in wanted], [w[1] for w in wanted])
            streams, _ = _rebuild_events([dict(r) for r in rows])
            by_key = {(e["sequence"], e["event_id"]): e for stream in streams.values() for e in stream}
            batch = [by_key[q] for q in wanted if q in by_key]
            await asyncio.to_thread(shadow.hydrate, batch)
            replayed += len(batch)
        return replayed

    async def _row_pages(self, conn, after: int, archived: bool = False):
        """Raw inspector rows newer than ``after``, ``_PAGE`` rows at a time, in (fabric-sequence, event_id) order.
        ``archived`` also reads the archive table (a full rebuild; an incremental fetch is above every archived row)."""
        last, last_id = after + 1, ""         # (after + 1, "") sorts before every real row above ``after``
        source = await _source(conn, archived)
        while True:
            page = await conn.fetch(
                f"SELECT {_COLUMNS} FROM {source} WHERE {_VALID} AND {_AFTER} "
                "AND ingest_timestamp < now() - make_interval(secs => $3) ORDER BY sequence, event_id LIMIT $4",
                last, last_id, self.settle, _PAGE)
            if not page:
                return
            last, last_id = int(page[-1]["sequence"]), page[-1]["event_id"]
            yield [dict(r) for r in page]
            if len(page) < _PAGE:
                return

    async def _pages(self, conn, after: int, archived: bool = False):
        """Like :meth:`_row_pages`, each page folded into the shape a boot sees (via ``_rebuild_events``)."""
        async for rows in self._row_pages(conn, after, archived):
            streams, _ = _rebuild_events(rows)
            yield [e for stream in streams.values() for e in stream]

    async def _fetch(self, conn, after: int) -> list[dict]:
        """Inspector events newer than ``after`` (all pages), in the same shape a boot sees (via ``_rebuild_events``)."""
        rows: list[dict] = []
        async for page in self._row_pages(conn, after):
            rows.extend(page)
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
