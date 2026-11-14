"""Read-only client of the artifact host's internal listener (``GET`` only, bearer key, MessagePack on the wire).

The dashboard never opens the host's database: ``/api/v1/library`` is a thin view over these calls.  Rules:

* Every request is a ``GET`` to ``<host_url>/v1/...``; the target is always built here from validated pieces (an id
  matching ``[A-Za-z0-9][A-Za-z0-9_-]{0,127}`` and, for content, a manifest path of clean segments), so the client is
  never an open proxy.  ``host_url`` is the GET-only relay (``http://artifact-host-relay:8091``), not the internal
  role itself: the dashboard shares a network with the relay only.
* The key travels only in the ``Authorization`` header of that request.  It is never returned, logged or put in a URL.
* Every call has a connect/read timeout.  A host that is down, slow, unauthorised, or answers anything unexpected is a
  ``HostUnavailable`` (the dashboard answers 503 for the library only); a ``4xx`` that the host meant for the caller
  (bad parameter, unknown id) is a ``HostRejected`` carrying that status.
* ``open_content`` streams a content response and hands back only the headers the dashboard may forward; it refuses
  (``UnsafeContent``) any response that lacks the host's ``sandbox`` content security policy.
"""
from __future__ import annotations

import http.client
import re
import ssl
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit

import msgpack

DEFAULT_TIMEOUT: Final = 5.0
MAX_BODY: Final = 8 * 1024 * 1024
CHUNK: Final = 64 * 1024
MSGPACK: Final = "application/msgpack"
FEED_PARAMS: Final = ("q", "sources", "folder_id", "type", "since", "until", "limit", "offset")
SHARE_PARAMS: Final = ("artifact_id", "state", "audience", "limit", "offset")
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SEGMENT_BAD: Final = re.compile(r"[\x00-\x1f\x7f\\]")
MAX_PATH: Final = 1024
CONTENT_DEADLINE: Final = 30.0       # total seconds one proxied content body may take (each read also has the timeout)
# What a proxied content response may carry back to the browser (everything else, cookies and hop-by-hop included, is dropped).
FORWARDED: Final = ("Content-Type", "Content-Security-Policy", "X-Content-Type-Options", "Content-Length")


class HostError(Exception):
    """Base of every client failure."""


class HostUnavailable(HostError):
    """The host is down, too slow, refused our key, or answered something that is not the contract."""


class HostRejected(HostError):
    """The host answered a request the caller got wrong (400, 404, ...): ``status`` and the host's ``code``."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"{status} {code}")
        self.status, self.code, self.message = status, code, message


class UnsafeContent(HostError):
    """A content response without the host's sandbox policy: never served."""


class BadReference(ValueError):
    """An id or content path that cannot name something on the host."""


def valid_id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise BadReference("invalid id")
    return value


def clean_path(value: object) -> list[str]:
    """The segments of a manifest path, or ``BadReference``: no empty, ``.`` or ``..`` piece, no control or backslash."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH:
        raise BadReference("invalid path")
    pieces = value.split("/")
    for piece in pieces:
        if not piece or piece in {".", ".."} or _SEGMENT_BAD.search(piece):
            raise BadReference("invalid path")
    return pieces


def csp_sandboxes(policy: str | None) -> bool:
    """True only for a policy that fully sandboxes: a ``sandbox`` directive with NO ``allow-*`` token (so no scripts,
    same-origin, forms or popups) and ``default-src 'none'``.  Anything else is not what the host promises."""
    if not policy:
        return False
    directives = [d.split() for d in (part.strip().lower() for part in policy.split(";")) if d]
    sandboxes = [d for d in directives if d[0] == "sandbox"]
    defaults = [d for d in directives if d[0] == "default-src"]
    return (bool(sandboxes) and not any(t.startswith("allow-") for d in sandboxes for t in d[1:])
            and len(defaults) == 1 and defaults[0][1:] == ["'none'"])


@dataclass(frozen=True)
class ContentHead:
    """The forwardable part of a content response."""
    headers: tuple[tuple[str, str], ...]
    length: int | None


class ArtifactHostClient:
    def __init__(self, host_url: str, key: str, *, timeout: float = DEFAULT_TIMEOUT,
                 content_deadline: float = CONTENT_DEADLINE) -> None:
        parts = urlsplit(host_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query or parts.fragment or parts.username:
            raise ValueError("host_url must be an http(s) URL without credentials, query or fragment")
        if not isinstance(key, str) or not key or not key.isascii() or any(c in key for c in "\r\n \t"):
            raise ValueError("host_key must be a non-empty ASCII token")
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._prefix = parts.path.rstrip("/")
        self._key = key
        self.timeout = float(timeout)
        self.content_deadline = float(content_deadline)

    def __repr__(self) -> str:
        return f"ArtifactHostClient({self._scheme}://{self._host}:{self._port}{self._prefix})"

    # -- plumbing -------------------------------------------------------------------------------------

    def _connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)

    def _content_target(self, item: str, version: str, path: str) -> str:
        pieces = [quote(p, safe="") for p in clean_path(path)]
        return f"{self._prefix}/v1/content/{valid_id(item)}/{valid_id(version)}/{'/'.join(pieces)}"

    def content_url(self, item: str, version: str, path: str) -> str:
        """The one URL a content request may target: ``<host_url>/v1/content/<item>/<version>/<path>``."""
        return f"{self._scheme}://{self._host}:{self._port}{self._content_target(item, version, path)}"

    def _target(self, route: str, query: Mapping[str, str] | None = None) -> str:
        suffix = f"?{urlencode(query)}" if query else ""
        return f"{self._prefix}/v1/{route}{suffix}"

    @contextmanager
    def _get(self, target: str, accept: str) -> Iterator[http.client.HTTPResponse]:
        conn = self._connection()
        try:
            try:
                conn.request("GET", target, headers={"Authorization": f"Bearer {self._key}", "Accept": accept,
                                                     "Accept-Encoding": "identity", "Connection": "close"})
                response = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:     # refused, reset, timeout, TLS: all "host down"
                raise HostUnavailable(f"host unreachable ({type(exc).__name__})") from None
            yield response
        finally:
            conn.close()

    def _call(self, route: str, query: Mapping[str, str] | None = None) -> dict[str, Any]:
        with self._get(self._target(route, query), MSGPACK) as response:
            try:
                body = response.read(MAX_BODY + 1)
            except (OSError, http.client.HTTPException) as exc:
                raise HostUnavailable(f"host unreachable ({type(exc).__name__})") from None
            if len(body) > MAX_BODY:
                raise HostUnavailable("host answer too large")
            kind = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            status = response.status
        if kind != MSGPACK:
            raise HostUnavailable(f"unexpected answer from the host ({status})")
        try:
            data = msgpack.unpackb(body, raw=False, strict_map_key=False)
        except (msgpack.UnpackException, ValueError, TypeError):
            raise HostUnavailable("host answer is not MessagePack") from None
        if status == 200 and isinstance(data, dict):
            return data
        error = data if isinstance(data, dict) else {}
        if status in {400, 404}:
            raise HostRejected(status, str(error.get("error", "invalid_request")), str(error.get("message", "")))
        raise HostUnavailable(f"host answered {status}")      # 401/403 (our key), 429, 5xx: the library is unavailable

    @staticmethod
    def _pick(query: Mapping[str, Any] | None, allowed: Sequence[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in (query or {}).items():
            if name not in allowed:
                raise HostRejected(400, "invalid_request", f"unknown parameter {name}")
            if value is not None and value != "":
                out[name] = str(value)
        return out

    # -- reads ----------------------------------------------------------------------------------------

    def feed(self, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._call("feed", self._pick(query, FEED_PARAMS))

    def folders(self) -> dict[str, Any]:
        return self._call("folders")

    def item(self, item_id: str) -> dict[str, Any]:
        return self._call(f"items/{valid_id(item_id)}")

    def versions(self, item_id: str) -> dict[str, Any]:
        return self._call(f"items/{valid_id(item_id)}/versions")

    def shares(self, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._call("shares", self._pick(query, SHARE_PARAMS))

    # -- content --------------------------------------------------------------------------------------

    @contextmanager
    def open_content(self, item: str, version: str, path: str) -> Iterator[tuple[ContentHead, Iterator[bytes]]]:
        """Open one content file.  Yields the forwardable head and a chunk iterator; raises ``HostRejected`` (404),
        ``UnsafeContent`` (no sandbox policy / no nosniff / no type) or ``HostUnavailable``."""
        target = self._content_target(item, version, path)
        with self._get(target, "*/*") as response:
            if response.status == 404:
                raise HostRejected(404, "not_found")
            if response.status != 200:
                raise HostUnavailable(f"host answered {response.status}")
            policy = response.getheader("Content-Security-Policy")
            kind = response.getheader("Content-Type")
            if not csp_sandboxes(policy):
                raise UnsafeContent("host content lacks the sandbox policy")
            if (response.getheader("X-Content-Type-Options") or "").strip().lower() != "nosniff" or not kind:
                raise UnsafeContent("host content lacks nosniff or a content type")
            if any(c in str(response.getheader(name) or "") for name in FORWARDED for c in "\r\n"):
                raise UnsafeContent("host header carries a line break")
            length = response.getheader("Content-Length")
            size = int(length) if length and length.isdigit() else None
            head = ContentHead(tuple((name, response.getheader(name) or "") for name in FORWARDED
                                     if response.getheader(name) is not None), size)

            started = time.monotonic()

            def chunks() -> Iterator[bytes]:
                while True:
                    if time.monotonic() - started > self.content_deadline:
                        raise HostUnavailable("host content too slow")
                    try:
                        block = response.read1(CHUNK)
                    except (OSError, http.client.HTTPException) as exc:
                        raise HostUnavailable(f"host unreachable ({type(exc).__name__})") from None
                    if not block:
                        return
                    yield block

            yield head, chunks()


LIBRARY_KEYS: Final = frozenset({"host_url", "host_key", "timeout_seconds"})
MIN_KEY_CHARS: Final = 32
MAX_TIMEOUT: Final = 30.0


@dataclass(frozen=True)
class LibraryHostConfig:
    host_url: str
    host_key: str
    timeout_seconds: float = DEFAULT_TIMEOUT

    def client(self) -> ArtifactHostClient:
        return ArtifactHostClient(self.host_url, self.host_key, timeout=self.timeout_seconds)


def parse_library_host(raw: Any) -> LibraryHostConfig | None:
    """``control_plane.artifact_library`` -> its host settings, ``None`` when the library is not configured (neither
    ``host_url`` nor ``host_key``).  ``ValueError`` names the bad key; the key value is never echoed."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("control_plane.artifact_library must be a mapping")
    unknown = sorted(set(raw) - LIBRARY_KEYS)
    if unknown:
        raise ValueError(f"control_plane.artifact_library has unknown keys: {', '.join(map(str, unknown))}")
    url, key = raw.get("host_url"), raw.get("host_key")
    if url is None and key is None:
        return None
    if url is None or key is None:
        raise ValueError("control_plane.artifact_library needs both host_url and host_key")
    if not isinstance(url, str):
        raise ValueError("control_plane.artifact_library.host_url must be a string")
    if not isinstance(key, str) or len(key) < MIN_KEY_CHARS:
        raise ValueError(f"control_plane.artifact_library.host_key must be a string of at least {MIN_KEY_CHARS} characters")
    timeout = raw.get("timeout_seconds", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"control_plane.artifact_library.timeout_seconds must be a number in (0, {MAX_TIMEOUT:g}]")
    try:
        ArtifactHostClient(url, key, timeout=float(timeout))
    except ValueError as exc:
        raise ValueError(f"control_plane.artifact_library: {exc}") from None
    return LibraryHostConfig(url, key, float(timeout))


__all__ = ["LibraryHostConfig", "parse_library_host", "ArtifactHostClient", "BadReference", "HostError", "HostRejected", "HostUnavailable", "UnsafeContent",
           "clean_path", "csp_sandboxes", "valid_id"]
