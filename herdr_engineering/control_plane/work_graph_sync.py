"""Work Graph producers: source readers, the reconciler and the projection worker (unified workspace P02).

The graph is empty until something projects the sources into it.  This module is that producer:

* ``PgSources`` reads the sources of truth that live in the same PostgreSQL: ``fabric_events`` (collab log, Pi session
  events, artifact events), the artifact registry tables (``artifacts`` / ``artifact_versions`` / ``artifact_bindings``)
  and the GitHub catalog (``github_issues``).  Nothing is copied into a second event log.
* ``WorkGraphReconciler.reconcile`` is the repair path: it anti-joins the source against the projection ledger, so a
  dropped, late or out-of-window event is found and applied whatever the live path did; it also derives the artifact
  links straight from the registry (the registry emits no events yet) and enriches issue nodes from the catalog.
* ``rebuild`` is "replay from clean".  It refuses (``SnapshotRequired``) when the sources no longer hold events the
  projection was built from (retention prune): a smaller graph must never be produced silently.
* ``WorkGraphWorker`` runs both on a schedule.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from . import work_graph_core as core
from . import workspace_contracts as wc
from .work_graph_store import SnapshotRequired, WorkGraphStore, to_dt

log = logging.getLogger(__name__)

FABRIC_COLS = ("event_id, entity_type, entity_id, event_type, sequence, source_timestamp, payload, source, source_sequence, "
               "actor, visibility_scope, room_id, workspace_id, task_id, agent_id, mission_id, session_id, project_id")
EVENT_KEY_SQL = ("CASE WHEN COALESCE(f.actor ->> 'id', '') <> '' THEN (f.actor ->> 'id') || '/' || f.event_id "
                 "ELSE f.event_id END")
LOOKBACK = timedelta(minutes=10)


class PgSources:
    """The source side of the reconciler, over the same database as the projection."""

    def __init__(self, pool, *, types: Sequence[str] = core.HANDLED_ENTITY_TYPES) -> None:
        self._pool = pool
        self.types = list(types)
        self._cols: dict[str, set[str]] = {}

    async def _columns(self, table: str) -> set[str]:
        if table not in self._cols:
            rows = await self._pool.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = $1",
                table)
            self._cols[table] = {r["column_name"] for r in rows}
        return self._cols[table]

    async def _fabric_cols(self) -> str:
        cols = await self._columns("fabric_events")
        return FABRIC_COLS + (", trace_id" if "trace_id" in cols else "")

    async def events_after(self, after: tuple, limit: int) -> list[dict]:
        ts, eid = after if after else (to_dt(core.ZERO_TS), "")
        rows = await self._pool.fetch(
            f"SELECT {await self._fabric_cols()} FROM fabric_events f WHERE f.entity_type = ANY($1::text[]) AND "
            f"(f.source_timestamp, f.event_id) > ($2::timestamptz, $3::text) ORDER BY f.source_timestamp, f.event_id LIMIT $4",
            self.types, ts, eid, limit)
        return [dict(r) for r in rows]

    async def missing_events(self, limit: int) -> list[dict]:
        rows = await self._pool.fetch(
            f"SELECT {await self._fabric_cols()} FROM fabric_events f WHERE f.entity_type = ANY($1::text[]) AND NOT EXISTS "
            f"(SELECT 1 FROM wg_event w WHERE w.event_key = {EVENT_KEY_SQL}) ORDER BY f.source_timestamp, f.event_id LIMIT $2",
            self.types, limit)
        return [dict(r) for r in rows]

    async def ledger_missing_from_source(self, limit: int = 1000) -> list[str]:
        """Ledgered fabric events the source no longer holds (retention prune)."""
        rows = await self._pool.fetch(
            f"SELECT w.event_key FROM wg_event w WHERE w.entity_type = ANY($1::text[]) AND NOT EXISTS "
            f"(SELECT 1 FROM fabric_events f WHERE {EVENT_KEY_SQL} = w.event_key) LIMIT $2", self.types, limit)
        return [r["event_key"] for r in rows]

    async def source_holes(self) -> list[dict]:
        """Sequence holes INSIDE the source itself (an emitter skipped numbers): reported, not repairable here."""
        rows = await self._pool.fetch(
            "SELECT source, min(source_sequence) AS lo, max(source_sequence) AS hi, count(DISTINCT source_sequence) AS n "
            "FROM fabric_events WHERE entity_type = ANY($1::text[]) AND source IS NOT NULL AND source_sequence IS NOT NULL "
            "GROUP BY source HAVING max(source_sequence) - min(source_sequence) + 1 <> count(DISTINCT source_sequence)",
            self.types)
        return [{"source": r["source"], "from": r["lo"], "to": r["hi"], "present": r["n"],
                 "missing": r["hi"] - r["lo"] + 1 - r["n"]} for r in rows]

    # ---- the artifact registry -------------------------------------------------------------------------------
    async def registry(self) -> dict[str, dict]:
        """``{artifact_id: {"artifact": row, "versions": [rows], "bindings": [rows]}}`` straight from the registry tables."""
        if "artifacts" not in await self._relations():
            return {}
        arts = {r["artifact_id"]: {"artifact": dict(r), "versions": [], "bindings": []}
                for r in await self._pool.fetch("SELECT * FROM artifacts ORDER BY artifact_id")}
        vcols = await self._columns("artifact_versions")
        if vcols:
            for r in await self._pool.fetch(
                    "SELECT version_id, artifact_id, version_number, parent_version_id, state, created_by, created_at, committed_at "
                    "FROM artifact_versions ORDER BY artifact_id, version_number"):
                if r["artifact_id"] in arts:
                    arts[r["artifact_id"]]["versions"].append(dict(r))
        bcols = await self._columns("artifact_bindings")
        want = [c for c in ("artifact_id", "mission_id", "session_id", "room_id", "issue_ref", "task_id", "project_id", "created_at")
                if c in bcols]
        if want:
            for r in await self._pool.fetch(f"SELECT {', '.join(want)} FROM artifact_bindings ORDER BY artifact_id, created_at"):
                if r["artifact_id"] in arts:
                    arts[r["artifact_id"]]["bindings"].append(dict(r))
        return arts

    async def _relations(self) -> set[str]:
        rows = await self._pool.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
        return {r["table_name"] for r in rows}

    async def issues(self) -> list[dict]:
        if "github_issues" not in await self._relations():
            return []
        return [json.loads(r["data"]) if isinstance(r["data"], str) else dict(r["data"])
                for r in await self._pool.fetch("SELECT data FROM github_issues ORDER BY repository_id, github_id")]


def registry_events(registry: Mapping[str, Mapping[str, Any]]) -> list[tuple[dict, list[dict]]]:
    """Registry rows -> (event, ops).  The event key carries a fingerprint of the bindings, so a binding added later is a
    NEW event (additive) and an unchanged registry yields the same keys on every pass."""
    out = []
    for aid in sorted(registry):
        entry = registry[aid]
        art, versions, bindings = entry["artifact"], entry["versions"], entry["bindings"]
        bl = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in b.items() if k != "artifact_id"} for b in bindings]
        fp = hashlib.sha256(json.dumps(bl, sort_keys=True, default=str).encode()).hexdigest()[:10]
        bind_times = [b["created_at"] for b in bindings if b.get("created_at")]
        for v in (versions or [None]):
            stamp = [x for x in ([v.get("committed_at") or v.get("created_at")] if v else []) + [art.get("created_at")] + bind_times if x]
            when = max(stamp) if stamp else None
            key = f"reg:art:{v['version_id'] if v else aid}:{fp}"
            ev = core.normalize_event({"event_id": key, "entity_type": "registry", "entity_id": aid, "source": "registry",
                                       "event_type": "artifact.registry", "source_timestamp": when, "payload": {}})
            out.append((ev, core.artifact_ops(art, v, bindings)))
    return out


def issue_events(issues: Sequence[Mapping[str, Any]], known_native: set[str]) -> list[tuple[dict, list[dict]]]:
    """Catalog rows for issues the graph already refers to -> enrichment (title, state, url) and the ``github_issue`` alias."""
    out = []
    for row in sorted(issues, key=lambda r: (str(r.get("repository_id")), r.get("number") or 0)):
        native = f"{row.get('repository_id')}#{row.get('number')}"
        if native not in known_native:
            continue
        key = f"gh:{native}:{row.get('updated_at')}"
        ev = core.normalize_event({"event_id": key, "entity_type": "github", "entity_id": native, "source": "github",
                                   "event_type": "github.issue", "source_timestamp": row.get("updated_at"), "payload": {}})
        op = core.ent("issue", native, display_name=core._text(row.get("title")), status=core._text(str(row.get("state") or "").lower(), 64) or None,
                      attrs={"url": row.get("url"), "number": row.get("number"), "repository_id": row.get("repository_id"),
                             "labels": list(row.get("labels") or [])[:20]},
                      aliases=[("github_issue", native)])
        out.append((ev, [op]))
    return out


class WorkGraphReconciler:
    def __init__(self, store: WorkGraphStore, sources: PgSources, *, batch: int = 500) -> None:
        self.store, self.sources, self.batch = store, sources, batch

    async def reconcile(self) -> dict:
        report: dict[str, Any] = {"repaired_events": 0, "poison": 0, "registry_events": 0, "issue_events": 0}
        # 1. anything in the source that the ledger does not hold (dropped, late, outside the live window)
        for _ in range(10_000):
            events = await self.sources.missing_events(self.batch)
            if not events:
                break
            res = await self.store.apply_events(events)
            report["repaired_events"] += res["applied"]
            report["poison"] += res["poison"]
            if res["applied"] + res["poison"] == 0:
                break
        if report["repaired_events"]:
            await self.store.record_gap("repaired_event", None, {"count": report["repaired_events"]})
        # 2. the registry is the source of artifact identity and links
        registry = await self.sources.registry()
        pairs = registry_events(registry)
        fresh = await self._unledgered(pairs)
        if fresh:
            res = await self.store.apply_events([e for e, _ in fresh], ops_for={e["event_key"]: o for e, o in fresh})
            report["registry_events"] = res["applied"]
        # 3. issue titles / states / aliases from the catalog, for issues the graph refers to
        issues = await self.sources.issues()
        if issues:
            known = {r["native_id"] for r in await self.store._pool.fetch("SELECT native_id FROM wg_entity WHERE kind = 'issue'")}
            fresh = await self._unledgered(issue_events(issues, known))
            if fresh:
                res = await self.store.apply_events([e for e, _ in fresh], ops_for={e["event_key"]: o for e, o in fresh})
                report["issue_events"] = res["applied"]
        # 4. holes in the source's own sequences (not repairable here, but never silent)
        await self.store._pool.execute("UPDATE wg_gap SET resolved_at = now() WHERE kind = 'source_hole' AND resolved_at IS NULL")
        holes = await self.sources.source_holes()
        for h in holes:
            await self.store.record_gap("source_hole", h["source"], h)
        report["source_holes"] = holes
        # 5. orphans in both directions, against the registry
        report["orphans"] = await self.orphans(registry)
        return report

    async def _unledgered(self, pairs):
        have = await self.store.known_event_keys([e["event_key"] for e, _ in pairs])
        return [(e, o) for e, o in pairs if e["event_key"] not in have]

    async def orphans(self, registry: Mapping[str, Mapping[str, Any]] | None = None) -> dict:
        registry = await self.sources.registry() if registry is None else registry
        pool = self.store._pool
        urns = {aid: wc.urn_for("artifact", aid) for aid in registry}
        ents = {r["entity_id"]: {"kind": r["kind"], "native_id": r["native_id"]} for r in await pool.fetch(
            "SELECT entity_id, kind, native_id FROM wg_entity WHERE kind = 'artifact'")}
        edges = {}
        for r in await pool.fetch("SELECT edge_id, edge_type, from_id, to_id, live FROM wg_edge WHERE edge_type = 'contains' "
                                  "AND to_id = ANY($1::text[])", list(urns.values())):
            edges[r["edge_id"]] = {"edge_type": r["edge_type"], "from_id": r["from_id"], "to_id": r["to_id"], "live": r["live"]}
        mini = {"entities": ents, "edges": edges}
        return core.artifact_orphans(mini, {aid: [{k: (str(v) if v is not None else None) for k, v in b.items()} for b in e["bindings"]]
                                             for aid, e in registry.items()})

    async def rebuild(self, *, force: bool = False) -> dict:
        """Replay from clean: drop the projection, project every source again.  Refuses when the sources lost events the
        projection holds (``SnapshotRequired``) unless ``force``."""
        gone = await self.sources.ledger_missing_from_source(1000)
        if gone and not force:
            await self.store.record_gap("retention_gap", None, {"missing_from_source": len(gone), "sample": gone[:5]})
            raise SnapshotRequired(f"{len(gone)} projected events are no longer in the source; restore a snapshot instead of "
                                   "replaying (pass force to accept a smaller graph)")
        await self.store.clear_projection()
        return await self.reconcile()


class WorkGraphWorker:
    """Incremental projection by watermark (with a look-back window for late arrivals) + a periodic full reconcile."""

    def __init__(self, store: WorkGraphStore, sources: PgSources, *, interval: float = 5.0, reconcile_every: int = 60) -> None:
        self.store, self.sources, self.interval, self.reconcile_every = store, sources, interval, reconcile_every
        self.reconciler = WorkGraphReconciler(store, sources)
        self._stop = asyncio.Event()
        self.steps = 0
        self.last_error: str | None = None

    async def step(self) -> int:
        cur = await self.store.get_cursor("fabric")
        after = (cur[0] - LOOKBACK, "") if cur else None
        total = 0
        while True:
            events = await self.sources.events_after(after, 500)
            if not events:
                break
            res = await self.store.apply_events(events)
            total += res["applied"]
            last = events[-1]
            after = (last["source_timestamp"], last["event_id"])
            await self.store.set_cursor("fabric", *after)
            if len(events) < 500:
                break
        self.steps += 1
        if self.steps % self.reconcile_every == 1:
            await self.reconciler.reconcile()
        return total

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.step()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:                              # noqa: BLE001 - the worker must outlive a bad pass
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("work graph pass failed: %s", self.last_error)
            try:
                await asyncio.wait_for(self._stop.wait(), self.interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
