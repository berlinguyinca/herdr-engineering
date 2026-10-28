"""Durable project-intelligence store and the bridge from live Herdr events (spec P1/P2; DECISIONS D2).

* ``PgPersistence`` writes/reads the ``pi_*`` tables of migration 0020 (asyncpg on one dedicated loop, one
  connection, a lock: the same pattern as ``github_store``). ``StoreNotMigrated`` means the tables are absent, so
  code and migration can roll in either order and the caller falls back to memory.
* ``DurableIntelligenceStore`` is the tested in-memory store plus write-through. Reads are served from memory
  (like ``graph.py``: a projection rebuilt from durable rows); a failed write rolls the memory change back and
  raises, so memory never gets ahead of the database and no cursor may advance on a failed append.
* ``EventBridge`` subscribes to ``repo`` and turns ``ArtifactRegistered`` and ``CIFix*`` events into normalized
  events. The repo calls listeners synchronously inside event recording, so the listener only enqueues (bounded,
  never blocks); a worker thread does the database work. A full queue drops and counts; ``backfill`` is the
  opt-in, repeatable repair (deterministic ids make a re-run a no-op).
* Events whose project cannot be resolved are skipped and counted, never guessed into a project.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from . import project_intelligence as pi

log = logging.getLogger(__name__)
TABLES = ("pi_record", "pi_source_state", "pi_event", "pi_outbox", "pi_relationship", "pi_finding", "pi_audit")


StoreUnavailable = pi.StoreUnavailable


class StoreNotMigrated(StoreUnavailable):
    pass


class PgPersistence:
    def __init__(self, dsn: str | None = None, *, conn=None, run: Callable | None = None, timeout: float = 15.0):
        if run is None:
            from .bridge import _LoopRunner
            run = _LoopRunner().run
        self._dsn, self._conn, self._run, self._timeout = dsn, conn, run, timeout
        self._lock = threading.Lock()

    def _do(self, coro_fn):
        async def go():
            if self._conn is None:
                import asyncpg
                self._conn = await asyncpg.connect(self._dsn, timeout=self._timeout, command_timeout=30)
            return await coro_fn(self._conn)
        with self._lock:
            try:
                return self._run(go())
            except Exception as exc:  # database down, tables missing, timeout
                self._conn = None if self._dsn else self._conn
                if "does not exist" in str(exc):
                    raise StoreNotMigrated(str(exc)) from exc
                raise StoreUnavailable(str(exc)) from exc

    def check(self) -> None:
        async def go(c):
            n = await c.fetchval("select count(*) from pg_tables where tablename = any($1::text[])", list(TABLES))
            if n != len(TABLES):
                raise RuntimeError("relation does not exist: 0020_project_intelligence is not applied")
        self._do(go)

    def save_event(self, e: pi.SourceEvent) -> None:
        async def go(c):
            async with c.transaction():
                await c.execute(
                    "insert into pi_event(event_id,org_id,source_system,source_instance,source_id,source_version,event_type,"
                    "occurred_at,ingested_at,project_ids,investigation_ids,object_ref,actor_ref,title,category,evidence_refs,"
                    "security_label,visibility_scope,payload_schema_version,payload,content_digest,correlation_id,causation_id,"
                    "supersedes_id,late,tombstoned) values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb,$17,$18,"
                    "$19,$20::jsonb,$21,$22,$23,$24,$25,$26) on conflict (event_id) do nothing",
                    e.event_id, e.org_id, e.source_system, e.source_instance, e.source_id, e.source_version, e.event_type,
                    e.occurred_at, e.ingested_at, list(e.project_ids), list(e.investigation_ids), e.object_ref, e.actor_ref,
                    e.title, e.category, json.dumps([r.as_dict() for r in e.evidence]), e.security_label, e.visibility_scope,
                    e.payload_schema_version, json.dumps(e.payload), e.content_digest, e.correlation_id, e.causation_id,
                    e.supersedes_id, e.late, e.tombstoned)
                for consumer in ("timeline", "intelligence"):
                    await c.execute("insert into pi_outbox(event_id,consumer) values($1,$2) on conflict do nothing", e.event_id, consumer)
        self._do(go)

    def mark_tombstoned(self, event_ids: list[str]) -> None:
        async def go(c):
            await c.execute("update pi_event set tombstoned=true, payload='{}'::jsonb, title='[removed at source]', actor_ref=null, "
                            "evidence_refs=(select coalesce(jsonb_agg(jsonb_build_object('source_ref', e->>'source_ref', "
                            "'anchor_type','removed','anchor_value','removed','redaction_policy','source_removed')), '[]'::jsonb) "
                            "from jsonb_array_elements(evidence_refs) e) "
                            "where event_id = any($1::text[])", event_ids)
        self._do(go)

    def ack(self, event_id: str, consumer: str) -> None:
        async def go(c):
            await c.execute("update pi_outbox set done=true, updated_at=now() where event_id=$1 and consumer=$2", event_id, consumer)
        self._do(go)

    def save_record(self, r) -> None:
        async def go(c):
            await c.execute(
                "insert into pi_record(org_id,kind,record_id,name,project_ids,owner,status,security_label,version) "
                "values($1,$2,$3,$4,$5,$6,$7,$8,$9) on conflict (org_id,kind,record_id) do update set name=excluded.name,"
                "owner=excluded.owner,status=excluded.status,version=excluded.version,updated_at=now()",
                r.org_id, r.kind, r.id, r.name, list(r.project_ids), r.owner, r.status, r.security_label, r.version)
        self._do(go)

    def save_relationship(self, r: pi.Relationship) -> None:
        async def go(c):
            await c.execute(
                "insert into pi_relationship(id,org_id,project_ids,from_ref,to_ref,predicate,derivation,evidence_refs,security_label,"
                "review_state,confidence,extractor_version,model_id,verified_by,superseded_by,version) "
                "values($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13,$14,$15,$16) on conflict (id) do update set "
                "derivation=excluded.derivation,review_state=excluded.review_state,verified_by=excluded.verified_by,"
                "superseded_by=excluded.superseded_by,version=excluded.version",
                r.id, r.org_id, list(r.project_ids), r.from_ref, r.to_ref, r.predicate, r.derivation,
                json.dumps([e.as_dict() for e in r.evidence]), r.security_label, r.review_state, r.confidence,
                r.extractor_version, r.model_id, r.verified_by, r.superseded_by, r.version)
        self._do(go)

    def save_finding(self, f: pi.Finding) -> None:
        async def go(c):
            await c.execute(
                "insert into pi_finding(id,org_id,project_ids,statement,evidence_refs,security_label,extractor_version,model_identifier) "
                "values($1,$2,$3,$4,$5::jsonb,$6,$7,$8) on conflict (id) do update set evidence_refs = excluded.evidence_refs",
                f.id, f.org_id, list(f.project_ids), f.statement, json.dumps([e.as_dict() for e in f.evidence]),
                f.security_label, f.extractor_version, f.model_identifier)
        self._do(go)

    def save_pause(self, org_id: str, source_system: str, paused: bool) -> None:
        async def go(c):
            await c.execute(
                "insert into pi_source_state(org_id,source_system,source_instance,paused) values($1,$2,'*',$3) "
                "on conflict (org_id,source_system,source_instance) do update set paused=excluded.paused, updated_at=now()",
                org_id, source_system, paused)
        self._do(go)

    PAGE = 2000

    def load(self) -> dict:
        async def go(c):
            events, after = [], ""
            while True:        # keyset pages, so start-up never holds one giant result set while building objects
                page = await c.fetch("select * from pi_event where event_id > $1 order by event_id limit $2", after, self.PAGE)
                events.extend(page)
                if len(page) < self.PAGE:
                    break
                after = page[-1]["event_id"]
            events.sort(key=lambda r: (r["occurred_at"], r["event_id"]))
            return {
                "events": events,
                "records": await c.fetch("select * from pi_record"),
                "rels": await c.fetch("select * from pi_relationship"),
                "findings": await c.fetch("select * from pi_finding"),
                "pauses": await c.fetch("select org_id, source_system from pi_source_state where paused and source_instance='*'"),
                "outbox": await c.fetch("select event_id, consumer, done from pi_outbox"),
            }
        return self._do(go)


def _ev(row) -> pi.SourceEvent:
    return pi.SourceEvent(
        event_id=row["event_id"], org_id=row["org_id"], source_system=row["source_system"],
        source_instance=row["source_instance"], source_id=row["source_id"], source_version=row["source_version"],
        event_type=row["event_type"], occurred_at=row["occurred_at"], ingested_at=row["ingested_at"],
        project_ids=tuple(row["project_ids"]), investigation_ids=tuple(row["investigation_ids"]), object_ref=row["object_ref"],
        actor_ref=row["actor_ref"], title=row["title"], category=row["category"],
        evidence=tuple(pi.EvidenceRef.parse(e, stored=True) for e in json.loads(row["evidence_refs"])),
        security_label=row["security_label"], visibility_scope=row["visibility_scope"],
        payload_schema_version=row["payload_schema_version"], payload=json.loads(row["payload"]),
        content_digest=row["content_digest"], correlation_id=row["correlation_id"], causation_id=row["causation_id"],
        supersedes_id=row["supersedes_id"], late=row["late"], tombstoned=row["tombstoned"])


class DurableIntelligenceStore(pi.IntelligenceStore):
    def __init__(self, persistence: PgPersistence, **kw):
        super().__init__(**kw)
        self._pg = persistence
        persistence.check()
        data = persistence.load()
        with self._lock:
            for row in data["events"]:
                self._events[row["event_id"]] = _ev(row)
            for row in data["records"]:
                key = f"{row['kind']}:{row['org_id']}:{row['record_id']}"
                self._records[key] = pi._Record(row["kind"], row["record_id"], row["org_id"], row["name"], tuple(row["project_ids"]),
                                                row["status"], row["owner"], row["version"], row["security_label"])
            for row in data["rels"]:
                self._rels[row["id"]] = pi.Relationship(
                    row["id"], row["org_id"], tuple(row["project_ids"]), row["from_ref"], row["to_ref"], row["predicate"],
                    row["derivation"], tuple(pi.EvidenceRef.parse(e, stored=True) for e in json.loads(row["evidence_refs"])),
                    row["security_label"], row["review_state"], float(row["confidence"]) if row["confidence"] is not None else None,
                    row["extractor_version"], row["model_id"], row["verified_by"], row["superseded_by"], row["version"])
            for row in data["findings"]:
                self._findings[row["id"]] = pi.Finding(
                    row["id"], row["org_id"], tuple(row["project_ids"]), row["statement"],
                    tuple(pi.EvidenceRef.parse(e, stored=True) for e in json.loads(row["evidence_refs"])), row["security_label"],
                    row["extractor_version"], row["model_identifier"])
            self._paused |= {(r["org_id"], r["source_system"]) for r in data["pauses"]}
            for r in data["outbox"]:
                self._outbox[(r["event_id"], r["consumer"])] = {"event_id": r["event_id"], "consumer": r["consumer"], "done": r["done"]}

    def _snapshot(self):
        with self._lock:
            return (dict(self._events), dict(self._records), dict(self._rels), dict(self._findings),
                    {k: dict(x) for k, x in self._outbox.items()}, set(self._paused))

    def _restore(self, snap) -> None:
        with self._lock:
            self._events, self._records, self._rels, self._findings, self._outbox, self._paused = (
                dict(snap[0]), dict(snap[1]), dict(snap[2]), dict(snap[3]), {k: dict(x) for k, x in snap[4].items()}, set(snap[5]))

    def _durable(self, mutate, persist):
        """Mutate memory, persist, and on a failed write put memory back: it is never ahead of the database."""
        with self._lock:
            snap = self._snapshot()
            try:
                result = mutate()
                persist(result)
                return result
            except StoreUnavailable:
                self._restore(snap)
                raise

    def append_event(self, raw, *, cfg):
        def persist(res):
            if res[1]:
                self._pg.save_event(res[0])
        # Held under the lock so a concurrent identical append can never observe a not-yet-durable event.
        return self._durable(lambda: super(DurableIntelligenceStore, self).append_event(raw, cfg=cfg), persist)

    def tombstone_source(self, org_id, source_ref):
        def persist(_):
            ids = [k for k, v in self._events.items() if v.tombstoned and v.org_id == org_id]
            if ids:
                self._pg.mark_tombstoned(ids)
            for rel in self._rels.values():
                if rel.org_id == org_id:
                    self._pg.save_relationship(rel)
            for f in self._findings.values():
                if f.org_id == org_id:
                    self._pg.save_finding(f)
        return self._durable(lambda: super(DurableIntelligenceStore, self).tombstone_source(org_id, source_ref), persist)

    def outbox_ack(self, event_id, consumer):
        self._durable(lambda: super(DurableIntelligenceStore, self).outbox_ack(event_id, consumer),
                      lambda _: self._pg.ack(event_id, consumer))

    def upsert_record(self, kind, **kw):
        return self._durable(lambda: super(DurableIntelligenceStore, self).upsert_record(kind, **kw), self._pg.save_record)

    def add_relationship(self, raw, *, cfg, actor):
        return self._durable(lambda: super(DurableIntelligenceStore, self).add_relationship(raw, cfg=cfg, actor=actor),
                             self._pg.save_relationship)

    def review_relationship(self, rel_id, **kw):
        return self._durable(lambda: super(DurableIntelligenceStore, self).review_relationship(rel_id, **kw), self._pg.save_relationship)

    def add_finding(self, raw):
        return self._durable(lambda: super(DurableIntelligenceStore, self).add_finding(raw), self._pg.save_finding)

    def pause_source(self, org_id, source_system, paused=True):
        self._durable(lambda: super(DurableIntelligenceStore, self).pause_source(org_id, source_system, paused),
                      lambda _: self._pg.save_pause(org_id, source_system, paused))


# ---- live-event bridge ---------------------------------------------------------------------------------------
def _when(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def normalise_fabric_event(ev: dict, *, org_id: str, instance: str, projects: tuple[str, ...],
                           security_label: str = "internal") -> dict | None:
    """Map one recorded Herdr event to a raw intelligence event; ``None`` for kinds this bridge does not cover."""
    etype, entity = ev.get("event_type"), ev.get("entity_type")
    payload = ev.get("payload") or {}
    base = {"org_id": org_id, "source_system": "herdr", "source_instance": instance, "source_version": "",
            "project_ids": list(projects), "security_label": security_label, "visibility_scope": "private",
            "occurred_at": _when(ev.get("source_timestamp")), "correlation_id": ev.get("correlation_id"),
            "source_id": f"{entity}:{ev.get('entity_id')}:{ev.get('sequence') or ev.get('event_id')}", "event_type": f"herdr.{etype}",
            "provenance_refs": [{"source_ref": f"herdr:{entity}:{ev.get('entity_id')}", "anchor_type": "agent_event",
                                 "anchor_value": str(ev.get("event_id"))}]}
    if entity == "artifact" and etype == "ArtifactRegistered":
        return dict(base, object_ref=f"artifact:{ev.get('entity_id')}", category="artifact",
                    title=f"artifact {payload.get('filename', '')}"[:200], actor_ref=payload.get("created_by") and f"agent:{payload['created_by']}",
                    payload={"sha256": payload.get("sha256"), "size_bytes": payload.get("size_bytes"), "mime_type": payload.get("mime_type")},
                    provenance_refs=base["provenance_refs"] + ([{"source_ref": f"artifact:{ev.get('entity_id')}", "anchor_type": "attachment_bytes",
                                                                 "anchor_value": "all", "content_hash": payload["sha256"]}] if payload.get("sha256") else []))
    if entity == "ci_fix" and isinstance(etype, str) and etype.startswith("CIFix"):
        keep = {k: payload.get(k) for k in ("repository", "number", "run", "revision", "state", "reason", "branch", "pr_url", "model", "host")
                if payload.get(k) is not None}
        return dict(base, object_ref=f"ci_fix:{ev.get('entity_id')}", category="ci",
                    title=f"CI fix {etype[5:]} {payload.get('state', '')}".strip()[:200], payload=keep, causation_id=payload.get("run") and str(payload["run"]))
    return None


class EventBridge:
    def __init__(self, store: pi.IntelligenceStore, cfg_fn: Callable[[], dict], resolve_projects: Callable[[dict], tuple[str, ...]],
                 *, org_id: str | Callable[[], str | None], instance: str = "herdr", security_label: str = "internal", capacity: int = 1000):
        self.store, self._cfg_fn, self._resolve = store, cfg_fn, resolve_projects
        self._org_src, self._instance, self._label = org_id, instance, security_label
        self._q: queue.Queue = queue.Queue(maxsize=capacity)
        from .intel_incidents import IncidentBoard
        self.board = IncidentBoard("v1")     # in-memory fold of ci_fix events; rebuilt by backfill/replay
        self.stats = {"accepted": 0, "replayed": 0, "unmapped": 0, "dropped": 0, "errors": 0, "ignored": 0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def _org(self) -> str:
        org = self._org_src() if callable(self._org_src) else self._org_src
        if not isinstance(org, str) or not org:
            raise pi.IntelligenceError('control_plane.project_intelligence.default_org_id is not set')
        return org

    # listener: must be instant and must never raise into the repo
    def on_event(self, ev: dict) -> None:
        try:
            if self._cfg_fn().get('enabled') is not True:
                return          # disabled (the default): not even queued
            self._q.put_nowait(ev)
        except queue.Full:
            self.stats["dropped"] += 1

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="herdr-intel-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.process(ev)
            finally:
                self._q.task_done()

    def drain(self, timeout: float = 5.0) -> None:
        """Test/ops helper: wait for the queue to empty."""
        end = datetime.now(UTC).timestamp() + timeout
        while self._q.unfinished_tasks and datetime.now(UTC).timestamp() < end:
            self._stop.wait(0.01)

    def process(self, ev: dict) -> str:
        try:
            cfg = pi.config(self._cfg_fn())
            if not (cfg["enabled"] and cfg["ingest"]) or cfg["kill_switch"]:
                return "off"
            if ev.get("entity_type") in ("pr_gate", "pr_gate_main"):
                return self._fold_gate(ev)
            probe = normalise_fabric_event(ev, org_id=self._org, instance=self._instance, projects=("_",), security_label=self._label)
            if probe is None:
                self.stats["ignored"] += 1
                return "ignored"
            projects = self._resolve(ev)
            if not projects:
                self.stats["unmapped"] += 1   # never guessed into a project
                return "unmapped"
            _, created = self.store.append_event(dict(probe, project_ids=list(projects)), cfg=cfg)
            self.stats["accepted" if created else "replayed"] += 1
            self._fold_incident(ev, projects)
            return "accepted" if created else "replayed"
        except (pi.IngestionPaused, pi.Conflict):
            return "paused"
        except Exception:  # never log content; the type is enough to alert on
            self.stats["errors"] += 1
            log.warning("intelligence bridge failed to process an event", exc_info=False)
            return "error"

    def rebuild_board(self, repo) -> dict:
        """Re-fold the durable ci_fix and review-gate event streams into the incident board (the board is derived
        state, so a restart rebuilds it instead of keeping a second copy). Writes nothing to the intelligence store."""
        seen = 0
        for entity in ("ci_fix", "pr_gate", "pr_gate_main"):      # repair runs first: they bind the PRs the others move
            for entity_id in repo.entity_ids(entity):
                for ev in repo.stream(entity, entity_id):
                    ev = dict(ev, entity_type=entity, entity_id=entity_id)
                    seen += 1
                    if entity == "ci_fix":
                        projects = self._resolve(ev)
                        if projects:
                            self._fold_incident(ev, projects)
                    else:
                        self._fold_gate(ev)
        return {"events": seen, "incidents": len(self.board.incidents)}

    def _fold_gate(self, ev: dict) -> str:
        """Review-gate events move an incident only when a repair run recorded this PR's URL (never by guesswork)."""
        at, eid = str(_when(ev.get("source_timestamp"))), str(ev.get("event_id"))
        payload = ev.get("payload") or {}
        if ev.get("entity_type") == "pr_gate_main":
            done = self.board.apply_main(str(ev.get("entity_id")), payload.get("sha"), event_id=eid, at=at,
                                         state=str(ev.get("event_type")))
        else:
            done = self.board.apply_gate(str(ev.get("entity_id")), event_id=eid, at=at, state=str(ev.get("event_type")))
            if done is not None and ev.get("event_type") == "merged":
                self.board.note_merge_sha(str(ev.get("entity_id")), payload.get("merge_sha"))
        self.stats["accepted" if done is not None else "ignored"] += 1
        return "folded" if done is not None else "ignored"

    def _fold_incident(self, ev: dict, projects: tuple[str, ...]) -> None:
        payload = ev.get("payload") or {}
        if ev.get("entity_type") != "ci_fix" or not isinstance(payload.get("state"), str):
            return
        source = f"{payload.get('repository')}#{payload.get('run') or payload.get('number')}"
        from .intel_incidents import pr_key_from_url
        self.board.bind_pr(pr_key_from_url(payload.get("pr_url")), projects[0], source)
        self.board.apply(project=projects[0], source_incident=source, event_id=str(ev.get("event_id")),
                         at=str(_when(ev.get("source_timestamp"))), kind="ci_fix", state=payload["state"])

    def backfill(self, repo, *, entities=("ci_fix", "artifact"), max_events: int = 10_000, dry_run: bool = True) -> dict:
        """Opt-in, repeatable replay of existing event streams. ``dry_run`` only counts; nothing is written.

        ``unmapped`` is the number of covered events skipped because no project could be resolved, so an operator
        can see what a run left out instead of silently losing it.
        """
        seen = would = unmapped = 0
        for entity in entities:
            for entity_id in repo.entity_ids(entity):
                for ev in repo.stream(entity, entity_id):
                    if seen >= max_events:
                        return {"seen": seen, "would_append": would, "unmapped": unmapped, "truncated": True, "dry_run": dry_run}
                    seen += 1
                    ev = dict(ev, entity_type=entity, entity_id=entity_id)
                    if normalise_fabric_event(ev, org_id=self._org, instance=self._instance, projects=("_",)) is None:
                        continue
                    if not self._resolve(ev):
                        unmapped += 1
                        if not dry_run:
                            self.stats["unmapped"] += 1
                        continue
                    would += 1
                    if not dry_run:
                        self.process(ev)
        return {"seen": seen, "would_append": would, "unmapped": unmapped, "truncated": False, "dry_run": dry_run}


def make_resolver(repo, forge_host: str | Callable[[], str | None] | None = None) -> Callable[[dict], tuple[str, ...]]:
    """Resolve an event's project from its repository via the existing ``project_links`` mapping.

    A ``ci_fix`` stream names its repository only in the first event, so later events look it up there.
    Artifacts carry no verified repository linkage, so they stay unmapped until a server-verified link exists.
    A bare ``owner/name`` names no forge, so it resolves only when the operator configured ``forge_host``
    (``control_plane.project_intelligence.forge_host``); without it the event is unmapped, never guessed.
    """
    from .project_links import canonical_repository, project_for_repository

    def resolve(ev: dict) -> tuple[str, ...]:
        # Only a ci_fix stream is placed, from its own validated first event (CIFixService checks the repository
        # allow-list). Artifact payloads are uploader-supplied text and never place an event in a project.
        if ev.get("entity_type") != "ci_fix":
            return ()
        first = repo.stream("ci_fix", ev.get("entity_id"))[:1]
        repository = ((first[0].get("payload") or {}).get("repository")) if first else None
        host = forge_host() if callable(forge_host) else forge_host
        identity = canonical_repository(repository, forge_host=host) if repository else None
        project = project_for_repository(identity) if identity else None
        return (project,) if project else ()
    return resolve


def install(repo, section_fn: Callable[[], dict], dsn: str | None = None):
    """Wire project intelligence into one control-plane server. Returns ``(routes, bridge)``.

    With the feature disabled (the default) this costs one cached dict read per recorded event: no database
    connection, no thread, no table load. The store (durable when ``dsn`` is set and the tables are migrated,
    else memory with a warning), and the worker thread are created the first time the feature is seen enabled.
    """
    from .project_intelligence_api import IntelligenceRoutes
    ttl = 2.0
    cache: dict = {"at": 0.0, "value": {}}

    def section() -> dict:
        now = time.monotonic()
        if now - cache["at"] > ttl:
            try:
                cache["value"] = dict(section_fn() or {})
            except Exception:  # noqa: BLE001 - config trouble means "off", never an exception into the repo
                cache["value"] = {}
            cache["at"] = now
        return cache["value"]

    lock = threading.Lock()
    holder: dict = {}

    def store() -> pi.IntelligenceStore:
        with lock:
            if "store" not in holder:
                built: pi.IntelligenceStore = pi.IntelligenceStore()
                if dsn:
                    try:
                        built = DurableIntelligenceStore(PgPersistence(dsn))
                    except StoreUnavailable as exc:
                        log.warning("project intelligence is running in memory only: %s", type(exc).__name__)
                holder["store"] = built
            return holder["store"]

    class LazyBridge(EventBridge):
        def __init__(self):
            super().__init__(None, section, make_resolver(repo, lambda: section().get("forge_host")),
                             org_id=lambda: section().get("default_org_id"), instance=str(section().get("instance") or "herdr"))

        @property
        def store(self):  # type: ignore[override]
            return store()

        @store.setter
        def store(self, _):
            pass

        def on_event(self, ev: dict) -> None:
            if section().get("enabled") is not True:
                return          # disabled (the default): no store, no thread, nothing queued
            if self._thread is None:
                with lock:
                    if self._thread is None:
                        try:
                            self.rebuild_board(repo)
                        except Exception:  # noqa: BLE001 - a rebuild problem must not stop live ingest
                            log.warning("incident board rebuild failed", exc_info=False)
                        self.start()
            super().on_event(ev)

    bridge = LazyBridge()
    repo.subscribe(bridge.on_event)
    return IntelligenceRoutes(store_fn=store, section_fn=section_fn), bridge
