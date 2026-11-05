"""The member web API (plan 5, Task 11): what the signed-in web UI of a group member calls on ``collab-public``.

Every route is a thin shell around ``MemberService`` (the very methods the MCP tools call), so the web session and the
agents of one person behave identically.  This module owns only the HTTP concerns:

* served only for ``Host: <public_host>`` (exactly one Host header); any other Host reaches none of it and gets the
  listener's ordinary 404;
* the session is ``Authorization: Member <session>`` (``member_session``: its own domain tag, never a cookie, so there
  is no ambient credential for a foreign site to ride on); a share session, a Bearer token or anything else is a 401;
* every non-GET needs exactly one ``Origin: https://<public_host>`` (403 otherwise, decided before authentication and
  before any body is read);
* error bodies are fixed strings (``{"error": code, "message": fixed}``); nothing about a token, a path on disk or an
  exception ever reaches a response;
* duplicate security-relevant headers are refused; bodies are bounded; the run PUT reads in bounded blocks and refuses
  an oversize ``Content-Length`` (413) before reading a byte of it.

The session exchange ``POST /api/member/session`` takes the Cognito access token (``Authorization: Bearer``) and the ID
token (``X-Id-Token``), verifies both exactly as the lab share sign-in does, requires an approved group member, and mints
a session no longer than the access token and ``session_seconds``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, unquote

from .artifact_member_service import MemberService, MemberServiceError, member_actor
from .artifact_members import member_owner_key
from .collab import CollabError, CollabService, Principal
from .collab_bridge_files import CHUNK_BYTES, SHA_RE, UPLOAD_RE, BridgeError, upload_id_for
from .member_session import check_session, mint_session

_LOG = logging.getLogger(__name__)

MAX_JSON_BODY: Final = 256 * 1024
MAX_TOKEN_CHARS: Final = 8192
READ_BLOCK: Final = 64 * 1024
_PATH = re.compile(r"[A-Za-z0-9_.~/-]{1,512}")
_ID = re.compile(r"[A-Za-z0-9_.~-]{1,200}\Z")
_UPLOAD = re.compile(UPLOAD_RE)
_SHA = re.compile(SHA_RE)
_DIGITS = re.compile(r"[0-9]{1,15}\Z")
_STATIC: Final = {"/": ("member.html", "text/html; charset=utf-8"),
                  "/my/callback": ("member.html", "text/html; charset=utf-8"),
                  "/my/member.js": ("member.js", "text/javascript; charset=utf-8"),
                  "/my/member.css": ("member.css", "text/css; charset=utf-8")}
_RETRY_AFTER: Final = "10"

_BODY_KEYS = {
    "create": ({"title", "filename", "profile", "idempotency_key"}, {"title", "filename", "profile"}),
    "delete": ({"confirm"}, {"confirm"}),
    "status": ({"name", "size_bytes", "file_sha256"}, {"name", "size_bytes", "file_sha256"}),
    "publish": ({"base_version_id", "files", "message", "entry", "idempotency_key"}, {"files", "message"}),
    "share_create": ({"version_id", "audience", "allow_groups", "allow_emails", "expires_in_days", "entry", "title",
                      "idempotency_key"}, {"version_id", "audience"}),
    "approve": ({"confirm", "idempotency_key"}, {"confirm"}),
}


class _Reply(Exception):
    """Internal control flow: a finished refusal (never reaches a client as anything but its fixed body)."""

    def __init__(self, status: int, code: str, message: str = "", headers: Mapping[str, str] | None = None):
        super().__init__(status, code)
        self.status, self.code, self.message, self.headers = status, code, message, dict(headers or {})


def _unauthorized() -> _Reply:
    return _Reply(401, "unauthorized", "sign in again")


def _bad(message: str = "bad request") -> _Reply:
    return _Reply(400, "bad_request", message)


class _Window:
    """A small per-key sliding-window counter (best effort, in memory)."""

    def __init__(self, limit: int, seconds: float, clock: Callable[[], float]):
        self.limit, self.seconds, self.clock = limit, seconds, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 20000:
                self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v and now - v[-1] < self.seconds})
            return True


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


class MemberWeb:
    """Mounted on the public handler by ``api.ControlPlaneServer(member_web=...)``; see the module docstring."""

    def __init__(self, service: CollabService, *, public_host: str, session_secret: bytes, session_seconds: int = 14400,
                 web_root: Path | None = None, public_config: Mapping[str, Any] | None = None,
                 require_https: bool = False, clock: Callable[[], float] | None = None):
        self.service = service
        self.public_host = public_host
        self.origin = f"https://{public_host}"
        self.secret = session_secret
        self.session_seconds = session_seconds
        self.web_root = Path(web_root) if web_root else None
        self.public_config = dict(public_config if public_config is not None else (service.public_config or {}))
        self.require_https = require_https
        self.clock = clock if clock is not None else service._now     # the service's clock: session and pr.exp agree
        self._exchange: _Window | None = None

    # ----------------------------------------------------------------------------------------------- entry points
    def owns(self, h) -> bool:
        """True when the request carries exactly one Host header naming the member host (port 443 allowed)."""
        hosts = h.headers.get_all("Host") or []
        if len(hosts) != 1:
            return False
        host = hosts[0].strip().lower()
        return host == self.public_host or host == self.public_host + ":443"

    def handle(self, h) -> None:
        """Answer the request completely; never raises."""
        try:
            self._handle(h)
        except _Reply as reply:
            self._error(h, reply)
        except MemberServiceError as exc:
            headers = {"Retry-After": _RETRY_AFTER} if exc.status == 429 else {}
            self._error(h, _Reply(exc.status, exc.code, exc.message, headers))
        except CollabError as exc:
            status = exc.status if exc.status in (400, 401, 403, 404, 409, 413, 429, 503) else 500
            self._error(h, _Reply(status, "unavailable" if status == 503 else "error", "request failed"))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            h.close_connection = True
        except Exception as exc:  # noqa: BLE001 - one fixed line: the type only, it could hold a path or a token
            _LOG.warning("member web: internal error (%s)", type(exc).__name__)
            self._error(h, _Reply(500, "internal", "internal error"))

    # ----------------------------------------------------------------------------------------------- responses
    def _send(self, h, status: int, payload: bytes, ctype: str, headers: Mapping[str, str] | None = None) -> None:
        h._response_status = status
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Referrer-Policy", "no-referrer")
        h.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            h.send_header(name, value)
        if h.close_connection:
            h.send_header("Connection", "close")
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(payload)

    def _json(self, h, body: Any, status: int = 200) -> None:
        self._send(h, status, json.dumps(body).encode("utf-8"), "application/json")

    def _error(self, h, reply: _Reply) -> None:
        h.close_connection = True          # an unread request body must never be parsed as the next request
        body: dict[str, str] = {"error": reply.code}
        if reply.message:
            body["message"] = reply.message
        try:
            self._send(h, reply.status, json.dumps(body).encode("utf-8"), "application/json", reply.headers)
        except (OSError, ValueError):
            pass

    # ----------------------------------------------------------------------------------------------- the pipeline
    def _handle(self, h) -> None:
        ms: MemberService | None = self.service.member_service
        if ms is None or self.service.member_group is None:
            raise _Reply(404, "not_found", "not found")
        if self.require_https and h.headers.get("X-Forwarded-Proto", "").lower() != "https":
            raise _bad("https is required")
        raw = h.path
        path, _, query = raw.partition("?")
        if len(raw) > 1024 or not _PATH.fullmatch(path):
            raise _Reply(404, "not_found", "not found")
        if h._rate_limit(h._proxied_client()):
            h.close_connection = True
            return
        method = h.command
        if method not in ("GET", "POST", "PUT"):
            raise _Reply(405, "method_not_allowed", "method not allowed")
        if method != "GET":
            origins = h.headers.get_all("Origin") or []
            if len(origins) != 1 or origins[0] != self.origin:
                raise _Reply(403, "forbidden", "forbidden")
        if method == "GET" and path in _STATIC:
            return self._static(h, path)
        segs = path.strip("/").split("/")
        if segs[:2] != ["api", "member"] or any(s in ("", ".", "..") for s in segs):
            raise _Reply(404, "not_found", "not found")
        rest = segs[2:]
        if method == "GET" and rest == ["config"]:
            return self._config(h)
        if method == "POST" and rest == ["session"]:
            return self._session(h)
        pr = self._authenticate(h)
        actor = member_actor(pr)
        route = self._route(method, rest)
        if route is None:
            raise _Reply(404, "not_found", "not found")
        return route(h, ms, pr, actor, rest, query)

    # ----------------------------------------------------------------------------------------------- public routes
    def _static(self, h, path: str) -> None:
        name, ctype = _STATIC[path]
        target = (self.web_root / name) if self.web_root else None
        if target is None or not target.is_file():
            raise _Reply(404, "not_found", "not found")
        cognito = self._cognito_origin()
        connect = "'self'" + (f" {cognito}" if cognito else "")
        csp = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
               f"connect-src {connect}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self._send(h, 200, target.read_bytes(), ctype, {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"})

    def _cognito_origin(self) -> str | None:
        domain = self.public_config.get("hosted_ui_domain")
        if not isinstance(domain, str) or not domain:
            return None
        domain = domain.rstrip("/")
        return domain if domain.startswith("https://") else f"https://{domain}"

    def _config(self, h) -> None:
        client_id, domain = self.public_config.get("client_id"), self._cognito_origin()
        if not isinstance(client_id, str) or not client_id or domain is None:
            raise _Reply(503, "unavailable", "sign-in is not configured")
        self._json(h, {"cognito_domain": domain, "client_id": client_id,
                       "redirect_uri": f"{self.origin}/my/callback", "logout_uri": f"{self.origin}/"})

    def _single(self, h, name: str, *, limit: int = MAX_TOKEN_CHARS) -> str:
        values = h.headers.get_all(name) or []
        if len(values) != 1 or not values[0] or len(values[0]) > limit or not values[0].isascii():
            raise _unauthorized()
        return values[0]

    def _session(self, h) -> None:
        ms: MemberService = self.service.member_service
        if self._exchange is None:
            self._exchange = _Window(ms.config.session_exchanges_per_minute, 60.0, self.clock)
        if not self._exchange.allow(h._proxied_client()):
            raise _Reply(429, "rate_limited", "too many sign-ins", {"Retry-After": "60"})
        if (h.headers.get_all("Content-Length") or ["0"]) not in (["0"], [""]):
            raise _bad()                                    # body-less: a body is never read
        try:
            auth = self._single(h, "Authorization")
            id_token = self._single(h, "X-Id-Token")
            scheme, _, token = auth.partition(" ")
            if scheme != "Bearer" or not token or token != token.strip():
                raise _unauthorized()
            pr = self.service.authenticate(auth, None)
            verify = getattr(self.service.auth, "verify_id_token", None)
            if pr.kind != "user" or verify is None or not verify(id_token, user=pr.id):
                raise _unauthorized()
            if not self.service.member_check(pr).ok:
                raise _unauthorized()
        except _Reply:
            raise
        except Exception:  # noqa: BLE001 - every authentication failure is the same 401
            raise _unauthorized() from None
        now = self.clock()
        exp = now + self.session_seconds
        if isinstance(pr.exp, (int, float)) and not isinstance(pr.exp, bool):
            exp = min(exp, float(pr.exp))
        if not exp > now + 1:
            raise _unauthorized()
        self._json(h, {"session": mint_session(self.secret, pr.id, exp), "expires_at": int(exp)})

    # ----------------------------------------------------------------------------------------------- authenticated
    def _authenticate(self, h) -> Principal:
        auth = self._single(h, "Authorization", limit=1024)
        scheme, _, token = auth.partition(" ")
        if scheme != "Member":
            raise _unauthorized()
        got = check_session(self.secret, token, self.clock())
        if got is None:
            raise _unauthorized()
        pr = Principal("user", got["identity"], got["identity"], exp=got["exp"])
        if not self.service.still_valid(pr):
            raise _unauthorized()
        return pr

    def _route(self, method: str, rest: list[str]) -> Callable[..., None] | None:
        n = len(rest)
        if method == "GET":
            if rest == ["me"]:
                return self._r_me
            if rest == ["links"]:
                return self._r_links
            if n == 2 and rest[0] == "results":
                return self._r_result
            if n == 3 and rest[0] == "artifacts" and rest[2] in ("history", "shares"):
                return self._r_history if rest[2] == "history" else self._r_shares
            return None
        if method == "POST":
            if rest == ["artifacts"]:
                return self._r_create
            if n == 3 and rest[0] == "artifacts":
                return {"delete": self._r_delete, "publish": self._r_publish,
                        "shares": self._r_share_create}.get(rest[2])
            if n == 4 and rest[0] == "artifacts" and rest[2:] == ["uploads", "status"]:
                return self._r_status
            if n == 5 and rest[0] == "artifacts" and rest[2] == "uploads" and rest[4] == "abort":
                return self._r_abort
            if n == 5 and rest[0] == "artifacts" and rest[2] == "shares":
                return {"approve": self._r_approve, "revoke": self._r_revoke, "events": self._r_events}.get(rest[4])
            return None
        if n == 6 and rest[0] == "artifacts" and rest[2] == "uploads" and rest[4] == "runs":
            return self._r_run
        return None

    @staticmethod
    def _live(ms, actor, artifact_id: str) -> str:
        """``artifact_id`` unless the caller's view holds it as anything but ``active`` (a tombstoned artifact does not
        exist for the web API, whatever the service would still answer)."""
        for row in ms.mine(actor).get("artifacts") or []:
            if isinstance(row, dict) and row.get("artifact_id") == artifact_id and row.get("lifecycle") != "active":
                raise _Reply(404, "not_found", "not found")
        return artifact_id

    @staticmethod
    def _ident(value: str) -> str:
        if not _ID.match(value):
            raise _Reply(404, "not_found", "not found")
        return value

    def _query(self, query: str, allowed: set[str]) -> dict[str, str]:
        if not query:
            return {}
        pairs = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        if set(pairs) - allowed or any(len(v) != 1 for v in pairs.values()):
            raise _bad()
        return {k: v[0] for k, v in pairs.items()}

    def _int_param(self, value: str | None, default: int) -> int:
        if value is None:
            return default
        if not _DIGITS.match(value):
            raise _bad()
        return int(value)

    def _body(self, h, kind: str | None) -> dict[str, Any]:
        """The JSON object of a POST (``kind`` None = no body expected, an empty object is the result)."""
        lengths = h.headers.get_all("Content-Length") or []
        if len(lengths) > 1 or (lengths and lengths[0].strip() and not _DIGITS.match(lengths[0].strip())):
            raise _bad()
        length = int(lengths[0]) if lengths and lengths[0].strip() else 0
        if length > MAX_JSON_BODY:
            raise _Reply(413, "too_large", "request body is too large")
        body: dict[str, Any] = {}
        if length:
            if h.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise _Reply(415, "unsupported_media_type", "application/json required")
            data = self._read_exactly(h, length)
            try:
                parsed = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
            except (ValueError, UnicodeError, RecursionError):
                raise _bad() from None
            if not isinstance(parsed, dict):
                raise _bad()
            body = parsed
        if kind is None:
            if body:
                raise _bad()
            return body
        allowed, required = _BODY_KEYS[kind]
        if set(body) - allowed:
            raise _bad("unknown argument")
        if required - set(body):
            raise _bad("missing argument")
        return body

    @staticmethod
    def _read_exactly(h, length: int) -> bytes:
        chunks: list[bytes] = []
        left = length
        while left:
            block = h.rfile.read(min(READ_BLOCK, left))
            if not block:
                raise _bad()
            chunks.append(block)
            left -= len(block)
        return b"".join(chunks)

    # ----------------------------------------------------------------------------------------------- reads
    def _r_me(self, h, ms, pr, actor, rest, query):
        self._query(query, set())
        out = ms.mine(actor)
        gone = {r.get("artifact_id") for r in out.get("artifacts") or [] if isinstance(r, dict)
                and r.get("lifecycle") != "active" and isinstance(r.get("artifact_id"), str)}
        if gone:
            out = {**out, "artifacts": [r for r in out["artifacts"] if r.get("artifact_id") not in gone],
                   "links": [r for r in out.get("links") or [] if not (isinstance(r, dict)
                                                                       and r.get("artifact_id") in gone)]}
        self._json(h, out)

    def _r_links(self, h, ms, pr, actor, rest, query):
        self._query(query, set())
        out = ms.share_list(actor)
        gone = {r.get("artifact_id") for r in ms.mine(actor).get("artifacts") or [] if isinstance(r, dict)
                and r.get("lifecycle") != "active" and isinstance(r.get("artifact_id"), str)}
        self._json(h, {**out, "links": [r for r in out["links"] if not (isinstance(r, dict)
                                                                         and r.get("artifact_id") in gone)]})

    def _r_shares(self, h, ms, pr, actor, rest, query):
        self._query(query, set())
        self._json(h, ms.share_list(actor, self._live(ms, actor, self._ident(rest[1]))))

    def _r_history(self, h, ms, pr, actor, rest, query):
        got = self._query(query, {"limit"})
        self._json(h, ms.history(actor, self._live(ms, actor, self._ident(rest[1])), self._int_param(got.get("limit"), 50)))

    def _r_result(self, h, ms, pr, actor, rest, query):
        got = self._query(query, {"seconds"})
        # the web default is 0 (poll; the browser decides how long to wait), unlike the MCP tool's 20
        self._json(h, ms.result(actor, self._ident(rest[1]), self._int_param(got.get("seconds"), 0)))

    # ----------------------------------------------------------------------------------------------- writes
    def _r_create(self, h, ms, pr, actor, rest, query):
        self._query(query, set())
        b = self._body(h, "create")
        self._json(h, ms.create(actor, b["title"], b["filename"], b["profile"], b.get("idempotency_key")))

    def _r_delete(self, h, ms, pr, actor, rest, query):
        b = self._body(h, "delete")
        self._json(h, ms.delete(actor, self._live(ms, actor, self._ident(rest[1])), b["confirm"]))

    def _r_publish(self, h, ms, pr, actor, rest, query):
        b = self._body(h, "publish")
        self._json(h, ms.publish(actor, self._live(ms, actor, self._ident(rest[1])), base_version_id=b.get("base_version_id"),
                                 files=b["files"], message=b["message"], entry=b.get("entry"),
                                 idempotency_key=b.get("idempotency_key")))

    def _r_share_create(self, h, ms, pr, actor, rest, query):
        b = self._body(h, "share_create")
        self._json(h, ms.share_create(actor, self._live(ms, actor, self._ident(rest[1])), b["version_id"], b["audience"],
                                      b.get("allow_groups"), b.get("allow_emails"), b.get("expires_in_days"),
                                      b.get("entry"), b.get("title"), b.get("idempotency_key")))

    def _r_approve(self, h, ms, pr, actor, rest, query):
        b = self._body(h, "approve")
        self._json(h, ms.share_approve(actor, self._live(ms, actor, self._ident(rest[1])), self._ident(rest[3]), b["confirm"],
                                       b.get("idempotency_key")))

    def _r_revoke(self, h, ms, pr, actor, rest, query):
        self._body(h, None)
        self._json(h, ms.share_revoke(actor, self._live(ms, actor, self._ident(rest[1])), self._ident(rest[3])))

    def _r_events(self, h, ms, pr, actor, rest, query):
        self._body(h, None)
        self._json(h, ms.share_events(actor, self._live(ms, actor, self._ident(rest[1])), self._ident(rest[3])))

    def _r_status(self, h, ms, pr, actor, rest, query):
        b = self._body(h, "status")
        self._json(h, ms.upload_status(actor, self._live(ms, actor, self._ident(rest[1])), b["name"], b["size_bytes"], b["file_sha256"]))

    def _r_abort(self, h, ms, pr, actor, rest, query):
        self._body(h, None)
        self._json(h, ms.upload_abort(actor, self._live(ms, actor, self._ident(rest[1])), rest[3]))

    # ----------------------------------------------------------------------------------------------- the run PUT
    def _header_once(self, h, name: str) -> str:
        values = h.headers.get_all(name) or []
        if len(values) != 1 or not values[0].strip() or not values[0].isascii() or len(values[0]) > 1024:
            raise _bad(f"{name} is required exactly once")
        return values[0].strip()

    def _r_run(self, h, ms, pr, actor, rest, query):
        """``PUT /api/member/artifacts/{id}/uploads/{upload_id}/runs/{first_index}``: a raw body of up to
        ``http_chunks_per_request`` consecutive chunks.  Every refusal below happens before the first body byte is read."""
        self._query(query, set())
        artifact_id, upload_id, first = self._live(ms, actor, self._ident(rest[1])), rest[3], rest[5]
        if not _UPLOAD.match(upload_id) or not _DIGITS.match(first):
            raise _bad()
        lengths = h.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not _DIGITS.match(lengths[0].strip()) or int(lengths[0]) < 1:
            raise _bad("Content-Length is required exactly once")
        length = int(lengths[0])
        if length > ms.config.http_chunks_per_request * CHUNK_BYTES:
            raise _Reply(413, "too_large", "run is too large")
        name = unquote(self._header_once(h, "X-Name"), errors="strict")
        size = self._header_once(h, "X-Size-Bytes")
        file_sha, run_sha = self._header_once(h, "X-File-SHA256"), self._header_once(h, "X-Run-SHA256")
        if not _DIGITS.match(size) or not _SHA.match(file_sha) or not _SHA.match(run_sha):
            raise _bad()
        check = self.service.member_check(pr)
        if not check.ok or check.principal is None:
            raise _Reply(403, "forbidden", "artifact tools are for group members")
        try:
            expected = upload_id_for(member_owner_key(check.principal), artifact_id, name, file_sha, int(size))
        except BridgeError:
            raise _bad() from None
        if expected != upload_id:
            raise _bad("upload_id does not match the headers")
        h._continue_100()
        data = self._read_exactly(h, length)
        self._json(h, ms.upload_run(actor, artifact_id, name, int(size), file_sha, int(first), data, run_sha))


def run_sha256(data: bytes) -> str:
    """The ``X-Run-SHA256`` value of a run body (used by clients and tests)."""
    return hashlib.sha256(data).hexdigest()
