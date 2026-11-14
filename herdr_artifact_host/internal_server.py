"""The internal listener: a read-only API for the dashboard (role ``dashboard-read``), GET only.

Routes: ``/v1/feed``, ``/v1/folders``, ``/v1/items/<id>``, ``/v1/items/<id>/versions``, ``/v1/shares``, ``/v1/search``
and ``/v1/content/<item>/<version>/<path>`` (plus an unauthenticated ``/internal/healthz``).

* One bearer key; only its sha256 digest is configured.  A missing or wrong key is the same 401 (same bytes) on every
  path, counted per client by a failed-key limiter (429 once a client is blocked).  Any method but GET is 405.
* The dashboard operator sees every member's artifacts (``OPERATOR_READ_ALL``); retracted, tombstoned and discarded ones
  are never listed or served.
* Nothing is ever written: every query runs in a ``READ ONLY`` transaction (``ReadOnlyPool``), reads record no view
  event, claim or counter, and shares are returned as metadata only (never a token, a token digest or a URL).
* Bodies are MessagePack; ``Accept: application/json`` (or ``*/json``) gets JSON.
* Content is served for embedding in a sandboxed iframe from this listener's own origin (its own port): always
  ``Content-Security-Policy: sandbox``, ``X-Content-Type-Options: nosniff`` and ``Cache-Control: no-store``, with a
  content type derived from the file name, never from stored text.  A path is only ever a manifest key.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import asyncpg
import msgpack
from aiohttp import web

from .blobs import BlobCorrupt, BlobMissing, BlobStore, InvalidSha, validate_sha
from .library import (
    _FOLDER_SELECT,
    MAX_PAGE,
    OPERATOR_READ_ALL,
    FeedQuery,
    LibraryError,
    LibraryStore,
    Source,
    Viewer,
)
from .share_bundle import content_type_for
from .share_http import CHUNK_BYTES, RateLimiter, decode_rel_path

log = logging.getLogger("herdr_artifact_host.internal")

ROLE = "dashboard-read"
KEY_FAILURES_PER_MINUTE = 20
MAX_KEY_BYTES = 512
MAX_QUERY_BYTES = 2048
MAX_SHARES_PAGE = 200
MSGPACK_TYPE = "application/msgpack"
JSON_TYPE = "application/json"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SHARE_STATES = frozenset({"pending", "live", "revoked"})
_AUDIENCES = frozenset({"lab", "public"})
# Same visibility as the feed: never a retracted, tombstoned or discarded artifact.
_VISIBLE = "a.lifecycle NOT IN ('retracted','tombstoned') AND a.intake IN ('accepted','pending')"
# Every response (API, errors and content) is sandboxed: a document served from here can run no script, reach no
# network and has an opaque origin (no allow-same-origin).
CONTENT_CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'"
MAX_INT = 10**9  # offsets and limits are clamped long before an int64 parameter could overflow
MAX_BODY = 1024
HEALTHZ_PER_MINUTE = 120
KEY_FAILURES_GLOBAL_PER_MINUTE = 200
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
_SECURITY_HEADERS = (
    ("Content-Security-Policy", CONTENT_CSP),
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Server", "herdr-artifact-host"),
)
SURFACE = web.AppKey("internal_surface", object)


@dataclass(frozen=True)
class InternalConfig:
    """``key_digest`` is the lowercase hex sha256 of the dashboard key (the key itself is never stored)."""

    key_digest: str
    role: str = ROLE
    key_failures_per_minute: int = KEY_FAILURES_PER_MINUTE
    key_failures_global_per_minute: int = KEY_FAILURES_GLOBAL_PER_MINUTE
    healthz_per_minute: int = HEALTHZ_PER_MINUTE

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.key_digest):
            raise ValueError("internal_key_digest must be a lowercase hex sha256")
        if self.key_digest == _EMPTY_DIGEST:
            raise ValueError("internal_key_digest is the digest of the empty string")
        if self.role != ROLE:
            raise ValueError(f"the internal listener serves only the {ROLE} role")


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ReadOnlyPool:
    """The slice of an asyncpg pool the library reads use, every call inside a ``READ ONLY`` transaction."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def fetch(self, query: str, *args: object) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: object) -> asyncpg.Record | None:
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: object) -> Any:
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            return await conn.fetchval(query, *args)


def plain(value: object) -> Any:
    """A wire-safe copy: dataclasses and mappings become dicts, enums their value, datetimes ISO-8601 strings."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return plain(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    return value


def wants_json(accept: str) -> bool:
    """JSON only when the client asks for it (``application/json`` or ``*/json``); MessagePack otherwise."""
    for part in accept.split(","):
        media = part.split(";", 1)[0].strip().lower()
        if media in (JSON_TYPE, "application/*+json") or media.endswith("/json"):
            return True
    return False


def encode(
    request: web.Request, status: int, payload: object, headers: Mapping[str, str] | None = None
) -> web.Response:
    data = plain(payload)
    if wants_json(request.headers.get("Accept", "")):
        body, kind = json.dumps(data, separators=(",", ":"), sort_keys=True).encode(), JSON_TYPE
    else:
        body, kind = msgpack.packb(data, use_bin_type=True), MSGPACK_TYPE
    out = web.Response(status=status, body=body, content_type=kind)
    for name, value in _SECURITY_HEADERS:
        out.headers[name] = value
    for name, value in (headers or {}).items():
        out.headers[name] = value
    return out


def _fail(
    request: web.Request,
    status: int,
    code: str,
    headers: Mapping[str, str] | None = None,
    message: str | None = None,
) -> web.Response:
    return encode(
        request, status, {"error": code} | ({"message": message} if message else {}), headers
    )


class _BadRequest(Exception):
    pass


def _one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if any("\x00" in v for v in values):
        raise _BadRequest(f"{name} holds a NUL")
    if len(values) != 1:
        raise _BadRequest(f"{name} may be given once")
    return values[0] or None


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _BadRequest("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise _BadRequest("timestamps need a time zone")
    try:
        parsed.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise _BadRequest("timestamp out of range") from exc
    return parsed


def _int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise _BadRequest("invalid number") from exc
    return max(-MAX_INT, min(number, MAX_INT))


def parse_feed_query(query: dict[str, list[str]], *, all_sources: bool = False) -> FeedQuery:
    """The dashboard ``/library`` parameters: q, sources, folder_id, type, since, until, limit, offset."""
    sources = _one(query, "sources")
    chosen: frozenset[Source] | None = None
    if sources:
        try:
            chosen = frozenset(Source(s) for s in sources.split(","))
        except ValueError as exc:
            raise _BadRequest(
                "sources must be from: " + ", ".join(sorted(s.value for s in Source))
            ) from exc
    elif all_sources:
        chosen = frozenset(Source)
    limit = min(_int(_one(query, "limit"), 50), MAX_PAGE)
    base = FeedQuery()
    return FeedQuery(
        q=_one(query, "q"),
        sources=chosen if chosen is not None else base.sources,
        folder_id=_one(query, "folder_id"),
        artifact_type=_one(query, "type"),
        since=_time(_one(query, "since")),
        until=_time(_one(query, "until")),
        limit=limit,
        offset=_int(_one(query, "offset"), 0),
    )


def _files(manifest: object) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(manifest, list):
        for e in manifest:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                out.append(
                    {
                        "path": e["name"],
                        "sha256": e.get("sha256"),
                        "size": e.get("size_bytes"),
                        "media_type": content_type_for(e["name"]),
                    }
                )
    return out


def _manifest(raw: object) -> object:
    return json.loads(raw) if isinstance(raw, str) else raw


class InternalSurface:
    def __init__(
        self,
        config: InternalConfig,
        pool: asyncpg.Pool,
        blobs: BlobStore,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.pool = pool
        self.reader = ReadOnlyPool(pool)
        self.library = LibraryStore(self.reader)  # type: ignore[arg-type]
        self.blobs = blobs
        self.failures = RateLimiter(config.key_failures_per_minute, now)
        self.failures_all = RateLimiter(config.key_failures_global_per_minute, now)
        self.health = RateLimiter(config.healthz_per_minute, now)

    # -- entry --------------------------------------------------------------------------------------------

    @staticmethod
    def _client(request: web.Request) -> str:
        """The limiter key: the socket peer only.  The only hop in front of this listener is the one-way relay, which
        does not set a trusted client address, so a forwarded header is attacker-controlled and never consulted."""
        peername = request.transport.get_extra_info("peername") if request.transport else None
        return str(peername[0]) if peername else "unknown"

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method != "GET":
            return _fail(request, 405, "method_not_allowed", {"Allow": "GET"})
        if request.body_exists or request.headers.getall("Transfer-Encoding", []):
            response = _fail(request, 400, "invalid_request")
            response.force_close()
            return response
        path = request.raw_path.split("?", 1)[0]
        client = self._client(request)
        if path == "/internal/healthz":
            if not self.health.allow(client):
                return _fail(request, 429, "rate_limited", {"Retry-After": "60"})
            return await self.healthz(request)
        if self.failures.blocked(client):
            return _fail(request, 429, "rate_limited", {"Retry-After": "60"})
        if not self.authorised(request):
            # The global backstop stops a distributed guesser; it never blocks a good key.
            if not self.failures_all.allow("*"):
                return _fail(request, 429, "rate_limited", {"Retry-After": "60"})
            self.failures.allow(client)  # counts the failure
            return _fail(request, 401, "unauthorized", {"WWW-Authenticate": "Bearer"})
        if len(request.rel_url.raw_query_string) > MAX_QUERY_BYTES:
            return _fail(request, 414, "uri_too_long")
        try:
            return await self.route(request, path)
        except _BadRequest as exc:
            return _fail(request, 400, "invalid_request", message=str(exc))
        except LibraryError as exc:
            return _fail(request, exc.status, exc.code)
        except Exception as exc:  # noqa: BLE001 - one uniform answer; the type only, never a parameter value
            log.error("internal: unexpected %s", type(exc).__name__)
            return _fail(request, 500, "internal_error")

    def authorised(self, request: web.Request) -> bool:
        values = request.headers.getall("Authorization", [])
        presented = ""
        if len(values) == 1 and len(values[0]) <= MAX_KEY_BYTES and values[0].isascii():
            scheme, _, token = values[0].partition(" ")
            if scheme == "Bearer":
                presented = token.strip()
        ok = hmac.compare_digest(key_digest(presented), self.config.key_digest)
        return ok and bool(presented)

    async def healthz(self, request: web.Request) -> web.Response:
        """A pool liveness check (a connection is available); no table is read."""
        try:
            async with self.pool.acquire(timeout=2):
                pass
        except (OSError, TimeoutError, asyncpg.PostgresError):
            return _fail(request, 503, "unavailable")
        return encode(request, 200, {"status": "ok"})

    async def route(self, request: web.Request, path: str) -> web.StreamResponse:
        parts = path.split("/")  # ['', 'v1', ...]
        query = {
            k: request.query.getall(k) for k in request.query
        }  # decoded exactly once, by aiohttp
        if any("\x00" in k for k in query):
            raise _BadRequest("NUL in a parameter name")
        if len(parts) < 3 or parts[0] != "" or parts[1] != "v1":
            return _fail(request, 404, "not_found")
        route, rest = parts[2], parts[3:]
        if route == "feed" and not rest:
            return await self.feed(request, query, search=False)
        if route == "search" and not rest:
            return await self.feed(request, query, search=True)
        if route == "folders" and not rest:
            return await self.folders(request, query)
        if route == "shares" and not rest:
            return await self.shares(request, query)
        if route == "items" and len(rest) in (1, 2) and _ID.fullmatch(rest[0]):
            if len(rest) == 1:
                return await self.item(request, rest[0])
            if rest[1] == "versions":
                return await self.versions(request, rest[0])
        if (
            route == "content"
            and len(rest) >= 3
            and _ID.fullmatch(rest[0])
            and _ID.fullmatch(rest[1])
        ):
            return await self.content(request, rest[0], rest[1], "/".join(rest[2:]))
        return _fail(request, 404, "not_found")

    # -- reads --------------------------------------------------------------------------------------------

    async def _folders(self, owner: str | None) -> list[dict[str, Any]]:
        if owner:
            rows = await self.reader.fetch(
                _FOLDER_SELECT + " WHERE f.owner=$1 ORDER BY lower(f.name), f.folder_id", owner
            )
        else:
            rows = await self.reader.fetch(
                _FOLDER_SELECT + " ORDER BY lower(f.name), f.owner, f.folder_id"
            )
        return [
            {
                "folder_id": r["folder_id"],
                "owner": r["owner"],
                "name": r["name"],
                "item_count": int(r["item_count"]),
            }
            for r in rows
        ]

    async def _pending(self, owner: str | None) -> int:
        where = "intake='pending' AND lifecycle NOT IN ('retracted','tombstoned')"
        if owner:
            return int(
                await self.reader.fetchval(
                    f"SELECT count(*) FROM artifacts WHERE owner=$1 AND {where}", owner
                )
            )
        return int(await self.reader.fetchval(f"SELECT count(*) FROM artifacts WHERE {where}"))

    async def feed(
        self, request: web.Request, query: dict[str, list[str]], *, search: bool
    ) -> web.Response:
        feed_query = parse_feed_query(query, all_sources=search)
        if search and not feed_query.q:
            raise _BadRequest("q is required")
        owner = _one(query, "owner")
        viewer = Viewer(member_id=owner or "", read_all=True) if owner else OPERATOR_READ_ALL
        page = await self.library.feed(viewer, feed_query)
        body: dict[str, Any] = {
            "items": page.items,
            "total": page.total,
            "next_offset": page.next_offset,
        }
        if not search:
            body["folders"] = await self._folders(owner)
            body["pending"] = await self._pending(owner)
        return encode(request, 200, body)

    async def folders(self, request: web.Request, query: dict[str, list[str]]) -> web.Response:
        return encode(request, 200, {"folders": await self._folders(_one(query, "owner"))})

    async def shares(self, request: web.Request, query: dict[str, list[str]]) -> web.Response:
        """Share metadata only: never ``token_hash``, a token or a URL."""
        args: list[object] = []
        where = [_VISIBLE]

        def arg(value: object) -> str:
            args.append(value)
            return f"${len(args)}"

        artifact_id, state, audience = (
            _one(query, "artifact_id"),
            _one(query, "state"),
            _one(query, "audience"),
        )
        if artifact_id:
            where.append(f"s.artifact_id={arg(artifact_id)}")
        if state:
            if state not in _SHARE_STATES:
                raise _BadRequest("unknown state")
            where.append(f"s.state={arg(state)}")
        if audience:
            if audience not in _AUDIENCES:
                raise _BadRequest("unknown audience")
            where.append(f"s.audience={arg(audience)}")
        limit = max(1, min(_int(_one(query, "limit"), 50), MAX_SHARES_PAGE))
        offset = max(0, _int(_one(query, "offset"), 0))
        condition = " AND ".join(where)
        total = int(
            await self.reader.fetchval(
                f"SELECT count(*) FROM shares s JOIN artifacts a ON a.artifact_id=s.artifact_id WHERE {condition}",
                *args,
            )
        )
        lim, off = arg(limit), arg(offset)
        rows = await self.reader.fetch(
            "SELECT s.share_id, s.artifact_id, s.version_id, s.audience, s.state, s.entry, s.title, s.expires_at, "
            "s.created_by, s.created_at, s.approved_at, s.revoked_at, s.created_via, s.allow_emails, s.allow_groups, "
            "(s.state='live' AND (s.expires_at IS NULL OR s.expires_at > now())) AS active "
            f"FROM shares s JOIN artifacts a ON a.artifact_id=s.artifact_id WHERE {condition} "
            f"ORDER BY s.created_at DESC, s.share_id LIMIT {lim} OFFSET {off}",
            *args,
        )
        items = [
            {
                "share_id": r["share_id"],
                "item": r["artifact_id"],
                "version_id": r["version_id"],
                "audience": r["audience"],
                "state": r["state"],
                "active": bool(r["active"]),
                "entry": r["entry"],
                "title": r["title"],
                "expires_at": r["expires_at"],
                "created_by": r["created_by"],
                "created_at": r["created_at"],
                "approved_at": r["approved_at"],
                "revoked_at": r["revoked_at"],
                "created_via": r["created_via"],
                "allow_emails": list(r["allow_emails"]),
                "allow_groups": list(r["allow_groups"]),
            }
            for r in rows
        ]
        nxt = offset + len(items)
        return encode(
            request,
            200,
            {"shares": items, "total": total, "next_offset": nxt if nxt < total else None},
        )

    async def item(self, request: web.Request, artifact_id: str) -> web.Response:
        row = await self.reader.fetchrow(
            "SELECT a.artifact_id, coalesce(nullif(a.display_name,''), nullif(a.original_filename,''), a.artifact_id) "
            "AS title, a.owner, a.original_filename, a.artifact_type, a.media_type, a.size_bytes, a.intake::text AS intake, "
            "a.lifecycle, a.created_via, a.created_by, a.created_at, a.updated_at, a.current_version_id, "
            "v.version_number, v.state AS version_state, v.output_manifest::text AS manifest, "
            "(SELECT count(*) FROM shares s WHERE s.artifact_id=a.artifact_id) AS share_count, "
            "coalesce((SELECT array_agg(i.folder_id ORDER BY i.folder_id) FROM library_items i "
            "WHERE i.artifact_id=a.artifact_id), '{}') AS folder_ids "
            "FROM artifacts a LEFT JOIN artifact_versions v ON v.version_id=a.current_version_id "
            f"AND v.artifact_id=a.artifact_id WHERE a.artifact_id=$1 AND {_VISIBLE}",
            artifact_id,
        )
        if row is None:
            return _fail(request, 404, "not_found")
        item = {
            k: row[k]
            for k in row.keys()
            if k not in ("manifest", "version_state", "version_number")
        }
        item["folder_ids"] = list(row["folder_ids"])
        item["share_count"] = int(row["share_count"])
        item["version_number"] = row["version_number"]
        item["version_state"] = row["version_state"]
        item["files"] = (
            _files(_manifest(row["manifest"]))
            if row["manifest"] and row["version_state"] == "ready"
            else []
        )
        return encode(request, 200, {"item": item})

    async def versions(self, request: web.Request, artifact_id: str) -> web.Response:
        head = await self.reader.fetchrow(
            f"SELECT a.current_version_id FROM artifacts a WHERE a.artifact_id=$1 AND {_VISIBLE}",
            artifact_id,
        )
        if head is None:
            return _fail(request, 404, "not_found")
        rows = await self.reader.fetch(
            "SELECT version_id, version_number, state, parent_version_ids, created_via, collaborator_agent_id, "
            "created_by, created_at, purged_at, output_manifest::text AS manifest FROM artifact_versions "
            "WHERE artifact_id=$1 ORDER BY version_number DESC",
            artifact_id,
        )
        versions = [
            {
                "version_id": r["version_id"],
                "version_number": r["version_number"],
                "state": r["state"],
                "current": r["version_id"] == head["current_version_id"],
                "parent_version_ids": list(r["parent_version_ids"]),
                "created_via": r["created_via"],
                "collaborator_agent_id": r["collaborator_agent_id"],
                "created_by": r["created_by"],
                "created_at": r["created_at"],
                "purged_at": r["purged_at"],
                "files": _files(_manifest(r["manifest"])) if r["state"] != "purged" else [],
            }
            for r in rows
        ]
        return encode(request, 200, {"item": artifact_id, "versions": versions})

    # -- content ------------------------------------------------------------------------------------------

    async def content(
        self, request: web.Request, artifact_id: str, version_id: str, raw_path: str
    ) -> web.StreamResponse:
        name = decode_rel_path(raw_path)
        if name is None:
            return _fail(request, 404, "not_found")
        row = await self.reader.fetchrow(
            "SELECT v.output_manifest::text AS manifest FROM artifact_versions v JOIN artifacts a "
            f"ON a.artifact_id=v.artifact_id WHERE v.artifact_id=$1 AND v.version_id=$2 AND v.state='ready' AND {_VISIBLE}",
            artifact_id,
            version_id,
        )
        entry = None
        if row is not None:
            manifest = _manifest(row["manifest"])
            if isinstance(manifest, list):
                entry = next(
                    (e for e in manifest if isinstance(e, dict) and e.get("name") == name), None
                )
        if entry is None:
            return _fail(request, 404, "not_found")
        try:
            sha = validate_sha(str(entry.get("sha256")))
            handle, _ = await asyncio.to_thread(self.blobs.open_checked, sha)
        except (InvalidSha, BlobMissing, BlobCorrupt):
            log.warning("internal: content unavailable")
            return _fail(request, 404, "not_found")
        try:
            size = handle.seek(0, 2)
            handle.seek(0)
            response = web.StreamResponse(status=200)
            response.content_type = content_type_for(name)
            response.content_length = size
            response.headers["Content-Security-Policy"] = "sandbox"
            for header, value in _SECURITY_HEADERS:
                response.headers[header] = value
            await response.prepare(request)
            remaining = size
            while remaining > 0:
                chunk = await asyncio.to_thread(handle.read, min(CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            handle.close()


async def _uniform(request: web.Request, handler: Callable[..., Any]) -> web.StreamResponse:
    """A routing error raised by aiohttp itself (an unroutable target) is the plain 404."""
    try:
        return await handler(request)
    except web.HTTPException:
        return _fail(request, 404, "not_found")
    except Exception as exc:  # noqa: BLE001 - encoded, with the security headers; the type only is logged
        log.error("internal: unexpected %s", type(exc).__name__)
        return _fail(request, 500, "internal_error")


def build_app(
    config: InternalConfig,
    pool: asyncpg.Pool,
    blobs: BlobStore,
    *,
    now: Callable[[], float] = time.time,
) -> web.Application:
    surface = InternalSurface(config, pool, blobs, now)

    @web.middleware
    async def middleware(request: web.Request, handler: Callable[..., Any]) -> web.StreamResponse:
        return await _uniform(request, handler)

    app = web.Application(middlewares=[middleware], client_max_size=MAX_BODY)
    app[SURFACE] = surface
    app.router.add_route("*", "/{tail:.*}", surface.handle)
    return app


class InternalServer:
    """Runs ``build_app`` on a TCP socket (the deploy entry point and the tests)."""

    def __init__(self, app: web.Application) -> None:
        self.app = app
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        runner = web.AppRunner(
            self.app, access_log=None, handle_signals=False, keepalive_timeout=15
        )
        self._runner = runner
        await runner.setup()
        site = web.TCPSite(runner, host, port, backlog=64)
        await site.start()
        sockets = site._server.sockets if site._server is not None else []  # type: ignore[union-attr]
        self.port = int(sockets[0].getsockname()[1]) if sockets else port
        return self.port

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
