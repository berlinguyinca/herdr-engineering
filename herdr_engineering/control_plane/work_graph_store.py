"""Persistent Work Graph on PostgreSQL (unified workspace P02).

``WorkGraphStore`` persists the structures of ``work_graph_core`` (assertions -> folded heads) and answers ACL-filtered
queries.  Writes are serialised by one advisory lock (a single projector at a time), run in one transaction per batch
and fold every touched subject from ALL of its stored assertions, so ingest is idempotent, order-tolerant and
retry-safe.  Reads are lock-free.

ACL: the visibility predicate is part of every SQL statement that returns rows (``vis_sql``), so ``LIMIT``, counts and
``has_more`` never see hidden rows, and graph expansion joins BOTH endpoints through the predicate (filter before
traversal and before edge expansion).  Rows are then re-checked in Python with ``work_graph_core.visible`` (defence in
depth) and, for artifacts, with the registry's own ACL gate (``artifact_ok``); there is no second artifact ACL.
"""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from . import work_graph_core as core
from . import workspace_contracts as wc

ADVISORY_LOCK = int.from_bytes(b"herdrwg\x00", "big") >> 1
TENANT = "default"
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
MAX_DEPTH, MAX_NODES = 4, 1000
MAX_LAYOUT_BYTES = 256 * 1024
MAX_LAYOUT_NODES = 5000
CENTRIC_EDGES = ("contains", "implements", "produced", "assigned_to", "spawned", "derived_from", "supersedes",
                 "discussed_in", "references", "depends_on", "delegated_to")

ArtifactOk = Callable[[Mapping[str, Any], str], bool]


class SnapshotRequired(RuntimeError):
    """A clean replay would lose events the sources no longer hold (retention): restore from a snapshot instead."""


def to_dt(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _ts_out(value: Any) -> str | None:
    return None if value is None else core.norm_ts(value)


async def init_connection(conn) -> None:
    """json/jsonb as Python objects on every pooled connection."""
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


# --------------------------------------------------------------------------- SQL visibility
def vis_sql(principal: Mapping[str, Any] | None, alias: str, params: list) -> str:
    """A boolean SQL expression equal to ``core.visible(...) is not False`` (artifact rows stay candidates).  Parameters are
    appended to ``params``.  An unauthenticated principal yields FALSE."""
    if not principal or principal.get("authenticated_by") in (None, "none") or principal.get("kind") in (None, "unknown"):
        return "FALSE"
    a = alias
    parts = [f"{a}.visibility = 'artifact'"]
    kind = principal.get("kind")
    if principal.get("id"):
        params.append(principal["id"])
        parts.append(f"${len(params)}::text = ANY({a}.grantees)")
    if kind != "member":
        parts.append(f"{a}.visibility = 'org'")
    if kind == "operator":
        parts.append(f"{a}.visibility IN ('room', 'mission')")
    access = principal.get("access")
    if kind == "member" and principal.get("authenticated_by") == "collab" and isinstance(access, Mapping):
        rooms = sorted(str(r) for r in access.get("rooms", ()))
        params.append(rooms)
        parts.append(f"({a}.visibility = 'room' AND {a}.room_ids && ${len(params)}::text[])")
        pairs = sorted(f"{r}|{m}" for r, m in access.get("room_missions", ()))
        params.append(pairs)
        parts.append(f"({a}.visibility = 'mission' AND {a}.mission_scope && ${len(params)}::text[])")
    return "(" + " OR ".join(parts) + ")"


ENTITY_COLS = ("entity_id, kind, native_id, display_name, status, project_id, visibility, room_ids, mission_scope, grantees, "
               "attrs, version, has_upsert, created_at, updated_at, tombstoned_at, source_event_id")


def _cols(alias: str) -> str:
    return ", ".join(f"{alias}.{c.strip()}" for c in ENTITY_COLS.split(","))


def _row_entity(r: Mapping[str, Any], aliases: Sequence = ()) -> dict:
    """Row -> the canonical head form used by ``Projection.dump`` (aliases from the alias table)."""
    return {
        "entity_id": r["entity_id"], "kind": r["kind"], "native_id": r["native_id"], "display_name": r["display_name"],
        "status": r["status"], "project_id": r["project_id"], "visibility": r["visibility"],
        "room_ids": list(r["room_ids"]), "mission_scope": list(r["mission_scope"]), "grantees": list(r["grantees"]),
        "attrs": dict(r["attrs"] or {}), "aliases": [list(a) for a in aliases], "version": r["version"],
        "has_upsert": r["has_upsert"], "created_at": _ts_out(r["created_at"]), "updated_at": _ts_out(r["updated_at"]),
        "tombstoned_at": _ts_out(r["tombstoned_at"]), "source_event_id": r["source_event_id"],
    }


def entity_doc(head: Mapping[str, Any]) -> dict:
    """The contract-shaped document returned to a viewer.  Scope lists (rooms, grantees) are NEVER echoed: they would name
    rooms and people the viewer may not see."""
    doc = {
        "id": head["entity_id"], "kind": head["kind"], "native_id": head["native_id"], "display_name": head["display_name"],
        "status": head["status"], "project_id": head["project_id"],
        "scope": {"tenant_id": TENANT, "visibility": head["visibility"]},
        "aliases": [{"system": a[0], "value": a[1]} for a in head["aliases"]],
        "attrs": head["attrs"], "version": head["version"], "created_at": head["created_at"],
        "updated_at": head["updated_at"], "tombstone": head["tombstoned_at"] is not None,
        "source_event_id": head["source_event_id"],
    }
    doc["etag"] = wc.compute_etag(doc)
    return doc


def edge_doc(r: Mapping[str, Any]) -> dict:
    return {"id": r["edge_id"], "type": r["edge_type"], "from_id": r["from_id"], "to_id": r["to_id"], "live": r["live"],
            "valid_from": _ts_out(r["valid_from"]), "tombstoned_at": _ts_out(r["tombstoned_at"]),
            "source_event_id": r["source_event_id"], "scope": {"tenant_id": TENANT}}


# --------------------------------------------------------------------------- cursors
def make_cursor(created_at: str, entity_id: str, tag: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at, entity_id, tag]).encode()).decode().rstrip("=")


def read_cursor(token: str | None, tag: str) -> tuple[datetime, str] | None:
    if not token:
        return None
    try:
        raw = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        created, eid, got = raw
        if got != tag or not isinstance(eid, str):
            raise ValueError
        return to_dt(created), eid
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("invalid cursor") from None


def _tag(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _like(q: str) -> str:
    return "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# --------------------------------------------------------------------------- the store
class WorkGraphStore:
    def __init__(self, pool, *, artifact_ok: ArtifactOk | None = None) -> None:
        self._pool = pool
        self.artifact_ok = artifact_ok

    # ---- writes ---------------------------------------------------------------------------------------------
    async def apply_events(self, raws: Iterable[Mapping[str, Any]], *, batch: int = 200, ops_for: dict | None = None) -> dict:
        """Project source events.  Idempotent per event; a failing batch is retried event by event so one poison event
        cannot block the rest (it is recorded in ``wg_gap`` and in the ledger so it is not retried forever)."""
        evs = []
        for r in raws:
            ev = core.normalize_event(r)
            if ev["event_id"]:
                evs.append((ev, (ops_for or {}).get(ev["event_key"])))
        out = {"applied": 0, "duplicates": 0, "poison": 0, "assertions": 0}
        for i in range(0, len(evs), batch):
            chunk = evs[i:i + batch]
            try:
                self._add(out, await self._apply_chunk(chunk))
            except Exception:                                    # noqa: BLE001 - isolate the poison event
                for item in chunk:
                    try:
                        self._add(out, await self._apply_chunk([item]))
                    except Exception as exc:                     # noqa: BLE001
                        await self._poison(item[0], exc)
                        out["poison"] += 1
        return out

    @staticmethod
    def _add(total: dict, part: dict) -> None:
        for k, v in part.items():
            total[k] = total.get(k, 0) + v

    async def _poison(self, ev: dict, exc: Exception) -> None:
        async with self._pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK)
            await c.execute(
                "INSERT INTO wg_event (event_key, event_id, source, source_sequence, entity_type, occurred_at, trace_id, assertions) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,0) ON CONFLICT DO NOTHING",
                ev["event_key"], ev["event_id"], ev["source"], ev["source_sequence"], ev["entity_type"],
                to_dt(ev["occurred_at"]), ev.get("trace_id"))
            await c.execute("INSERT INTO wg_gap (kind, source, detail) VALUES ('poison_event', $1, $2)", ev["source"],
                            {"event_key": ev["event_key"], "error": str(exc)[:300]})

    async def _apply_chunk(self, chunk: list[tuple[dict, list | None]]) -> dict:
        out = {"applied": 0, "duplicates": 0, "assertions": 0}
        touched: dict[str, set[str]] = {"entity": set(), "edge": set(), "fact": set()}
        async with self._pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK)
            for ev, ops in chunk:
                try:
                    asserts = core.assertions_for_event(ev, ops)
                except wc.ContractError as exc:
                    asserts = []
                    await c.execute("INSERT INTO wg_gap (kind, source, detail) VALUES ('poison_event', $1, $2)",
                                    ev["source"], {"event_key": ev["event_key"], "error": str(exc)[:300]})
                got = await c.fetchrow(
                    "INSERT INTO wg_event (event_key, event_id, source, source_sequence, entity_type, occurred_at, trace_id, "
                    "assertions) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (event_key) DO NOTHING RETURNING event_key",
                    ev["event_key"], ev["event_id"], ev["source"], ev["source_sequence"], ev["entity_type"],
                    to_dt(ev["occurred_at"]), ev.get("trace_id"), len(asserts))
                if got is None:
                    out["duplicates"] += 1
                    continue
                out["applied"] += 1
                if asserts:
                    await c.executemany(
                        "INSERT INTO wg_assertion (aid, event_key, source, source_sequence, occurred_at, rank, subject_kind, "
                        "subject_id, mode, body) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (aid) DO NOTHING",
                        [(a["aid"], a["event_key"], a["source"], a["source_sequence"], to_dt(a["occurred_at"]), a["rank"],
                          a["subject_kind"], a["subject_id"], a["mode"], a["body"]) for a in asserts])
                    out["assertions"] += len(asserts)
                    for a in asserts:
                        touched[a["subject_kind"]].add(a["subject_id"])
            await self._refold(c, touched)
        return out

    async def _load_asserts(self, c, kind: str, ids: Sequence[str]) -> dict[str, list[dict]]:
        by: dict[str, list[dict]] = {}
        if not ids:
            return by
        rows = await c.fetch(
            "SELECT aid, event_key, source, source_sequence, occurred_at, rank, subject_kind, subject_id, mode, body "
            "FROM wg_assertion WHERE subject_kind = $1 AND subject_id = ANY($2::text[])", kind, list(ids))
        for r in rows:
            by.setdefault(r["subject_id"], []).append({
                "aid": r["aid"], "event_key": r["event_key"], "source": r["source"], "source_sequence": r["source_sequence"],
                "occurred_at": core.norm_ts(r["occurred_at"]), "rank": r["rank"], "subject_kind": r["subject_kind"],
                "subject_id": r["subject_id"], "mode": r["mode"], "body": r["body"]})
        return by

    async def _refold(self, c, touched: dict[str, set[str]]) -> None:
        ents = await self._load_asserts(c, "entity", sorted(touched["entity"]))
        heads = [core.fold_entity(i, a) for i, a in sorted(ents.items())]
        if heads:
            await c.executemany(
                "INSERT INTO wg_entity (entity_id, kind, native_id, display_name, status, project_id, visibility, room_ids, "
                "mission_scope, grantees, attrs, version, has_upsert, created_at, updated_at, tombstoned_at, source_event_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) "
                "ON CONFLICT (entity_id) DO UPDATE SET display_name=EXCLUDED.display_name, status=EXCLUDED.status, "
                "project_id=EXCLUDED.project_id, visibility=EXCLUDED.visibility, room_ids=EXCLUDED.room_ids, "
                "mission_scope=EXCLUDED.mission_scope, grantees=EXCLUDED.grantees, attrs=EXCLUDED.attrs, "
                "version=EXCLUDED.version, has_upsert=EXCLUDED.has_upsert, created_at=EXCLUDED.created_at, "
                "updated_at=EXCLUDED.updated_at, tombstoned_at=EXCLUDED.tombstoned_at, source_event_id=EXCLUDED.source_event_id",
                [(h["entity_id"], h["kind"], h["native_id"], h["display_name"], h["status"], h["project_id"], h["visibility"],
                  h["room_ids"], h["mission_scope"], h["grantees"], h["attrs"], h["version"], h["has_upsert"],
                  to_dt(h["created_at"]), to_dt(h["updated_at"]),
                  to_dt(h["tombstoned_at"]) if h["tombstoned_at"] else None, h["source_event_id"]) for h in heads])
            alias_rows = [(s, v, h["entity_id"], h["source_event_id"]) for h in heads for s, v in h["aliases"]]
            await c.executemany(
                "INSERT INTO wg_alias (system, value, entity_id, source_event_id) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                alias_rows)
            # a (system, value) already owned by ANOTHER entity is a conflict: first owner wins, the clash is reported
            if alias_rows:
                clashes = await c.fetch(
                    "SELECT a.system, a.value, a.entity_id AS owner, x.entity_id AS claimant FROM wg_alias a "
                    "JOIN unnest($1::text[], $2::text[], $3::text[]) AS x(system, value, entity_id) "
                    "ON a.system = x.system AND a.value = x.value AND a.entity_id <> x.entity_id",
                    [r[0] for r in alias_rows], [r[1] for r in alias_rows], [r[2] for r in alias_rows])
                for k in clashes:
                    detail = {"system": k["system"], "value": k["value"], "owner": k["owner"], "claimant": k["claimant"]}
                    await c.execute(
                        "INSERT INTO wg_gap (kind, source, detail) SELECT 'alias_conflict', 'projection', $1::jsonb "
                        "WHERE NOT EXISTS (SELECT 1 FROM wg_gap WHERE kind = 'alias_conflict' AND resolved_at IS NULL "
                        "AND detail = $1::jsonb)", detail)
        edges = await self._load_asserts(c, "edge", sorted(touched["edge"]))
        eheads = [core.fold_edge(i, a) for i, a in sorted(edges.items())]
        if eheads:
            await c.executemany(
                "INSERT INTO wg_edge (edge_id, edge_type, from_id, to_id, live, valid_from, tombstoned_at, room_ids, project_id, "
                "source_event_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (edge_id) DO UPDATE SET live=EXCLUDED.live, "
                "valid_from=EXCLUDED.valid_from, tombstoned_at=EXCLUDED.tombstoned_at, room_ids=EXCLUDED.room_ids, "
                "project_id=EXCLUDED.project_id, source_event_id=EXCLUDED.source_event_id",
                [(h["edge_id"], h["edge_type"], h["from_id"], h["to_id"], h["live"],
                  to_dt(h["valid_from"]) if h["valid_from"] else None,
                  to_dt(h["tombstoned_at"]) if h["tombstoned_at"] else None, h["room_ids"], h["project_id"],
                  h["source_event_id"]) for h in eheads])
            await c.execute("DELETE FROM wg_edge_interval WHERE edge_id = ANY($1::text[])", [h["edge_id"] for h in eheads])
            await c.executemany(
                "INSERT INTO wg_edge_interval (edge_id, valid_from, valid_to) VALUES ($1,$2,$3)",
                [(h["edge_id"], to_dt(i[0]), to_dt(i[1]) if i[1] else None) for h in eheads for i in h["intervals"]])
        facts = await self._load_asserts(c, "fact", sorted(touched["fact"]))
        fheads = [core.fold_fact(i, a) for i, a in sorted(facts.items())]
        if fheads:
            await c.executemany(
                "INSERT INTO wg_fact (fact_id, fact_kind, subject_ref, object_id, segments, open, room_ids, grantees, project_id, "
                "source_event_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (fact_id) DO UPDATE SET "
                "segments=EXCLUDED.segments, open=EXCLUDED.open, room_ids=EXCLUDED.room_ids, grantees=EXCLUDED.grantees, "
                "project_id=EXCLUDED.project_id, source_event_id=EXCLUDED.source_event_id",
                [(h["fact_id"], h["fact_kind"], h["subject_ref"], h["object_id"], h["segments"], h["open"], h["room_ids"],
                  h["grantees"], h["project_id"], h["source_event_id"]) for h in fheads])

    async def clear_projection(self) -> None:
        """Drop the projection (NOT layouts, which are user data) ahead of a replay from clean."""
        async with self._pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock($1)", ADVISORY_LOCK)
            await c.execute("TRUNCATE wg_edge_interval, wg_alias, wg_edge, wg_fact, wg_entity, wg_assertion, wg_event, wg_cursor "
                            "RESTART IDENTITY CASCADE")

    # ---- ledger / diagnostics ---------------------------------------------------------------------------------
    async def known_event_keys(self, keys: Sequence[str]) -> set[str]:
        if not keys:
            return set()
        rows = await self._pool.fetch("SELECT event_key FROM wg_event WHERE event_key = ANY($1::text[])", list(keys))
        return {r["event_key"] for r in rows}

    async def ledger_keys(self) -> set[str]:
        return {r["event_key"] for r in await self._pool.fetch("SELECT event_key FROM wg_event")}

    async def get_cursor(self, name: str) -> tuple[datetime, str] | None:
        r = await self._pool.fetchrow("SELECT watermark_ts, watermark_id FROM wg_cursor WHERE name = $1", name)
        return (r["watermark_ts"], r["watermark_id"]) if r and r["watermark_ts"] else None

    async def set_cursor(self, name: str, ts: datetime, event_id: str) -> None:
        await self._pool.execute(
            "INSERT INTO wg_cursor (name, watermark_ts, watermark_id) VALUES ($1,$2,$3) ON CONFLICT (name) DO UPDATE SET "
            "watermark_ts = EXCLUDED.watermark_ts, watermark_id = EXCLUDED.watermark_id, updated_at = now()", name, ts, event_id)

    async def record_gap(self, kind: str, source: str | None, detail: Mapping[str, Any]) -> None:
        await self._pool.execute("INSERT INTO wg_gap (kind, source, detail) VALUES ($1,$2,$3)", kind, source, dict(detail))

    async def resolve_gaps(self, kind: str) -> int:
        r = await self._pool.execute("UPDATE wg_gap SET resolved_at = now() WHERE kind = $1 AND resolved_at IS NULL", kind)
        return int(r.split()[-1])

    async def status(self) -> dict:
        c = self._pool
        counts = {k: await c.fetchval(f"SELECT count(*) FROM {t}") for k, t in
                  (("entities", "wg_entity"), ("edges", "wg_edge"), ("facts", "wg_fact"), ("events", "wg_event"),
                   ("assertions", "wg_assertion"))}
        gaps = {r["kind"]: r["n"] for r in await c.fetch(
            "SELECT kind, count(*) AS n FROM wg_gap WHERE resolved_at IS NULL GROUP BY kind")}
        return {"counts": counts, "open_gaps": gaps, "snapshot_required": bool(gaps.get("retention_gap"))}

    # ---- canonical dump (parity + replay-equality gate) ----------------------------------------------------------
    async def dump(self) -> dict:
        c = self._pool
        aliases: dict[str, list] = {}
        for r in await c.fetch("SELECT system, value, entity_id FROM wg_alias ORDER BY system, value"):
            aliases.setdefault(r["entity_id"], []).append([r["system"], r["value"]])
        entities = {r["entity_id"]: _row_entity(r, aliases.get(r["entity_id"], ()))
                    for r in await c.fetch(f"SELECT {ENTITY_COLS} FROM wg_entity ORDER BY entity_id")}
        iv: dict[str, list] = {}
        for r in await c.fetch("SELECT edge_id, valid_from, valid_to FROM wg_edge_interval ORDER BY edge_id, valid_from"):
            iv.setdefault(r["edge_id"], []).append([_ts_out(r["valid_from"]), _ts_out(r["valid_to"])])
        edges = {r["edge_id"]: {"edge_id": r["edge_id"], "edge_type": r["edge_type"], "from_id": r["from_id"],
                                "to_id": r["to_id"], "live": r["live"], "intervals": iv.get(r["edge_id"], []),
                                "valid_from": _ts_out(r["valid_from"]), "tombstoned_at": _ts_out(r["tombstoned_at"]),
                                "room_ids": list(r["room_ids"]), "project_id": r["project_id"],
                                "source_event_id": r["source_event_id"]}
                 for r in await c.fetch("SELECT * FROM wg_edge ORDER BY edge_id")}
        facts = {r["fact_id"]: {"fact_id": r["fact_id"], "fact_kind": r["fact_kind"], "subject_ref": r["subject_ref"],
                                "object_id": r["object_id"], "segments": r["segments"], "open": r["open"],
                                "room_ids": list(r["room_ids"]), "grantees": list(r["grantees"]),
                                "project_id": r["project_id"], "source_event_id": r["source_event_id"]}
                 for r in await c.fetch("SELECT * FROM wg_fact ORDER BY fact_id")}
        return {"schema": core.SCHEMA_VERSION, "entities": entities, "edges": edges, "facts": facts}

    # ---- ACL-aware reads ----------------------------------------------------------------------------------------
    def _keep(self, principal, head: Mapping[str, Any]) -> bool:
        v = core.visible(principal, head)
        if v is None:
            aid = head["native_id"] if head["kind"] == "artifact" else (head.get("attrs") or {}).get("artifact_id")
            return bool(self.artifact_ok and aid and self.artifact_ok(principal, str(aid)))
        return bool(v)

    async def _head(self, c, row: Mapping[str, Any]) -> dict:
        al = await c.fetch("SELECT system, value FROM wg_alias WHERE entity_id = $1 ORDER BY system, value", row["entity_id"])
        return _row_entity(row, [[a["system"], a["value"]] for a in al])

    async def _heads(self, c, rows: Sequence[Mapping[str, Any]]) -> list[dict]:
        if not rows:
            return []
        al: dict[str, list] = {}
        for a in await c.fetch("SELECT system, value, entity_id FROM wg_alias WHERE entity_id = ANY($1::text[]) ORDER BY system, value",
                               [r["entity_id"] for r in rows]):
            al.setdefault(a["entity_id"], []).append([a["system"], a["value"]])
        return [_row_entity(r, al.get(r["entity_id"], ())) for r in rows]

    async def resolve(self, c, ref: str) -> str:
        """urn | ``kind:native`` | ``system:alias`` -> entity urn (existence/visibility is checked by the caller)."""
        if not isinstance(ref, str) or not ref or len(ref) > 600:
            raise ValueError("bad reference")
        if ref.startswith("urn:"):
            wc.parse_urn(ref)
            return ref
        if ":" not in ref:
            raise ValueError("bad reference")
        system, value = ref.split(":", 1)
        if system in wc.ENTITY_KINDS:
            return wc.urn_for(system, value)
        r = await c.fetchrow("SELECT entity_id FROM wg_alias WHERE system = $1 AND value = $2", system, value)
        if r is None:
            raise wc.NotFoundOrForbidden
        return r["entity_id"]

    async def _visible_root(self, c, principal, ref: str, as_of: datetime | None = None) -> dict:
        try:
            eid = await self.resolve(c, ref)
        except wc.ContractError as exc:
            raise ValueError(str(exc)) from None
        params: list = [eid]
        pred = vis_sql(principal, "e", params)
        row = await c.fetchrow(f"SELECT {_cols('e')} FROM wg_entity e WHERE e.entity_id = $1 AND {pred}", *params)
        if row is None:
            raise wc.NotFoundOrForbidden
        head = await self._head(c, row)
        if as_of is not None:
            head = await self._as_of_head(c, eid, as_of)
        if not self._keep(principal, head):
            raise wc.NotFoundOrForbidden
        return head

    async def _as_of_head(self, c, eid: str, t: datetime) -> dict:
        asserts = (await self._load_asserts(c, "entity", [eid])).get(eid, [])
        asserts = [a for a in asserts if a["occurred_at"] <= core.norm_ts(t)]
        if not asserts:
            raise wc.NotFoundOrForbidden
        head = core.fold_entity(eid, asserts)
        if head["tombstoned_at"]:
            raise wc.NotFoundOrForbidden
        return head

    async def _as_of_heads(self, c, ids: Sequence[str], t: datetime) -> dict[str, dict]:
        allv = await self._load_asserts(c, "entity", list(ids))
        cut = core.norm_ts(t)
        out = {}
        for i in ids:
            a = [x for x in allv.get(i, ()) if x["occurred_at"] <= cut]
            if a:
                h = core.fold_entity(i, a)
                if not h["tombstoned_at"]:
                    out[i] = h
        return out

    async def get_entity(self, principal, ref: str, *, as_of: str | None = None) -> dict:
        t = self._parse_as_of(as_of)
        async with self._pool.acquire() as c:
            head = await self._visible_root(c, principal, ref, t)
            # a version / artifact lookup lists its linked artifacts: that is the snapshot's job
            return entity_doc(head)

    @staticmethod
    def _parse_as_of(value: str | None) -> datetime | None:
        if not value:
            return None
        s = core.norm_ts(value)
        if s == core.ZERO_TS:
            raise ValueError("as_of must be an ISO-8601 timestamp")
        return to_dt(s)

    @staticmethod
    def _limit(limit: Any, default: int = DEFAULT_LIMIT) -> int:
        try:
            n = int(limit) if limit not in (None, "") else default
        except (TypeError, ValueError):
            raise ValueError("limit must be an integer") from None
        return max(1, min(n, MAX_LIMIT))

    async def _page(self, principal, sql_head: str, params: list, tag: str, limit: int, cursor: str | None,
                    order: str = "e.created_at, e.entity_id") -> dict:
        """Keyset paging that fills ``limit`` VISIBLE rows (artifact candidates are re-checked in Python) and reports
        ``has_more`` only from visible rows."""
        after = read_cursor(cursor, tag)
        out: list[dict] = []
        last: tuple[str, str] | None = None
        has_more = False
        async with self._pool.acquire() as c:
            pos = after
            for _ in range(25):
                q, ps = sql_head, list(params)
                if pos:
                    ps += [pos[0], pos[1]]
                    q += f" AND (e.created_at, e.entity_id) > (${len(ps) - 1}::timestamptz, ${len(ps)}::text)"
                ps.append(limit * 2 + 1)
                rows = await c.fetch(f"{q} ORDER BY {order} LIMIT ${len(ps)}", *ps)
                heads = await self._heads(c, rows)
                for h in heads:
                    pos = (to_dt(h["created_at"]), h["entity_id"])
                    if not self._keep(principal, h):
                        continue
                    if len(out) >= limit:
                        has_more = True
                        break
                    out.append(h)
                    last = (h["created_at"], h["entity_id"])
                if has_more or len(rows) < limit * 2 + 1:
                    break
            else:
                has_more = True                      # budget spent on hidden rows: keep paging from here
                if pos:
                    last = (core.norm_ts(pos[0]), pos[1])
        return {"items": [entity_doc(h) for h in out], "has_more": has_more,
                "next_cursor": make_cursor(last[0], last[1], tag) if has_more and last else None}

    async def list_entities(self, principal, *, kinds: Sequence[str] = (), project_id: str | None = None,
                            status: str | None = None, include_tombstoned: bool = False, as_of: str | None = None,
                            limit: Any = None, cursor: str | None = None) -> dict:
        for k in kinds:
            if k not in wc.ENTITY_KINDS:
                raise ValueError(f"unknown kind {k!r}")
        t = self._parse_as_of(as_of)
        params: list = []
        vis = vis_sql(principal, "e", params)
        sql = f"SELECT {_cols('e')} FROM wg_entity e WHERE {vis}"
        if kinds:
            params.append(list(kinds))
            sql += f" AND e.kind = ANY(${len(params)}::text[])"
        if project_id:
            params.append(project_id)
            sql += f" AND e.project_id = ${len(params)}"
        if status:
            params.append(status)
            sql += f" AND e.status = ${len(params)}"
        if t is not None:
            params.append(t)
            sql += f" AND e.created_at <= ${len(params)} AND (e.tombstoned_at IS NULL OR e.tombstoned_at > ${len(params)})"
        elif not include_tombstoned:
            sql += " AND e.tombstoned_at IS NULL"
        tag = _tag("list", list(kinds), project_id, status, include_tombstoned, as_of, sorted(principal.get("access", {}).get("rooms", ())) if principal else None, principal.get("id") if principal else None)
        res = await self._page(principal, sql, params, tag, self._limit(limit), cursor)
        if t is not None:
            async with self._pool.acquire() as c:
                heads = await self._as_of_heads(c, [d["id"] for d in res["items"]], t)
            res["items"] = [entity_doc(heads[d["id"]]) for d in res["items"] if d["id"] in heads]
        return res

    async def search(self, principal, q: str, *, kinds: Sequence[str] = (), project_id: str | None = None,
                     limit: Any = None, cursor: str | None = None) -> dict:
        q = (q or "").strip()
        if not q or len(q) > 200:
            raise ValueError("q is required (at most 200 characters)")
        params: list = []
        vis = vis_sql(principal, "e", params)
        params.append(_like(q))
        n = len(params)
        sql = (f"SELECT {_cols('e')} FROM wg_entity e WHERE {vis} AND e.tombstoned_at IS NULL AND "
               f"(lower(e.display_name) LIKE ${n} ESCAPE '\\' OR lower(e.native_id) LIKE ${n} ESCAPE '\\' OR EXISTS "
               f"(SELECT 1 FROM wg_alias a WHERE a.entity_id = e.entity_id AND lower(a.value) LIKE ${n} ESCAPE '\\'))")
        if kinds:
            for k in kinds:
                if k not in wc.ENTITY_KINDS:
                    raise ValueError(f"unknown kind {k!r}")
            params.append(list(kinds))
            sql += f" AND e.kind = ANY(${len(params)}::text[])"
        if project_id:
            params.append(project_id)
            sql += f" AND e.project_id = ${len(params)}"
        tag = _tag("search", q, list(kinds), project_id, principal.get("id") if principal else None,
                   sorted(principal.get("access", {}).get("rooms", ())) if principal else None)
        res = await self._page(principal, sql, params, tag, self._limit(limit), cursor)
        return {"hits": res["items"], "has_more": res["has_more"], "next_cursor": res["next_cursor"]}

    async def snapshot(self, principal, root: str, *, depth: Any = 2, max_nodes: Any = 200,
                       edge_types: Sequence[str] | None = None, as_of: str | None = None,
                       include_tombstoned: bool = False) -> dict:
        """Subgraph around ``root`` as of now or a past time.  One SQL statement per level; both endpoints of every edge go
        through the visibility predicate inside the statement, so a hidden node is never expanded, counted or named."""
        try:
            depth = max(0, min(int(depth), MAX_DEPTH))
            max_nodes = max(1, min(int(max_nodes), MAX_NODES))
        except (TypeError, ValueError):
            raise ValueError("depth and max_nodes must be integers") from None
        t = self._parse_as_of(as_of)
        types = list(edge_types) if edge_types else None
        for e in types or ():
            if e not in wc.EDGE_ENDPOINTS:
                raise ValueError(f"unknown edge type {e!r}")
        async with self._pool.acquire() as c:
            rootd = await self._visible_root(c, principal, root, t)
            nodes: dict[str, dict] = {rootd["entity_id"]: rootd}
            order = [rootd["entity_id"]]
            frontier = [rootd["entity_id"]]
            truncated = False
            queries = 1
            denied: set[str] = set()
            for _ in range(depth):
                rows = await self._edge_rows(c, principal, frontier, None, types, t, include_tombstoned)
                queries += 1
                nxt: list[str] = []
                cand: dict[str, dict] = {}
                fset = set(frontier)
                for r in rows:
                    for side, far, pre in (("from_id", "to_id", "t_"), ("to_id", "from_id", "f_")):
                        other = r[far]
                        if r[side] in fset and other not in nodes and other not in denied and other not in cand:
                            cand[other] = {k.strip(): r[pre + k.strip()] for k in ENTITY_COLS.split(",")}
                for head in await self._heads(c, list(cand.values())):
                    if not self._keep(principal, head):
                        denied.add(head["entity_id"])
                        continue
                    if len(nodes) >= max_nodes:
                        truncated = True
                        continue
                    nodes[head["entity_id"]] = head
                    order.append(head["entity_id"])
                    nxt.append(head["entity_id"])
                frontier = nxt
                if not frontier:
                    break
            edges: dict[str, dict] = {}
            if len(nodes) > 1 or depth:
                rows = await self._edge_rows(c, principal, order, order, types, t, include_tombstoned)
                queries += 1
                for r in rows:
                    if r["from_id"] in nodes and r["to_id"] in nodes:
                        edges[r["edge_id"]] = edge_doc(r)
            if t is not None:
                nodes = await self._as_of_heads(c, list(nodes), t)
            degree = dict.fromkeys(order, 0)
            for e in edges.values():
                degree[e["from_id"]] += 1
                degree[e["to_id"]] += 1
            return {"root": rootd["entity_id"], "as_of": core.norm_ts(t) if t else None,
                    "nodes": [{**entity_doc(nodes[i]), "degree": degree[i]} for i in order],
                    "edges": sorted(edges.values(), key=lambda e: (e["type"], e["id"])), "has_more": truncated,
                    "queries": queries}

    async def _edge_rows(self, c, principal, side_a: Sequence[str], side_b: Sequence[str] | None, types, t, include_tomb):
        params: list = [list(side_a)]
        fv, tv = vis_sql(principal, "f", params), vis_sql(principal, "t", params)
        if side_b is None:
            where = "(ed.from_id = ANY($1::text[]) OR ed.to_id = ANY($1::text[]))"
        else:
            params.append(list(side_b))
            where = f"ed.from_id = ANY($1::text[]) AND ed.to_id = ANY(${len(params)}::text[])"
        if types:
            params.append(list(types))
            where += f" AND ed.edge_type = ANY(${len(params)}::text[])"
        if t is not None:
            params.append(t)
            n = len(params)
            where += (f" AND EXISTS (SELECT 1 FROM wg_edge_interval i WHERE i.edge_id = ed.edge_id AND i.valid_from <= ${n} "
                      f"AND (i.valid_to IS NULL OR i.valid_to > ${n})) AND f.created_at <= ${n} AND t.created_at <= ${n}")
        elif not include_tomb:
            where += " AND ed.live AND f.tombstoned_at IS NULL AND t.tombstoned_at IS NULL"
        return await c.fetch(
            f"SELECT ed.edge_id, ed.edge_type, ed.from_id, ed.to_id, ed.live, ed.valid_from, ed.tombstoned_at, "
            f"ed.source_event_id, {', '.join(f'f.{k.strip()} AS f_{k.strip()}' for k in ENTITY_COLS.split(','))}, "
            f"{', '.join(f't.{k.strip()} AS t_{k.strip()}' for k in ENTITY_COLS.split(','))} "
            f"FROM wg_edge ed JOIN wg_entity f ON f.entity_id = ed.from_id JOIN wg_entity t ON t.entity_id = ed.to_id "
            f"WHERE {where} AND {fv} AND {tv} ORDER BY ed.edge_type, ed.edge_id", *params)

    async def centric(self, principal, ref: str, *, depth: Any = 2, max_nodes: Any = 400, as_of: str | None = None) -> dict:
        """Project / issue / task lookup: the node, what it contains and the authorized artifacts (with versions) linked to it."""
        snap = await self.snapshot(principal, ref, depth=depth, max_nodes=max_nodes, edge_types=CENTRIC_EDGES, as_of=as_of)
        groups: dict[str, list] = {}
        for n in snap["nodes"]:
            if n["id"] != snap["root"]:
                groups.setdefault(n["kind"], []).append(n)
        versions: dict[str, list] = {}
        for e in snap["edges"]:
            if e["type"] == "contains":
                a = next((n for n in groups.get("artifact", ()) if n["id"] == e["from_id"]), None)
                v = next((n for n in groups.get("artifact_version", ()) if n["id"] == e["to_id"]), None)
                if a and v:
                    versions.setdefault(a["id"], []).append(v["id"])
        root = next(n for n in snap["nodes"] if n["id"] == snap["root"])
        return {"root": root, "groups": {k: groups[k] for k in sorted(groups)},
                "artifact_versions": {k: sorted(v) for k, v in versions.items()},
                "counts": {k: len(v) for k, v in sorted(groups.items())}, "edges": snap["edges"],
                "has_more": snap["has_more"], "as_of": snap["as_of"]}

    async def facts(self, principal, ref: str, *, fact_kind: str | None = None, as_of: str | None = None,
                    limit: Any = None) -> dict:
        """Historical facts (team roles, agent sessions, grants) about a visible entity; never deleted, so a closed room's
        roster and the sessions of agents that exited stay readable."""
        t = self._parse_as_of(as_of)
        lim = self._limit(limit, 100)
        async with self._pool.acquire() as c:
            root = await self._visible_root(c, principal, ref)
            rows = await c.fetch(
                "SELECT fact_id, fact_kind, subject_ref, object_id, segments, open, source_event_id FROM wg_fact "
                "WHERE (object_id = $1 OR subject_ref IN (SELECT 'agent:' || native_id FROM wg_entity WHERE entity_id = $1)) "
                "AND ($2::text IS NULL OR fact_kind = $2) ORDER BY fact_id LIMIT $3", root["entity_id"], fact_kind, lim + 1)
        items = []
        for r in rows[:lim]:
            segs = r["segments"]
            if t is not None:
                ts = core.norm_ts(t)
                segs = [s for s in segs if s["from"] <= ts and (s["to"] is None or s["to"] > ts)]
                if not segs:
                    continue
            items.append({"id": r["fact_id"], "kind": r["fact_kind"], "subject": r["subject_ref"], "object_id": r["object_id"],
                          "segments": segs, "open": r["open"], "source_event_id": r["source_event_id"]})
        return {"items": items, "has_more": len(rows) > lim, "as_of": core.norm_ts(t) if t else None}

    # ---- layouts -------------------------------------------------------------------------------------------------
    async def _visible_ids(self, c, principal, ids: Sequence[str]) -> set[str]:
        if not ids:
            return set()
        params: list = [list(ids)]
        pred = vis_sql(principal, "e", params)
        rows = await c.fetch(f"SELECT {_cols('e')} FROM wg_entity e WHERE e.entity_id = ANY($1::text[]) AND {pred}", *params)
        out = set()
        for h in await self._heads(c, rows):
            if self._keep(principal, h):
                out.add(h["entity_id"])
        return out

    @staticmethod
    def _layout_doc(row: Mapping[str, Any] | None, scope_id: str, owner: str) -> dict:
        if row is None:
            body = {"scope_id": scope_id, "owner": owner, "version": 0, "document": {"nodes": {}}}
        else:
            body = {"scope_id": scope_id, "owner": row["owner_user_id"], "version": row["version"], "document": row["document"]}
        body["etag"] = row["etag"] if row else wc.compute_etag(body)
        return body

    async def get_layout(self, principal, ref: str, *, which: str = "effective") -> dict:
        """``effective`` = the caller's override, else the shared default.  Positions of nodes the viewer cannot see are
        removed, so a shared layout never names hidden entities."""
        if which not in ("effective", "default", "mine"):
            raise ValueError("which must be effective, default or mine")
        async with self._pool.acquire() as c:
            root = await self._visible_root(c, principal, ref)
            me = layout_owner(principal)
            row = None
            if which in ("effective", "mine") and me:
                row = await c.fetchrow("SELECT * FROM wg_layout WHERE scope_id = $1 AND owner_user_id = $2", root["entity_id"], me)
            if row is None and which in ("effective", "default"):
                row = await c.fetchrow("SELECT * FROM wg_layout WHERE scope_id = $1 AND owner_user_id = ''", root["entity_id"])
            doc = self._layout_doc(row, root["entity_id"], row["owner_user_id"] if row else ("" if which == "default" else (me or "")))
            nodes = doc["document"].get("nodes") or {}
            keep = await self._visible_ids(c, principal, list(nodes))
            doc["document"] = {**doc["document"], "nodes": {k: v for k, v in nodes.items() if k in keep}}
            return doc

    async def put_layout(self, principal, ref: str, document: Any, *, shared: bool, if_match: str | None) -> dict:
        if not if_match:
            raise wc.PreconditionRequired("If-Match required")
        if not isinstance(document, Mapping) or not isinstance(document.get("nodes", {}), Mapping):
            raise ValueError("document.nodes must be an object")
        if len(json.dumps(document, default=str)) > MAX_LAYOUT_BYTES or len(document.get("nodes", {})) > MAX_LAYOUT_NODES:
            raise ValueError("layout document too large")
        for k, v in document.get("nodes", {}).items():
            if not isinstance(k, str) or not isinstance(v, Mapping) or any(
                    not isinstance(x, (int, float, bool, str, type(None))) for x in v.values()):
                raise ValueError("layout node entries must be flat objects")
        async with self._pool.acquire() as c, c.transaction():
            root = await self._visible_root(c, principal, ref)
            me = layout_owner(principal)
            if shared:
                if principal.get("kind") not in ("operator", "member"):
                    raise wc.NotFoundOrForbidden
                owner = ""
            else:
                if not me:
                    raise wc.NotFoundOrForbidden
                owner = me
            ids = list(document.get("nodes", {}))
            keep = await self._visible_ids(c, principal, ids)
            clean = {**document, "nodes": {k: v for k, v in document.get("nodes", {}).items() if k in keep}}
            await c.execute("SELECT pg_advisory_xact_lock(hashtext($1))", root["entity_id"] + "|" + owner)
            cur = await c.fetchrow("SELECT * FROM wg_layout WHERE scope_id = $1 AND owner_user_id = $2 FOR UPDATE",
                                   root["entity_id"], owner)
            current = self._layout_doc(cur, root["entity_id"], owner)
            if if_match != current["etag"]:
                raise wc.PreconditionFailed("stale etag")
            body = {"scope_id": root["entity_id"], "owner": owner, "version": current["version"] + 1, "document": clean}
            etag = wc.compute_etag(body)
            await c.execute(
                "INSERT INTO wg_layout (scope_id, owner_user_id, document, version, etag, updated_by) VALUES ($1,$2,$3,$4,$5,$6) "
                "ON CONFLICT (scope_id, owner_user_id) DO UPDATE SET document = EXCLUDED.document, version = EXCLUDED.version, "
                "etag = EXCLUDED.etag, updated_by = EXCLUDED.updated_by, updated_at = now()",
                root["entity_id"], owner, clean, body["version"], etag, principal.get("id"))
            return {**body, "etag": etag}


def layout_owner(principal: Mapping[str, Any] | None) -> str | None:
    pid = (principal or {}).get("id")
    return pid if isinstance(pid, str) and pid and (principal or {}).get("authenticated_by") not in (None, "none") else None
