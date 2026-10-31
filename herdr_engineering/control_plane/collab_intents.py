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
   charged against the quota) and one with a ``submitted_at`` too far ahead is ``rejected``, both even while the
   received set is full (deciding them needs no memory); otherwise its ``received`` row is written, and from then on
   no age check applies, so an intent queued behind its own lane's multi-GB assembly never expires. A ``received`` row
   whose ``request_sha256``, actor, kind or idempotency key differ from the re-validated file is ``rejected``: a
   rewritten file never inherits the exemption. ``request_sha256`` (``request_digest``) covers kind, actor,
   artifact_id and payload only; the key and ``submitted_at`` are compared separately, the key against the row and
   ``submitted_at`` against the received record (the row does not store it; see the lane below).
5. An agent actor must be ``active`` and unexpired in a fresh ``agents.json`` (``FEED_MAX_AGE``).

The intent then waits for its lane (``lane_key``; default one lane per actor). Lanes run concurrently up to
``limits.apply_concurrency``; within a lane intents run oldest first, never two at once; a lane still busy from an
earlier tick is skipped, never awaited. Only a small record of a received ticket is kept in memory (no payload; the
set is capped). In the lane, under session advisory locks on the lane and on the idempotency key (Postgres, so a second
worker process cannot interleave), the file is re-read and re-validated; it is ``rejected`` "intent changed after it
was received" unless its digest, kind, actor and key equal the ledger row's and the record's (the key is what the
advisory lock and the replay lookup are keyed on, so a moved key would be applied twice) and its ``submitted_at``
equals the record's (it decided the lane order and the age check); a withdrawn file is forgotten, and steps 1, 3 and 5
are checked again just before the handler runs: the store does not enforce key uniqueness or quota itself.

The handler returns ``{result, version_id?, reason?, attempt_id?, …}``; the applier records it with ``record_outcome``
(which decides once: if another path decided first, the decided row is what gets written), writes
``results/<ticket>.json`` into the mirror with ``atomic_write_json`` and unlinks the intent with ``unlink_beneath``.
Every outbox access is fd-relative and no-follow (``list_beneath``/``read_json_beneath``/``unlink_beneath``); all
blocking file I/O runs in worker threads. Log lines carry only ticket, kind, actor, artifact and outcome.

The ``commit`` kind (Task 5b; ``register_commit``) runs in the lane as ``collab:<agent_id>``:

6. ``grant_for`` must show ``write``, unexpired, re-read now (a grant revoked after the intent was written is "no write
   grant", whatever its staging holds).
7. The artifact must exist and be writable (not a report, no ``report_descriptor`` / ``source_manifest_blob`` on base
   or head); neither it nor its base or head version may be retracted, by ``lifecycle`` / ``state`` or by the worker's
   ``ArtifactGraphGate`` (reloaded with ``load_retractions`` at least every ``GATE_RELOAD`` s). Put names must not clash
   with the base/head files (``check_names``: duplicates and file/directory prefixes).
9. Daily quota: ``bytes_last_24h(agent, exclude_ticket=ticket) + inline_bytes <= daily_bytes_per_agent`` (the
   ticket's own ``received`` row is excluded, so a crash-retry is never counted twice).
10. Inline text is stored with ``rustfs.put`` and ``register_report_blob``; staged puts go through ``ctx.assemble``
    (Task 5c).
11. ``merge_commit`` (no outer transaction, so a conflict's attempt row persists) with ``ticket_id`` and
    ``max_text_bytes = limits.max_text_merge_bytes``; ``ArtifactMergeConflict`` is ``conflict`` (its ``attempt_id``
    goes on the ledger row; ``merged_text`` capped at ``max_conflict_text_bytes`` per result, measured as encoded
    JSON), any other ``ArtifactError`` is ``rejected`` with its message.
12. After the outcome is recorded and published: the artifact id is handed to a background mirror refresher (the lane
    never waits behind the mirror's lock; a failed refresh is kept for ``take_mirror_force``, which the mirror task
    passes to ``sync(force=...)``), and ``lineage(version)`` runs. Neither can fail the tick.

Wiring (Task 9): the scan must never wait for a lane's connection, or fresh intents stop being received while every
lane is busy and then expire. Pass ``tick(..., scan_acquire=...)`` over a dedicated connection, or size the applier's
pool to at least ``limits.apply_concurrency + 1``.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import mimetypes
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from .artifact_collab_store import CollabGrantStore
from .artifact_scope import ArtifactGraphGate
from .artifacts import ArtifactError, ArtifactMergeConflict, ArtifactStore
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
    _walk_dirs,
    agent_owner_key,
    atomic_write_json,
    ensure_dir,
    file_exists_beneath,
    list_beneath,
    read_json_beneath,
    unlink_beneath,
    validate_intent,
)
from .share_bundle import ShareBundleError, check_names

log = logging.getLogger(__name__)

INTENTS_DIR = "intents"
AGENTS_FEED = "agents.json"
RESULTS_DIR = "results"
AGENTS_FEED_MAX_BYTES = 4 * 1024 * 1024
# A commit body is capped at 64 KiB on the wire, but JSON escaping can grow text up to 6x; plan 4/5 kinds are small.
INTENT_MAX_BYTES = 1024 * 1024
INTENTS_MIN_ENTRIES = 4096
# collab-public's own clock may run ahead a little; a submitted_at further in the future is refused (never received).
INTENT_FUTURE_SKEW = 60.0
# Stray regular files in intents/ (stale .tmp-* of an interrupted atomic write, junk) older than this are removed.
STRAY_MAX_AGE = 3600.0
STRAY_SWEEP_BATCH = 10_000      # deletions per tick
STRAY_SCAN_ENTRIES = 50_000     # entries one stray pass looks at (it streams; nothing is held in memory)
# sweep_results deletes at most this many files per call (the results dir is the worker's own tree: no cap, no
# fail-closed listing; an oversized backlog drains over successive calls).
RESULTS_SWEEP_BATCH = 10_000
# Received-but-unfinished tickets are remembered as small records (no payload), at most
# max(RECEIVED_MIN_CAP, agents x RECEIVED_PER_AGENT); past that, new tickets stay on disk un-received (fail closed) and
# are retried once there is room. The base is injectable (``received_min_cap``) so tests can fill the set cheaply.
RECEIVED_MIN_CAP = 4096
RECEIVED_PER_AGENT = 8

REASON_UNREADABLE = "intent is not readable"
REASON_UNKNOWN_KIND = "unknown intent kind"
REASON_KEY_REUSED = "idempotency key was reused for a different request"
REASON_CHANGED = "intent changed after it was received"
REASON_EXPIRED = "intent expired"
REASON_FEED_STALE = "agent feed stale"
REASON_AGENT = "agent not active"
REASON_INTERNAL = "internal error"
REASON_FUTURE = "invalid submitted_at"
REASON_NO_GRANT = "no write grant"
REASON_NO_ARTIFACT = "artifact not found"
REASON_RETRACTED = "artifact or version is retracted"
REASON_NOT_WRITABLE = "not writable by collaboration"
REASON_NAMES = "invalid or clashing file names"
REASON_QUOTA = "daily quota exceeded"
REASON_BASE = "base version does not belong to the artifact"
REASON_NO_HEAD = "artifact has no head version"
REASON_MAX = 300                    # an ArtifactError message on a result is cut to this many characters
COMMIT_OUTCOMES = frozenset({"created", "merged"})
GATE_RELOAD = 30.0                  # the worker-local retraction gate is reloaded at least this often (seconds)

_RESULT_KEYS = ("version_id", "version_number", "head_version_id", "merged_names", "conflicts", "reason")
_STR_KEYS = ("reason", "version_id", "head_version_id", "attempt_id")
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
    after: Callable[[IntentContext, Intent, dict, dict], Awaitable[None]] | None = None


@dataclass(frozen=True)
class _Received:
    """What the applier remembers of a received ticket: identifiers and order only, never the payload. The lane
    re-reads and re-validates the file and checks its digest, kind, actor and key against the ledger row and this
    record, and its ``submitted_at`` against this record, before applying it."""
    ticket: str
    name: str
    kind: str
    actor: Actor
    key: str
    lane: str
    submitted_at: float
    mtime: float
    request_sha256: str

    def order(self) -> tuple:
        return (self.submitted_at, self.mtime, self.ticket)


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
                 clock: Callable[[], float] = time.time, max_per_tick: int = 20,
                 received_min_cap: int | None = None, gate: ArtifactGraphGate | None = None):
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
        self._received: dict[str, _Received] = {}   # received (ledger row written) but not yet finished
        self._received_min_cap = received_min_cap
        self._received_cap = self._min_cap()
        self._feed: dict | None = None
        self._sem = asyncio.Semaphore(limits.apply_concurrency)
        self._counts = self._zero()
        # the worker's retraction gate (shared with the mirror when Task 9 passes one), reloaded every GATE_RELOAD s
        self.gate = gate if gate is not None else ArtifactGraphGate(None)
        self._gate_loaded: float | None = None
        self._acquire = None                               # the latest tick's acquire, for the mirror refresher
        self._refresh_due: set[str] = set()
        self._mirror_force: set[str] = set()
        self._refresher: asyncio.Task | None = None

    # -- registry ---------------------------------------------------------------------------------------------
    def register(self, kind: str, handler: Callable[[IntentContext, Intent], Awaitable[dict]], *,
                 lane_key: Callable[[Intent, dict | None], str] | None = None,
                 replay_outcomes: frozenset[str] = frozenset(),
                 after: Callable[[IntentContext, Intent, dict, dict], Awaitable[None]] | None = None) -> None:
        """Attach the handler of an intent kind (its validator is registered with ``register_intent_kind``).

        A handler must be idempotent on ``(actor, kind, idempotency key)``: a crash between its return and
        ``record_outcome`` leaves the ticket ``received``, and the restarted worker runs the handler again (age-exempt).
        ``commit`` gets that from ``merge_commit``'s own idempotency key.

        Keys of the handler's return value that start with ``_`` are private: they are never recorded or published,
        and are handed to ``after(ctx, intent, out, private)``, which runs only once this call decided the ticket and
        its result is written. Errors in ``after`` are logged (type only) and never change the outcome.
        """
        if not isinstance(kind, str) or not _KIND_NAME.match(kind):
            raise ValueError("invalid intent kind name")
        if kind in self._kinds:
            raise ValueError("intent kind already has a handler")
        if not callable(handler) or any(f is not None and not callable(f) for f in (lane_key, after)):
            raise ValueError("handler, lane_key and after must be callable")
        outcomes = frozenset(replay_outcomes)
        if not all(isinstance(o, str) and _OUTCOME.match(o) for o in outcomes):
            raise ValueError("invalid replay outcome")
        self._kinds[kind] = _Kind(handler, lane_key, outcomes, after)

    def register_commit(self) -> None:
        """Register plan 3's ``commit`` kind (steps 6-12; replays ``created`` / ``merged``)."""
        self.register("commit", self._commit, replay_outcomes=COMMIT_OUTCOMES, after=self._after_commit)

    def take_mirror_force(self) -> set[str]:
        """Artifact ids whose post-commit mirror refresh failed; the mirror task passes them to ``sync(force=...)``."""
        due, self._mirror_force = self._mirror_force, set()
        return due

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

    async def tick(self, acquire, *, scan_acquire=None, wait: float = 1.0) -> dict:
        """Scan the outbox once and start the free lanes; returns the outcomes decided since the previous tick.

        ``acquire()`` is an async context manager yielding a connection of the applier's own pool; each running lane
        holds one (at most ``limits.apply_concurrency`` at once). The scan uses ``scan_acquire()`` (default:
        ``acquire``). **Pool sizing:** the scan must never wait for a lane's connection, or fresh intents stop being
        received while every lane is busy and expire. Either pass ``scan_acquire`` over a dedicated connection, or
        size ``acquire``'s pool to at least ``apply_concurrency + 1``.

        The per-tick scan budget (``max_per_tick``) goes only to files with no known ``received`` row (only they are on
        the age clock). A received ticket is remembered as a small record (capped; see ``RECEIVED_MIN_CAP``) and not
        rescanned: when its lane is free, the lane starts with ALL of its received tickets, oldest first
        (``submitted_at``, then mtime, then ticket), so in steady state a fresh ticket always runs behind its lane's
        older ones. After a restart the received rows are rediscovered in mtime order under the scan budget, so until
        all are rediscovered the order follows mtime. Lanes started here are awaited for at most ``wait`` seconds; a longer one keeps running and its lane is
        skipped by later ticks until it finishes (``drain`` awaits them all).
        """
        self._acquire = acquire
        feed, listing = await asyncio.to_thread(self._load_outbox)
        self._feed = feed
        if listing is None:
            return self._take_counts()
        mtimes = {name: st.st_mtime for name, st in listing
                  if stat.S_ISREG(st.st_mode) and _INTENT_FILE.match(name) and name[:-5] not in self._inflight}
        present = {name[:-5] for name in mtimes}
        self._received = {t: rec for t, rec in self._received.items() if t in present or t in self._inflight}
        fresh = sorted((n for n in mtimes if n[:-5] not in self._received), key=lambda n: (mtimes[n], n))
        names = fresh[:self.max_per_tick]
        raws = await asyncio.to_thread(self._read_intents, names) if names else {}
        async with (scan_acquire or acquire)() as conn:
            for name in names:
                try:
                    job = await self._scan(conn, name, raws.get(name))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad intent never stops the scan; retried next tick
                    log.error("collab intent %s: scan failed (%s)", name[:-5], type(exc).__name__)
                    continue
                if job is not None:
                    rec = self._remember(job, mtimes[name])
                    if rec.lane in self._busy:
                        self._counts["skipped"] += 1
        lanes: dict[str, list[_Received]] = {}
        for ticket, rec in self._received.items():
            if ticket not in self._inflight and rec.lane not in self._busy:
                lanes.setdefault(rec.lane, []).append(rec)
        started = []
        for lane, jobs in lanes.items():
            jobs.sort(key=_Received.order)
            self._inflight.update(j.ticket for j in jobs)
            task = asyncio.create_task(self._run_lane(lane, jobs, acquire))
            self._busy[lane] = task
            started.append(task)
        if started and wait > 0:
            await asyncio.wait(started, timeout=wait)
        return self._take_counts()

    async def drain(self, timeout: float | None = None) -> None:
        """Await every running lane and the mirror refresher (shutdown and tests)."""
        tasks = list(self._busy.values())
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        refresher = self._refresher
        if refresher is not None and not refresher.done():
            await asyncio.wait([refresher], timeout=timeout)

    async def aclose(self, timeout: float = 5.0) -> None:
        """Let running lanes finish for ``timeout`` seconds, then cancel the rest (their intents stay ``received``
        and are picked up again, age-exempt, after a restart)."""
        await self.drain(timeout)
        tasks = list(self._busy.values())
        if self._refresher is not None and not self._refresher.done():
            tasks.append(self._refresher)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    def _min_cap(self) -> int:
        return RECEIVED_MIN_CAP if self._received_min_cap is None else self._received_min_cap

    def _load_outbox(self):
        self._sweep_strays()
        feed = read_json_beneath(self.outbox, [AGENTS_FEED], max_bytes=AGENTS_FEED_MAX_BYTES)
        agents = feed.get("agents") if isinstance(feed, dict) else None
        self._received_cap = max(self._min_cap(), (len(agents) if isinstance(agents, dict) else 0) * RECEIVED_PER_AGENT)
        cap = max(INTENTS_MIN_ENTRIES,
                  (len(agents) if isinstance(agents, dict) else 0) * self.limits.max_pending_intents_per_agent * 2)
        try:
            listing = list_beneath(self.outbox, [INTENTS_DIR], max_entries=cap)
        except BridgeError:
            log.warning("collab intents: outbox intents dir has too many entries; tick skipped")
            return feed, None
        return feed, listing

    def _sweep_strays(self) -> int:
        """Remove stale stray regular files (``.tmp-*`` of an interrupted write, junk) from ``intents/``.

        It runs before, and independently of, the capped listing, so strays alone can never wedge an over-cap dir:
        a bounded streaming pass (``STRAY_SCAN_ENTRIES`` entries, ``STRAY_SWEEP_BATCH`` deletions) over the
        no-follow-opened dir fd. A ticket-named file, a symlink, a directory or anything younger than
        ``STRAY_MAX_AGE`` is never touched; ``unlinkat`` never follows a link.
        """
        dfd = _walk_dirs(self.outbox, [INTENTS_DIR])
        if dfd is None:
            return 0
        cutoff, seen, removed = self.clock() - STRAY_MAX_AGE, 0, 0
        try:
            with os.scandir(dfd) as entries:
                for entry in entries:
                    seen += 1
                    if seen > STRAY_SCAN_ENTRIES or removed >= STRAY_SWEEP_BATCH:
                        break
                    if _INTENT_FILE.match(entry.name):
                        continue
                    try:
                        st = os.stat(entry.name, dir_fd=dfd, follow_symlinks=False)
                        if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                            os.unlink(entry.name, dir_fd=dfd)
                            removed += 1
                    except OSError:
                        continue
        except OSError:
            pass
        finally:
            os.close(dfd)
        return removed

    def _reread(self, name: str) -> tuple[bool, dict | None]:
        """``(present, raw)``: a file unlinked between the existence check and the read counts as absent (forgotten),
        never as a rewritten one; a present file that does not parse is ``(True, None)``."""
        if not file_exists_beneath(self.outbox, [INTENTS_DIR], name):
            return False, None
        raw = read_json_beneath(self.outbox, [INTENTS_DIR, name], max_bytes=INTENT_MAX_BYTES)
        if raw is None and not file_exists_beneath(self.outbox, [INTENTS_DIR], name):
            return False, None
        return True, raw

    def _read_intents(self, names: list[str]) -> dict[str, dict | None]:
        return {n: read_json_beneath(self.outbox, [INTENTS_DIR, n], max_bytes=INTENT_MAX_BYTES) for n in names}

    # -- generic steps 1-5 (scan time) ------------------------------------------------------------------------
    async def _scan(self, conn, name: str, raw: dict | None) -> tuple[str, Intent, str] | None:
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
        if row is not None and not self._row_matches(row, intent):
            await self._decide(conn, intent, row, {"result": "rejected", "reason": REASON_CHANGED})
            return None
        if await self._replayed(conn, name, intent, spec, row):                                       # step 3
            return None
        if row is None:                                                                             # step 4
            if intent.submitted_at > self.clock() + INTENT_FUTURE_SKEW:
                await self._decide(conn, intent, None, {"result": "rejected", "reason": REASON_FUTURE}, charge=False)
                return None
            if self.clock() - intent.submitted_at > INTENT_MAX_AGE:
                await self._decide(conn, intent, None, {"result": "expired", "reason": REASON_EXPIRED}, charge=False)
                return None
        if ticket not in self._received and len(self._received) >= self._received_cap:
            return None                                    # full: stays on disk un-received, retried with room
        if row is None:
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
        return name, intent, lane

    def _remember(self, job: tuple[str, Intent, str], mtime: float) -> _Received:
        name, intent, lane = job
        rec = _Received(ticket=intent.ticket, name=name, kind=intent.kind, actor=intent.actor, key=self._key(intent),
                        lane=lane, submitted_at=intent.submitted_at, mtime=mtime,
                        request_sha256=intent.request_sha256)
        self._received[intent.ticket] = rec
        return rec

    @staticmethod
    def _key(intent: Intent) -> str:
        return intent.idempotency_key or intent.ticket

    @classmethod
    def _row_matches(cls, row: dict, intent: Intent) -> bool:
        """The re-validated file is the request its ``received`` row was written for: same digest (kind, actor,
        artifact, payload), kind and actor (named in the row on their own) and idempotency key (not in the digest; it
        keys the advisory lock and the replay lookup, so a moved key would run the handler twice)."""
        return (row["request_sha256"] == intent.request_sha256 and row["kind"] == intent.kind
                and (row["actor_kind"], row["actor_id"]) == (intent.actor.kind, intent.actor.id)
                and row["idempotency_key"] == cls._key(intent))

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
        await self._decide(conn, intent, row, out, charge=False, name=name, zero_bytes=True)
        return True

    # -- lanes ------------------------------------------------------------------------------------------------
    async def _run_lane(self, lane: str, jobs: list[_Received], acquire) -> None:
        try:
            for index, job in enumerate(jobs):
                async with self._sem:
                    ran = await self._apply(job, acquire)
                self._inflight.discard(job.ticket)
                if not ran:                                # locked elsewhere: keep the lane's order, retry next tick
                    self._counts["skipped"] += len(jobs) - index
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("collab intents: lane failed (%s)", type(exc).__name__)
        finally:
            for job in jobs:
                self._inflight.discard(job.ticket)
            if self._busy.get(lane) is asyncio.current_task():
                del self._busy[lane]

    async def _apply(self, job: _Received, acquire) -> bool:
        async with acquire() as conn:
            locks = [_LOCK_SALT + "lane:" + job.lane,
                     _LOCK_SALT + f"key:{job.actor.kind}:{job.actor.id}:{job.kind}:{job.key}"]
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

    async def _apply_locked(self, conn, job: _Received) -> None:
        name = job.name
        row = await self._ledger(conn, job.ticket)
        if row is None:                                    # the received row vanished: forget it; a later scan
            self._received.pop(job.ticket, None)           # treats the file as fresh (age clock and all)
            return
        if row["outcome"] != "received":
            await self._settle_from_row(name, row)
            return
        present, raw = await asyncio.to_thread(self._reread, name)
        if not present:                                    # withdrawn (even mid-reread) or now a non-file: forgotten
            self._received.pop(job.ticket, None)
            return
        try:
            intent = validate_intent(raw, filename=name, limits=self.limits) if raw is not None else None
        except IntentError:
            intent = None
        spec = self._kinds.get(intent.kind) if intent is not None else None
        # The file must still be the request the row and the record were made for: digest, kind, actor and key
        # against both (the lock just taken is on the record's key), submitted_at against the record (it set the
        # lane order and passed the age check; the row does not store it).
        if (intent is None or spec is None or not self._row_matches(row, intent)
                or (job.kind, job.actor, job.key, job.submitted_at)
                != (intent.kind, intent.actor, self._key(intent), intent.submitted_at)):
            await self._reject_unvalidated(conn, name, row, None, REASON_CHANGED)
            return
        if await self._replayed(conn, name, intent, spec, row):
            return
        agent, reason = self._agent_for(intent)            # the newest feed: revoked while queued is still refused
        if reason is not None:
            await self._finish(conn, name, intent, {"result": "rejected", "reason": reason})
            return
        ctx = IntentContext(actor=intent.actor, agent=agent, conn=conn, store=ArtifactStore(conn, self.rustfs),
                            grants=CollabGrantStore(conn), limits=self.limits, assemble=self.assemble,
                            extras=self.extras)
        private: dict = {}
        try:
            out = await spec.handler(ctx, intent)
            if isinstance(out, dict):
                private = {k: out[k] for k in out if isinstance(k, str) and k.startswith("_")}
                out = {k: v for k, v in out.items() if k not in private}
            _check_outcome(out)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("collab intent %s (%s): handler failed (%s)", intent.ticket, intent.kind, type(exc).__name__)
            out, private = {"result": "rejected", "reason": REASON_INTERNAL}, {}
        if await self._finish(conn, name, intent, out) and spec.after is not None:
            try:
                await spec.after(ctx, intent, out, private)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the outcome is durable and published: a hook never changes it
                log.error("collab intent %s (%s): after-hook failed (%s)", intent.ticket, intent.kind,
                          type(exc).__name__)

    # -- deciding and results ---------------------------------------------------------------------------------
    @staticmethod
    async def _ledger(conn, ticket: str) -> dict | None:
        row = await conn.fetchrow("SELECT * FROM artifact_collab_intents WHERE ticket_id=$1", ticket)
        return dict(row) if row else None

    async def _decide(self, conn, intent: Intent, row: dict | None, out: dict, *, charge: bool = True,
                      name: str | None = None, zero_bytes: bool = False) -> None:
        """Record a validated intent's outcome (writing its ``received`` row first when it has none) and finish it."""
        if row is None:
            existing = await CollabGrantStore(conn).record_received(
                intent.ticket, intent.kind, intent.actor, intent.artifact_id, _base_of(intent), self._key(intent),
                intent.request_sha256, _inline_bytes(intent) if charge else 0)
            if existing is not None and existing["outcome"] != "received":
                await self._settle_from_row(name or intent.ticket + ".json", existing)
                return
        await self._finish(conn, name or intent.ticket + ".json", intent, out, zero_bytes=zero_bytes)

    async def _finish(self, conn, name: str, intent: Intent, out: dict, *, zero_bytes: bool = False) -> bool:
        """Record and publish ``out``, which ``_check_outcome`` accepts (the applier's own outcomes always do); True
        when this call decided the ticket."""
        result = out["result"]
        decided = await CollabGrantStore(conn).record_outcome(
            intent.ticket, result, version_id=out.get("version_id"), attempt_id=out.get("attempt_id"),
            reason=out.get("reason"), bytes=0 if zero_bytes else None)
        if not decided:                                    # decided elsewhere first: that decision is the result
            row = await self._ledger(conn, intent.ticket)
            if row is not None and row["outcome"] != "received":
                await self._settle_from_row(name, row)
            return False
        doc = {"ticket": intent.ticket, "kind": intent.kind,
               "actor": {"kind": intent.actor.kind, "id": intent.actor.id}, "artifact_id": intent.artifact_id,
               "result": result}
        doc.update({k: out[k] for k in _RESULT_KEYS if out.get(k) is not None})
        doc["decided_at"] = self.clock()
        await asyncio.to_thread(self._write_and_consume, name, doc)
        self._done(intent.ticket, intent.kind, intent.actor.kind, intent.actor.id, intent.artifact_id, result)
        return True

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
        self._received.pop(ticket, None)
        self._count(outcome)
        log.info("collab intent %s kind=%s actor=%s:%s artifact=%s outcome=%s",
                 ticket, kind, actor_kind, actor_id, artifact_id, outcome)

    # -- the commit kind (Task 5b) -----------------------------------------------------------------------------
    async def _commit(self, ctx: IntentContext, intent: Intent) -> dict:
        """Steps 6-11. Every refusal before ``merge_commit`` stores nothing (no blob, no scoped row)."""
        agent_id, aid, payload = intent.actor.id, intent.artifact_id, intent.payload
        grant = await ctx.grants.grant_for(aid, agent_id)                                           # step 6
        expires = _epoch(grant["expires_at"]) if grant is not None else None
        if grant is None or grant["permission"] != "write" or expires is None or expires <= self.clock():
            return _rejected(REASON_NO_GRANT)
        store = ctx.store                                                                           # step 7
        artifact = await store.get(aid)
        if artifact is None:
            return _rejected(REASON_NO_ARTIFACT)
        await self._load_gate(store)
        if artifact["lifecycle"] == "retracted" or self.gate.artifact_retracted(aid):
            return _rejected(REASON_RETRACTED)
        base = await store.get_version(payload.base_version_id, aid)
        if base is None:
            return _rejected(REASON_BASE)
        head_id = artifact["current_version_id"]
        head = base if head_id == base["version_id"] else (
            await store.get_version(head_id, aid) if head_id else None)
        if head is None:
            return _rejected(REASON_NO_HEAD)
        if any(v.get("state") == "retracted" or self.gate.version_retracted(aid, v["version_id"]) for v in (base, head)):
            return _rejected(REASON_RETRACTED)
        if artifact["artifact_type"] == "report" or any(
                v.get("report_descriptor") or v.get("source_manifest_blob") for v in (base, head)):
            return _rejected(REASON_NOT_WRITABLE)
        head_files = _files_of(head)
        if not _names_fit(payload.changes, _files_of(base), head_files):
            return _rejected(REASON_NAMES)
        used = await ctx.grants.bytes_last_24h(agent_id, exclude_ticket=intent.ticket)               # step 9
        if used + payload.inline_bytes > ctx.limits.daily_bytes_per_agent:
            return _rejected(REASON_QUOTA)
        changes = []                                                                                # step 10
        for change in payload.changes:
            if change.op == "delete":
                changes.append({"name": change.name, "op": "delete"})
            elif change.text is not None:
                changes.append({"name": change.name, "op": "put",
                                "entry": await self._store_inline(ctx, aid, change.name, change.text, head_files)})
            else:
                entry = await ctx.assemble(agent_owner_key(agent_id), change.upload_id, artifact_id=aid,
                                           name=change.name, charge=True)
                changes.append({"name": change.name, "op": "put", "entry": entry})
        try:                                                                                        # step 11
            out = await store.merge_commit(
                aid, base_version_id=base["version_id"], changes=changes, message=payload.message,
                created_by=f"collab:{agent_id}", collaborator_agent_id=agent_id, idempotency_key=self._key(intent),
                ticket_id=intent.ticket, max_text_bytes=ctx.limits.max_text_merge_bytes)
        except ArtifactMergeConflict as exc:
            return {"result": "conflict", "head_version_id": exc.head_version_id, "attempt_id": exc.attempt_id,
                    "conflicts": _conflicts(exc.conflicts, ctx.limits.max_conflict_text_bytes)}
        except ArtifactError as exc:
            return _rejected(str(exc)[:REASON_MAX] or "commit refused")
        version = out["version"]
        result = {"result": out["result"], "version_id": version["version_id"],
                  "merged_names": [n for n in out.get("merged_names") or [] if isinstance(n, str)],
                  "_version": dict(version)}
        number = version.get("version_number")
        if isinstance(number, int) and not isinstance(number, bool):
            result["version_number"] = number
        return result

    async def _store_inline(self, ctx: IntentContext, aid: str, name: str, text: str, head_files: dict) -> dict:
        """An inline put's entry: the bytes go to RustFS and are registered as a blob scoped to this artifact."""
        data = text.encode("utf-8")
        key, digest = await asyncio.to_thread(self.rustfs.put, data)
        # the scoped row is immutable per (artifact, digest): reuse its media type so the entry always matches it
        prior = await ctx.conn.fetchval(
            "SELECT media_type FROM artifact_blobs WHERE scope=$1 AND blob_sha256=$2", aid, digest)
        media_type = prior or (head_files.get(name) or {}).get("media_type") or mimetypes.guess_type(name)[0] \
            or "text/plain"
        await ctx.store.register_report_blob(aid, storage_key=key, sha256=digest, size_bytes=len(data),
                                             media_type=media_type)
        return {"name": name, "media_type": media_type, "sha256": digest, "size_bytes": len(data), "storage_key": key}

    async def _load_gate(self, store: ArtifactStore) -> None:
        now = self.clock()
        if self._gate_loaded is None or now - self._gate_loaded >= GATE_RELOAD or now < self._gate_loaded:
            self.gate.load_retractions(await store.load_retractions())
            self._gate_loaded = now

    async def _after_commit(self, ctx: IntentContext, intent: Intent, out: dict, private: dict) -> None:
        """Step 12, once the outcome is durable and published: mirror refresh (handed off) and lineage."""
        if out.get("result") not in COMMIT_OUTCOMES:
            return
        self._schedule_refresh(intent.artifact_id)
        version = private.get("_version")
        if self.lineage is None or not isinstance(version, dict):
            return
        try:
            emitted = await asyncio.to_thread(self.lineage, version)
            if inspect.isawaitable(emitted):
                await emitted
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("collab intent %s: lineage emission failed artifact=%s (%s)", intent.ticket,
                        intent.artifact_id, type(exc).__name__)

    def _schedule_refresh(self, artifact_id: str) -> None:
        self._refresh_due.add(artifact_id)
        if self._refresher is None or self._refresher.done():
            self._refresher = asyncio.create_task(self._run_refreshes())

    async def _run_refreshes(self) -> None:
        """Refresh each due artifact on a connection of its own, off the lanes; a failure is kept for the mirror
        task's next ``sync(force=...)``."""
        while self._refresh_due:
            artifact_id = min(self._refresh_due)
            self._refresh_due.discard(artifact_id)
            try:
                async with self._acquire() as conn:
                    await self.mirror.refresh(ArtifactStore(conn, self.rustfs), artifact_id, gate=self.gate)
            except asyncio.CancelledError:
                self._mirror_force.add(artifact_id)
                self._mirror_force.update(self._refresh_due)
                raise
            except Exception as exc:
                log.warning("collab mirror refresh after commit failed artifact=%s (%s)", artifact_id,
                            type(exc).__name__)
                self._mirror_force.add(artifact_id)

    # -- results sweep ----------------------------------------------------------------------------------------
    async def sweep_results(self, max_age: float = 3600) -> int:
        """Delete result files older than ``max_age`` seconds (by mtime), at most ``RESULTS_SWEEP_BATCH`` per call;
        returns how many. The results dir is the worker's own tree, so it streams the directory with no entry cap
        (nothing is held in memory) instead of failing closed: a backlog drains over successive calls."""
        def go() -> int:
            try:
                dfd = os.open(Path(self.mirror.root) / RESULTS_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                return 0
            cutoff, removed = self.clock() - max_age, 0
            try:
                with os.scandir(dfd) as entries:
                    for entry in entries:
                        if removed >= RESULTS_SWEEP_BATCH:
                            break
                        if not _INTENT_FILE.match(entry.name):
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                            if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                                os.unlink(entry.name, dir_fd=dfd)
                                removed += 1
                        except OSError:
                            continue
            finally:
                os.close(dfd)
            return removed
        return await asyncio.to_thread(go)


def _check_outcome(out) -> None:
    """A handler's return value, or ``TypeError``: checked before ``record_outcome`` so ledger and result agree."""
    if not isinstance(out, dict) or not isinstance(out.get("result"), str) or not _OUTCOME.match(out["result"]) \
            or out["result"] == "received":
        raise TypeError("invalid outcome")
    for key in _STR_KEYS:
        if out.get(key) is not None and not isinstance(out[key], str):
            raise TypeError("invalid outcome field")
    number = out.get("version_number")
    if number is not None and (isinstance(number, bool) or not isinstance(number, int)):
        raise TypeError("invalid outcome field")
    names = out.get("merged_names")
    if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
        raise TypeError("invalid outcome field")
    conflicts = out.get("conflicts")
    if conflicts is not None and (not isinstance(conflicts, list) or not all(isinstance(c, dict) for c in conflicts)):
        raise TypeError("invalid outcome field")
    json.dumps(out, ensure_ascii=False, allow_nan=False)   # TypeError/ValueError: not writable as a result


def _rejected(reason: str) -> dict:
    return {"result": "rejected", "reason": reason}


def _files_of(version: dict) -> dict[str, dict]:
    manifest = version.get("output_manifest")
    return {e["name"]: e for e in manifest if isinstance(e, dict) and isinstance(e.get("name"), str)} \
        if isinstance(manifest, list) else {}


def _names_fit(changes, base_files: dict, head_files: dict) -> bool:
    """No put name duplicates or file/directory-clashes with a file that stays in base or head."""
    deleted = {c.name for c in changes if c.op == "delete"}
    puts = {c.name for c in changes if c.op == "put"}
    kept = (set(base_files) | set(head_files)) - deleted - puts
    try:
        check_names(sorted(kept | puts))
    except ShareBundleError:
        return False
    return True


def _conflicts(conflicts, cap: int) -> list[dict]:
    """``[{name, kind, merged_text, truncated}]``; all merged text of one result fits ``cap`` bytes as written (JSON,
    so escaping counts), else that file's text is left out and marked ``truncated``."""
    out, budget = [], cap
    for c in conflicts:
        text = c.merged_text if c.kind == "modify_modify_text" and isinstance(c.merged_text, str) else None
        item = {"name": c.name, "kind": c.kind, "merged_text": None, "truncated": False}
        if text is not None:
            size = len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
            if size <= budget:
                item["merged_text"], budget = text, budget - size
            else:
                item["truncated"] = True
        out.append(item)
    return out


def _base_of(intent: Intent) -> str | None:
    base = getattr(intent.payload, "base_version_id", None)
    return base if isinstance(base, str) else None


def _inline_bytes(intent: Intent) -> int:
    if intent.kind != "commit":
        return 0
    value = getattr(intent.payload, "inline_bytes", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["CollabIntentApplier", "IntentContext", "INTENT_MAX_BYTES", "RESULTS_DIR", "INTENTS_DIR", "AGENTS_FEED"]
