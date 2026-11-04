"""Durable, bounded, host-local build registry (spec 05).

An *additional* guard next to the host ``flock`` model in ``build_coord`` /
``build_cleanup``; never a replacement. The registry records builds, attempts and
state transitions, hands out owner leases with strictly increasing fencing epochs,
and lets cleanup see which workspaces have a live owner. Deleting the database is
always safe: the flock gate and maintenance lock are unchanged and sufficient.

Design rules:

* stdlib ``sqlite3`` only, WAL, file ``<state_dir>/build-registry.db`` (0600),
  ``busy_timeout`` 5 s, every statement parameterized, ``schema_version`` table.
* Bounded: on open, terminal builds older than 30 days are pruned and the table is
  capped at 5000 builds (oldest terminal first; active builds are never pruned).
* Fencing: ``transition``/``finish``/``heartbeat``/``release`` of a leased attempt
  must present the :class:`Lease` it received; a stale epoch or a lease that was
  taken over or released raises :class:`FencedError`.
* Liveness is never assumed dead: ``reconcile`` only fails an owner whose lease
  expired AND whose pid is provably gone; unreadable ``/proc`` means alive.
* Callers in the build path go through :class:`BuildRun`, which swallows every
  registry error so a registry fault can never fail or block a build.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DB_NAME = "build-registry.db"
BUSY_TIMEOUT_MS = 5000
RETENTION_S = 30 * 24 * 3600
MAX_BUILDS = 5000
JOIN_TTL_S = 600.0
DEFAULT_LEASE_TTL_S = 30.0

TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "rejected"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "requested": frozenset({"classified", "rejected", "cancelled", "failed"}),
    "classified": frozenset({"admitted", "rejected", "cancelled", "failed"}),
    "admitted": frozenset({"running", "cancelled", "failed", "timed_out"}),
    "running": frozenset({"publishing", "completed", "failed", "cancelled", "timed_out"}),
    "publishing": frozenset({"completed", "failed", "cancelled", "timed_out"}),
    **{s: frozenset() for s in TERMINAL},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS builds (
  build_id TEXT PRIMARY KEY, request_key TEXT NOT NULL, repo_id TEXT, source_rev TEXT,
  input_digest TEXT, toolchain_digest TEXT, target_triple TEXT, profile TEXT,
  features_digest TEXT, trust_domain TEXT NOT NULL, cmd_kind TEXT, owner_session TEXT,
  mission_id TEXT, host TEXT, state TEXT NOT NULL, created_ts REAL NOT NULL,
  updated_ts REAL NOT NULL, terminal_reason TEXT, cancel_requested_ts REAL,
  cancel_reason TEXT);
CREATE INDEX IF NOT EXISTS builds_key ON builds (request_key, state);
CREATE INDEX IF NOT EXISTS builds_updated ON builds (updated_ts);
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY,
  build_id TEXT NOT NULL REFERENCES builds (build_id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL, state TEXT NOT NULL, pid INTEGER, pid_starttime INTEGER,
  joined INTEGER NOT NULL DEFAULT 0, started_ts REAL NOT NULL, ended_ts REAL,
  exit_code INTEGER);
CREATE INDEX IF NOT EXISTS attempts_build ON attempts (build_id);
CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL REFERENCES attempts (attempt_id) ON DELETE CASCADE,
  from_state TEXT, to_state TEXT NOT NULL, reason TEXT, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS transitions_attempt ON transitions (attempt_id);
CREATE TABLE IF NOT EXISTS leases (
  scope TEXT PRIMARY KEY, owner_attempt_id TEXT NOT NULL, epoch INTEGER NOT NULL,
  ttl_s REAL NOT NULL, heartbeat_ts REAL NOT NULL, acquired_ts REAL NOT NULL,
  released_ts REAL);
CREATE TABLE IF NOT EXISTS scope_epochs (scope TEXT PRIMARY KEY, last_epoch INTEGER NOT NULL);
"""


class RegistryError(Exception):
    """Registry unreadable/unusable (corrupt, locked beyond timeout, newer schema)."""


class IllegalTransition(RegistryError):
    """Transition not in the lifecycle table, or the attempt is already terminal."""


class FencedError(RegistryError):
    """The presented lease is stale, taken over, released, or required but missing."""


class LeaseHeld(RegistryError):
    """Another owner holds an unexpired lease on the scope."""


@dataclass(frozen=True)
class Lease:
    scope: str
    attempt_id: str
    epoch: int
    ttl_s: float


@dataclass(frozen=True)
class Registration:
    build_id: str
    attempt_id: str
    attempt_no: int
    request_key: str
    joined: bool
    dedup_excluded: bool


def normalize_scope(scope: str | os.PathLike[str]) -> str:
    """Absolute paths -> realpath without trailing slash; other strings are named scopes."""
    text = os.fspath(scope)
    if os.path.isabs(text):
        real = os.path.realpath(text)
        return real if real == "/" else real.rstrip("/")
    return text


def compute_request_key(*, repo_id, input_digest, toolchain_digest, profile,
                        features_digest, cmd_kind, trust_domain) -> tuple[str, bool]:
    """sha256 of canonical JSON; returns ``(key, dedup_excluded)``.

    Rule: a build whose ``input_digest`` is None (dirty tree without a deterministic
    snapshot digest) is EXCLUDED from dedup: a random nonce makes its key unique.
    ``trust_domain`` is part of the key, so dedup never crosses trust domains.
    """
    material: dict[str, Any] = {
        "repo_id": repo_id, "input_digest": input_digest,
        "toolchain_digest": toolchain_digest, "profile": profile,
        "features_digest": features_digest, "cmd_kind": cmd_kind,
        "trust_domain": trust_domain}
    excluded = input_digest is None
    if excluded:
        material["nonce"] = uuid.uuid4().hex
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest(), excluded


# --- process liveness ---------------------------------------------------------

def proc_starttime(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    try:
        data = (proc_root / str(pid) / "stat").read_text()
        return int(data[data.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def pid_alive(pid: int | None, starttime: int | None = None,
              proc_root: Path = Path("/proc")) -> bool:
    """False only when the owner is provably gone; every doubt means alive."""
    if pid is None or pid <= 0:
        return True
    if not (proc_root / str(os.getpid()) / "stat").is_file():
        return True  # /proc unreadable: fail safe
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM etc.: exists
    try:
        data = (proc_root / str(pid) / "stat").read_text()
        rest = data[data.rindex(")") + 2:].split()
    except (OSError, ValueError):
        return True
    if rest and rest[0] == "Z":
        return False  # finished, only awaiting reap
    if starttime is not None and len(rest) > 19:
        try:
            if int(rest[19]) != starttime:
                return False  # pid reused by a different process
        except ValueError:
            return True
    return True


# --- registry -----------------------------------------------------------------

def db_path(state_dir: str | os.PathLike[str]) -> Path:
    return Path(state_dir) / DB_NAME


def _connect(path: Path, create: bool) -> sqlite3.Connection:
    if create:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
    uri = f"file:{path}?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000,
                               isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error as exc:
        raise RegistryError(f"cannot open registry: {exc}") from exc


class Registry:
    """Handle on one registry file; each operation uses its own short connection."""

    def __init__(self, state_dir: str | os.PathLike[str], *, now: Callable[[], float] = time.time):
        self.path = db_path(state_dir)
        self._now = now
        try:
            Path(state_dir).mkdir(parents=True, exist_ok=True)
            conn = _connect(self.path, create=True)
            try:
                self._init(conn)
            finally:
                conn.close()
        except RegistryError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise RegistryError(f"cannot initialise registry: {exc}") from exc

    # -- plumbing
    def _init(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] > SCHEMA_VERSION:
                raise RegistryError(f"registry schema {row[0]} is newer than {SCHEMA_VERSION}")
            self._prune(conn)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _prune(self, conn: sqlite3.Connection) -> None:
        cutoff = self._now() - RETENTION_S
        marks = ",".join("?" for _ in TERMINAL)
        term = tuple(sorted(TERMINAL))
        conn.execute(f"DELETE FROM builds WHERE state IN ({marks}) AND updated_ts < ?",
                     (*term, cutoff))
        (count,) = conn.execute("SELECT COUNT(*) FROM builds").fetchone()
        if count > MAX_BUILDS:
            conn.execute(
                f"DELETE FROM builds WHERE build_id IN (SELECT build_id FROM builds "
                f"WHERE state IN ({marks}) ORDER BY updated_ts ASC LIMIT ?)",
                (*term, count - MAX_BUILDS))
        conn.execute("DELETE FROM leases WHERE released_ts IS NOT NULL AND released_ts < ?",
                     (cutoff,))
        # scope_epochs rows are kept on purpose: epoch monotonicity outranks a few bytes.

    def _tx(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        try:
            conn = _connect(self.path, create=False)
        except RegistryError:
            raise
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                out = fn(conn)
                conn.execute("COMMIT")
                return out
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as exc:
            raise RegistryError(f"registry operation failed: {exc}") from exc
        finally:
            conn.close()

    # -- registration
    def register(self, *, repo_id: str | None = None, source_rev: str | None = None,
                 input_digest: str | None = None, toolchain_digest: str | None = None,
                 target_triple: str | None = None, profile: str | None = None,
                 features_digest: str | None = None, trust_domain: str = "local",
                 cmd_kind: str | None = None, owner_session: str | None = None,
                 mission_id: str | None = None, host: str | None = None,
                 pid: int | None = None, build_id: str | None = None) -> Registration:
        """Create a build (or join an active same-key one) plus a fresh attempt.

        A joined attempt is independently correlated: new attempt id, same build id,
        ``joined=True``. It is only advisory single-flight; the caller still runs.
        """
        key, excluded = compute_request_key(
            repo_id=repo_id, input_digest=input_digest, toolchain_digest=toolchain_digest,
            profile=profile, features_digest=features_digest, cmd_kind=cmd_kind,
            trust_domain=trust_domain)
        pid = os.getpid() if pid is None else pid
        starttime = proc_starttime(pid)

        def work(conn: sqlite3.Connection) -> Registration:
            now = self._now()
            existing = None
            if not excluded:
                marks = ",".join("?" for _ in TERMINAL)
                existing = conn.execute(
                    f"SELECT build_id FROM builds WHERE request_key=? AND trust_domain=? "
                    f"AND state NOT IN ({marks}) AND updated_ts > ? "
                    f"ORDER BY created_ts LIMIT 1",
                    (key, trust_domain, *sorted(TERMINAL), now - JOIN_TTL_S)).fetchone()
            if existing:
                bid, joined = existing[0], True
                (no,) = conn.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts "
                                     "WHERE build_id=?", (bid,)).fetchone()
            else:
                bid, joined, no = build_id or str(uuid.uuid4()), False, 1
                conn.execute(
                    "INSERT INTO builds (build_id, request_key, repo_id, source_rev, "
                    "input_digest, toolchain_digest, target_triple, profile, features_digest, "
                    "trust_domain, cmd_kind, owner_session, mission_id, host, state, "
                    "created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bid, key, repo_id, source_rev, input_digest, toolchain_digest,
                     target_triple, profile, features_digest, trust_domain, cmd_kind,
                     owner_session, mission_id, host, "requested", now, now))
            aid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO attempts (attempt_id, build_id, attempt_no, state, pid, "
                "pid_starttime, joined, started_ts) VALUES (?,?,?,?,?,?,?,?)",
                (aid, bid, no, "requested", pid, starttime, int(joined), now))
            conn.execute("INSERT INTO transitions (attempt_id, from_state, to_state, reason, ts) "
                         "VALUES (?,?,?,?,?)",
                         (aid, None, "requested", "joined" if joined else "registered", now))
            return Registration(bid, aid, no, key, joined, excluded)

        return self._tx(work)

    # -- leases
    def acquire_lease(self, scope: str | os.PathLike[str], attempt_id: str,
                      ttl_s: float = DEFAULT_LEASE_TTL_S) -> Lease:
        norm = normalize_scope(scope)
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")

        def work(conn: sqlite3.Connection) -> Lease:
            now = self._now()
            if conn.execute("SELECT 1 FROM attempts WHERE attempt_id=?",
                            (attempt_id,)).fetchone() is None:
                raise RegistryError("unknown attempt")
            row = conn.execute("SELECT owner_attempt_id, ttl_s, heartbeat_ts, released_ts "
                               "FROM leases WHERE scope=?", (norm,)).fetchone()
            if row and row[3] is None and row[2] + row[1] >= now:
                raise LeaseHeld(f"scope {norm} is leased by attempt {row[0]}")
            prev = conn.execute("SELECT last_epoch FROM scope_epochs WHERE scope=?",
                                (norm,)).fetchone()
            epoch = (prev[0] if prev else 0) + 1
            conn.execute("INSERT INTO scope_epochs (scope, last_epoch) VALUES (?,?) "
                         "ON CONFLICT(scope) DO UPDATE SET last_epoch=excluded.last_epoch",
                         (norm, epoch))
            conn.execute(
                "INSERT INTO leases (scope, owner_attempt_id, epoch, ttl_s, heartbeat_ts, "
                "acquired_ts, released_ts) VALUES (?,?,?,?,?,?,NULL) "
                "ON CONFLICT(scope) DO UPDATE SET owner_attempt_id=excluded.owner_attempt_id, "
                "epoch=excluded.epoch, ttl_s=excluded.ttl_s, heartbeat_ts=excluded.heartbeat_ts, "
                "acquired_ts=excluded.acquired_ts, released_ts=NULL",
                (norm, attempt_id, epoch, float(ttl_s), now, now))
            return Lease(norm, attempt_id, epoch, float(ttl_s))

        return self._tx(work)

    @staticmethod
    def _check_fence(conn: sqlite3.Connection, lease: Lease) -> None:
        row = conn.execute("SELECT owner_attempt_id, epoch, released_ts FROM leases "
                           "WHERE scope=?", (lease.scope,)).fetchone()
        if row is None or row[0] != lease.attempt_id or row[1] != lease.epoch:
            raise FencedError(f"lease for {lease.scope} epoch {lease.epoch} was taken over")
        if row[2] is not None:
            raise FencedError(f"lease for {lease.scope} epoch {lease.epoch} was released")

    def heartbeat(self, lease: Lease) -> None:
        def work(conn: sqlite3.Connection) -> None:
            self._check_fence(conn, lease)
            now = self._now()
            conn.execute("UPDATE leases SET heartbeat_ts=? WHERE scope=?", (now, lease.scope))
            conn.execute("UPDATE builds SET updated_ts=? WHERE build_id=(SELECT build_id "
                         "FROM attempts WHERE attempt_id=?)", (now, lease.attempt_id))
        self._tx(work)

    def release(self, lease: Lease) -> None:
        def work(conn: sqlite3.Connection) -> None:
            self._check_fence(conn, lease)
            conn.execute("UPDATE leases SET released_ts=? WHERE scope=?",
                         (self._now(), lease.scope))
        self._tx(work)

    # -- transitions
    def _holds_active_lease(self, conn: sqlite3.Connection, attempt_id: str) -> bool:
        return conn.execute("SELECT 1 FROM leases WHERE owner_attempt_id=? AND "
                            "released_ts IS NULL", (attempt_id,)).fetchone() is not None

    def _apply(self, conn: sqlite3.Connection, attempt_id: str, to_state: str,
               reason: str, exit_code: int | None) -> None:
        row = conn.execute("SELECT state, attempt_no, build_id, exit_code FROM attempts "
                           "WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise RegistryError("unknown attempt")
        current, no, bid, recorded = row
        if to_state not in TRANSITIONS.get(current, frozenset()):
            raise IllegalTransition(f"{current} -> {to_state} is not allowed")
        effective = exit_code if exit_code is not None else recorded
        if to_state == "completed" and effective != 0:
            raise IllegalTransition("completed requires a recorded exit_code of 0")
        now = self._now()
        terminal = to_state in TERMINAL
        conn.execute("UPDATE attempts SET state=?, ended_ts=COALESCE(?, ended_ts), "
                     "exit_code=COALESCE(?, exit_code) WHERE attempt_id=?",
                     (to_state, now if terminal else None, exit_code, attempt_id))
        conn.execute("INSERT INTO transitions (attempt_id, from_state, to_state, reason, ts) "
                     "VALUES (?,?,?,?,?)", (attempt_id, current, to_state, reason, now))
        if no == 1:
            conn.execute("UPDATE builds SET state=?, updated_ts=?, "
                         "terminal_reason=CASE WHEN ? THEN ? ELSE terminal_reason END "
                         "WHERE build_id=?", (to_state, now, int(terminal), reason, bid))

    def transition(self, attempt_id: str, to_state: str, reason: str, *,
                   lease: Lease | None = None, exit_code: int | None = None) -> None:
        def work(conn: sqlite3.Connection) -> None:
            if lease is not None:
                if lease.attempt_id != attempt_id:
                    raise FencedError("lease belongs to a different attempt")
                self._check_fence(conn, lease)
            elif self._holds_active_lease(conn, attempt_id):
                raise FencedError("attempt holds a lease; present it to mutate state")
            self._apply(conn, attempt_id, to_state, reason, exit_code)
        self._tx(work)

    def finish(self, attempt_id: str, to_state: str, reason: str, *,
               exit_code: int | None = None, lease: Lease | None = None) -> None:
        """Terminal transition and lease release in one fenced transaction."""
        if to_state not in TERMINAL:
            raise IllegalTransition(f"{to_state} is not terminal")

        def work(conn: sqlite3.Connection) -> None:
            if lease is not None:
                if lease.attempt_id != attempt_id:
                    raise FencedError("lease belongs to a different attempt")
                self._check_fence(conn, lease)
            elif self._holds_active_lease(conn, attempt_id):
                raise FencedError("attempt holds a lease; present it to mutate state")
            self._apply(conn, attempt_id, to_state, reason, exit_code)
            if lease is not None:
                conn.execute("UPDATE leases SET released_ts=? WHERE scope=?",
                             (self._now(), lease.scope))
        self._tx(work)

    # -- cancellation flag (minimal; the owning wrapper acts on it)
    def request_cancel(self, build_id: str, reason: str) -> bool:
        """Only sets a flag. Never signals anything; returns False for unknown/terminal."""
        def work(conn: sqlite3.Connection) -> bool:
            marks = ",".join("?" for _ in TERMINAL)
            cur = conn.execute(
                f"UPDATE builds SET cancel_requested_ts=?, cancel_reason=? "
                f"WHERE build_id=? AND state NOT IN ({marks})",
                (self._now(), reason[:200], build_id, *sorted(TERMINAL)))
            return cur.rowcount > 0
        return self._tx(work)

    def cancel_requested(self, build_id: str) -> bool:
        def work(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT cancel_requested_ts FROM builds WHERE build_id=?",
                               (build_id,)).fetchone()
            return bool(row and row[0] is not None)
        return self._tx(work)

    # -- reads
    def get_build(self, build_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM builds WHERE build_id=?", (build_id,))

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,))

    def get_lease(self, scope: str | os.PathLike[str]) -> dict[str, Any] | None:
        return self._one("SELECT * FROM leases WHERE scope=?", (normalize_scope(scope),))

    def transitions(self, attempt_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM transitions WHERE attempt_id=? ORDER BY id",
                         (attempt_id,))

    def _all(self, sql: str, args: tuple) -> list[dict[str, Any]]:
        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            cur = conn.execute(sql, args)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
        return self._tx(work)

    def _one(self, sql: str, args: tuple) -> dict[str, Any] | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # -- reconcile
    def reconcile(self, *, proc_root: Path = Path("/proc")) -> list[str]:
        """Release expired leases whose owner is provably dead; fail their attempts.

        Returns the attempt ids marked ``failed(owner_vanished)``. A live owner, an
        unexpired lease, or an unreadable ``/proc`` never changes anything.
        """
        now = self._now()
        rows = self._all(
            "SELECT l.scope, l.owner_attempt_id AS aid, a.pid, a.pid_starttime, a.state "
            "FROM leases l JOIN attempts a ON a.attempt_id=l.owner_attempt_id "
            "WHERE l.released_ts IS NULL AND l.heartbeat_ts + l.ttl_s < ?", (now,))
        failed: list[str] = []
        for r in rows:
            if pid_alive(r["pid"], r["pid_starttime"], proc_root):
                continue

            def work(conn: sqlite3.Connection, r=r) -> bool:
                cur = conn.execute(
                    "UPDATE leases SET released_ts=? WHERE scope=? AND owner_attempt_id=? "
                    "AND released_ts IS NULL AND heartbeat_ts + ttl_s < ?",
                    (self._now(), r["scope"], r["aid"], now))
                if cur.rowcount == 0:
                    return False  # heartbeat or takeover raced us
                if r["state"] not in TERMINAL:
                    self._apply(conn, r["aid"], "failed", "owner_vanished", None)
                return True
            if self._tx(work):
                failed.append(r["aid"])
        return failed


def active_scopes(state_dir: str | os.PathLike[str], *, now: float | None = None) -> list[str]:
    """Scopes with an unexpired, unreleased lease. Raises RegistryError if unreadable.

    Read-only on the success path: never creates, migrates or prunes the file.
    """
    path = db_path(state_dir)
    when = time.time() if now is None else now
    try:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True,
                               timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    except sqlite3.Error as exc:
        raise RegistryError(f"cannot open registry: {exc}") from exc
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        rows = conn.execute("SELECT scope FROM leases WHERE released_ts IS NULL "
                            "AND heartbeat_ts + ttl_s >= ?", (when,)).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error as exc:
        raise RegistryError(f"cannot read registry: {exc}") from exc
    finally:
        conn.close()


# --- build-path wrapper (never raises) ---------------------------------------------

def _git(args: list[str], cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                           timeout=3, check=False)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _identity(cwd: str) -> tuple[str | None, str | None, str | None]:
    """(repo_id, source_rev, input_digest). Digest only for a CLEAN tree (HEAD sha)."""
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    if not top:
        return None, None, None
    repo_id = hashlib.sha256(os.path.realpath(top).encode()).hexdigest()[:16]
    head = _git(["rev-parse", "HEAD"], cwd)
    dirty = _git(["status", "--porcelain"], cwd)
    clean_digest = head if head and dirty == "" else None
    return repo_id, head, clean_digest


class BuildRun:
    """Registry bookkeeping for one recorded build. Every method swallows errors."""

    def __init__(self) -> None:
        self.registry: Registry | None = None
        self.reg: Registration | None = None
        self.lease: Lease | None = None
        self.unavailable = False
        self.joined_unleased = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel_sent = False
        self._forwarded = False
        self._ttl = DEFAULT_LEASE_TTL_S

    @classmethod
    def open(cls, state_dir: str | os.PathLike[str], *, cwd: str, cmd_kind: str,
             host: str, cmd: list[str], toolchain_digest: str | None = None,
             build_id: str | None = None, ttl_s: float | None = None) -> BuildRun:
        run = cls()
        run._ttl = DEFAULT_LEASE_TTL_S if ttl_s is None else ttl_s
        try:
            run.registry = Registry(state_dir)
            repo_id, rev, digest = _identity(cwd)
            profile = "release" if "--release" in cmd or "-r" in cmd[1:] else "dev"
            run.reg = run.registry.register(
                repo_id=repo_id, source_rev=rev, input_digest=digest,
                toolchain_digest=toolchain_digest, profile=profile, cmd_kind=cmd_kind,
                trust_domain=f"uid:{os.getuid()}", host=host, build_id=build_id)
            aid = run.reg.attempt_id
            run.registry.transition(aid, "classified", "classified")
            run.registry.transition(aid, "admitted", "gate_slot_held")
            try:
                run.lease = run.registry.acquire_lease(cwd, aid, run._ttl)
            except LeaseHeld:
                run.joined_unleased = True  # another owner is active; run unfenced
        except Exception:  # noqa: BLE001 - registry failure must never fail a build
            run._fail()
        return run

    def _fail(self) -> None:
        self.unavailable = True

    def ids(self) -> dict[str, Any]:
        if self.reg is None:
            return {}
        return {"attempt_id": self.reg.attempt_id, "request_key": self.reg.request_key,
                "joined": self.reg.joined}

    @property
    def build_id(self) -> str | None:
        return self.reg.build_id if self.reg else None

    def on_spawn(self, child_pid: int) -> None:
        if self.unavailable or self.reg is None or self.registry is None:
            return
        try:
            self.registry.transition(self.reg.attempt_id, "running", "child_started",
                                     lease=self.lease)
        except Exception:  # noqa: BLE001
            self._fail()
            return
        self._thread = threading.Thread(target=self._beat, args=(child_pid,), daemon=True)
        self._thread.start()

    def note_forwarded_signal(self) -> None:
        self._forwarded = True

    def _beat(self, child_pid: int) -> None:
        interval = max(0.05, self._ttl / 3)
        while not self._stop.wait(interval):
            try:
                if self.lease is not None and self.registry is not None:
                    self.registry.heartbeat(self.lease)
                if (self.registry is not None and self.reg is not None
                        and not self._cancel_sent
                        and self.registry.cancel_requested(self.reg.build_id)):
                    self._cancel_sent = True
                    os.kill(child_pid, signal.SIGTERM)  # our own child only
            except FencedError:
                return  # taken over / released: stop heartbeating, keep the build running
            except Exception:  # noqa: BLE001
                self._fail()

    def on_exit(self, exit_code: int | None, *, signaled: bool = False,
                spawn_failed: bool = False) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.reg is None or self.registry is None:
            return
        if spawn_failed or exit_code is None:
            state, reason = "failed", "spawn_failed"
        elif exit_code == 0:
            state, reason = "completed", "exit_0"
        elif self._cancel_sent or (signaled and self._forwarded):
            state, reason = "cancelled", "signal" if self._forwarded else "cancel_requested"
        else:
            state, reason = "failed", f"exit_{exit_code}" if not signaled else "killed_by_signal"
        try:
            self.registry.finish(self.reg.attempt_id, state, reason,
                                 exit_code=exit_code, lease=self.lease)
        except Exception:  # noqa: BLE001
            self._fail()
