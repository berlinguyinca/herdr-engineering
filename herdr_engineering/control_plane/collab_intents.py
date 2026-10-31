"""The worker-side intent applier of the collab artifact bridge (plan 3, Tasks 5a–5d).

``collab-public`` writes ``intents/<ticket>.json`` into the outbox; this module consumes them. Everything in an intent
file is untrusted: the ticket comes only from a file name that matched ``TICKET_RE``, the envelope and payload are
re-validated with ``validate_intent`` (Task 3), and nothing from a rejected intent is copied into a result or a log line
except an actor that passes the Task 3 actor check on its own.

Per intent file, at scan time (before any lane wait), for every kind (Task 5a):

1. A ticket already decided in the ledger gets its result file (kept if present, else rewritten from the row) and the
   intent is unlinked.
2. The intent is validated; an invalid one or a kind with no registered handler is ``rejected`` with a fixed reason.
3. A decided ticket of the same actor, kind and idempotency key (``idempotency_key or ticket``) with one of the kind's
   ``replay_outcomes`` is replayed when its ``request_sha256`` matches, else this ticket is ``rejected`` "idempotency
   key was reused for a different request". This runs before any upload lookup.
4. With no ledger row, an intent older than ``INTENT_MAX_AGE`` is ``expired`` (recorded with 0 bytes, so it is never
   charged against the quota); otherwise its ``received`` row is written, and from then on no age check applies, so an
   intent queued behind its own lane's multi-GB assembly never expires. A ``received`` row whose ``request_sha256``,
   actor or kind differ from the re-validated file is ``rejected``: a rewritten file never inherits the exemption.
5. An agent actor must be ``active`` and unexpired in a fresh ``agents.json`` (``FEED_MAX_AGE``).

The intent then waits for its lane (``lane_key``; default one lane per actor). Lanes run concurrently up to
``limits.apply_concurrency``; within a lane intents run oldest first, never two at once; a lane still busy from an
earlier tick is skipped (its files are rescanned next tick), never awaited. In the lane, under session advisory locks on
the lane and on the idempotency key (Postgres, so a second worker process cannot interleave), steps 1, 3 and 5 are
checked again just before the handler runs: the store does not enforce key uniqueness or quota itself.

The handler returns ``{result, version_id?, reason?, attempt_id?, …}``; the applier records it with ``record_outcome``
(which decides once: if another path decided first, the decided row is what gets written), writes
``results/<ticket>.json`` into the mirror with ``atomic_write_json`` and unlinks the intent with ``unlink_beneath``.
Every outbox access is fd-relative and no-follow (``list_beneath``/``read_json_beneath``/``unlink_beneath``); all
blocking file I/O runs in worker threads. Log lines carry only ticket, kind, actor, artifact and outcome.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from .artifact_collab_store import CollabGrantStore
from .artifacts import ArtifactStore
from .collab_bridge_files import (
    DIR_MODE,
    FEED_MAX_AGE,
    INTENT_MAX_AGE,
    TICKET_RE,
    Actor,
    BridgeError,
    BridgeLimits,
    Intent,
    IntentError,
    _validate_actor,
    atomic_write_json,
    ensure_dir,
    list_beneath,
    read_json_beneath,
    unlink_beneath,
    validate_intent,
)
from .collab_mirror import MIRROR_MAX_ENTRIES

log = logging.getLogger(__name__)

INTENTS_DIR = "intents"
AGENTS_FEED = "agents.json"
RESULTS_DIR = "results"
AGENTS_FEED_MAX_BYTES = 4 * 1024 * 1024
# A commit body is capped at 64 KiB on the wire, but JSON escaping can grow text up to 6x; plan 4/5 kinds are small.
INTENT_MAX_BYTES = 1024 * 1024
INTENTS_MIN_ENTRIES = 4096

REASON_UNREADABLE = "intent is not readable"
REASON_UNKNOWN_KIND = "unknown intent kind"
REASON_KEY_REUSED = "idempotency key was reused for a different request"
REASON_CHANGED = "intent changed after it was received"
REASON_EXPIRED = "intent expired"
REASON_FEED_STALE = "agent feed stale"
REASON_AGENT = "agent not active"
REASON_INTERNAL = "internal error"

_RESULT_KEYS = ("version_id", "version_number", "head_version_id", "merged_names", "conflicts", "reason")
_OUTCOME = re.compile(r"[a-z_]{1,32}\Z")
_INTENT_FILE = re.compile(TICKET_RE[:-2] + r"\.json\Z")
_KIND_NAME = re.compile(r"[a-z][a-z0-9_.]{0,47}\Z")
_LOCK_SALT = "herdr.collab-intents:"


async def _assemble_unavailable(*_args, **_kwargs):
    raise NotImplementedError("staged-upload assembly arrives with Task 5c")


@dataclass
class IntentContext:
    actor: Actor
    agent: dict | None
    conn: object
    store: ArtifactStore
    grants: CollabGrantStore
    limits: BridgeLimits
    assemble: Callable[..., Awaitable[dict]]
    extras: Mapping[str, object]


@dataclass(frozen=True)
class _Kind:
    handler: Callable[[IntentContext, Intent], Awaitable[dict]]
    lane_key: Callable[[Intent, dict | None], str] | None
    replay_outcomes: frozenset[str]


@dataclass
class _Job:
    name: str
    intent: Intent
    spec: _Kind
    lane: str


def _default_lane(intent: Intent, _agent: dict | None) -> str:
    return f"{intent.actor.kind}:{intent.actor.id}"


def _finite(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _epoch(value) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value) if _finite(value) else None


def _safe_actor(raw) -> dict | None:
    """The raw intent's actor only if it passes the Task 3 check on its own; otherwise nothing is echoed."""
    try:
        actor = _validate_actor(raw.get("actor") if isinstance(raw, dict) else None)
    except IntentError:
        return None
    return {"kind": actor.kind, "id": actor.id}


class CollabIntentApplier:
    def __init__(self, outbox: Path, mirror, *, rustfs, limits: BridgeLimits, spool_dir: Path,
                 lineage: Callable[[dict], dict] | None = None, context_extras: Mapping[str, object] | None = None,
                 clock: Callable[[], float] = time.time, max_per_tick: int = 20):
        self.outbox = Path(outbox)
        self.mirror = mirror
        self.rustfs = rustfs
        self.limits = limits
        self.spool_dir = Path(spool_dir)
        self.lineage = lineage
        self.extras = MappingProxyType(dict(context_extras or {}))
        self.clock = clock
        self.max_per_tick = max_per_tick
        self.assemble: Callable[..., Awaitable[dict]] = _assemble_unavailable
        self._kinds: dict[str, _Kind] = {}
        self._busy: dict[str, asyncio.Task] = {}
        self._inflight: set[str] = set()
        self._feed: dict | None = None
        self._sem = asyncio.Semaphore(limits.apply_concurrency)
        self._counts = self._zero()

    # -- registry ---------------------------------------------------------------------------------------------
    def register(self, kind: str, handler: Callable[[IntentContext, Intent], Awaitable[dict]], *,
                 lane_key: Callable[[Intent, dict | None], str] | None = None,
                 replay_outcomes: frozenset[str] = frozenset()) -> None:
        """Attach the handler of an intent kind (its validator is registered with ``register_intent_kind``)."""
        if not isinstance(kind, str) or not _KIND_NAME.match(kind):
            raise ValueError("invalid intent kind name")
        if kind in self._kinds:
            raise ValueError("intent kind already has a handler")
        if not callable(handler) or (lane_key is not None and not callable(lane_key)):
            raise ValueError("handler and lane_key must be callable")
        outcomes = frozenset(replay_outcomes)
        if not all(isinstance(o, str) and _OUTCOME.match(o) for o in outcomes):
            raise ValueError("invalid replay outcome")
        self._kinds[kind] = _Kind(handler, lane_key, outcomes)

    # -- the tick ---------------------------------------------------------------------------------------------
    @staticmethod
    def _zero() -> dict:
        return {"applied": 0, "conflicts": 0, "rejected": 0, "skipped": 0}

    def _count(self, outcome: str) -> None:
        if outcome == "conflict":
            self._counts["conflicts"] += 1
        elif outcome in ("rejected", "expired"):
            self._counts["rejected"] += 1
        else:
            self._counts["applied"] += 1

    def _take_counts(self) -> dict:
        counts, self._counts = self._counts, self._zero()
        return counts

    async def tick(self, acquire, *, wait: float = 1.0) -> dict:
        """Scan the outbox once and start the free lanes; returns the outcomes decided since the previous tick.

        ``acquire()`` is an async context manager yielding a connection of the applier's own pool (one for the scan,
        one per running lane). Lanes started here are awaited for at most ``wait`` seconds; a longer one keeps
        running and its lane is skipped by later ticks until it finishes (``drain`` awaits them all).
        """
        feed, listing = await asyncio.to_thread(self._load_outbox)
        self._feed = feed
        if listing is None:
            return self._take_counts()
        mtimes = {name: st.st_mtime for name, st in listing
                  if stat.S_ISREG(st.st_mode) and _INTENT_FILE.match(name) and name[:-5] not in self._inflight}
        names = sorted(mtimes, key=lambda n: (mtimes[n], n))
        names = names[:self.max_per_tick]
        raws = await asyncio.to_thread(self._read_intents, names) if names else {}
        lanes: dict[str, list[_Job]] = {}
        async with acquire() as conn:
            for name in names:
                try:
                    job = await self._scan(conn, name, raws.get(name))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad intent never stops the scan; retried next tick
                    log.error("collab intent %s: scan failed (%s)", name[:-5], type(exc).__name__)
                    continue
                if job is not None:
                    lanes.setdefault(job.lane, []).append(job)
        started = []
        for lane, jobs in lanes.items():
            if lane in self._busy:
                self._counts["skipped"] += len(jobs)
                continue
            jobs.sort(key=lambda j: (j.intent.submitted_at, j.intent.ticket))
            self._inflight.update(j.intent.ticket for j in jobs)
            task = asyncio.create_task(self._run_lane(lane, jobs, acquire))
            self._busy[lane] = task
            started.append(task)
        if started and wait > 0:
            await asyncio.wait(started, timeout=wait)
        return self._take_counts()

    async def drain(self, timeout: float | None = None) -> None:
        """Await every running lane (shutdown and tests)."""
        tasks = list(self._busy.values())
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def aclose(self, timeout: float = 5.0) -> None:
        """Let running lanes finish for ``timeout`` seconds, then cancel the rest (their intents stay ``received``
        and are picked up again, age-exempt, after a restart)."""
        await self.drain(timeout)
        tasks = list(self._busy.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    def _load_outbox(self):
        feed = read_json_beneath(self.outbox, [AGENTS_FEED], max_bytes=AGENTS_FEED_MAX_BYTES)
        agents = feed.get("agents") if isinstance(feed, dict) else None
        cap = max(INTENTS_MIN_ENTRIES,
                  (len(agents) if isinstance(agents, dict) else 0) * self.limits.max_pending_intents_per_agent * 2)
        try:
            listing = list_beneath(self.outbox, [INTENTS_DIR], max_entries=cap)
        except BridgeError:
            log.warning("collab intents: outbox intents dir has too many entries; tick skipped")
            return feed, None
        return feed, listing

    def _read_intents(self, names: list[str]) -> dict[str, dict | None]:
        return {n: read_json_beneath(self.outbox, [INTENTS_DIR, n], max_bytes=INTENT_MAX_BYTES) for n in names}

    # -- generic steps 1-5 (scan time) ------------------------------------------------------------------------
    async def _scan(self, conn, name: str, raw: dict | None) -> _Job | None:
        ticket = name[:-5]
        grants = CollabGrantStore(conn)
        row = await self._ledger(conn, ticket)
        if row is not None and row["outcome"] != "received":                                        # step 1
            await self._settle_from_row(name, row)
            return None
        if raw is None:                                                                             # step 2
            await self._reject_unvalidated(conn, name, row, None, REASON_UNREADABLE)
            return None
        try:
            intent = validate_intent(raw, filename=name, limits=self.limits)
        except IntentError as exc:
            await self._reject_unvalidated(conn, name, row, raw, exc.reason)
            return None
        spec = self._kinds.get(intent.kind)
        if spec is None:
            await self._decide(conn, intent, row, {"result": "rejected", "reason": REASON_UNKNOWN_KIND})
            return None
        if row is not None and (row["request_sha256"] != intent.request_sha256 or row["kind"] != intent.kind
                                or (row["actor_kind"], row["actor_id"]) != (intent.actor.kind, intent.actor.id)):
            await self._decide(conn, intent, row, {"result": "rejected", "reason": REASON_CHANGED})
            return None
        if await self._replayed(conn, name, intent, spec, row):                                       # step 3
            return None
        if row is None:                                                                             # step 4
            if self.clock() - intent.submitted_at > INTENT_MAX_AGE:
                await self._decide(conn, intent, None, {"result": "expired", "reason": REASON_EXPIRED}, charge=False)
                return None
            existing = await grants.record_received(
                intent.ticket, intent.kind, intent.actor, intent.artifact_id, _base_of(intent), self._key(intent),
                intent.request_sha256, _inline_bytes(intent))
            if existing is not None:                       # another worker received it first: start over next tick
                return None
        agent, reason = self._agent_for(intent)                                                      # step 5
        if reason is not None:
            await self._finish(conn, name, intent, {"result": "rejected", "reason": reason})
            return None
        try:
            lane = (spec.lane_key or _default_lane)(intent, agent)
            if not isinstance(lane, str) or not lane:
                raise ValueError("lane key must be a non-empty string")
        except Exception as exc:
            log.error("collab intent %s: lane key failed (%s)", ticket, type(exc).__name__)
            await self._finish(conn, name, intent, {"result": "rejected", "reason": REASON_INTERNAL})
            return None
        return _Job(name, intent, spec, lane)

    @staticmethod
    def _key(intent: Intent) -> str:
        return intent.idempotency_key or intent.ticket

    def _agent_for(self, intent: Intent) -> tuple[dict | None, str | None]:
        """Step 5: the actor's ``agents.json`` row, or the rejection reason. User actors skip it (their handler
        authenticates them)."""
        if intent.actor.kind != "agent":
            return None, None
        feed = self._feed
        now = self.clock()
        generated = feed.get("generated_at") if isinstance(feed, dict) else None
        agents = feed.get("agents") if isinstance(feed, dict) else None
        if not _finite(generated) or now - generated > FEED_MAX_AGE or not isinstance(agents, dict):
            return None, REASON_FEED_STALE
        row = agents.get(intent.actor.id)
        if not isinstance(row, dict) or row.get("status") != "active":
            return None, REASON_AGENT
        expires, hard = row.get("expires_at"), row.get("hard_expiry")
        if not _finite(expires) or expires <= now or (hard is not None and (not _finite(hard) or hard <= now)):
            return None, REASON_AGENT
        return dict(row), None

    async def _replayed(self, conn, name: str, intent: Intent, spec: _Kind, row: dict | None) -> bool:
        """Step 3: True when this ticket was settled by an earlier decided ticket with the same key."""
        if not spec.replay_outcomes:
            return False
        prior = await CollabGrantStore(conn).decided_for_key(intent.actor, intent.kind, self._key(intent),
                                                             spec.replay_outcomes)
        if prior is None or prior["ticket_id"] == intent.ticket:
            return False
        if prior["request_sha256"] != intent.request_sha256:
            out = {"result": "rejected", "reason": REASON_KEY_REUSED}
        else:
            out = await asyncio.to_thread(self._result_of_row, prior)
            out = {k: v for k, v in out.items() if k == "result" or k in _RESULT_KEYS}
            out["attempt_id"] = prior.get("attempt_id")
        await self._decide(conn, intent, row, out, charge=False, name=name)
        return True

    # -- lanes ------------------------------------------------------------------------------------------------
    async def _run_lane(self, lane: str, jobs: list[_Job], acquire) -> None:
        try:
            for index, job in enumerate(jobs):
                async with self._sem:
                    ran = await self._apply(job, acquire)
                self._inflight.discard(job.intent.ticket)
                if not ran:                                # locked elsewhere: keep the lane's order, retry next tick
                    self._counts["skipped"] += len(jobs) - index
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("collab intents: lane failed (%s)", type(exc).__name__)
        finally:
            for job in jobs:
                self._inflight.discard(job.intent.ticket)
            if self._busy.get(lane) is asyncio.current_task():
                del self._busy[lane]

    async def _apply(self, job: _Job, acquire) -> bool:
        intent = job.intent
        async with acquire() as conn:
            locks = [_LOCK_SALT + "lane:" + job.lane,
                     _LOCK_SALT + f"key:{intent.actor.kind}:{intent.actor.id}:{intent.kind}:{self._key(intent)}"]
            held = []
            try:
                for lock in locks:
                    if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", lock):
                        return False
                    held.append(lock)
                await self._apply_locked(conn, job)
                return True
            finally:
                for lock in reversed(held):
                    try:
                        await conn.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock)
                    except Exception as exc:  # a broken connection drops its session locks anyway
                        log.warning("collab intents: advisory unlock failed (%s)", type(exc).__name__)

    async def _apply_locked(self, conn, job: _Job) -> None:
        intent, name = job.intent, job.name
        row = await self._ledger(conn, intent.ticket)
        if row is None:
            return                                         # the received row vanished: rescanned next tick
        if row["outcome"] != "received":
            await self._settle_from_row(name, row)
            return
        if await self._replayed(conn, name, intent, job.spec, row):
            return
        agent, reason = self._agent_for(intent)            # the newest feed: revoked while queued is still refused
        if reason is not None:
            await self._finish(conn, name, intent, {"result": "rejected", "reason": reason})
            return
        ctx = IntentContext(actor=intent.actor, agent=agent, conn=conn, store=ArtifactStore(conn, self.rustfs),
                            grants=CollabGrantStore(conn), limits=self.limits, assemble=self.assemble,
                            extras=self.extras)
        try:
            out = await job.spec.handler(ctx, intent)
            if not isinstance(out, dict) or not isinstance(out.get("result"), str) \
                    or not _OUTCOME.match(out["result"]) or out["result"] == "received":
                raise TypeError("handler returned an invalid outcome")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("collab intent %s (%s): handler failed (%s)", intent.ticket, intent.kind, type(exc).__name__)
            out = {"result": "rejected", "reason": REASON_INTERNAL}
        await self._finish(conn, name, intent, out)

    # -- deciding and results ---------------------------------------------------------------------------------
    @staticmethod
    async def _ledger(conn, ticket: str) -> dict | None:
        row = await conn.fetchrow("SELECT * FROM artifact_collab_intents WHERE ticket_id=$1", ticket)
        return dict(row) if row else None

    async def _decide(self, conn, intent: Intent, row: dict | None, out: dict, *, charge: bool = True,
                      name: str | None = None) -> None:
        """Record a validated intent's outcome (writing its ``received`` row first when it has none) and finish it."""
        if row is None:
            existing = await CollabGrantStore(conn).record_received(
                intent.ticket, intent.kind, intent.actor, intent.artifact_id, _base_of(intent), self._key(intent),
                intent.request_sha256, _inline_bytes(intent) if charge else 0)
            if existing is not None and existing["outcome"] != "received":
                await self._settle_from_row(name or intent.ticket + ".json", existing)
                return
        await self._finish(conn, name or intent.ticket + ".json", intent, out)

    async def _finish(self, conn, name: str, intent: Intent, out: dict) -> None:
        result = out["result"]
        reason = out.get("reason") if isinstance(out.get("reason"), str) else None
        version_id = out.get("version_id") if isinstance(out.get("version_id"), str) else None
        attempt_id = out.get("attempt_id") if isinstance(out.get("attempt_id"), str) else None
        decided = await CollabGrantStore(conn).record_outcome(intent.ticket, result, version_id=version_id,
                                                              attempt_id=attempt_id, reason=reason)
        if not decided:                                    # decided elsewhere first: that decision is the result
            row = await self._ledger(conn, intent.ticket)
            if row is not None and row["outcome"] != "received":
                await self._settle_from_row(name, row)
            return
        doc = {"ticket": intent.ticket, "kind": intent.kind,
               "actor": {"kind": intent.actor.kind, "id": intent.actor.id}, "artifact_id": intent.artifact_id,
               "result": result}
        doc.update({k: out[k] for k in _RESULT_KEYS if out.get(k) is not None})
        doc["decided_at"] = self.clock()
        await asyncio.to_thread(self._write_and_consume, name, doc)
        self._done(intent.ticket, intent.kind, intent.actor.kind, intent.actor.id, intent.artifact_id, result)

    async def _reject_unvalidated(self, conn, name: str, row: dict | None, raw, reason: str) -> None:
        """An intent that failed validation: nothing of its content is kept except an independently valid actor."""
        ticket = name[:-5]
        if row is not None:                                # received earlier, rewritten since: decide that row
            await CollabGrantStore(conn).record_outcome(ticket, "rejected", reason=reason)
            row = await self._ledger(conn, ticket)
            if row is not None and row["outcome"] != "received":
                await self._settle_from_row(name, row)
            return
        doc = {"ticket": ticket, "kind": None, "actor": _safe_actor(raw), "artifact_id": None, "result": "rejected",
               "reason": reason, "decided_at": self.clock()}
        await asyncio.to_thread(self._write_and_consume, name, doc)
        actor = doc["actor"] or {}
        self._done(ticket, None, actor.get("kind"), actor.get("id"), None, "rejected")

    async def _settle_from_row(self, name: str, row: dict) -> None:
        """Step 1: a decided ticket keeps the result file it has; one with none is rewritten from its row."""
        def go() -> None:
            existing = self._read_result(row["ticket_id"])
            if existing is None or existing.get("result") != row["outcome"]:
                self._write_result(self._doc_of_row(row))
            self._consume(name)
        await asyncio.to_thread(go)
        self._done(row["ticket_id"], row["kind"], row["actor_kind"], row["actor_id"], row["artifact_id"],
                   row["outcome"])

    def _doc_of_row(self, row: dict) -> dict:
        doc = {"ticket": row["ticket_id"], "kind": row["kind"],
               "actor": {"kind": row["actor_kind"], "id": row["actor_id"]}, "artifact_id": row["artifact_id"]}
        doc.update(self._result_of_row(row))
        doc["decided_at"] = _epoch(row.get("decided_at")) or self.clock()
        return doc

    def _result_of_row(self, row: dict) -> dict:
        """``{result, …}`` for a decided row: its own result file's details when that file agrees, else the row."""
        out = {"result": row["outcome"]}
        existing = self._read_result(row["ticket_id"])
        if existing is not None and existing.get("result") == row["outcome"]:
            out.update({k: existing[k] for k in _RESULT_KEYS if existing.get(k) is not None})
        else:
            if row.get("version_id"):
                out["version_id"] = row["version_id"]
            if row.get("reason"):
                out["reason"] = row["reason"]
        return out

    def _read_result(self, ticket: str) -> dict | None:
        return read_json_beneath(self.mirror.root, [RESULTS_DIR, ticket + ".json"],
                                 max_bytes=self.limits.max_conflict_text_bytes + 1_048_576)

    def _write_result(self, doc: dict) -> None:
        ensure_dir(Path(self.mirror.root), mode=DIR_MODE, public_mode=True)
        atomic_write_json(Path(self.mirror.root) / RESULTS_DIR, doc["ticket"] + ".json", doc)

    def _consume(self, name: str) -> None:
        unlink_beneath(self.outbox, [INTENTS_DIR], name)

    def _write_and_consume(self, name: str, doc: dict) -> None:
        self._write_result(doc)
        self._consume(name)

    def _done(self, ticket, kind, actor_kind, actor_id, artifact_id, outcome: str) -> None:
        self._count(outcome)
        log.info("collab intent %s kind=%s actor=%s:%s artifact=%s outcome=%s",
                 ticket, kind, actor_kind, actor_id, artifact_id, outcome)

    # -- results sweep ----------------------------------------------------------------------------------------
    async def sweep_results(self, max_age: float = 3600) -> int:
        """Delete result files older than ``max_age`` seconds (by mtime); returns how many."""
        def go() -> int:
            try:
                entries = list_beneath(Path(self.mirror.root), [RESULTS_DIR], max_entries=MIRROR_MAX_ENTRIES)
            except BridgeError:
                log.warning("collab intents: results dir has too many entries; sweep skipped")
                return 0
            cutoff = self.clock() - max_age
            removed = 0
            for name, st in entries or ():
                if stat.S_ISREG(st.st_mode) and _INTENT_FILE.match(name) and st.st_mtime < cutoff:
                    removed += unlink_beneath(Path(self.mirror.root), [RESULTS_DIR], name)
            return removed
        return await asyncio.to_thread(go)


def _base_of(intent: Intent) -> str | None:
    base = getattr(intent.payload, "base_version_id", None)
    return base if isinstance(base, str) else None


def _inline_bytes(intent: Intent) -> int:
    if intent.kind != "commit":
        return 0
    value = getattr(intent.payload, "inline_bytes", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["CollabIntentApplier", "IntentContext", "INTENT_MAX_BYTES", "RESULTS_DIR", "INTENTS_DIR", "AGENTS_FEED"]
