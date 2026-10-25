"""Persistent GitHub catalog store: token tag, revisions, tombstones (Task G1).

The GitHub monitor keeps its repository/issue catalog here so a restart serves
what was stored instead of re-reading GitHub. Two implementations share one
interface:

* ``GitHubStore``: Postgres on ``control_plane.pg_dsn`` (migration
  ``0006_github_catalog.sql``), driven by asyncpg on a dedicated event loop
  (``bridge._LoopRunner``) and serialised by a lock, the same pattern as
  ``DurableEventSink``.
* ``MemoryGitHubStore``: no ``pg_dsn`` configured (collab-only mode, tests);
  the catalog lives only as long as the process, as it did before G1. Its
  revisions start at a per-process base (``_memory_base``), never at 0.

Rules (spec "Builder-check rulings" 1 and 2):

* The catalog is tagged with the credential fingerprint (``identity``, the
  SHA-256 of the token; the token itself never reaches this module) and the
  viewer login. ``load()`` with a different tag drops every row, tombstones
  included, and returns ``None``; a store whose tag no longer matches cannot
  write (``GitHubStoreIdentityMismatch``) or read. ``viewer=None`` means "not
  known yet" (before the first GraphQL call) and matches on the fingerprint
  alone; a later store constructed with the real viewer purges on mismatch.
* One revision per family, increasing on every *visible* change and never
  reset (not even by a purge). Each row keeps the revision of its last change.
  Re-upserting identical data changes nothing, so ETags stay stable across the
  overlapping re-reads of an incremental pass.
* Issue bodies (``put_detail``/``detail``, migration ``0007``) are kept only
  for issues whose detail was opened, with the ``updated_at`` they were fetched
  at. They never bump the revision, and they go with their issue, repository,
  owner and identity like every other row.
* Removals are kept as tombstones for ``TOMBSTONE_RETENTION``. ``pruned_through``
  is the newest revision whose tombstone was discarded (or the revision of the
  last purge); ``changes_since(since)`` with ``since`` below it, or above the
  current revision, answers ``resync`` with the full list.
"""
from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

FAMILY = "github"
TOMBSTONE_RETENTION = timedelta(days=7)
# Session advisory-lock key wrapping one sync pass (b"herdrgh" as an integer).
ADVISORY_LOCK_KEY = int.from_bytes(b"herdrgh", "big")
# Bounds on one store call (seconds): a hung database surfaces as GitHubStoreUnavailable
# (asyncio.TimeoutError is an OSError) instead of blocking the monitor, which may hold its lock.
STORE_CONNECT_TIMEOUT = 10.0
STORE_COMMAND_TIMEOUT = 30.0


class GitHubStoreUnavailable(RuntimeError):
    """The durable store cannot be used (tables not migrated yet, database down)."""


class GitHubStoreNotMigrated(GitHubStoreUnavailable):
    """The catalog tables do not exist yet (code rolled before the migration): nothing is stored."""


class GitHubStoreRetired(GitHubStoreUnavailable):
    """The monitor replaced this store (token rotation, viewer bind): it may no longer read,
    claim, purge or write, so a pass still holding it cannot touch the new catalog."""


class GitHubStoreIdentityMismatch(RuntimeError):
    """The stored catalog belongs to another credential/viewer; call ``load()`` first."""


@dataclass
class Snapshot:
    identity: str
    viewer: str | None
    revision: int
    repositories: dict[str, dict] = field(default_factory=dict)
    issues: dict[str, dict[str, dict]] = field(default_factory=dict)
    watermarks: dict[str, str] = field(default_factory=dict)


def _normal(row: dict) -> dict:
    """The row as it round-trips through jsonb, so equality means "unchanged"."""
    return json.loads(json.dumps(row))


def _owner(row: dict) -> str:
    name = row.get("name_with_owner")
    if isinstance(name, str) and "/" in name:
        return name.split("/", 1)[0].lower()
    parts = str(row["repository_id"]).split("/")
    return parts[-2].lower() if len(parts) >= 2 else ""


def _owners(values: Iterable[str]) -> set[str]:
    return {value.lower() for value in values}


def _matches(meta: dict, identity: str, viewer: str | None) -> bool:
    return meta["identity"] == identity and (
        viewer is None or meta["viewer"] is None or meta["viewer"] == viewer)


def _item(kind: str, repository_id: str, key: str, rev: int, data: dict | None = None) -> dict:
    item = {"kind": kind, "key": key, "repository_id": repository_id, "rev": rev}
    if data is not None:
        item["data"] = data
    return item


def _order(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (i["rev"], i["kind"] != "repository", i["repository_id"], i["key"]))


def _delta(revision: int, pruned_through: int, since: int, rows: list[dict], tombstones: list[dict]) -> dict:
    """Shape ``changes_since``: ``rows``/``tombstones`` are every stored item."""
    since = max(0, int(since))
    if since < pruned_through or since > revision:
        return {"revision": revision, "resync": True, "upserted": _order(rows), "removed": []}
    return {"revision": revision, "resync": False,
            "upserted": _order([r for r in rows if r["rev"] > since]),
            "removed": _order([t for t in tombstones if t["rev"] > since])}


# ---------------------------------------------------------------- issue search (read-only)

SEARCH_LIMIT = 20
# a search reads the issues of at most this many matching repositories (exact names and numbered
# matches first, then shorter names), so a one-letter prefix never sorts the whole catalog
MAX_SEARCH_REPOS = 25
# what a `#` query may be: `binv`, `binview-4`, `acme/binv`, `acme/binview#4` (lower-cased, `#` optional)
_QUERY_CHARS = re.compile(r"[a-z0-9_./#-]{0,160}\Z")
_QUERY_LONG = re.compile(r"([a-z0-9-]{1,39})/([a-z0-9_.-]{0,100})(?:#([0-9]{0,10}))?\Z")
_QUERY_SHORT = re.compile(r"([a-z0-9_.-]{1,100}?)-([0-9]{1,10})\Z")


def parse_issue_query(query) -> dict | None:
    """A typed `#` query as ``{owner, repo, exact, number, short}``; ``None`` when it cannot name an issue.

    ``owner/repo#n`` names one owner (``exact``: the repository name is complete once ``#`` is typed);
    otherwise ``repo`` is a name prefix and ``short`` is the ``(name, number prefix)`` reading of
    ``name-n``. Bounded: at most 160 characters, no backtracking-heavy patterns.
    """
    q = str(query or "").strip().lower()
    q = q[1:] if q.startswith("#") else q
    if not _QUERY_CHARS.match(q):
        return None
    m = _QUERY_LONG.match(q)
    if m:
        return {"owner": m[1], "repo": m[2], "exact": m[3] is not None, "number": m[3] or "", "short": None}
    if "/" in q or "#" in q:
        return None
    m = _QUERY_SHORT.match(q)
    return {"owner": None, "repo": q, "exact": False, "number": "", "short": (m[1], m[2]) if m else None}


def _split_name(name_with_owner: str) -> tuple[str, str]:
    owner, _, repo = str(name_with_owner or "").partition("/")
    return owner, repo


def _repo_routes(parsed: dict, owner: str, repo: str) -> tuple[bool, str | None, bool]:
    """How a repository matches: (any of its issues match, number prefix its issues must match, name exact)."""
    owner, repo = owner.lower(), repo.lower()
    if parsed["owner"] is not None:
        if owner != parsed["owner"] or not (repo == parsed["repo"] if parsed["exact"] else repo.startswith(parsed["repo"])):
            return False, None, False
        return not parsed["number"], parsed["number"] or None, repo == parsed["repo"]
    short = parsed["short"]
    numbered = short[1] if short and repo == short[0] else None
    return repo.startswith(parsed["repo"]), numbered, repo == parsed["repo"]


def _shared_names(repos: Iterable[tuple[str, str]]) -> set[str]:
    """Repository names (lower-case) two or more owners share: `#name-n` would be ambiguous."""
    seen: dict[str, set[str]] = {}
    for owner, repo in repos:
        seen.setdefault(repo.lower(), set()).add(owner.lower())
    return {name for name, owners in seen.items() if len(owners) > 1}


def ref_insert(owner: str, repo: str, number: int, shared: set[str]) -> str:
    """The shortest unambiguous text for one issue: `#repo-n`, or `owner/repo#n` when the name is shared."""
    return f"{owner}/{repo}#{number}" if repo.lower() in shared else f"#{repo}-{number}"


def _pick_repos(parsed: dict, repos: Iterable[dict], public_only: bool) -> list[tuple[dict, tuple]]:
    """The matching repositories (``(data, routes)``), best first, at most ``MAX_SEARCH_REPOS``."""
    picked = []
    for data in repos:
        if public_only and data.get("private"):
            continue
        owner, name = _split_name(data.get("name_with_owner"))
        routes = _repo_routes(parsed, owner, name)
        if routes[0] or routes[1] is not None:
            rank = 0 if routes[1] is not None else 1 if routes[2] else 2
            picked.append(((rank, len(name), name.lower(), str(data.get("repository_id"))), data, routes))
    picked.sort(key=lambda p: p[0])
    return [(data, routes) for _, data, routes in picked[:MAX_SEARCH_REPOS]]


def _search_key(parsed: dict, routes: tuple, issue: dict) -> tuple | None:
    any_issue, prefix, name_exact = routes
    number = str(issue.get("number"))
    numbered = prefix is not None and number.startswith(prefix)
    if not (any_issue or numbered):
        return None
    rank = 0 if numbered and number == prefix else 1 if numbered else 2 if name_exact else 3
    return (rank, str(issue.get("state") or "").upper() != "OPEN")


def _ref_row(repo_data: dict, issue: dict, shared: set[str]) -> dict:
    owner, repo = _split_name(repo_data.get("name_with_owner"))
    url = issue.get("url") if isinstance(issue.get("url"), str) else None
    return {"kind": "pr" if url and "/pull/" in url else "issue", "owner": owner, "repo": repo,
            "number": issue["number"], "title": issue.get("title") or "",
            "state": str(issue.get("state") or "").lower() or None, "url": url,
            "repository_id": repo_data.get("repository_id"), "private": bool(repo_data.get("private")),
            "insert": ref_insert(owner, repo, issue["number"], shared)}


def search_rows(repos: Iterable[dict], issues_of: Callable[[str], Iterable[dict]], query,
                owners: set[str] | None, limit: int = SEARCH_LIMIT, public_only: bool = False) -> list[dict]:
    """The in-memory search both the memory store and the monitor use; the Postgres store orders the same way.

    ``owners`` limits the owners searched (``None``: every stored owner). Exact numbers first, then
    number prefixes, exact names, name prefixes; open before closed; newest first. Only the issues of
    ``MAX_SEARCH_REPOS`` matching repositories are read; ``public_only`` skips private repositories.
    """
    parsed = parse_issue_query(query)
    limit = max(1, min(int(limit), SEARCH_LIMIT))
    if parsed is None:
        return []
    scope = None if owners is None else _owners(owners)
    inside = [r for r in repos if scope is None or _owner(r) in scope]
    shared = _shared_names(_split_name(r.get("name_with_owner")) for r in inside)
    found = []
    for data, routes in _pick_repos(parsed, inside, public_only):
        for issue in issues_of(data["repository_id"]):
            key = _search_key(parsed, routes, issue)
            if key is not None:
                found.append((key, issue.get("updated_at") or "", data, issue))
    found.sort(key=lambda f: (f[0], _desc(f[1]), f[2]["repository_id"], f[3]["number"]))
    return [_ref_row(data, issue, shared) for _, _, data, issue in found[:limit]]


def _desc(text: str) -> tuple:
    """Sort key putting the newest ISO timestamp first (strings: no parsing)."""
    return tuple(-ord(c) for c in text) + (1,)


# ---------------------------------------------------------------- in memory


_MEMORY_SYNC_LOCK = threading.Lock()
_MEMORY_REVISIONS = {"lock": threading.Lock(), "high": 0}


def _note_memory_revision(revision: int) -> int:
    with _MEMORY_REVISIONS["lock"]:
        _MEMORY_REVISIONS["high"] = max(_MEMORY_REVISIONS["high"], revision)
    return revision


def _memory_base() -> int:
    """Where a new in-memory catalog's revisions start.

    The wall clock in microseconds (exact in a browser's JSON numbers), above
    every revision this process has produced: a catalog that did not survive a
    restart starts above the revisions its clients hold, and ``pruned_through``
    starts there too, so every ``since`` from before answers ``resync``
    instead of a silent, wrong delta. Postgres keeps its revisions and needs none.
    """
    with _MEMORY_REVISIONS["lock"]:
        base = max(time.time_ns() // 1000, _MEMORY_REVISIONS["high"] + 1)
        _MEMORY_REVISIONS["high"] = base
        return base


class _MemoryState:
    def __init__(self):
        self.lock = threading.RLock()
        self.base = _memory_base()
        self.meta: dict | None = None
        self.repos: dict[str, dict] = {}       # rid -> {owner, data, watermark, rev}
        self.issues: dict[tuple, dict] = {}    # (rid, gid) -> {data, rev}
        self.tombstones: dict[tuple, dict] = {}  # (kind, rid, key) -> {rev, removed_at}
        self.details: dict[tuple, dict] = {}     # (rid, gid) -> {number, updated_at, data}


class MemoryGitHubStore:
    """Process-local store with the same interface (no ``pg_dsn`` configured)."""

    persisted = False

    def __init__(self, *, identity: str, viewer: str | None = None,
                 now: Callable[[], float] | None = None, shared: dict | None = None):
        self._identity, self._viewer = identity, viewer
        self._now = now or time.time
        self._state: _MemoryState = (shared if shared is not None else {}).setdefault("state", _MemoryState())
        self._retired = False

    def __repr__(self):
        return f"MemoryGitHubStore(identity={self._identity[:12]}..., viewer={self._viewer!r})"

    def retire(self) -> None:
        """Refuse every later operation (an operation in flight finishes first)."""
        with self._state.lock:
            self._retired = True

    def close(self) -> None:
        self.retire()

    # -- identity

    def _claim(self) -> dict:
        state = self._state
        if self._retired:
            raise GitHubStoreRetired("this GitHub catalog store was replaced")
        if state.meta is None:
            state.meta = {"identity": self._identity, "viewer": self._viewer,
                          "revision": state.base, "pruned_through": state.base}
        elif not _matches(state.meta, self._identity, self._viewer):
            raise GitHubStoreIdentityMismatch("stored GitHub catalog belongs to another credential")
        elif state.meta["viewer"] is None and self._viewer is not None:
            state.meta["viewer"] = self._viewer
        self._prune()
        return state.meta

    def _prune(self) -> None:
        state = self._state
        cutoff = self._now() - TOMBSTONE_RETENTION.total_seconds()
        expired = [key for key, tomb in state.tombstones.items() if tomb["removed_at"] < cutoff]
        for key in expired:
            state.meta["pruned_through"] = max(state.meta["pruned_through"], state.tombstones.pop(key)["rev"])

    def _bump(self) -> int:
        self._state.meta["revision"] += 1
        return _note_memory_revision(self._state.meta["revision"])

    def _tombstone(self, kind: str, rid: str, key: str, rev: int) -> None:
        self._state.tombstones[(kind, rid, key)] = {"rev": rev, "removed_at": self._now()}

    def _remove_repos(self, rids: list[str], rev: int) -> None:
        state = self._state
        for rid in rids:
            for key in [k for k in state.issues if k[0] == rid]:
                del state.issues[key]
                self._tombstone("issue", rid, key[1], rev)
            del state.repos[rid]
            self._tombstone("repository", rid, rid, rev)
            for key in [k for k in state.details if k[0] == rid]:
                del state.details[key]

    # -- interface

    def load(self, *, rows: bool = True) -> Snapshot | None:
        """The stored catalog; ``rows=False`` claims/purges the same way but returns only the revision."""
        state = self._state
        with state.lock:
            if self._retired:
                raise GitHubStoreRetired("this GitHub catalog store was replaced")
            if state.meta is None:
                self._claim()
                return None
            if not _matches(state.meta, self._identity, self._viewer):
                state.repos, state.issues, state.tombstones, state.details = {}, {}, {}, {}
                revision = _note_memory_revision(state.meta["revision"] + 1)
                state.meta = {"identity": self._identity, "viewer": self._viewer,
                              "revision": revision, "pruned_through": revision}
                return None
            meta = self._claim()
            if not rows:
                return Snapshot(identity=meta["identity"], viewer=meta["viewer"], revision=meta["revision"])
            issues: dict[str, dict[str, dict]] = {}
            for (rid, gid), row in state.issues.items():
                issues.setdefault(rid, {})[gid] = _normal(row["data"])
            return Snapshot(identity=meta["identity"], viewer=meta["viewer"], revision=meta["revision"],
                            repositories={rid: _normal(r["data"]) for rid, r in state.repos.items()},
                            issues=issues,
                            watermarks={rid: r["watermark"] for rid, r in state.repos.items()
                                        if r["watermark"] is not None})

    def _lazy_revision(self) -> Callable[[], int]:
        """One revision for one operation, bumped on its first visible change only."""
        taken: list[int] = []

        def rev() -> int:
            if not taken:
                taken.append(self._bump())
            return taken[0]
        return rev

    def _put_repos(self, incoming: dict[str, dict], listed: set[str] | None, rev: Callable[[], int]) -> None:
        state = self._state
        changed = [rid for rid, data in incoming.items()
                   if rid not in state.repos or state.repos[rid]["data"] != data]
        gone = [rid for rid, r in state.repos.items()
                if rid not in incoming and (listed is None or r["owner"] in listed)]
        if not changed and not gone:
            return
        for rid in changed:
            previous = state.repos.get(rid)
            state.repos[rid] = {"owner": _owner(incoming[rid]), "data": incoming[rid], "rev": rev(),
                                "watermark": previous["watermark"] if previous else None}
            state.tombstones.pop(("repository", rid, rid), None)
        if gone:
            self._remove_repos(gone, rev())

    def _put_issues(self, repo_id: str, incoming: dict[str, dict], rev: Callable[[], int]) -> None:
        state = self._state
        for gid, data in incoming.items():
            if (repo_id, gid) not in state.issues or state.issues[(repo_id, gid)]["data"] != data:
                state.issues[(repo_id, gid)] = {"data": data, "rev": rev()}
                state.tombstones.pop(("issue", repo_id, gid), None)

    def _drop_issues(self, repo_id: str, wanted: list[str], rev: Callable[[], int]) -> None:
        state = self._state
        for gid in wanted:
            state.details.pop((repo_id, gid), None)
        for gid in [gid for gid in wanted if (repo_id, gid) in state.issues]:
            del state.issues[(repo_id, gid)]
            self._tombstone("issue", repo_id, gid, rev())

    def upsert_repositories(self, rows: Iterable[dict], scope: set[str] | None) -> None:
        state = self._state
        with state.lock:
            self._claim()
            incoming = {row["repository_id"]: _normal(row) for row in rows}
            self._put_repos(incoming, None if scope is None else _owners(scope), self._lazy_revision())

    def upsert_issues(self, repo_id: str, rows: Iterable[dict], watermark: str | None) -> None:
        state = self._state
        with state.lock:
            self._claim()
            if repo_id not in state.repos:
                raise KeyError(repo_id)
            self._put_issues(repo_id, {row["github_id"]: _normal(row) for row in rows}, self._lazy_revision())
            state.repos[repo_id]["watermark"] = watermark

    def remove_issues(self, repo_id: str, github_ids: Iterable[str]) -> None:
        state = self._state
        with state.lock:
            self._claim()
            self._drop_issues(repo_id, list(dict.fromkeys(github_ids)), self._lazy_revision())

    def apply_repository(self, row: dict, issues: Iterable[dict], removed: Iterable[str],
                         watermark: str | None) -> None:
        """One repository's completed sync, all or nothing: its row (with the ``synced``
        probe), changed issues, removals and watermark, under one revision."""
        state = self._state
        rid = row["repository_id"]
        incoming = {issue["github_id"]: _normal(issue) for issue in issues}
        wanted = list(dict.fromkeys(removed))
        with state.lock:
            self._claim()
            saved = (dict(state.meta), {k: dict(v) for k, v in state.repos.items()}, dict(state.issues),
                     dict(state.tombstones), dict(state.details))
            try:
                rev = self._lazy_revision()
                self._put_repos({rid: _normal(row)}, set(), rev)
                self._put_issues(rid, incoming, rev)
                if wanted:
                    self._drop_issues(rid, wanted, rev)
                state.repos[rid]["watermark"] = watermark
            except BaseException:
                state.meta, state.repos, state.issues, state.tombstones, state.details = saved
                raise

    def purge(self) -> None:
        """Drop this credential's whole catalog, tombstones and details included (a revoked
        token); the revision moves on and every older ``since`` resyncs. Another
        credential's catalog is never touched."""
        state = self._state
        with state.lock:
            if self._retired:
                raise GitHubStoreRetired("this GitHub catalog store was replaced")
            if state.meta is None or not _matches(state.meta, self._identity, self._viewer):
                return
            if not (state.repos or state.issues or state.tombstones or state.details):
                return  # nothing left: a repeated 401 must not move the revision
            state.repos, state.issues, state.tombstones, state.details = {}, {}, {}, {}
            revision = _note_memory_revision(state.meta["revision"] + 1)
            state.meta.update(revision=revision, pruned_through=revision)

    def purge_owners_not_in(self, owners: set[str]) -> None:
        """Drop repositories (and issues) of owners outside the allowlist.

        An empty allowlist means "every owner" (config semantics), so it is a no-op.
        """
        if not owners:
            return
        allowed = _owners(owners)
        state = self._state
        with state.lock:
            self._claim()
            gone = [rid for rid, r in state.repos.items() if r["owner"] not in allowed]
            if gone:
                self._remove_repos(gone, self._bump())

    def put_detail(self, repo_id: str, issue: dict, fetched_at: float | None = None) -> None:
        """Store an opened issue's detail (with its body) as fetched at ``issue["updated_at"]``,
        at ``fetched_at`` (epoch seconds; default now)."""
        state = self._state
        with state.lock:
            self._claim()
            state.details[(repo_id, issue["github_id"])] = {
                "number": issue["number"], "updated_at": issue.get("updated_at"), "data": _normal(issue),
                "fetched_at": self._now() if fetched_at is None else float(fetched_at)}

    def stored_detail(self, repo_id: str, number: int) -> tuple[dict, float] | None:
        """``(detail, fetched_at)`` of issue ``number``, or ``None`` when it was never opened."""
        state = self._state
        with state.lock:
            self._claim()
            for (rid, _), row in state.details.items():
                if rid == repo_id and row["number"] == number:
                    return _normal(row["data"]), row["fetched_at"]
            return None

    def detail(self, repo_id: str, number: int) -> dict | None:
        """The stored detail of issue ``number``, or ``None`` when it was never opened."""
        found = self.stored_detail(repo_id, number)
        return None if found is None else found[0]

    def search_issues(self, query, owners: set[str] | None, *, limit: int = SEARCH_LIMIT,
                      public_only: bool = False) -> list[dict]:
        """Read-only: at most ``limit`` (<= ``SEARCH_LIMIT``) stored issues a `#` query names, of
        ``owners`` only (``None``: every owner). See ``search_rows`` for the order and bounds."""
        state = self._state
        with state.lock:
            self._claim()
            by_repo: dict[str, list[dict]] = {}
            for (rid, _), row in state.issues.items():
                by_repo.setdefault(rid, []).append(row["data"])
            return _normal(search_rows([r["data"] for r in state.repos.values()],
                                       lambda rid: by_repo.get(rid, ()), query, owners, limit, public_only))

    def watermark(self, repo_id: str) -> str | None:
        state = self._state
        with state.lock:
            self._claim()
            record = state.repos.get(repo_id)
            return record["watermark"] if record else None

    def revision(self) -> int:
        with self._state.lock:
            return self._claim()["revision"]

    def changes_since(self, rev: int, *, full: bool = True) -> dict:
        """``full=False``: a ``since`` that cannot be answered returns no rows (the caller pages the catalog itself)."""
        state = self._state
        with state.lock:
            meta = self._claim()
            if not full and (max(0, int(rev)) < meta["pruned_through"] or max(0, int(rev)) > meta["revision"]):
                return {"revision": meta["revision"], "resync": True, "upserted": [], "removed": []}
            rows = [_item("repository", rid, rid, r["rev"], _normal(r["data"])) for rid, r in state.repos.items()]
            rows += [_item("issue", rid, gid, r["rev"], _normal(r["data"])) for (rid, gid), r in state.issues.items()]
            tombs = [_item(kind, rid, key, t["rev"]) for (kind, rid, key), t in state.tombstones.items()]
            return _delta(meta["revision"], meta["pruned_through"], rev, rows, tombs)

    @contextlib.contextmanager
    def advisory_lock(self):
        """Exclusive sync-pass lock (process-wide); yields whether it was acquired."""
        acquired = _MEMORY_SYNC_LOCK.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                _MEMORY_SYNC_LOCK.release()


# ---------------------------------------------------------------- Postgres


_META_READ = "SELECT identity, viewer, revision, pruned_through FROM github_catalog_meta WHERE family=$1"
_META_SELECT = _META_READ + " FOR UPDATE"


async def _drop_details(conn, sql: str, *args) -> None:
    """Delete issue details inside a savepoint; a missing ``0007`` table (mid-roll) is not an error."""
    import asyncpg
    try:
        async with conn.transaction():
            await conn.execute(sql, *args)
    except asyncpg.exceptions.UndefinedTableError:
        pass


class _LazyRevision:
    """One revision for one transaction, bumped on its first visible change only."""

    def __init__(self, conn):
        self._conn, self._value = conn, None

    async def __call__(self) -> int:
        if self._value is None:
            self._value = await GitHubStore._bump(self._conn)
        return self._value


class GitHubStore:
    """Postgres-backed store (``control_plane.pg_dsn``).

    ``conn_factory`` is a zero-argument callable returning an awaitable asyncpg
    connection; it is called lazily, again after a dropped connection, and once
    more for the dedicated advisory-lock connection (a session advisory lock is
    tied to the connection that took it). ``run`` executes a coroutine on the
    store's event loop (default: a new ``_LoopRunner``).
    """

    persisted = True

    def __init__(self, conn_factory: Callable[[], Any], *, identity: str, viewer: str | None = None,
                 now: Callable[[], float] | None = None, run: Callable | None = None):
        if run is None:
            from .bridge import _LoopRunner
            run = _LoopRunner().run
        self._factory = conn_factory
        self._identity, self._viewer = identity, viewer
        self._now = now or time.time
        self._run = run
        self._conn = None
        self._lock_conn = None
        # asyncpg allows one operation per connection; request threads share the store.
        self._guard = threading.RLock()
        self._sync_guard = threading.Lock()
        self._retired = False

    def __repr__(self):
        return f"GitHubStore(identity={self._identity[:12]}..., viewer={self._viewer!r})"

    def retire(self) -> None:
        """Refuse every later operation; one in flight (it holds the guard) finishes first."""
        with self._guard:
            self._retired = True

    def close(self) -> None:
        with self._guard:
            self._retired = True
            for name in ("_conn", "_lock_conn"):
                conn = getattr(self, name)
                setattr(self, name, None)
                if conn is not None and not conn.is_closed():
                    with contextlib.suppress(Exception):
                        self._run(conn.close())

    # -- plumbing

    async def _connect(self, current):
        if current is None or current.is_closed():
            return await self._factory()
        return current

    def _call(self, work: Callable[[Any], Any]):
        """Run ``work(conn)`` (a coroutine) on the data connection, mapping failures."""
        import asyncpg
        with self._guard:
            if self._retired:
                raise GitHubStoreRetired("this GitHub catalog store was replaced")
            try:
                self._conn = self._run(self._connect(self._conn))
                return self._run(work(self._conn))
            except asyncpg.exceptions.UndefinedTableError as exc:
                raise GitHubStoreNotMigrated("GitHub catalog tables are not migrated yet") from exc
            except (asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InterfaceError,
                    ConnectionError, OSError) as exc:
                # a timeout (TimeoutError is an OSError) can leave the connection mid-query or
                # mid-transaction: never reuse it, the next call connects afresh
                self._discard()
                raise GitHubStoreUnavailable("GitHub catalog store is unreachable") from exc

    def _discard(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return

        async def terminate():
            conn.terminate()  # on the store's loop: asyncio transports are not thread-safe
        with contextlib.suppress(Exception):
            self._run(terminate())

    def _stamp(self) -> datetime:
        return datetime.fromtimestamp(self._now(), UTC)

    async def _claim(self, conn) -> dict:
        """Inside a transaction: lock the family row, claim it or refuse a foreign catalog, prune."""
        meta = await conn.fetchrow(_META_SELECT, FAMILY)
        if meta is None:
            await conn.execute(
                "INSERT INTO github_catalog_meta (family, identity, viewer) VALUES ($1,$2,$3) "
                "ON CONFLICT (family) DO NOTHING", FAMILY, self._identity, self._viewer)
            meta = await conn.fetchrow(_META_SELECT, FAMILY)
        meta = dict(meta)
        if not _matches(meta, self._identity, self._viewer):
            raise GitHubStoreIdentityMismatch("stored GitHub catalog belongs to another credential")
        if meta["viewer"] is None and self._viewer is not None:
            await conn.execute("UPDATE github_catalog_meta SET viewer=$2 WHERE family=$1", FAMILY, self._viewer)
            meta["viewer"] = self._viewer
        cutoff = self._stamp() - TOMBSTONE_RETENTION
        pruned = await conn.fetchval(
            "WITH gone AS (DELETE FROM github_catalog_tombstones WHERE removed_at < $1 RETURNING rev) "
            "SELECT max(rev) FROM gone", cutoff)
        if pruned is not None and pruned > meta["pruned_through"]:
            await conn.execute("UPDATE github_catalog_meta SET pruned_through=$2 WHERE family=$1", FAMILY, pruned)
            meta["pruned_through"] = pruned
        return meta

    async def _read_meta(self, conn) -> dict:
        """Inside a read-only transaction: the family row without locking, claiming or pruning it.

        Tombstones past ``TOMBSTONE_RETENTION`` are pruned by the next write;
        a read treats them as already pruned (excluded, and ``pruned_through``
        raised over them), so answers match what a pruning read would give.
        No row yet reads as an empty catalog at revision 0.
        """
        meta = await conn.fetchrow(_META_READ, FAMILY)
        if meta is None:
            meta = {"identity": self._identity, "viewer": self._viewer, "revision": 0, "pruned_through": 0}
        meta = dict(meta)
        if not _matches(meta, self._identity, self._viewer):
            raise GitHubStoreIdentityMismatch("stored GitHub catalog belongs to another credential")
        meta["cutoff"] = self._stamp() - TOMBSTONE_RETENTION
        expired = await conn.fetchval(
            "SELECT max(rev) FROM github_catalog_tombstones WHERE removed_at < $1", meta["cutoff"])
        if expired is not None and expired > meta["pruned_through"]:
            meta["pruned_through"] = expired
        return meta

    def _read(self, work: Callable[[Any, dict], Any]):
        """Run ``work(conn, meta)`` in a read-only snapshot (rows agree with the revision read)."""
        async def run(conn):
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                return await work(conn, await self._read_meta(conn))
        return self._call(run)

    @staticmethod
    async def _bump(conn) -> int:
        return await conn.fetchval(
            "UPDATE github_catalog_meta SET revision=revision+1, updated_at=now() "
            "WHERE family=$1 RETURNING revision", FAMILY)

    async def _remove_repos(self, conn, rids: list[str], rev: int) -> None:
        now = self._stamp()
        await _drop_details(conn, "DELETE FROM github_issue_details WHERE repository_id = ANY($1::text[])", rids)
        await conn.execute(
            "WITH gone AS (DELETE FROM github_issues WHERE repository_id = ANY($1::text[]) "
            "RETURNING repository_id, github_id) "
            "INSERT INTO github_catalog_tombstones (kind, repository_id, key, rev, removed_at) "
            "SELECT 'issue', repository_id, github_id, $2, $3 FROM gone "
            "ON CONFLICT (kind, repository_id, key) DO UPDATE SET rev=EXCLUDED.rev, removed_at=EXCLUDED.removed_at",
            rids, rev, now)
        await conn.execute(
            "WITH gone AS (DELETE FROM github_repositories WHERE repository_id = ANY($1::text[]) "
            "RETURNING repository_id) "
            "INSERT INTO github_catalog_tombstones (kind, repository_id, key, rev, removed_at) "
            "SELECT 'repository', repository_id, repository_id, $2, $3 FROM gone "
            "ON CONFLICT (kind, repository_id, key) DO UPDATE SET rev=EXCLUDED.rev, removed_at=EXCLUDED.removed_at",
            rids, rev, now)

    # -- interface

    async def _wipe(self, conn) -> None:
        """Drop every row and tombstone and tag the family with this store's credential."""
        await conn.execute("DELETE FROM github_issues")
        await conn.execute("DELETE FROM github_repositories")
        await conn.execute("DELETE FROM github_catalog_tombstones")
        await _drop_details(conn, "DELETE FROM github_issue_details")
        await conn.execute(
            "UPDATE github_catalog_meta SET identity=$2, viewer=$3, revision=revision+1, "
            "pruned_through=revision+1, updated_at=now() WHERE family=$1",
            FAMILY, self._identity, self._viewer)

    def purge(self) -> None:
        """Drop this credential's whole catalog, tombstones and details included (a revoked
        token); the revision moves on and every older ``since`` resyncs. Another
        credential's catalog is never touched."""
        async def work(conn):
            async with conn.transaction():
                meta = await conn.fetchrow(_META_SELECT, FAMILY)
                if meta is None or not _matches(dict(meta), self._identity, self._viewer):
                    return
                stored = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM github_repositories) OR EXISTS (SELECT 1 FROM github_issues) "
                    "OR EXISTS (SELECT 1 FROM github_catalog_tombstones)")
                details = False
                with contextlib.suppress(Exception):
                    async with conn.transaction():
                        details = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM github_issue_details)")
                if stored or details:  # nothing left: a repeated 401 must not move the revision
                    await self._wipe(conn)
        self._call(work)

    def load(self, *, rows: bool = True) -> Snapshot | None:
        async def work(conn):
            async with conn.transaction():
                meta = await conn.fetchrow(_META_SELECT, FAMILY)
                if meta is None:
                    await self._claim(conn)
                    return None
                if not _matches(dict(meta), self._identity, self._viewer):
                    await self._wipe(conn)
                    return None
                meta = await self._claim(conn)
                if not rows:
                    return Snapshot(identity=meta["identity"], viewer=meta["viewer"], revision=meta["revision"])
                repos = await conn.fetch("SELECT repository_id, data, watermark FROM github_repositories")
                issues = await conn.fetch("SELECT repository_id, github_id, data FROM github_issues")
            snapshot = Snapshot(identity=meta["identity"], viewer=meta["viewer"], revision=meta["revision"])
            for row in repos:
                snapshot.repositories[row["repository_id"]] = json.loads(row["data"])
                if row["watermark"] is not None:
                    snapshot.watermarks[row["repository_id"]] = row["watermark"]
            for row in issues:
                snapshot.issues.setdefault(row["repository_id"], {})[row["github_id"]] = json.loads(row["data"])
            return snapshot
        try:
            return self._call(work)
        except GitHubStoreNotMigrated:
            return None  # truly nothing stored; an unreachable database raises (it is not a purge)

    async def _put_repos(self, conn, incoming: dict[str, dict], listed: list[str] | None, rev) -> None:
        if listed is not None and not listed:  # nothing can be gone: read only the incoming rows
            found = await conn.fetch("SELECT repository_id, owner, data FROM github_repositories "
                                     "WHERE repository_id = ANY($1::text[])", list(incoming))
        else:
            found = await conn.fetch("SELECT repository_id, owner, data FROM github_repositories")
        stored = {r["repository_id"]: (r["owner"], json.loads(r["data"])) for r in found}
        changed = [rid for rid, data in incoming.items() if rid not in stored or stored[rid][1] != data]
        gone = [rid for rid, (owner, _) in stored.items()
                if rid not in incoming and (listed is None or owner in listed)]
        if not changed and not gone:
            return
        if changed:
            await conn.executemany(
                "INSERT INTO github_repositories (repository_id, owner, data, rev) "
                "VALUES ($1,$2,$3::jsonb,$4) ON CONFLICT (repository_id) DO UPDATE SET "
                "owner=EXCLUDED.owner, data=EXCLUDED.data, rev=EXCLUDED.rev",
                [(rid, _owner(incoming[rid]), json.dumps(incoming[rid]), await rev()) for rid in changed])
            await conn.execute(
                "DELETE FROM github_catalog_tombstones WHERE kind='repository' "
                "AND repository_id = ANY($1::text[])", changed)
        if gone:
            await self._remove_repos(conn, gone, await rev())

    async def _put_issues(self, conn, repo_id: str, incoming: dict[str, dict], rev) -> None:
        stored = {r["github_id"]: json.loads(r["data"]) for r in await conn.fetch(
            "SELECT github_id, data FROM github_issues WHERE repository_id=$1 "
            "AND github_id = ANY($2::text[])", repo_id, list(incoming))}
        changed = [gid for gid, data in incoming.items() if stored.get(gid) != data]
        if changed:
            revision = await rev()
            await conn.executemany(
                "INSERT INTO github_issues (repository_id, github_id, data, rev) "
                "VALUES ($1,$2,$3::jsonb,$4) ON CONFLICT (repository_id, github_id) DO UPDATE SET "
                "data=EXCLUDED.data, rev=EXCLUDED.rev",
                [(repo_id, gid, json.dumps(incoming[gid]), revision) for gid in changed])
            await conn.execute(
                "DELETE FROM github_catalog_tombstones WHERE kind='issue' AND repository_id=$1 "
                "AND key = ANY($2::text[])", repo_id, changed)

    async def _drop_issues(self, conn, repo_id: str, wanted: list[str], rev) -> None:
        await _drop_details(conn, "DELETE FROM github_issue_details WHERE repository_id=$1 "
                                  "AND github_id = ANY($2::text[])", repo_id, wanted)
        present = [r["github_id"] for r in await conn.fetch(
            "SELECT github_id FROM github_issues WHERE repository_id=$1 AND github_id = ANY($2::text[]) "
            "FOR UPDATE", repo_id, wanted)]
        if not present:
            return
        await conn.execute(
            "WITH gone AS (DELETE FROM github_issues WHERE repository_id=$1 AND github_id = ANY($2::text[]) "
            "RETURNING github_id) "
            "INSERT INTO github_catalog_tombstones (kind, repository_id, key, rev, removed_at) "
            "SELECT 'issue', $1, github_id, $3, $4 FROM gone "
            "ON CONFLICT (kind, repository_id, key) DO UPDATE SET rev=EXCLUDED.rev, removed_at=EXCLUDED.removed_at",
            repo_id, present, await rev(), self._stamp())

    def upsert_repositories(self, rows: Iterable[dict], scope: set[str] | None) -> None:
        incoming = {row["repository_id"]: _normal(row) for row in rows}
        listed = None if scope is None else sorted(_owners(scope))

        async def work(conn):
            async with conn.transaction():
                await self._claim(conn)
                await self._put_repos(conn, incoming, listed, _LazyRevision(conn))
        self._call(work)

    def upsert_issues(self, repo_id: str, rows: Iterable[dict], watermark: str | None) -> None:
        incoming = {row["github_id"]: _normal(row) for row in rows}

        async def work(conn):
            async with conn.transaction():
                await self._claim(conn)
                known = await conn.fetchval(
                    "SELECT 1 FROM github_repositories WHERE repository_id=$1 FOR UPDATE", repo_id)
                if known is None:
                    raise KeyError(repo_id)
                await self._put_issues(conn, repo_id, incoming, _LazyRevision(conn))
                await conn.execute(
                    "UPDATE github_repositories SET watermark=$2 WHERE repository_id=$1", repo_id, watermark)
        self._call(work)

    def remove_issues(self, repo_id: str, github_ids: Iterable[str]) -> None:
        wanted = list(dict.fromkeys(github_ids))

        async def work(conn):
            async with conn.transaction():
                await self._claim(conn)
                await self._drop_issues(conn, repo_id, wanted, _LazyRevision(conn))
        self._call(work)

    def apply_repository(self, row: dict, issues: Iterable[dict], removed: Iterable[str],
                         watermark: str | None) -> None:
        """One repository's completed sync in one transaction: its row (with the ``synced``
        probe), changed issues, removals and watermark, under one revision."""
        rid = row["repository_id"]
        data = _normal(row)
        incoming = {issue["github_id"]: _normal(issue) for issue in issues}
        wanted = list(dict.fromkeys(removed))

        async def work(conn):
            async with conn.transaction():
                await self._claim(conn)
                rev = _LazyRevision(conn)
                await self._put_repos(conn, {rid: data}, [], rev)
                await self._put_issues(conn, rid, incoming, rev)
                if wanted:
                    await self._drop_issues(conn, rid, wanted, rev)
                await conn.execute(
                    "UPDATE github_repositories SET watermark=$2 WHERE repository_id=$1", rid, watermark)
        self._call(work)

    def purge_owners_not_in(self, owners: set[str]) -> None:
        """Drop repositories (and issues) of owners outside the allowlist.

        An empty allowlist means "every owner" (config semantics), so it is a no-op.
        """
        if not owners:
            return
        allowed = sorted(_owners(owners))

        async def work(conn):
            async with conn.transaction():
                await self._claim(conn)
                gone = [r["repository_id"] for r in await conn.fetch(
                    "SELECT repository_id FROM github_repositories WHERE NOT (owner = ANY($1::text[]))", allowed)]
                if gone:
                    await self._remove_repos(conn, gone, await self._bump(conn))
        self._call(work)

    def put_detail(self, repo_id: str, issue: dict, fetched_at: float | None = None) -> None:
        """Store an opened issue's detail, fetched at ``fetched_at`` (epoch seconds; default
        now); ``GitHubStoreUnavailable`` before migration 0007."""
        data = _normal(issue)
        stamp = self._stamp() if fetched_at is None else datetime.fromtimestamp(fetched_at, UTC)

        async def work(conn):
            async with conn.transaction():
                await self._claim(conn)
                await conn.execute(
                    "INSERT INTO github_issue_details (repository_id, github_id, number, updated_at, data, fetched_at) "
                    "VALUES ($1,$2,$3,$4,$5::jsonb,$6) ON CONFLICT (repository_id, github_id) DO UPDATE SET "
                    "number=EXCLUDED.number, updated_at=EXCLUDED.updated_at, data=EXCLUDED.data, "
                    "fetched_at=EXCLUDED.fetched_at",
                    repo_id, data["github_id"], data["number"], data.get("updated_at"), json.dumps(data),
                    stamp)
        self._call(work)

    def stored_detail(self, repo_id: str, number: int) -> tuple[dict, float] | None:
        """``(detail, fetched_at)`` of issue ``number``, or ``None`` when it was never opened."""
        async def work(conn, meta):
            return await conn.fetchrow(
                "SELECT data, fetched_at FROM github_issue_details WHERE repository_id=$1 AND number=$2 "
                "ORDER BY fetched_at DESC LIMIT 1", repo_id, number)
        row = self._read(work)
        return None if row is None else (json.loads(row["data"]), row["fetched_at"].timestamp())

    def detail(self, repo_id: str, number: int) -> dict | None:
        """The stored detail of issue ``number``, or ``None`` when it was never opened."""
        found = self.stored_detail(repo_id, number)
        return None if found is None else found[0]

    def search_issues(self, query, owners: set[str] | None, *, limit: int = SEARCH_LIMIT,
                      public_only: bool = False) -> list[dict]:
        """Read-only (a repeatable-read snapshot that never claims or prunes): at most ``limit``
        (<= ``SEARCH_LIMIT``) stored issues a `#` query names, of ``owners`` only (``None``: every
        owner). Repositories are matched in Python (one owner-filtered read of the small repository
        table); issues are read by ``repository_id`` (the primary key's leading column), ordered and
        limited in SQL, so a query never reads more than ``MAX_SEARCH_REPOS`` repositories' issues."""
        parsed = parse_issue_query(query)
        limit = max(1, min(int(limit), SEARCH_LIMIT))
        if parsed is None:
            return []
        scope = None if owners is None else sorted(_owners(owners))

        async def work(conn, meta):
            repos = [{"repository_id": r["repository_id"], "data": json.loads(r["data"])} for r in await conn.fetch(
                "SELECT repository_id, data FROM github_repositories WHERE $1::text[] IS NULL OR owner = ANY($1::text[])",
                scope)]
            shared = _shared_names(_split_name(r["data"].get("name_with_owner")) for r in repos)
            anyone, numbered, exact = [], [], []
            for data, (any_issue, prefix, name_exact) in _pick_repos(parsed, (r["data"] for r in repos), public_only):
                if any_issue:
                    anyone.append(data["repository_id"])
                if prefix is not None:
                    numbered.append(data["repository_id"])
                if name_exact:
                    exact.append(data["repository_id"])
            prefix = parsed["number"] or (parsed["short"][1] if parsed["short"] else "")
            if not anyone and not numbered:
                return []
            rows = await conn.fetch(
                "WITH hit AS (SELECT repository_id, data, "
                " (repository_id = ANY($2::text[]) AND (data->>'number') LIKE $3 || '%') AS numbered "
                " FROM github_issues WHERE repository_id = ANY($1::text[]) "
                " OR (repository_id = ANY($2::text[]) AND (data->>'number') LIKE $3 || '%')) "
                "SELECT repository_id, data FROM hit ORDER BY "
                " CASE WHEN numbered AND data->>'number' = $3 THEN 0 WHEN numbered THEN 1 "
                "      WHEN repository_id = ANY($4::text[]) THEN 2 ELSE 3 END, "
                " upper(coalesce(data->>'state', '')) <> 'OPEN', "
                " coalesce(data->>'updated_at', '') COLLATE \"C\" DESC, repository_id COLLATE \"C\", "
                " (data->>'number')::bigint LIMIT $5",
                anyone, numbered, prefix, exact, limit)
            by_id = {r["repository_id"]: r["data"] for r in repos}
            return [_ref_row(by_id[r["repository_id"]], json.loads(r["data"]), shared) for r in rows]
        return self._read(work)

    def watermark(self, repo_id: str) -> str | None:
        async def work(conn, meta):
            return await conn.fetchval(
                "SELECT watermark FROM github_repositories WHERE repository_id=$1", repo_id)
        try:
            return self._read(work)
        except GitHubStoreUnavailable:
            return None

    def revision(self) -> int:
        async def work(conn, meta):
            return meta["revision"]
        return self._read(work)

    def changes_since(self, rev: int, *, full: bool = True) -> dict:
        """Rows and live tombstones newer than ``rev``; the full list when ``rev`` cannot be answered.

        ``full=False`` skips reading those rows (``resync`` with no rows): a caller that
        pages the catalog itself must not make Postgres read every jsonb row to discard it.
        """
        since = max(0, int(rev))

        async def work(conn, meta):
            resync = since < meta["pruned_through"] or since > meta["revision"]
            if resync and not full:
                return {"revision": meta["revision"], "resync": True, "upserted": [], "removed": []}
            floor = -1 if resync else since
            rows = [_item("repository", r["repository_id"], r["repository_id"], r["rev"], json.loads(r["data"]))
                    for r in await conn.fetch(
                        "SELECT repository_id, data, rev FROM github_repositories WHERE rev > $1", floor)]
            rows += [_item("issue", r["repository_id"], r["github_id"], r["rev"], json.loads(r["data"]))
                     for r in await conn.fetch(
                         "SELECT repository_id, github_id, data, rev FROM github_issues WHERE rev > $1", floor)]
            tombs = [] if resync else [
                _item(r["kind"], r["repository_id"], r["key"], r["rev"]) for r in await conn.fetch(
                    "SELECT kind, repository_id, key, rev FROM github_catalog_tombstones "
                    "WHERE rev > $1 AND removed_at >= $2", floor, meta["cutoff"])]
            return _delta(meta["revision"], meta["pruned_through"], since, rows, tombs)
        return self._read(work)

    @contextlib.contextmanager
    def advisory_lock(self):
        """Postgres session advisory lock around one sync pass; yields whether it was acquired.

        Held on a dedicated connection (a pooled or shared one would lose it) and
        never waits: a pass that cannot take the lock is skipped by the caller.
        """
        import asyncpg
        if not self._sync_guard.acquire(blocking=False):
            yield False
            return
        acquired = False
        try:
            try:
                self._lock_conn = self._run(self._connect(self._lock_conn))
                acquired = bool(self._run(self._lock_conn.fetchval(
                    "SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY)))
            except (asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InterfaceError,
                    ConnectionError, OSError) as exc:
                raise GitHubStoreUnavailable("GitHub catalog store is unreachable") from exc
            yield acquired
        finally:
            if acquired:
                with contextlib.suppress(Exception):
                    self._run(self._lock_conn.fetchval("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY))
            self._sync_guard.release()


def _connector(pg_dsn: str, command_timeout: float):
    def connect():
        import asyncpg
        return asyncpg.connect(pg_dsn, timeout=STORE_CONNECT_TIMEOUT, command_timeout=command_timeout)
    return connect


def catalog_store_factory(pg_dsn: str | None, *, now: Callable[[], float] | None = None,
                          command_timeout: float = STORE_COMMAND_TIMEOUT):
    """The monitor's store factory ``(identity, viewer) -> store`` for ``control_plane.pg_dsn``.

    With a DSN every store shares one asyncpg event-loop thread (started on
    first use); the monitor builds a new store per credential and viewer and
    closes the superseded one. Without a DSN the stores share one in-memory
    catalog that lives as long as the process. Every query is bounded by
    ``command_timeout`` and every connect by ``STORE_CONNECT_TIMEOUT``.
    """
    if not pg_dsn:
        shared: dict = {}
        return lambda identity, viewer=None: MemoryGitHubStore(identity=identity, viewer=viewer,
                                                                now=now, shared=shared)
    runner: list = []
    start = threading.Lock()

    def run(coro):
        with start:
            if not runner:
                from .bridge import _LoopRunner
                runner.append(_LoopRunner())
        return runner[0].run(coro)

    connect = _connector(pg_dsn, command_timeout)
    return lambda identity, viewer=None: GitHubStore(connect, identity=identity, viewer=viewer,
                                                     now=now, run=run)


def open_github_store(pg_dsn: str | None, *, identity: str, viewer: str | None = None,
                      now: Callable[[], float] | None = None):
    """The store for ``control_plane.pg_dsn``; in memory when no DSN is configured."""
    if not pg_dsn:
        return MemoryGitHubStore(identity=identity, viewer=viewer, now=now)
    return GitHubStore(_connector(pg_dsn, STORE_COMMAND_TIMEOUT), identity=identity, viewer=viewer, now=now)
