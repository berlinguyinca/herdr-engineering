"""Per-run resource capture: the ``resource_run`` ingest event, its table and the per-project profile read.

One event per ended agent session (``entity_type="resource_run"``, ``event_type="resource_run.ended"``,
``entity_id`` = ``session_id``). The model holds NO free text: every value is a bounded token, a UTC timestamp, a
bounded number, a closed enum or a boolean, and unknown fields are rejected, so nothing secret can ride along. The
host-side collector (``herdr_engineering.resource_runs_collector``) validates with this same function before it posts.

Storage is the ``resource_runs`` table (migration 0052) when the control plane has a DSN, keyed by project and agent
kind, one row per ``(host, session_id)`` so a replayed event is a no-op. Without a DSN the same contract runs in
memory (bounded), like the other optional stores. ``profiles(project)`` returns count, median, p95 and max per metric
over the most recent ``PROFILE_WINDOW`` runs of each agent kind. There is no learning here: estimation, headroom and
use belong to the Slurm project.
"""
from __future__ import annotations

import heapq
import logging
import math
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)

ENTITY_TYPE = "resource_run"
EVENT_TYPE = "resource_run.ended"
#: How the session ended. ``exited``: it ended and the observer cannot know its exit status (a host collector that
#: watches processes it did not start); ``completed`` / ``failed``: exit status known (zero / non-zero); ``oom`` and
#: ``disk_full``: killed or failed on a resource; ``timeout`` / ``killed``: stopped from outside.
EXIT_REASONS = ("exited", "completed", "failed", "oom", "disk_full", "timeout", "killed")
METRICS = ("peak_memory_bytes", "cpu_seconds", "disk_growth_bytes")
FIELDS = frozenset({"project", "agent_kind", "session_id", "host", "started_at", "ended_at", *METRICS,
                    "exit_reason", "hit_limit"})
#: Upper bounds that no real run reaches (1 PiB, about 31 CPU-years, one year of wall time). They keep a corrupt
#: counter from poisoning a percentile and keep every value inside a Postgres bigint / double.
MAX_BYTES = 2 ** 50
MAX_CPU_SECONDS = 1e9
MAX_RUN = timedelta(days=366)
MAX_FUTURE = timedelta(minutes=5)
PROFILE_WINDOW = 1000
MAX_AGENT_KINDS = 32
MAX_MEMORY_RUNS = 20_000

_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?")
_AGENT_KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}")


def _bad(message: str) -> ValueError:
    return ValueError(f"resource_run {message}")


def _token(name: str, value, pattern: re.Pattern, limit: int) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit or not pattern.fullmatch(value):
        raise _bad(f"{name} must be a short identifier of at most {limit} characters")
    return value


def valid_project(value) -> str:
    """The project key, as both the ingest event and the profile query take it."""
    return _token("project", value, _PROJECT, 128)


#: The ONE accepted timestamp form: extended calendar date and time in UTC, ``Z`` or ``+00:00``, optional fraction.
#: ``datetime.fromisoformat`` alone also takes week dates, ordinal dates, the basic format and a space separator, which
#: Postgres then refuses (a 503 for a bad value); those are a 400 here.
_TIMESTAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?(Z|\+00:00)", re.ASCII)
MIN_YEAR, MAX_YEAR = 2000, 2999


def _utc(name: str, value) -> datetime:
    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    parsed = None
    if match and MIN_YEAR <= int(match.group(1)) <= MAX_YEAR:
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except ValueError:
            parsed = None   # e.g. 2026-02-30, 24:00:00, 23:59:60
    if parsed is None:
        raise _bad(f"{name} must be a UTC timestamp like 2026-11-04T10:00:00Z (years {MIN_YEAR}-{MAX_YEAR})")
    return parsed


def canonical_time(parsed: datetime) -> str:
    """The stored form: always ``+00:00``, microseconds only when present."""
    return parsed.astimezone(UTC).isoformat()


def _bytes(name: str, value, nullable: bool = False):
    if value is None and nullable:
        return None
    if type(value) is not int or not 0 <= value <= MAX_BYTES:
        raise _bad(f"{name} must be a non-negative integer of at most {MAX_BYTES}")
    return value


def _seconds(name: str, value) -> float:
    # The range is compared on the value as sent (an int far beyond float range would overflow ``float()`` and
    # ``math.isfinite``); NaN fails every comparison, so it is out of range too.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= MAX_CPU_SECONDS:
        raise _bad(f"{name} must be a finite non-negative number of at most {MAX_CPU_SECONDS:.0f}")
    return float(value)


def validate_resource_run(entity_id, event_type, payload, *, now: datetime | None = None) -> dict:
    """Strict allow-list for one ended run; returns a clean copy (``disk_growth_bytes`` may be null: unmeasured)."""
    if event_type != EVENT_TYPE:
        raise _bad(f"event_type must be {EVENT_TYPE}")
    if not isinstance(payload, dict):
        raise _bad("payload must be an object")
    unknown = sorted(set(payload) - FIELDS)
    if unknown:
        raise _bad(f"unknown field: {unknown[0]}")
    missing = sorted(FIELDS - set(payload))
    if missing:
        raise _bad(f"requires {missing[0]}")
    session_id = _token("session_id", payload["session_id"], _SESSION, 128)
    if session_id != entity_id:
        raise _bad("session_id must equal entity_id")
    started, ended = _utc("started_at", payload["started_at"]), _utc("ended_at", payload["ended_at"])
    if ended < started:
        raise _bad("ended_at must not be before started_at")
    if ended - started > MAX_RUN:
        raise _bad("a run may last at most 366 days")
    if ended > (now or datetime.now(UTC)) + MAX_FUTURE:
        raise _bad("ended_at is in the future")
    if payload["exit_reason"] not in EXIT_REASONS:
        raise _bad(f"exit_reason must be one of {', '.join(EXIT_REASONS)}")
    if type(payload["hit_limit"]) is not bool:
        raise _bad("hit_limit must be a boolean")
    return {
        "project": valid_project(payload["project"]),
        "agent_kind": _token("agent_kind", payload["agent_kind"], _AGENT_KIND, 32),
        "session_id": session_id,
        "host": _token("host", payload["host"], _HOST, 64),
        "started_at": canonical_time(started),
        "ended_at": canonical_time(ended),
        "peak_memory_bytes": _bytes("peak_memory_bytes", payload["peak_memory_bytes"]),
        "cpu_seconds": _seconds("cpu_seconds", payload["cpu_seconds"]),
        "disk_growth_bytes": _bytes("disk_growth_bytes", payload["disk_growth_bytes"], nullable=True),
        "exit_reason": payload["exit_reason"],
        "hit_limit": payload["hit_limit"],
    }


# --------------------------------------------------------------------------- profile math

def percentile(sorted_values: list, fraction: float) -> float:
    """Linear interpolation between closest ranks (Postgres ``percentile_cont``) over an ascending list."""
    if not sorted_values:
        raise ValueError("percentile of no values")
    rank = fraction * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (rank - low)


def summarize(values) -> dict:
    """count / median / p95 / max of the known (non-null) values; median, p95 and max are null when there are none."""
    known = sorted(v for v in values if v is not None)
    if not known:
        return {"count": 0, "median": None, "p95": None, "max": None}
    return {"count": len(known), "median": percentile(known, 0.5), "p95": percentile(known, 0.95), "max": known[-1]}


def build_profiles(project: str, rows) -> dict:
    """``rows``: (agent_kind, peak_memory_bytes, cpu_seconds, disk_growth_bytes), already windowed per kind."""
    kinds: dict[str, list] = {}
    for row in rows:
        kinds.setdefault(row[0], []).append(row[1:])
    profiles = []
    for kind in sorted(kinds):
        runs = kinds[kind]
        profiles.append({"agent_kind": kind, "count": len(runs),
                         "metrics": {name: summarize(run[i] for run in runs) for i, name in enumerate(METRICS)}})
    return {"project": project, "window": PROFILE_WINDOW, "profiles": profiles}


# --------------------------------------------------------------------------- stores

class ResourceRunsUnavailable(RuntimeError):
    """The table could not be read or written; nothing was acknowledged (HTTP 503, the collector retries)."""


class MemoryResourceRuns:
    """No-DSN store with the table's contract: one row per (host, session_id), at most ``MAX_MEMORY_RUNS`` rows."""

    def __init__(self, max_runs: int = MAX_MEMORY_RUNS):
        self._rows: dict[tuple[str, str], dict] = {}
        self._ended: dict[tuple[str, str], datetime] = {}
        self._heap: list[tuple[datetime, tuple[str, str]]] = []   # oldest ended_at first, for eviction
        self._max = max_runs
        self._lock = threading.Lock()

    def record(self, run: dict) -> bool:
        key = (run["host"], run["session_id"])
        ended = _utc("ended_at", run["ended_at"])
        with self._lock:
            if key in self._rows:
                return False
            self._rows[key], self._ended[key] = dict(run), ended
            heapq.heappush(self._heap, (ended, key))
            while len(self._rows) > self._max:
                _, oldest = heapq.heappop(self._heap)
                del self._rows[oldest], self._ended[oldest]
        return True

    def profiles(self, project: str) -> dict:
        """The same window as ``_WINDOW``: the newest runs per kind (ties by session id), the first kinds by name."""
        with self._lock:
            runs = [(self._ended[k], r) for k, r in self._rows.items() if r["project"] == project]
        kinds = set(sorted({r["agent_kind"] for _, r in runs})[:MAX_AGENT_KINDS])
        runs.sort(key=lambda item: item[1]["session_id"])
        runs.sort(key=lambda item: item[0], reverse=True)
        per_kind: dict[str, int] = {}
        rows = []
        for _, r in runs:
            kind = r["agent_kind"]
            if kind not in kinds or per_kind.get(kind, 0) >= PROFILE_WINDOW:
                continue
            per_kind[kind] = per_kind.get(kind, 0) + 1
            rows.append((kind, *(r[m] for m in METRICS)))
        return build_profiles(project, rows)


_INSERT = ("INSERT INTO resource_runs (host, session_id, project, agent_kind, started_at, ended_at, "
           "peak_memory_bytes, cpu_seconds, disk_growth_bytes, exit_reason, hit_limit) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (host, session_id) DO NOTHING")
# The newest PROFILE_WINDOW runs of each agent kind of one project, for at most MAX_AGENT_KINDS kinds.
_WINDOW = ("SELECT agent_kind, peak_memory_bytes, cpu_seconds, disk_growth_bytes FROM ("
           "  SELECT agent_kind, peak_memory_bytes, cpu_seconds, disk_growth_bytes,"
           "         row_number() OVER (PARTITION BY agent_kind ORDER BY ended_at DESC, session_id) AS rn,"
           "         dense_rank() OVER (ORDER BY agent_kind) AS kind_rank"
           "  FROM resource_runs WHERE project = %s) AS windowed "
           "WHERE rn <= %s AND kind_rank <= %s")


class PostgresResourceRuns:
    """``resource_runs`` (migration 0052). Parameterized statements only, short timeouts; the DSN is never logged."""

    def __init__(self, dsn: str, connect: Callable | None = None):
        self._dsn, self._connect = dsn, connect

    def _run(self, sql: str, args: tuple, fetch: bool):
        try:
            if self._connect is not None:
                conn = self._connect()
            else:
                import psycopg2
                conn = psycopg2.connect(self._dsn, connect_timeout=3,
                                        options="-c statement_timeout=3000 -c lock_timeout=3000")
        except Exception as exc:  # noqa: BLE001 - the driver's message can carry the DSN; only the type is kept
            raise ResourceRunsUnavailable(f"resource_runs unavailable ({type(exc).__name__})") from None
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                out = cur.fetchall() if fetch else cur.rowcount
            conn.commit()
            return out
        except Exception as exc:  # noqa: BLE001
            raise ResourceRunsUnavailable(f"resource_runs unavailable ({type(exc).__name__})") from None
        finally:
            conn.close()

    def record(self, run: dict) -> bool:
        return self._run(_INSERT, (run["host"], run["session_id"], run["project"], run["agent_kind"],
                                   run["started_at"], run["ended_at"], run["peak_memory_bytes"], run["cpu_seconds"],
                                   run["disk_growth_bytes"], run["exit_reason"], run["hit_limit"]), False) == 1

    def profiles(self, project: str) -> dict:
        rows = self._run(_WINDOW, (project, PROFILE_WINDOW, MAX_AGENT_KINDS), True)
        return build_profiles(project, [(r[0], r[1], float(r[2]), r[3]) for r in rows])


def build_store(dsn: str | None):
    return PostgresResourceRuns(dsn) if dsn else MemoryResourceRuns()
