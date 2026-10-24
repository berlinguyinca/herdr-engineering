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

import logging
from dataclasses import dataclass

import yaml

from herdr_engineering.control_plane.redact import redact_text

from .worker import (
    _ID,
    WorkerConfig,
    WorkerConfigError,
    _int,
    _keys,
    _mapping,
    parse_worker_config,
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
