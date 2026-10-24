"""Reviewer pool for the review gate: one process, a growing and shrinking set of workers.

``herdr-eng review-gate pool`` runs one ``ReviewWorker`` per slot, each on its own
thread under its own worker id (``<worker_id>-<n>``). A supervisor reads the
control plane's review queue and adds slots while jobs this pool's model may
claim are waiting, and retires slots that have sat idle. The pool is the
worker's trust model unchanged: it adds no credential, reads no key contents
itself, and logs only class names and counts.

Sizing is deliberately conservative (see ``wanted_slots``): idle slots already
claim queued jobs, so a non-empty queue means every slot is busy or backing off.
A slot is retired only between claim cycles, so scaling never abandons a claim.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from dataclasses import dataclass

import yaml

from herdr_engineering.control_plane.redact import redact_text

from .worker import (
    _ID,
    CONTROL_PLANE_TIMEOUT,
    MAX_BACKOFF_SECONDS,
    ReviewWorker,
    WorkerAuthError,
    WorkerConfig,
    WorkerConfigError,
    _BadResponse,
    _int,
    _json_object,
    _keys,
    _mapping,
    _urllib_http,
    parse_worker_config,
    read_key,
)

MAX_POOL_WORKERS = 64
_POOL_KEYS = frozenset({"min_workers", "max_workers", "idle_seconds", "interval_seconds"})
_QUEUE_PATH = "/api/v1/review-gate/queue"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolConfig:
    worker: WorkerConfig  # worker_id is the slot-id prefix
    min_workers: int = 1
    max_workers: int = 4
    idle_seconds: int = 120
    interval_seconds: int = 10


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with a repeated key (last-wins hides typos)."""


def _construct_unique(loader, node, deep=False):
    seen = set()
    for key_node, _value in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise WorkerConfigError("pool config: duplicate key")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique)


def load_pool_config(path: str) -> PoolConfig:
    """Read and validate the pool YAML (worker YAML plus a ``pool`` mapping)."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.load(fh, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except WorkerConfigError:
        raise
    except (OSError, UnicodeDecodeError):
        raise WorkerConfigError("pool config: cannot read the file") from None
    except (yaml.YAMLError, TypeError):
        raise WorkerConfigError("pool config: not valid YAML") from None
    raw = dict(_mapping(raw, "pool config"))
    pool = _mapping(raw.pop("pool", {}), "pool config: pool")
    _keys(pool, _POOL_KEYS, set(), "pool config: pool")
    worker = parse_worker_config(raw)
    where = "pool config: pool"
    lo = _int(pool, "min_workers", where, 1, 0, MAX_POOL_WORKERS)
    hi = _int(pool, "max_workers", where, 4, 1, MAX_POOL_WORKERS)
    if lo > hi:
        raise WorkerConfigError("pool config: pool: min_workers must not exceed max_workers")
    if not _ID.fullmatch(f"{worker.worker_id}-{hi}"):
        raise WorkerConfigError("pool config: worker_id is too long for a slot id suffix")
    return PoolConfig(
        worker=worker,
        min_workers=lo,
        max_workers=hi,
        idle_seconds=_int(pool, "idle_seconds", where, 120, 10, 86400),
        interval_seconds=_int(pool, "interval_seconds", where, 10, 1, 3600),
    )


# --------------------------------------------------------------------- sizing
def eligible_depth(queue: dict, model: str) -> int:
    """Queued jobs this pool's model may claim (mirrors ``ReviewQueue.claim``).

    Rows that are not the expected shape are skipped; a body that is not a
    ``{"jobs": [...]}`` object raises ``ValueError`` so the caller keeps its size.
    """
    if not isinstance(queue, dict) or not isinstance(queue.get("jobs"), list):
        raise ValueError("malformed queue")
    reviewer = redact_text(model)
    count = 0
    for j in queue["jobs"]:
        if not isinstance(j, dict) or j.get("state") != "queued":
            continue
        author = j.get("author_model")
        if author is not None and not isinstance(author, str):
            continue
        if author and author == reviewer:
            continue
        count += 1
    return count


def wanted_slots(current: int, eligible_depth: int, idle: int, cfg: PoolConfig) -> int:
    """The slot count to move to: grow by the eligible depth, or retire one idle slot.

    ``idle`` counts slots idle longer than ``idle_seconds``; at most one is
    retired per call so a brief lull does not collapse the pool.
    """
    if eligible_depth > 0:
        want = current + min(eligible_depth, cfg.max_workers - current)
    elif idle > 0:
        want = current - 1
    else:
        want = current
    return max(cfg.min_workers, min(cfg.max_workers, want))


# ----------------------------------------------------------------- supervisor
class _Slot:
    """One worker thread plus the bookkeeping that makes retiring it safe.

    ``lock`` makes "mark retiring" and ``before_cycle`` mutually exclusive: a
    slot is retired only while outside a claim cycle, and a retiring slot's
    ``before_cycle`` answers False, so it can never claim after being marked.
    """

    def __init__(self, slot_id: str, now: float):
        self.id = slot_id
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.in_cycle = False
        self.retiring = False
        self.idle_since = now
        self.thread: threading.Thread | None = None

    def before_cycle(self) -> bool:
        with self.lock:
            if self.retiring:
                return False
            self.in_cycle = True
            return True

    def after_cycle_factory(self, clock):
        def after_cycle(claimed: bool) -> None:
            with self.lock:
                self.in_cycle = False
                if claimed:
                    self.idle_since = clock()

        return after_cycle

    def try_retire(self) -> bool:
        with self.lock:
            if self.in_cycle or self.retiring:
                return False
            self.retiring = True
        self.stop.set()
        return True


class ReviewPool:
    """Runs ``ReviewWorker`` slots and sizes them from the review queue's depth.

    ``http`` is the same transport seam the worker has (``http(method, url,
    headers, body, timeout)``) and is shared with the slot workers;
    ``worker_factory(config, http=...)`` builds a slot's worker.
    """

    def __init__(self, cfg: PoolConfig, *, http=None, clock=time.monotonic, worker_factory=None):
        self._cfg = cfg
        self._http = http or _urllib_http
        self._clock = clock
        self._factory = worker_factory or ReviewWorker
        self._slots: list[_Slot] = []
        self._slots_lock = threading.Lock()
        self._stop = threading.Event()
        self._fatal: Exception | None = None
        self._queue_failures = 0

    # ------------------------------------------------------------------ slots
    def _alive(self) -> list[_Slot]:
        with self._slots_lock:
            self._slots = [s for s in self._slots if s.thread is not None and s.thread.is_alive()]
            return list(self._slots)

    def _active(self) -> list[_Slot]:
        return [s for s in self._alive() if not s.retiring]

    def _start_slot(self) -> bool:
        alive = self._alive()
        if len(alive) >= self._cfg.max_workers:
            return False  # retiring slots are still on their way out
        used = {s.id for s in alive}
        n = next(
            i
            for i in range(1, self._cfg.max_workers + 1)
            if f"{self._cfg.worker.worker_id}-{i}" not in used
        )
        slot = _Slot(f"{self._cfg.worker.worker_id}-{n}", self._clock())
        config = dataclasses.replace(self._cfg.worker, worker_id=slot.id)
        worker = self._factory(config, http=self._http)
        slot.thread = threading.Thread(
            target=self._run_slot, args=(slot, worker), name=f"review-gate-{slot.id}", daemon=True
        )
        with self._slots_lock:  # started inside the lock: _alive() drops unstarted threads
            slot.thread.start()
            self._slots.append(slot)
        _log.info("review gate pool slot %s started", slot.id)
        return True

    def _run_slot(self, slot: _Slot, worker) -> None:
        try:
            worker.run(
                slot.stop,
                before_cycle=slot.before_cycle,
                after_cycle=slot.after_cycle_factory(self._clock),
            )
        except (WorkerAuthError, WorkerConfigError) as exc:
            self._fatal = self._fatal or exc
            self._stop.set()
        except Exception as exc:  # noqa: BLE001 - the slot is replaced on the next evaluation
            _log.error("review gate pool slot %s error: %s", slot.id, type(exc).__name__)
        else:
            _log.info("review gate pool slot %s stopped", slot.id)

    def _idle_slots(self) -> list[_Slot]:
        now = self._clock()
        idle = []
        for s in self._active():
            with s.lock:
                if not s.in_cycle and now - s.idle_since > self._cfg.idle_seconds:
                    idle.append(s)
        return sorted(idle, key=lambda s: s.idle_since)

    # ------------------------------------------------------------------ queue
    def _fetch_queue(self) -> dict:
        """The decoded queue body. Raises ``WorkerAuthError`` on 401/403; anything else
        that goes wrong raises an ordinary exception the caller treats as a failed read."""
        headers = {"X-API-Key": read_key(self._cfg.worker.api_key_file)}
        status, _hdrs, raw = self._http(
            "GET",
            self._cfg.worker.control_plane_url + _QUEUE_PATH,
            headers,
            None,
            CONTROL_PLANE_TIMEOUT,
        )
        if status in (401, 403):
            raise WorkerAuthError("the control plane refused the reviewer key")
        if status != 200:
            raise _BadResponse()
        return _json_object(raw)

    # --------------------------------------------------------------- evaluate
    def evaluate(self) -> None:
        """One scaling decision: top up to ``min_workers``, then follow the queue."""
        active = self._active()
        try:
            depth = eligible_depth(self._fetch_queue(), self._cfg.worker.model.model)
        except (WorkerAuthError, WorkerConfigError):
            raise
        except Exception as exc:  # noqa: BLE001 - keep the size; class name only
            self._queue_failures = min(self._queue_failures + 1, 32)
            _log.warning("review gate pool queue read failed: %s", type(exc).__name__)
            for _ in range(self._cfg.min_workers - len(active)):
                self._start_slot()
            return
        self._queue_failures = 0
        idle = self._idle_slots()
        want = wanted_slots(len(active), depth, len(idle), self._cfg)
        for _ in range(want - len(active)):  # a fixed count: a crashing slot cannot spin this
            if not self._start_slot():
                break
        if want < len(active):
            for slot in idle:
                if slot.try_retire():
                    _log.info("review gate pool slot %s retiring", slot.id)
                    break
        _log.info("review gate pool size %d eligible %d", len(self._active()), depth)

    def _wait_seconds(self) -> float:
        wait = self._cfg.interval_seconds
        for _ in range(self._queue_failures):
            wait = min(wait * 2, MAX_BACKOFF_SECONDS)
        return wait

    # -------------------------------------------------------------------- run
    def run(self, stop: threading.Event) -> None:
        """Supervise until ``stop`` is set; raises a slot's auth or config error.

        Slots still holding a job when the pool stops abandon it, as
        ``ReviewWorker.run`` documents.
        """
        self._stop = stop
        try:
            for _ in range(self._cfg.min_workers):
                self._start_slot()
            while not stop.is_set():
                self.evaluate()
                stop.wait(self._wait_seconds())
        finally:
            stop.set()
            for slot in list(self._slots):
                slot.stop.set()
            for slot in list(self._slots):
                if slot.thread is not None:
                    slot.thread.join()
        if self._fatal is not None:
            raise self._fatal
