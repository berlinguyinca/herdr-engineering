"""The public share surface of the artifact host: ``/s/*`` (public links and lab shares).

Ported from the control plane's ``share_public.py``, whose security contract carries over unchanged:

* Every failure is ONE response: the same status, headers and body (``Not found``), ``Connection: close``. The reason is
  only ever a fixed code, written to a rate-limited log line and, when the share is known, a ``denied`` event. Tokens,
  sessions and CSRF values are never logged or put in an event.
* The request target is checked exactly as before (length, printable ASCII, strict percent-escapes, one decode per
  segment, no dot segments). The token is looked up by its sha256 digest.
* HTML over ``MODE_SWITCH_MAX_BYTES`` (2 MiB) cannot be labelled or get the mode-switch link.  An unlabelled one is
  streamed untouched, as before; a member-labelled one ("Shared by ...") is the uniform 404, event reason
  ``html_too_large`` (``share_public.py``: ``if size > MODE_SWITCH_MAX_BYTES: if bar: deny``).
* ``/healthz`` is NOT on the public listener (there it is the uniform 404).  It is served by a second listener of
  the same process bound to 127.0.0.1 only (``PublicServer.health_port``; the port option is provided by the Task 10
  deploy entry point, which does not exist yet), so the compose healthcheck target is ``http://127.0.0.1:<health-port>/healthz`` run inside the container.
  Reachability is decided by the socket's bind address, not by inspecting the peer.
* Whole-blob hashing of large blobs is capped (``HASH_CONCURRENCY`` passes, own thread pool, no queue): a request that
  finds every slot busy gets the uniform 503 with ``Retry-After: 5`` (as a busy render does; it needs a resolved token
  and bundle member, so it reveals nothing to a stranger), and a pass is abandoned between chunks once the client's
  transport is closing.  Cheap reads and renders use a separate pool that hashing cannot fill.
* A client that half-closes (SHUT_WR) after its request still gets the full response: the handler is not cancelled
  and the connection closes after that one response.
* No request body is read except for the lab annotation POSTs; any other request that announces a body is closed.
* A lab share is decided by its allow-list and is never claimed; a public link is claimed only with its claim cookie, a
  different cookie, key use and format from a lab session and from a member session.

What changed with the move to the host (all listed in the task report): share state is read from the database on EVERY
request, so a revoke or an expiry is seen by the next request; bytes come from the content-addressed blob store
(hash-verified; corruption is the uniform 404 with a logged reason code); events, claims and annotations are written to
the database directly instead of through files and a worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

import aiohttp
import asyncpg
from aiohttp import web
from aiohttp.web_protocol import RequestHandler
from aiohttp.web_request import BaseRequest
from multidict import CIMultiDict

from .annotation_anchor import (
    MAX_BODY_BYTES,
    AnnotationInputError,
    annotation_settings,
    parse_annotation_request,
    parse_delete_request,
    parse_edit_request,
)
from .blobs import BlobAborted, BlobCorrupt, BlobMissing, BlobStore, InvalidSha
from .cognito import CognitoAuthenticator, SyncedDirectory
from .https_origin import https_origin
from .library import LibraryError, LibraryStore, Viewer
from .members_config import valid_public_host
from .share_bundle import content_type_for
from .share_http import (
    _CLIENT_ID,
    _CONTENT_LENGTH,
    _NOT_FOUND_HEADERS,
    _SESSION_SEGMENT,
    _VIEW_PLACEHOLDER,
    ANNOTATE_CSP,
    CLAIM_COOKIE,
    CLAIM_SEEN_MAX,
    CORS_HEADERS,
    DEFAULT_STATIC_DIR,
    DENIED_PER_MINUTE,
    FONT_CACHE,
    FONT_FILES,
    JSON_HEADERS,
    LOG_PER_MINUTE,
    MAX_RAW_PATH,
    MODE_SWITCH_ETAG_SUFFIX,
    MODE_SWITCH_MAX_BYTES,
    NONCE_BYTES,
    NOT_FOUND_BODY,
    OPENED_PER_MINUTE,
    PDFJS_CONFIG,
    PREFLIGHT_HEADERS,
    RAW_PATH_OK,
    SESSION_GLOBAL_PER_MINUTE,
    SESSION_PER_SHARE_PER_MINUTE,
    SHARED_BY_ETAG_SUFFIX,
    STATIC_FILES,
    STATIC_MAX_BYTES,
    TOKEN_RE,
    TOO_LARGE_NOTE,
    UNAVAILABLE_BODY,
    VENDOR_CACHE,
    VENDOR_FILES,
    VENDOR_MAX_BYTES,
    VIEW_CSP,
    VIEW_FILE,
    VIEWER_CSP,
    VIEWER_FILE,
    RateLimiter,
    ascii_compatible,
    content_headers,
    decode_rel_path,
    etag_matches,
    inject_mode_switch,
    labelled_html,
    landing_bar,
    landing_tag,
    mode_switch_link,
    open_beneath,
    parse_etags,
    parse_range,
    read_all,
    sandbox_csp,
    shared_by_bar,
    vendor_etag,
    weak,
)
from .share_lab import authorise, still_admitted
from .share_render import (
    MAX_RENDER_INPUT,
    RESOLVER_VERSION,
    RenderBusy,
    RenderCache,
    RenderFailed,
    RenderTooLarge,
    render_kind,
    render_text,
    sanitize_html,
    share_resolver,
    wrap_content,
)
from .share_session import (
    CLAIM_DOMAIN,
    MIN_SECRET_BYTES,
    check_csrf,
    check_session,
    check_write_cookie,
    csrf_token,
    mint_session,
    write_cookie_header,
)
from .share_store import (
    AnnotationLimits,
    ShareState,
    change_annotation,
    create_annotation,
    load_share,
    own_annotations,
    record_event,
)

log = logging.getLogger("herdr_artifact_host.public")

_ROUTE_METHODS: dict[str, tuple[str, ...]] = {
    "link": ("GET", "HEAD"),
    "file": ("GET", "HEAD"),
    "session": ("POST",),
    "lab_file": ("GET", "HEAD"),
    "lab_view": ("GET", "HEAD"),
    "annotations": ("GET", "POST"),
    "annotation_item": ("POST",),
    "signin": ("GET", "HEAD"),
}
_NO_PREFLIGHT = frozenset({"session", "lab_view", "annotations", "annotation_item"})
_CLAIM_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"
_Headers = list[tuple[str, str]]


@dataclass(frozen=True)
class PublicConfig:
    """Everything the public surface needs; lab sign-in is configured only when every part is valid (fail closed)."""

    public_url: str | None = None
    public_host: str | None = None  # /s/ is served on this Host only
    signing_secret: bytes = field(default=b"", repr=False)
    session_seconds: int = 14400
    sync_dir: Path | None = None
    pool_id: str | None = None
    client_ids: tuple[str, ...] = ()
    web_client_id: str | None = None
    cognito_domain: str | None = None
    static_dir: Path | None = None
    annotations: dict[str, bool | int] = field(default_factory=lambda: annotation_settings(None))
    owner_emails: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.public_host is not None and not valid_public_host(self.public_host):
            raise ValueError("public_host must be a lowercase DNS name (no scheme, port or path)")


@dataclass(frozen=True)
class LabSettings:
    authenticator: CognitoAuthenticator
    secret: bytes = field(repr=False)
    session_seconds: int
    static_dir: Path
    viewer_csp: str
    config_json: bytes


def lab_settings(config: PublicConfig) -> LabSettings | None:
    """The validated lab settings, or None (fail closed) when anything required is missing or malformed."""
    secret = config.signing_secret
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        return None
    cognito = https_origin(config.cognito_domain)
    public = https_origin(config.public_url)
    web_client = config.web_client_id
    if (
        cognito is None
        or public is None
        or not config.sync_dir
        or not isinstance(config.pool_id, str)
        or not isinstance(web_client, str)
        or not _CLIENT_ID.fullmatch(web_client)
        or isinstance(config.session_seconds, bool)
        or not isinstance(config.session_seconds, int)
        or config.session_seconds <= 0
    ):
        return None
    client_ids = [c for c in (*config.client_ids, web_client) if _CLIENT_ID.fullmatch(c)]
    directory = SyncedDirectory(
        str(config.sync_dir), "share-lab"
    )  # only jwks() is used: fail closed if absent
    try:
        authenticator = CognitoAuthenticator(
            pool_id=config.pool_id,
            client_ids=client_ids,
            required_group="share-lab",
            jwks_fetch=directory.jwks,
            open_signup=True,
        )
    except ValueError:
        return None
    config_json = json.dumps(
        {
            "cognito_domain": cognito,
            "client_id": web_client,
            "redirect_uri": public + "/s/callback",
        },
        sort_keys=True,
        ensure_ascii=True,
    ).encode("ascii")
    return LabSettings(
        authenticator=authenticator,
        secret=secret,
        session_seconds=config.session_seconds,
        static_dir=Path(config.static_dir) if config.static_dir else DEFAULT_STATIC_DIR,
        viewer_csp=VIEWER_CSP.format(origin=cognito, public=public),
        config_json=config_json,
    )


# ----------------------------------------------------------------------------- blocking work

BLOB_WORKERS = 8
HEALTH_DB_TIMEOUT_SECONDS = 2.0
HEALTH_HOST = "127.0.0.1"  # the health listener is never bound anywhere else
PINNED_AIOHTTP = (
    3,
    13,
    5,
)  # the exact release the manifests pin; the private attributes used below are its own
# Hashing, file reads and rendering run here, never on the loop's default executor, so a flood of large blobs cannot
# starve the sign-in verification (or anything else) that uses the default pool, and is itself bounded.
_BLOB_POOL = ThreadPoolExecutor(BLOB_WORKERS, thread_name_prefix="share-blob")
# Whole-blob hashing of a large blob is the one slow, unbounded-by-request operation, so it gets its own small pool
# AND a semaphore of the same size: at most HASH_CONCURRENCY such passes run, a request that finds them all busy is
# answered at once with the 503 (never queued), and the cheap pool above (opens, small reads, renders, chunk reads)
# can never be filled by hashing.  Blobs up to HASH_INLINE_MAX_BYTES are cheap and hash on the cheap pool, ungated.
HASH_CONCURRENCY = 4
HASH_INLINE_MAX_BYTES = 4 * 1024 * 1024
_HASH_POOL = ThreadPoolExecutor(HASH_CONCURRENCY, thread_name_prefix="share-hash")


async def run_blocking[T](fn: Callable[..., T], *args: object) -> T:
    return await asyncio.get_running_loop().run_in_executor(_BLOB_POOL, fn, *args)


def aiohttp_version_ok(version: str) -> bool:
    """True only for exactly the pinned ``X.Y.Z`` release; a newer one may have changed the internals we override."""
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return False
    return tuple(int(p) for p in parts) == PINNED_AIOHTTP


# ----------------------------------------------------------------------------- logging and responses

_log_limiter = RateLimiter(LOG_PER_MINUTE, time.monotonic)


def _log(reason: str, extra: str = "") -> None:
    """A rate-limited warning carrying a fixed reason code only (never a path, token or header value)."""
    if _log_limiter.allow(reason):
        log.warning("share-public: %s %s", reason, extra)


def respond(
    status: int,
    headers: _Headers,
    body: bytes | None = None,
    *,
    cors: bool = True,
    close: bool = False,
) -> web.Response:
    """One response with the host's fixed trimmings: our ``Server``, a default ``Referrer-Policy``, CORS when asked."""
    out: CIMultiDict[str] = CIMultiDict()
    out["Server"] = "herdr-share"
    for name, value in headers:
        out.add(name, value)
    if "Referrer-Policy" not in out:
        out["Referrer-Policy"] = "no-referrer"  # every response: the URL path may carry a session
    if cors:
        for name, value in CORS_HEADERS:
            out.add(name, value)
    response = web.Response(status=status, headers=out, body=body)
    if close:
        response.force_close()
    return response


def not_found() -> web.Response:
    """The uniform 404: identical bytes for every failure, CORS headers, ``Connection: close``."""
    # Connection is explicit: a request aiohttp rejects as HTTP/1.0 would otherwise omit it (close is its default)
    return respond(404, [*_NOT_FOUND_HEADERS, ("Connection", "close")], NOT_FOUND_BODY, close=True)


def _refused_framing(request: web.Request) -> bool:
    """``Expect`` (aiohttp would answer 100-continue or a 417 echoing the header before any routing) and any request
    ``Content-Encoding`` (a body we never want, let alone decompressed) are the uniform 404 on every route."""
    return "Expect" in request.headers or "Content-Encoding" in request.headers


async def _no_expect(request: web.Request) -> None:
    """The route-level expect handler: does nothing (no ``100 Continue``, no 417); ``handle`` refuses the request."""
    return None


def _announces_body(request: web.Request) -> bool:
    """True unless the request provably has no body: no Transfer-Encoding header at all, and either no Content-Length
    or exactly one whose value is exactly ``0``. Every occurrence is checked, so a duplicate or conflicting header
    cannot smuggle a second request past the close-on-body rule."""
    headers = request.headers
    if headers.getall("Transfer-Encoding", []):
        return True
    lengths = headers.getall("Content-Length", [])
    return bool(lengths) and lengths != ["0"]


def _single(request: web.Request, name: str) -> str | None:
    values = request.headers.getall(name, [])
    return values[0] if len(values) == 1 else None


def classify(parts: list[str]) -> str | None:
    """The route name of the request path parts (``['', 's', token, ...]``), or None."""
    n = len(parts)
    if n == 4 and parts[3] == "":
        return "link"
    if n == 4 and parts[3] == "session":
        return "session"
    if n == 4 and parts[3] == "signin":
        return "signin"
    if n >= 5 and parts[3] == "f":
        return "file"
    if n >= 6 and parts[3] == "k":
        if n >= 7 and parts[5] == "f":
            return "lab_file"
        if n >= 7 and parts[5] in ("v", "a"):
            return "lab_view"
        if n == 6 and parts[5] == "annotations":
            return "annotations"
        if n == 8 and parts[5] == "annotations" and parts[7] in ("edit", "delete"):
            return "annotation_item"
    return None


class Fault(Exception):
    """A blob that cannot be served; ``reason`` is the fixed code for the log and the ``denied`` event."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Saturated(Fault):
    """Every whole-blob hash slot is taken: the request is answered with the uniform 503 and may retry.  Only raised
    after the token resolved to a known share and the file to a bundle member, so it tells a stranger nothing."""

    def __init__(self) -> None:
        super().__init__("hash_busy")


def _client_gone(request: web.Request) -> bool:
    transport = request.transport
    return transport is None or transport.is_closing()


@dataclass(frozen=True)
class Claim:
    """A visitor this server verified from the claim cookie of a public link."""

    identity: str
    email: str
    session: str


class PublicSurface:
    """The request handlers; one instance per app."""

    def __init__(
        self,
        config: PublicConfig,
        pool: asyncpg.Pool,
        blobs: BlobStore,
        library: LibraryStore,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.pool = pool
        self.blobs = blobs
        self.library = library
        self._hash_gate = asyncio.Semaphore(HASH_CONCURRENCY)
        self.now = now
        self.lab = lab_settings(config)
        self.public_origin = https_origin(
            config.public_url
        )  # None: the sandbox CSP keeps 'self' only
        self.session_per_share = RateLimiter(SESSION_PER_SHARE_PER_MINUTE, now)
        self.session_global = RateLimiter(SESSION_GLOBAL_PER_MINUTE, now, max_keys=1)
        limits = config.annotations
        self.annotations_enabled = bool(self.lab is not None and limits["enabled"])
        self.annotate_user = RateLimiter(int(limits["per_user_per_minute"]), now)
        self.annotate_share = RateLimiter(int(limits["per_share_per_minute"]), now)
        self.annotate_global = RateLimiter(int(limits["global_per_minute"]), now, max_keys=1)
        self.annotation_limits = AnnotationLimits(
            per_user_per_day=int(limits["per_user_per_day"]),
            max_per_share=int(limits["max_per_share"]),
        )
        self._denied = RateLimiter(DENIED_PER_MINUTE, now)
        self._opened_limit = RateLimiter(OPENED_PER_MINUTE, now)
        self.render_cache = RenderCache()
        self._claims_seen: set[tuple[str, str]] = set()
        self.view_template = self._load_view_template()

    def _load_view_template(self) -> str:
        """``view.html`` once at start-up; every placeholder must occur exactly once (else the app does not start)."""
        static = Path(self.config.static_dir) if self.config.static_dir else DEFAULT_STATIC_DIR
        path = static / VIEW_FILE
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        found = sorted(m.group(1) for m in _VIEW_PLACEHOLDER.finditer(text))
        wanted = sorted(
            ["NONCE", "MODE", "CSRF_META", "SWITCH", "DATA_JSON", "PANEL", "CONTENT", "BANNER"]
        )
        if self.lab is not None and found != wanted:
            raise RuntimeError("share_static/view.html must hold each placeholder exactly once")
        return text

    # -- audit

    async def _event(self, share_id: str, kind: str, actor: str, detail: dict[str, object]) -> None:
        """One audit row; a failure is logged and never reaches the reader (the audit trail must not break serving)."""
        try:
            await record_event(self.pool, share_id, kind, actor, detail)
        except Exception:
            _log("event write failed")

    async def _opened(self, share_id: str, actor: str, detail: dict[str, object]) -> None:
        if self._opened_limit.allow(share_id):
            await self._event(share_id, "opened", actor, detail)

    async def deny(
        self, reason: str, share_id: str | None = None, actor: str = "public"
    ) -> web.Response:
        """The uniform 404, recording ``reason``: an event when the share is known, a rate-limited log line always."""
        _log("denied", reason)
        if share_id is not None and self._denied.allow(f"{share_id}\x00{reason}"):
            await self._event(share_id, "denied", actor, {"reason": reason})
        return not_found()

    async def _claimed(self, share_id: str, claim: Claim | None) -> None:
        """Keep the share in the verified visitor's library and log one ``claimed`` event per (session, share)."""
        if claim is None:
            return
        key = (share_id, hashlib.sha256(claim.session.encode("ascii", "replace")).hexdigest())
        if key in self._claims_seen:
            return
        try:
            await self.library.record_claim(
                Viewer(member_id=claim.identity, email=claim.email), share_id
            )
        except LibraryError:
            _log("claim refused")
            return
        except Exception:
            _log("claim write failed")
            return
        if len(self._claims_seen) >= CLAIM_SEEN_MAX:
            self._claims_seen.clear()
        self._claims_seen.add(key)
        await self._event(share_id, "claimed", claim.identity, {"share_id": share_id})

    # -- entry points

    async def handle(self, request: web.Request) -> web.StreamResponse:
        try:
            if _refused_framing(request):
                request["close"] = True
                return await self.deny("refused_framing")
            if _announces_body(request):
                request["close"] = (
                    True  # a request that announces a body never shares its connection with another
                )
            response = await self.route(request)
            if request.get("close") and not response.prepared:
                response.force_close()
            return response
        except Exception as exc:  # never a traceback (it could hold the path): one fixed line, then the uniform 404
            _log("internal_error", type(exc).__name__)
            if request.get("started"):
                if request.transport is not None:
                    request.transport.close()
                return web.Response()
            return not_found()

    def host_ok(self, request: web.Request) -> bool:
        """With a configured ``public_host`` the request must carry exactly one Host naming it."""
        wanted = self.config.public_host
        if wanted is None:
            return True
        hosts = request.headers.getall("Host", [])
        if len(hosts) != 1:
            return False
        host = hosts[0].strip().lower()
        return host == wanted or host == wanted + ":443"

    async def route(self, request: web.Request) -> web.StreamResponse:
        if not self.host_ok(request):
            return await self.deny("wrong_host")
        raw = request.raw_path
        if len(raw) > MAX_RAW_PATH or not raw.startswith("/s/"):
            return await self.deny("unknown_route")
        raw_path = raw.split("?", 1)[0]
        if not RAW_PATH_OK.fullmatch(raw):
            return await self.deny("bad_path")
        parts = raw_path.split("/")
        method = request.method
        if parts[2] in ("static", "callback"):
            return await self.viewer_asset(request, parts)
        route = classify(parts)
        if method == "OPTIONS":
            if route in _NO_PREFLIGHT:
                return await self.deny(
                    "method"
                )  # no preflight: a foreign origin cannot send Authorization or CSRF
            return respond(204, list(PREFLIGHT_HEADERS))
        if len(parts) == 3 and method in ("GET", "HEAD") and TOKEN_RE.fullmatch(parts[2]):
            # a link pasted without its trailing slash: answered for ANY well-formed token without looking it up,
            # so the redirect says nothing about which tokens exist
            return self.redirect(parts[2] + "/")
        if len(parts) < 4 or not TOKEN_RE.fullmatch(parts[2]):
            return await self.deny("bad_token")
        if route is None:
            return await self.deny("unknown_route")
        if method not in _ROUTE_METHODS[route]:
            return await self.deny("method")
        token = parts[2]
        loaded = await load_share(self.pool, token, self.now())
        state = loaded.state
        if state is None:
            return await self.deny(loaded.reason, loaded.share_id)
        audience = state["audience"]
        if audience == "public" and route in ("link", "file", "signin", "session"):
            if route == "signin":
                return await self.signin_page(state)
            if route == "session":
                return await self.claim_session(request, state, token)
            if route == "link":
                return self.redirect("f/" + quote(state["entry"], safe="/"))
            claim = self.claim_identity(request, state)
            rel = "/".join(parts[4:])
            return await self.serve_file(
                request,
                state,
                rel,
                token=token,
                landing=self.landing(request, state, rel, token, signed_in=claim is not None),
                claim=claim,
            )
        if audience != "lab" or route in ("file", "signin"):
            return await self.deny("audience", state["share_id"])  # lab files only under a session
        lab = self.lab
        if lab is None:
            return await self.deny("lab_unconfigured", state["share_id"])
        if route == "link":
            return await self.send_static(request, lab, VIEWER_FILE, "text/html; charset=utf-8")
        if route == "session":
            return await self.lab_session(request, state, token, lab)
        session = parts[4]
        got = (
            check_session(lab.secret, state["share_id"], session, self.now())
            if _SESSION_SEGMENT.fullmatch(session)
            else None
        )
        if got is None:
            return await self.deny("bad_session", state["share_id"])
        actor = got["identity"]
        enabled = self.annotations_enabled
        if route == "lab_file":
            rel = "/".join(parts[6:])
            link = self.mode_switch(request, rel) if enabled else None
            return await self.serve_file(
                request,
                state,
                rel,
                actor=actor,
                token=token,
                mode_switch=link,
                sandbox_pdf=True,
                landing=self.landing(request, state, rel, token, signed_in=True),
                claim=None,  # a lab share is decided by its allow-list, never claimed
            )
        if route == "lab_view":
            if parts[5] == "v":
                return await self.view(request, state, parts, got, "live", lab)
            if not enabled:
                return await self.deny("annotations_disabled", state["share_id"], actor)
            return await self.view(request, state, parts, got, "annotate", lab)
        if not enabled:
            return await self.deny("annotations_disabled", state["share_id"], actor)
        if route == "annotations" and method == "GET":
            if not self.has_write_cookie(request, state, session, lab):
                return await self.deny(
                    "no_write_cookie", state["share_id"], actor
                )  # the URL alone is no credential
            return await self.annotations_get(state, got)
        return await self.annotation_post(request, state, parts, got, lab)

    @staticmethod
    def redirect(location: str) -> web.Response:
        return respond(
            302,
            [
                ("Location", location),
                ("Content-Length", "0"),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Robots-Tag", "noindex"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )

    # -- blobs

    async def fetch(
        self, request: web.Request, item: dict[str, object], *, force: bool = False
    ) -> tuple[BinaryIO, bool]:
        """The hash-verified blob of a manifest ``item`` and whether a hash pass was made for this request.

        A blob whose exact file identity (inode, size, mtime, ctime) was verified before is not hashed again unless
        ``force``; the caller upgrades with ``ensure_full`` before it serves the whole body. Raises ``Fault``
        (``Saturated`` when no hash slot is free)."""
        sha = str(item["sha256"])
        try:
            handle = await run_blocking(self.blobs.open_handle, sha)
        except BlobMissing:
            raise Fault("missing_blob") from None
        except (InvalidSha, OSError):
            raise Fault("blob_unreadable") from None
        try:
            hashed = force or not await run_blocking(self.blobs.is_verified, handle, sha)
            if hashed:  # first: a damaged file is "blob_corrupt", whatever its size has become
                await self._hash(request, handle, item)
            if os.fstat(handle.fileno()).st_size != item["size"]:
                raise Fault("size_mismatch")
        except BaseException:
            handle.close()
            raise
        return handle, hashed

    async def _hash(self, request: web.Request, handle: BinaryIO, item: dict[str, object]) -> None:
        """One whole-blob hash pass, gated (large blobs), abandoned when the client has gone."""
        sha = str(item["sha256"])

        def run() -> bool:
            return self.blobs.verify_open(handle, sha, should_stop=lambda: _client_gone(request))

        large = (
            os.fstat(handle.fileno()).st_size > HASH_INLINE_MAX_BYTES
        )  # the real size, not the manifest's
        try:
            if not large:
                await run_blocking(run)
                return
            if self._hash_gate.locked():
                raise Saturated
            async with self._hash_gate:
                if _client_gone(request):
                    raise Fault("client_gone")
                await asyncio.get_running_loop().run_in_executor(_HASH_POOL, run)
        except BlobAborted:
            raise Fault("client_gone") from None
        except BlobCorrupt:
            raise Fault("blob_corrupt") from None
        except OSError:
            raise Fault("blob_unreadable") from None

    async def ensure_full(
        self, request: web.Request, handle: BinaryIO, item: dict[str, object], hashed: bool
    ) -> None:
        """Before a whole body is served: hash it now unless this very request already did (cost follows bytes)."""
        if hashed:
            return
        await self._hash(request, handle, item)

    async def fault_response(self, fault: Fault, share_id: str, actor: str) -> web.Response:
        """The answer for a ``Fault``: the uniform 503 when only capacity is short, otherwise the uniform 404."""
        if isinstance(fault, Saturated):
            _log("busy", fault.reason)
            return self.unavailable()
        return await self.deny(fault.reason, share_id, actor)

    @staticmethod
    async def read_blob(handle: BinaryIO, size: int) -> bytes:
        data = await run_blocking(handle.read, size)
        if len(data) != size:
            raise Fault("size_mismatch")
        return data

    # -- the recipient landing bar and the claim session of a public link

    def landing_bar(
        self, state: ShareState, token: str, *, signed_in: bool, margin_top: int
    ) -> bytes:
        return landing_bar(
            state["shared_by"],
            state["title"],
            state["expires"],
            self.now(),
            token=token,
            signed_in=signed_in,
            can_sign_in=state["audience"] == "public",
            margin_top=margin_top,
        )

    def landing(
        self, request: web.Request, state: ShareState, rel: str, token: str, *, signed_in: bool
    ) -> bytes | None:
        """The bar for an HTML document navigation (never a subresource), or None; needs lab sign-in configured."""
        if self.lab is None or request.headers.getall("Sec-Fetch-Dest", []) != ["document"]:
            return None
        name = decode_rel_path(rel)
        if name is None or content_type_for(name).split(";")[0].strip() != "text/html":
            return None
        return self.landing_bar(state, token, signed_in=signed_in, margin_top=38)

    def claim_identity(self, request: web.Request, state: ShareState) -> Claim | None:
        """The verified visitor of this public link's claim cookie, or None (signed out, forged, expired)."""
        lab = self.lab
        if lab is None:
            return None
        values = []
        for header in request.headers.getall("Cookie", []):
            for piece in header.split(";"):
                name, _, value = piece.strip().partition("=")
                if name == CLAIM_COOKIE:
                    values.append(value)
        if len(values) != 1 or not _SESSION_SEGMENT.fullmatch(values[0]):
            return None
        got = check_session(lab.secret, state["share_id"], values[0], self.now(), CLAIM_DOMAIN)
        return None if got is None else Claim(got["identity"], got["email"], values[0])

    async def signin_page(self, state: ShareState) -> web.Response:
        """The sign-in step of a public link: the static viewer page with this share's bar data."""
        lab = self.lab
        if lab is None:
            return await self.deny("lab_unconfigured", state["share_id"])
        page = await self.read_static(lab, (), VIEWER_FILE, STATIC_MAX_BYTES)
        if page is None:
            return await self.deny("static_missing")
        data = json.dumps(
            {"sharer": state["shared_by"], "title": state["title"], "expiresAt": state["expires"]},
            sort_keys=True,
            ensure_ascii=True,
        )
        for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
            data = data.replace(ch, esc)
        block = f'<script type="application/json" id="herdr-landing">{data}</script>'.encode(
            "ascii"
        )
        return self.viewer_bytes(
            lab, page.replace(b"<!--herdr-landing-->", block, 1), "text/html; charset=utf-8"
        )

    async def verify_tokens(self, request: web.Request, lab: LabSettings) -> tuple[str, str] | None:
        """``(access_token, id_token)`` from exactly one ``Authorization: Bearer`` and one ``X-Id-Token``, else None."""
        authorization, id_token = _single(request, "Authorization"), _single(request, "X-Id-Token")
        if authorization is None or id_token is None:
            return None
        scheme, _, access_token = authorization.strip().partition(" ")
        if scheme.lower() != "bearer":
            return None
        return access_token.strip(), id_token.strip()

    @staticmethod
    def json_ok(body: dict[str, object], cookie: str) -> web.Response:
        return respond(
            200,
            [
                ("Content-Type", "application/json"),
                ("Set-Cookie", cookie),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Robots-Tag", "noindex"),
                ("Content-Security-Policy", _CLAIM_CSP),
            ],
            json.dumps(body, sort_keys=True, ensure_ascii=True).encode("ascii"),
            cors=False,
        )

    async def claim_session(
        self, request: web.Request, state: ShareState, token: str
    ) -> web.Response:
        """Exchange verified Cognito tokens for the claim cookie of a public link. No allow-list applies (the link is
        public); the cookie only says who the verified visitor is. Never echoes a token; every failure is the 404."""
        share_id = state["share_id"]
        lab = self.lab
        if lab is None:
            return await self.deny("lab_unconfigured", share_id)
        if _announces_body(request):
            return await self.deny("body", share_id)
        if not self.session_per_share.allow(share_id) or not self.session_global.allow("*"):
            return await self.deny("rate_limited", share_id)
        tokens = await self.verify_tokens(request, lab)
        if tokens is None:
            return await self.deny("bad_headers", share_id)
        access_token, id_token = tokens

        def verify() -> tuple[str | None, float | None, str | None]:
            try:
                claims = (
                    lab.authenticator.verify_claims(access_token)
                    if access_token.isascii()
                    else None
                )
                user = claims.get("user") if claims else None
                exp = claims.get("exp") if claims else None
                email = (
                    lab.authenticator.verify_id_token(id_token, user=user)
                    if isinstance(user, str) and id_token.isascii()
                    else None
                )
                return user, exp, email
            except Exception:
                return None, None, None

        user, exp, email = await asyncio.to_thread(verify)
        if (
            not isinstance(user, str)
            or not isinstance(exp, float)
            or not isinstance(email, str)
            or not email
        ):
            return await self.deny(
                "bad_access_token" if not isinstance(user, str) else "bad_id_token", share_id
            )
        now = self.now()
        until = min(now + lab.session_seconds, exp)
        if state["expires"] is not None:
            until = min(until, state["expires"])
        until_int = int(until // 1)
        if until_int <= now:
            return await self.deny("token_expired", share_id, user)
        try:
            session = mint_session(
                lab.secret, share_id, user, until_int, email=email, basis="g", domain=CLAIM_DOMAIN
            )
        except ValueError:
            return await self.deny("bad_identity", share_id, user)
        cookie = (
            f"{CLAIM_COOKIE}={session}; Max-Age={until_int - int(now)}; Path=/s/{token}/; HttpOnly; Secure; "
            "SameSite=Lax"
        )
        return self.json_ok({"landing": f"/s/{token}/", "expires_at": until_int}, cookie)

    async def lab_session(
        self, request: web.Request, state: ShareState, token: str, lab: LabSettings
    ) -> web.Response:
        """Exchange verified Cognito tokens for a lab session path. Never echoes a token; every failure is the 404."""
        share_id = state["share_id"]
        if _announces_body(request):
            return await self.deny("body", share_id)
        if not self.session_per_share.allow(share_id):
            return await self.deny("rate_limited", share_id)
        if not self.session_global.allow(
            "*"
        ):  # charged only by resolved live lab shares: an unknown
            return await self.deny(
                "rate_limited", share_id
            )  # token cannot drain it and lock every share out
        tokens = await self.verify_tokens(request, lab)
        if tokens is None:
            return await self.deny("bad_headers", share_id)
        grant, reason, identity = await asyncio.to_thread(
            authorise, state, tokens[0], tokens[1], lab.authenticator
        )
        if grant is None:
            return await self.deny(reason, share_id, identity or "public")
        now = self.now()
        exp = min(now + lab.session_seconds, grant["exp"])
        if state["expires"] is not None:
            exp = min(exp, state["expires"])
        exp_int = int(exp // 1)
        if exp_int <= now:
            return await self.deny("token_expired", share_id, grant["identity"])
        try:
            session = mint_session(
                lab.secret,
                share_id,
                grant["identity"],
                exp_int,
                email=grant["email"],
                basis=grant["basis"],
            )
        except ValueError:
            return await self.deny("bad_identity", share_id, grant["identity"])
        except RuntimeError:
            log.error("cryptography missing for lab sessions: no session can be minted")
            return await self.deny("session_unavailable", share_id, grant["identity"])
        entry = state["entry"]
        kind = "f" if content_type_for(entry).split(";")[0].strip() == "text/html" else "v"
        landing = f"/s/{token}/k/{session}/{kind}/{quote(entry, safe='/')}"
        cookie = write_cookie_header(lab.secret, share_id, token, session, exp_int - int(now))
        return self.json_ok({"landing": landing, "expires_at": exp_int}, cookie)

    @staticmethod
    def mode_switch(request: web.Request, rel: str) -> str | None:
        """The link to inject into this lab ``f/`` response, or None: only HTML, only a document navigation."""
        if request.headers.getall("Sec-Fetch-Dest", []) != ["document"]:
            return None
        name = decode_rel_path(rel)
        if name is None or content_type_for(name).split(";")[0].strip() != "text/html":
            return None
        return mode_switch_link(rel.count("/") + 1, rel)

    # -- bundle files

    async def serve_file(
        self,
        request: web.Request,
        state: ShareState,
        rel_path: str,
        *,
        actor: str = "public",
        token: str | None = None,
        mode_switch: str | None = None,
        sandbox_pdf: bool = False,
        landing: bytes | None = None,
        claim: Claim | None = None,
    ) -> web.StreamResponse:
        """Serve ``rel_path`` (raw, still percent-encoded) from the share's version; the uniform 404 on any failure.

        Handles HEAD, If-Match (412), If-None-Match (304), If-Range and a single Range (206/416); a multi-range request
        gets the full 200. ``mode_switch`` (a lab document navigation of an HTML file) and ``landing`` are links injected
        into HTML: the response is then the full 200 of the modified bytes under a suffixed ETag, no ranges."""
        share_id = state["share_id"]
        name = decode_rel_path(rel_path)
        if name is None:
            return await self.deny("bad_path", share_id, actor)
        item = state.index.get(name)
        if item is None:
            return await self.deny("not_in_bundle", share_id, actor)
        try:
            handle, hashed = await self.fetch(request, item)
        except Fault as fault:
            return await self.fault_response(fault, share_id, actor)
        try:
            return await self._serve_handle(
                request,
                state,
                name,
                item,
                handle,
                actor,
                token,
                mode_switch,
                sandbox_pdf,
                landing,
                claim,
                hashed,
            )
        except Fault as fault:
            return await self.fault_response(fault, share_id, actor)
        finally:
            handle.close()

    async def _serve_handle(
        self,
        request: web.Request,
        state: ShareState,
        name: str,
        item: dict[str, object],
        handle: BinaryIO,
        actor: str,
        token: str | None,
        mode_switch: str | None,
        sandbox_pdf: bool,
        landing: bytes | None,
        claim: Claim | None,
        hashed: bool,
    ) -> web.StreamResponse:
        share_id = state["share_id"]
        size = int(item["size"])  # type: ignore[call-overload]
        etag = f'"{item["sha256"]}"'
        csp = sandbox_csp(self.public_origin, token)
        is_html = content_type_for(name).split(";")[0].strip() == "text/html"
        bar = shared_by_bar(state["shared_by"]) if is_html else b""
        if is_html and (bar or mode_switch is not None or landing):
            if size > MODE_SWITCH_MAX_BYTES:
                if bar:  # a labelled page is never served unlabelled; over the limit it cannot be labelled
                    return await self.deny("html_too_large", share_id, actor)
            else:
                await self.ensure_full(
                    request, handle, item, hashed
                )  # the modified body is a whole-body serve
                hashed = True
                data = await self.read_blob(handle, size)
                link = (mode_switch.encode("ascii") if mode_switch is not None else b"") + (
                    landing or b""
                )
                injected: bytes | None = None
                if bar:
                    injected = labelled_html(data, content_type_for(name), bar, link)
                    if injected is None:  # fail closed: a labelled page is never served unlabelled
                        return await self.deny("banner_unavailable", share_id, actor)
                elif ascii_compatible(content_type_for(name), data):
                    injected = inject_mode_switch(data, link)
                if injected is not None:
                    suffix = (
                        (SHARED_BY_ETAG_SUFFIX if bar else "")
                        + (MODE_SWITCH_ETAG_SUFFIX if mode_switch is not None else "")
                        + (landing_tag(landing) if landing else "")
                    )
                    return await self.serve_injected(
                        request, state, name, item, injected, csp, actor, suffix, claim
                    )
                handle.seek(0)
        headers = content_headers(name, etag, csp, sandbox_pdf=sandbox_pdf)
        if_match = request.headers.get("If-Match")
        if if_match is not None:
            tags = parse_etags(if_match)
            if "*" not in tags and etag not in tags:
                return respond(
                    412,
                    [
                        ("Content-Length", "0"),
                        ("X-Content-Type-Options", "nosniff"),
                        ("Cache-Control", "no-store"),
                    ],
                )
        if_none_match = request.headers.get("If-None-Match")
        if if_none_match is not None:
            tags = [weak(t) for t in parse_etags(if_none_match)]
            if "*" in tags or etag in tags:
                return respond(304, [h for h in headers if h[0] != "Content-Type"])
        start, end, status = 0, size - 1, 200
        range_value = request.headers.get("Range")
        if_range = request.headers.get("If-Range")
        if range_value is not None and (if_range is None or if_range.strip() == etag):
            parsed = parse_range(range_value, size)
            if parsed == "unsatisfiable":
                return respond(
                    416,
                    [
                        ("Content-Range", f"bytes */{size}"),
                        ("Content-Length", "0"),
                        ("X-Content-Type-Options", "nosniff"),
                        ("Cache-Control", "no-store"),
                    ],
                )
            if parsed is not None:
                start, end = parsed
                status = 206
                headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
        length = end - start + 1 if size else 0
        if request.method == "GET" and status == 200:
            await self.ensure_full(
                request, handle, item, hashed
            )  # a whole-body stream is always hash-verified
        headers.append(("Content-Length", str(length)))
        if request.method == "GET" and status == 200 and name == state["entry"]:
            await self._opened(share_id, actor, {"file": name[:200]})
            await self._claimed(share_id, claim)
        return await self.stream(request, status, headers, handle, start, length)

    async def stream(
        self,
        request: web.Request,
        status: int,
        headers: _Headers,
        handle: BinaryIO,
        start: int,
        length: int,
    ) -> web.StreamResponse:
        """Headers, then ``length`` bytes of ``handle`` from ``start`` in chunks; a vanished client ends the stream."""
        out = respond(status, headers, None).headers
        response = web.StreamResponse(status=status, headers=out)
        for name in ("Content-Length",):
            response.headers[name] = str(length)
        if request.get("close"):
            response.force_close()
        request["started"] = True
        await response.prepare(request)
        if request.method != "HEAD":
            try:
                await run_blocking(handle.seek, start)
                remaining = length
                while remaining > 0:
                    chunk = await run_blocking(handle.read, min(1024 * 1024, remaining))
                    if (
                        not chunk
                    ):  # the file shrank underneath us: the length promised cannot be kept
                        response.force_close()
                        break
                    await response.write(chunk)
                    remaining -= len(chunk)
            except (OSError, ValueError, ConnectionError):
                response.force_close()
                if request.transport is not None:
                    request.transport.close()
        try:
            await response.write_eof()
        except (OSError, ConnectionError):
            pass
        return response

    async def serve_injected(
        self,
        request: web.Request,
        state: ShareState,
        name: str,
        item: dict[str, object],
        body: bytes,
        csp: str,
        actor: str,
        etag_suffix: str,
        claim: Claim | None,
    ) -> web.Response:
        """The full 200 of ``body`` (an HTML file with the bar and/or mode-switch link injected)."""
        etag = f'"{item["sha256"]}{etag_suffix}"'
        headers = [h for h in content_headers(name, etag, csp) if h[0] != "Accept-Ranges"]
        headers.append(("Vary", "Sec-Fetch-Dest"))
        if_match = request.headers.get("If-Match")
        if if_match is not None:
            tags = parse_etags(if_match)
            if "*" not in tags and etag not in tags:
                return respond(
                    412,
                    [
                        ("Content-Length", "0"),
                        ("X-Content-Type-Options", "nosniff"),
                        ("Cache-Control", "no-store"),
                    ],
                )
        if_none_match = request.headers.get("If-None-Match")
        if if_none_match is not None:
            tags = [weak(t) for t in parse_etags(if_none_match)]
            if "*" in tags or etag in tags:
                return respond(304, [h for h in headers if h[0] != "Content-Type"])
        if request.method == "GET" and name == state["entry"]:
            await self._opened(state["share_id"], actor, {"file": name[:200]})
            await self._claimed(state["share_id"], claim)
        return respond(200, headers, body)

    # -- lab: live and annotate views

    def unavailable(self) -> web.Response:
        """The uniform 503 (a render is busy): fixed body, no detail."""
        return respond(
            503,
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Retry-After", "5"),
                ("X-Content-Type-Options", "nosniff"),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Robots-Tag", "noindex"),
            ],
            UNAVAILABLE_BODY,
            cors=False,
        )

    async def view(
        self,
        request: web.Request,
        state: ShareState,
        parts: list[str],
        got: dict[str, object],
        mode: str,
        lab: LabSettings,
    ) -> web.Response:
        """``v/<path>`` (live) and ``a/<path>`` (annotate): one template, our own markup around the static render.

        ``v/`` of an HTML file is the 404 (its live view is ``f/``). ``a/`` serves every kind: HTML is sanitised in the
        isolated render child, text is escaped, image / PDF / other kinds are built by ``annotate.js`` from the data."""
        token, share_id, actor = parts[2], state["share_id"], str(got["identity"])
        origin = self.public_origin
        name = decode_rel_path("/".join(parts[6:]))
        item = state.index.get(name) if name is not None else None
        if origin is None or name is None or item is None:
            return await self.deny(
                "not_in_bundle" if origin is not None else "invalid_state", share_id, actor
            )
        kind = render_kind(name)
        if kind == "html" and mode == "live":
            return await self.deny("not_in_mode", share_id, actor)
        try:
            handle, _ = await self.fetch(request, item, force=True)  # the whole blob is rendered
        except Fault as fault:
            return await self.fault_response(fault, share_id, actor)
        try:
            content, shown = "", kind
            if kind in ("text", "html"):
                if int(item["size"]) > MAX_RENDER_INPUT:  # type: ignore[call-overload]
                    content, shown = self.too_large(), "document"
                else:
                    data = await self.read_blob(handle, int(item["size"]))  # type: ignore[call-overload]
                    try:
                        content = await run_blocking(
                            self.static_copy,
                            kind,
                            share_id,
                            name,
                            str(item["sha256"]),
                            data,
                            state.index,
                        )
                    except RenderTooLarge:
                        content, shown = self.too_large(), "document"
                    except RenderBusy:
                        return self.unavailable()
                    except RenderFailed:
                        return await self.deny("render_failed", share_id, actor)
        except Fault as fault:
            return await self.fault_response(fault, share_id, actor)
        finally:
            handle.close()
        nonce, body = self.render_view(state, parts, got, name, shown, content, mode, lab)
        csp = (
            ANNOTATE_CSP.format(origin=origin, token=token, nonce=nonce)
            if mode == "annotate"
            else VIEW_CSP.format(origin=origin, token=token, nonce=nonce)
        )
        headers = [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Security-Policy", csp),
            ("X-Frame-Options", "DENY"),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Robots-Tag", "noindex"),
        ]
        if mode == "annotate":
            headers += [
                ("Cross-Origin-Opener-Policy", "same-origin"),
                ("Cross-Origin-Resource-Policy", "same-origin"),
            ]
        if request.method == "GET" and name == state["entry"]:
            await self._opened(share_id, actor, {"file": name[:200], "mode": mode})
        return respond(200, headers, body, cors=False)

    @staticmethod
    def too_large() -> str:
        return f'<p class="herdr-static-note">{html.escape(TOO_LARGE_NOTE)}</p>'

    def static_copy(
        self,
        kind: str,
        share_id: str,
        name: str,
        sha256: str,
        data: bytes,
        index: dict[str, dict[str, object]],
    ) -> str:
        """The wrapped static render of ``data`` (raises ``RenderTooLarge`` / ``RenderBusy`` / ``RenderFailed``).

        HTML: the sanitised output depends on the bytes, the file name and WHICH names the bundle holds (the resolver
        drops links to absent files), so the cache key names all of them plus the share and the resolver version."""
        if kind == "html":
            fingerprint = hashlib.sha256("\0".join(sorted(index)).encode("utf-8")).hexdigest()
            key = (share_id, name, sha256, RESOLVER_VERSION, fingerprint)
            return wrap_content(
                "html", sanitize_html(data, resolve=share_resolver(name, index), cache_key=key)
            )
        cached = self.render_cache.get(sha256, "text\0" + name)
        if cached is None:
            cached = wrap_content("text", render_text(data))
            self.render_cache.put(sha256, "text\0" + name, cached)
        return cached

    def render_view(
        self,
        state: ShareState,
        parts: list[str],
        got: dict[str, object],
        name: str,
        kind: str,
        content: str,
        mode: str,
        lab: LabSettings,
    ) -> tuple[str, bytes]:
        """``(nonce, page)``: ``view.html`` with each placeholder replaced once, in one pass (so no substituted value is
        ever re-scanned). Every value of ours is escaped or JSON-encoded; ``content`` is the only untrusted markup and
        it is already sanitised and sits inside a shadow root."""
        up = "../" * (name.count("/") + 1)
        quoted = quote(name, safe="/")
        nonce = secrets.token_urlsafe(NONCE_BYTES)
        annotate = mode == "annotate"
        live_href = up + ("f/" if render_kind(name) == "html" else "v/") + quoted
        links = [
            f'<a data-mode="live" href="{html.escape(live_href)}"'
            + ("" if annotate else ' aria-current="page"')
            + ">Live</a>"
        ]
        if self.annotations_enabled:
            links.append(
                f'<a data-mode="annotate" href="{html.escape(up + "a/" + quoted)}"'
                + (' aria-current="page"' if annotate else "")
                + ">Annotate</a>"
            )
        data: dict[str, object] = {
            "file": name,
            "kind": kind,
            "mode": mode,
            "entry": state["entry"],
            "expires_at": int(float(got["exp"])),  # type: ignore[arg-type]
            "max_comment_chars": self.config.annotations["max_comment_chars"],
            "raw_href": up + "f/" + quoted,
        }
        if annotate and kind == "pdf":
            data["pdfjs"] = PDFJS_CONFIG
        data_json = json.dumps(data, sort_keys=True, ensure_ascii=True)
        for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
            data_json = data_json.replace(ch, esc)
        csrf = panel = ""
        if annotate:
            value = csrf_token(lab.secret, state["share_id"], parts[4])
            csrf = f'<meta name="herdr-annotate-csrf" content="{html.escape(value)}">'
            panel = '<aside data-annotate-panel aria-label="Annotations"></aside>'
        banner = (
            f'<aside class="herdr-shared-by" role="note">Shared by {html.escape(state["shared_by"])} — content '
            "was not reviewed by the operator</aside>"
        ) + self.landing_bar(state, parts[2], signed_in=True, margin_top=0).decode("utf-8")
        values = {
            "BANNER": banner,
            "NONCE": nonce,
            "MODE": mode,
            "CSRF_META": csrf,
            "PANEL": panel,
            "SWITCH": '<nav data-mode-switch aria-label="View mode">' + "".join(links) + "</nav>",
            "DATA_JSON": f'<script type="application/json" id="herdr-view-data" nonce="{nonce}">{data_json}</script>',
            "CONTENT": content,
        }
        page = _VIEW_PLACEHOLDER.sub(lambda m: values[m.group(1)], self.view_template)
        return nonce, page.encode("utf-8")

    # -- lab: annotations API

    @staticmethod
    def json_response(status: int, payload: object) -> web.Response:
        # "<" is escaped so that a viewer's text never appears as markup in any body this host sends; the escape is
        # plain JSON, so every client parses the same value.
        body = (
            json.dumps(payload, sort_keys=True, ensure_ascii=True)
            .replace("<", "\\u003c")
            .encode("ascii")
        )
        return respond(status, [*JSON_HEADERS], body, cors=False)

    async def annotate_error(
        self, state: ShareState, got: dict[str, object], status: int, code: str
    ) -> web.Response:
        """A JSON refusal (400/409/429/503) that also leaves a ``denied`` event with a fixed reason code."""
        reason = "annotate_" + code
        if self._denied.allow(f"{state['share_id']}\x00{reason}"):
            await self._event(state["share_id"], "denied", str(got["identity"]), {"reason": reason})
        return self.json_response(status, {"error": code})

    @staticmethod
    def has_write_cookie(
        request: web.Request, state: ShareState, session: str, lab: LabSettings
    ) -> bool:
        return check_write_cookie(
            lab.secret, state["share_id"], session, request.headers.getall("Cookie", [])
        )

    async def annotations_get(self, state: ShareState, got: dict[str, object]) -> web.Response:
        """The caller's OWN annotations; never another viewer's."""
        try:
            rows = await own_annotations(self.pool, state["share_id"], str(got["identity"]))
        except Exception:
            return await self.annotate_error(state, got, 503, "unavailable")
        return self.json_response(200, {"annotations": rows})

    def gate_ok(
        self, request: web.Request, state: ShareState, session: str, lab: LabSettings
    ) -> bool:
        """Every POST precondition except the body: Origin, CSRF, content type, framing and the write cookie."""
        headers = request.headers
        if headers.getall("Transfer-Encoding", []):
            return False
        origin, csrf, ctype = (
            _single(request, n) for n in ("Origin", "X-Herdr-Annotate", "Content-Type")
        )
        lengths = headers.getall("Content-Length", [])
        expected = self.public_origin
        if (
            expected is None
            or origin is None
            or origin != expected
            or csrf is None
            or ctype is None
            or len(lengths) != 1
            or not _CONTENT_LENGTH.fullmatch(lengths[0])
            or not 1 <= int(lengths[0]) <= MAX_BODY_BYTES
        ):
            return False
        if ctype != "application/json":  # exactly, case-sensitive, no parameters
            return False
        return check_csrf(lab.secret, state["share_id"], session, csrf) and self.has_write_cookie(
            request, state, session, lab
        )

    async def annotation_post(
        self,
        request: web.Request,
        state: ShareState,
        parts: list[str],
        got: dict[str, object],
        lab: LabSettings,
    ) -> web.Response:
        share_id, identity = state["share_id"], str(got["identity"])
        request["close"] = (
            True  # the body is read once; the connection never carries a second request
        )
        if not self.gate_ok(request, state, parts[4], lab):
            return await self.deny("annotate_refused", share_id, identity)
        if not still_admitted(state, got["email"], got["basis"]):
            return await self.deny(
                "not_on_list", share_id, identity
            )  # removed from the allow-list since the session
        route_edit = len(parts) == 8
        annotation_id = parts[6] if route_edit else None
        if route_edit and not _annotation_id_ok(str(annotation_id)):
            return await self.deny("annotate_refused", share_id, identity)
        if not (
            self.annotate_user.allow(f"{share_id}\x00{identity}")
            and self.annotate_share.allow(share_id)
            and self.annotate_global.allow("*")
        ):
            return await self.annotate_error(state, got, 429, "rate_limited")
        length = int(request.headers["Content-Length"])
        try:
            raw = await request.content.readexactly(length)
        except (asyncio.IncompleteReadError, OSError, ConnectionError):
            return await self.deny("annotate_refused", share_id, identity)
        max_chars = int(self.config.annotations["max_comment_chars"])
        email, basis = str(got["email"]), str(got["basis"])
        try:
            if not route_edit:
                anchor, comment = parse_annotation_request(raw, max_chars=max_chars)
                if anchor["file"] not in state.index:
                    return await self.annotate_error(state, got, 400, "bad_file")
                new_id = "ann_" + secrets.token_urlsafe(16)
                written = await create_annotation(
                    self.pool,
                    share_id=share_id,
                    annotation_id=new_id,
                    identity=identity,
                    email=email,
                    basis=basis,
                    anchor=anchor,
                    body=comment,
                    owner_emails=self.config.owner_emails,
                    limits=self.annotation_limits,
                )
                if written.outcome == "quota":
                    return await self.annotate_error(state, got, 429, "quota")
                if written.outcome != "ok" or written.created_at is None:
                    return await self.deny("annotate_" + written.outcome, share_id, identity)
                return self.json_response(
                    201,
                    {
                        "annotation": {
                            "annotation_id": new_id,
                            "anchor": anchor,
                            "body": comment,
                            "status": "submitted",
                            "created_at": written.created_at.isoformat(),
                        }
                    },
                )
            verb = parts[7]
            new_body: str | None = None
            if verb == "edit":
                new_body = parse_edit_request(raw, max_chars=max_chars)
            else:
                parse_delete_request(raw)
        except AnnotationInputError as exc:
            return await self.annotate_error(state, got, 400, exc.code)
        except Exception:
            _log("annotation write failed")
            return await self.annotate_error(state, got, 503, "unavailable")
        try:
            changed = await change_annotation(
                self.pool,
                share_id=share_id,
                annotation_id=str(annotation_id),
                identity=identity,
                email=email,
                basis=basis,
                new_body=new_body,
            )
        except Exception:
            _log("annotation write failed")
            return await self.annotate_error(state, got, 503, "unavailable")
        if changed.outcome == "not_editable":
            return await self.annotate_error(state, got, 409, "not_editable")
        if changed.outcome != "ok":
            return await self.deny("annotate_" + changed.outcome, share_id, identity)
        return self.json_response(202, {"annotation_id": annotation_id, "status": "pending"})

    # -- lab: viewer page and static assets

    async def viewer_asset(self, request: web.Request, parts: list[str]) -> web.Response:
        if request.method not in ("GET", "HEAD"):
            return await self.deny("method")
        lab = self.lab
        if lab is None:
            return await self.deny("lab_unconfigured")
        if len(parts) == 3 and parts[2] == "callback":
            return await self.send_static(request, lab, VIEWER_FILE, "text/html; charset=utf-8")
        if len(parts) == 4 and parts[2] == "static":
            name = parts[3]
            if name == "config.json":
                return self.viewer_bytes(lab, lab.config_json, "application/json")
            if name in STATIC_FILES:
                return await self.send_static(request, lab, name, STATIC_FILES[name])
        if len(parts) == 5 and parts[2:4] == ["static", "fonts"] and parts[4] in FONT_FILES:
            return await self.send_static(
                request, lab, parts[4], FONT_FILES[parts[4]], subdir=("fonts",)
            )
        if (
            len(parts) == 6
            and parts[2:5] == ["static", "vendor", "pdfjs"]
            and parts[5] in VENDOR_FILES
        ):
            return await self.send_static(
                request, lab, parts[5], VENDOR_FILES[parts[5]], subdir=("vendor", "pdfjs")
            )
        return await self.deny("unknown_route")

    @staticmethod
    async def read_static(
        lab: LabSettings, subdir: tuple[str, ...], name: str, cap: int
    ) -> bytes | None:
        def read() -> bytes | None:
            opened = open_beneath(lab.static_dir, [*subdir, name], max_size=cap)
            if opened is None:
                return None
            fd, st = opened
            try:
                return read_all(fd, st.st_size)
            finally:
                os.close(fd)

        return await run_blocking(read)

    async def send_static(
        self,
        request: web.Request,
        lab: LabSettings,
        name: str,
        content_type: str,
        subdir: tuple[str, ...] = (),
    ) -> web.Response:
        """One allow-listed file straight from ``static_dir`` (no directory walk, no symlink), or the 404.
        ``subdir`` is the fixed vendored-library directory: bigger cap, revalidated by ETag."""
        cap = VENDOR_MAX_BYTES if subdir == ("vendor", "pdfjs") else STATIC_MAX_BYTES
        body = await self.read_static(lab, subdir, name, cap)
        if body is None:
            return await self.deny("static_missing")
        if not subdir:
            return self.viewer_bytes(lab, body, content_type)
        if subdir == ("fonts",):
            return self.viewer_bytes(lab, body, content_type, FONT_CACHE)
        etag = vendor_etag(body)
        if etag_matches(request.headers.get("If-None-Match"), etag):
            return self.viewer_bytes(lab, body, content_type, VENDOR_CACHE, etag=etag, status=304)
        return self.viewer_bytes(lab, body, content_type, VENDOR_CACHE, etag=etag)

    @staticmethod
    def viewer_bytes(
        lab: LabSettings,
        body: bytes,
        content_type: str,
        cache: str = "no-store",
        *,
        etag: str | None = None,
        status: int = 200,
    ) -> web.Response:
        headers = [
            ("Content-Type", content_type),
            ("Content-Security-Policy", lab.viewer_csp),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", cache),
            ("Referrer-Policy", "no-referrer"),
            ("X-Robots-Tag", "noindex"),
            ("X-Frame-Options", "DENY"),
        ]
        if etag:
            headers.append(("ETag", etag))
        if status == 304:
            return respond(304, [h for h in headers if h[0] != "Content-Type"], cors=False)
        return respond(status, headers, body, cors=False)


def _annotation_id_ok(value: str) -> bool:
    from .annotation_anchor import ANNOTATION_ID_RE

    return ANNOTATION_ID_RE.fullmatch(value) is not None


SURFACE: web.AppKey[PublicSurface] = web.AppKey("surface", PublicSurface)


@web.middleware
async def _uniform_errors(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """A routing error raised by aiohttp itself (an unroutable request target) is the same uniform 404."""
    try:
        return await handler(request)
    except web.HTTPException:
        _log("denied", "unroutable")
        return not_found()


def build_app(
    config: PublicConfig,
    db_pool: asyncpg.Pool,
    blobs: BlobStore,
    library: LibraryStore,
    *,
    now: Callable[[], float] = time.time,
) -> web.Application:
    """The public app: the ``/s/*`` surface (``/healthz`` lives on ``build_health_app``).

    The catch-all route is registered LAST, so the member API of a later task adds its routes before it (and onto the
    separate member roles) without changing the uniform-404 behaviour of everything else."""
    surface = PublicSurface(config, db_pool, blobs, library, now)
    app = web.Application(middlewares=[_uniform_errors], client_max_size=MAX_BODY_BYTES + 1)
    app[SURFACE] = surface
    app.router.add_route("*", "/{tail:.*}", surface.handle, expect_handler=_no_expect)
    return app


def build_health_app(db_pool: asyncpg.Pool) -> web.Application:
    """The health app: ``GET /healthz`` and the uniform 404 for everything else.  It is only ever served on the
    loopback-only listener of ``PublicServer``."""

    async def healthz(request: web.Request) -> web.StreamResponse:
        if _refused_framing(request):
            return not_found()
        try:  # an exhausted pool must answer 503 promptly, not hang the healthcheck
            await asyncio.wait_for(db_pool.fetchval("SELECT 1"), HEALTH_DB_TIMEOUT_SECONDS)
        except Exception:
            return respond(
                503, [("Content-Type", "text/plain; charset=utf-8")], b"unavailable\n", cors=False
            )
        return respond(
            200,
            [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")],
            b"ok\n",
            cors=False,
        )

    async def anything_else(request: web.Request) -> web.StreamResponse:
        return not_found()

    app = web.Application(middlewares=[_uniform_errors])
    app.router.add_get("/healthz", healthz, expect_handler=_no_expect)
    app.router.add_route("*", "/{tail:.*}", anything_else, expect_handler=_no_expect)
    return app


class _UniformHandler(RequestHandler):
    """aiohttp answers a request it cannot even parse (bad framing, a bad method line) with its own 400 and text. That
    would be a second kind of failure on the wire, so it is the uniform 404 too."""

    def handle_error(
        self,
        request: BaseRequest,
        status: int = 500,
        exc: BaseException | None = None,
        message: str | None = None,
    ) -> web.StreamResponse:
        _log("denied", "protocol_error")
        return not_found()

    def eof_received(self) -> bool | None:
        """The client half-closed its side.  aiohttp's default lets asyncio close the whole transport, which loses the
        response of any request still being answered (every known-share request awaits the database).  Keep the write
        side open for the one in-flight response and close after it; an idle connection just closes."""
        if self._current_request is None and not self._messages:
            return None  # nothing in flight: asyncio closes the transport
        request = self._current_request
        if request is not None and not request.content.is_eof():
            return (
                None  # a request body that will never complete: drop the connection, write nothing
            )
        self.close()  # stop after the in-flight response (the handler task itself is not cancelled)
        return True


class _UniformServer(web.Server):
    def __call__(self) -> RequestHandler:
        return _UniformHandler(self, loop=self._loop, **self._kwargs)


class _UniformRunner(web.AppRunner):
    async def _make_server(self) -> web.Server:
        server = await super()._make_server()
        server.__class__ = _UniformServer
        return server


_MALFORMED_PROBE = b"GET /uniform-probe HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
_WELL_FORMED_PROBE = b"GET /uniform-probe HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
_HANDLER_INTERNALS = ("_current_request", "_messages")  # read by _UniformHandler.eof_received


def require_handler_internals(handler: object) -> None:
    """``eof_received`` leans on two private attributes of aiohttp's request handler; fail closed when they are gone."""
    missing = [name for name in _HANDLER_INTERNALS if not hasattr(handler, name)]
    if missing:
        raise RuntimeError(
            f"aiohttp request handler lacks {', '.join(missing)}; eof_received cannot be trusted"
        )


def assert_uniform_hook(server: web.Server) -> None:
    """Fail closed when the protocol-error hook is not in place (an aiohttp internal changed): without it a request
    aiohttp cannot parse would get its own 400 text, a second kind of failure on the wire."""
    if (
        type(server) is not _UniformServer
        or _UniformHandler.handle_error is RequestHandler.handle_error
    ):
        raise RuntimeError("uniform protocol-error hook is not installed")
    if not aiohttp_version_ok(aiohttp.__version__):
        raise RuntimeError(f"aiohttp {aiohttp.__version__} is not the pinned release")
    require_handler_internals(server())


async def _raw_exchange(host: str, port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    try:
        writer.write(payload)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 5)
    finally:
        writer.close()


def probe_host(bound: str) -> str:
    """The address to dial to reach a listener bound to ``bound`` (a wildcard bind is dialled over loopback)."""
    return {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(bound, bound)


def _without_date(raw: bytes) -> bytes:
    return b"\r\n".join(
        line for line in raw.split(b"\r\n") if not line.lower().startswith(b"date:")
    )


async def verify_uniform_listener(port: int, host: str = "127.0.0.1") -> None:
    """Behavioural startup check on the bound listener: a request aiohttp cannot parse must be answered with exactly
    the bytes of the uniform 404 (what an ordinary unknown path gets).  Anything else, including no answer, raises."""
    try:
        malformed = _without_date(await _raw_exchange(host, port, _MALFORMED_PROBE))
        reference = _without_date(await _raw_exchange(host, port, _WELL_FORMED_PROBE))
    except (OSError, TimeoutError) as exc:
        raise RuntimeError(
            f"uniform 404 check could not talk to the listener at {host}:{port}: {exc!r}"
        ) from exc
    if not reference.startswith(b"HTTP/1.1 404 Not Found\r\n") or not reference.endswith(
        NOT_FOUND_BODY
    ):
        raise RuntimeError(
            f"uniform 404 check: {host}:{port} does not answer an ordinary request with the uniform 404"
        )
    # aiohttp answers a request it could not parse as HTTP/1.0 (it never learned the version); every other byte matches
    if malformed.replace(b"HTTP/1.0 ", b"HTTP/1.1 ", 1) != reference:
        raise RuntimeError(
            f"uniform 404 check: {host}:{port} does not answer a malformed request with the uniform 404"
        )


class PublicServer:
    """Runs ``build_app`` on a TCP socket and ``build_health_app`` on a second, loopback-only one (the deploy entry
    point and the tests)."""

    def __init__(self, app: web.Application, health_app: web.Application | None = None) -> None:
        self.app = app
        self.health_app = (
            health_app if health_app is not None else build_health_app(app[SURFACE].pool)
        )
        self._runners: list[_UniformRunner] = []
        self.port = 0
        self.health_port = 0

    @property
    def _runner(self) -> _UniformRunner | None:
        """The public listener's runner."""
        return self._runners[0] if self._runners else None

    async def _serve(self, app: web.Application, host: str, port: int) -> int:
        runner = _UniformRunner(
            app,
            access_log=None,
            handle_signals=False,
            keepalive_timeout=15,
            auto_decompress=False,  # a request body is never inflated (decompression bomb)
        )
        self._runners.append(
            runner
        )  # tracked at once, so a failed setup() is still cleaned up by stop()
        await runner.setup()
        assert_uniform_hook(runner.server)  # type: ignore[arg-type]
        site = web.TCPSite(runner, host, port, backlog=64)
        await site.start()
        sockets = site._server.sockets if site._server is not None else []  # type: ignore[union-attr]
        return int(sockets[0].getsockname()[1]) if sockets else port

    async def start(self, host: str = "127.0.0.1", port: int = 0, health_port: int = 0) -> int:
        """Bind both listeners, then probe each at the address it is bound to.  The probe cannot run before the
        socket accepts (aiohttp listens and accepts in one step), so the window between bind and probe is not
        closed; it is safe because ``assert_uniform_hook`` has already verified the server class and the handler
        internals before the bind, and the probe fails closed (the server is torn down) when behaviour differs."""
        try:
            self.port = await self._serve(self.app, host, port)
            self.health_port = await self._serve(self.health_app, HEALTH_HOST, health_port)
            await verify_uniform_listener(self.port, probe_host(host))
            await verify_uniform_listener(self.health_port, probe_host(HEALTH_HOST))
        except BaseException:
            try:
                await self.stop()
            except Exception:
                pass  # the start failure is the error worth reporting
            raise
        return self.port

    async def stop(self) -> None:
        """Clean up every runner even when one cleanup raises; the first error is re-raised afterwards."""
        runners, self._runners = list(reversed(self._runners)), []
        first: BaseException | None = None
        for runner in runners:
            try:
                await runner.cleanup()
            except Exception as exc:
                first = first or exc
        if first is not None:
            raise first
