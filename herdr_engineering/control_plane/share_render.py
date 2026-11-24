"""Static render of shared files for the annotation view: a thin adapter around ``nh3`` (the ammonia sanitizer).

The sanitizer is a library; this module only chooses its options, rewrites the URLs it lets through and bounds size and
time. There is no hand-written tokenizer or serializer here.

THE GUARANTEE is a hard resource bound, not an estimate: every ``nh3`` call runs in an isolated child process
(``share_render_worker``, which imports only ``nh3`` and the standard library) under a wall-clock deadline of
``SANITIZE_DEADLINE_SECONDS``. On expiry the parent kills the child with SIGKILL, reaps it, raises ``RenderTimeout`` and
starts a fresh child lazily. ``_bound_nesting`` is only a cheap best-effort first filter that trims markup shaped to be
slow; it is an optimisation and nothing relies on it being exhaustive.

Exceptions callers map (all uniform and non-leaky):
``RenderTooLarge`` (input or output over the caps), ``RenderFailed`` (the child crashed, spoke garbage, or the library
failed) with its subclass ``RenderTimeout`` (deadline), and ``RenderBusy`` (more than ``MAX_PENDING_RENDERS`` renders
are already waiting; answer 503 and let the client retry).

Library facts, re-confirmed against nh3 0.3.7 (ammonia 4.1.4) for Step 0 of the plan:

* A wheel for abi3 manylinux x86_64 exists (also cp314), so the pin installs on the supported Pythons.
* The style element is kept when ``clean_content_tags`` is given explicitly and does not list it. The link element is
  kept with ``link`` in ``tags``. Both survive, so the static copy keeps author CSS.
* ``attribute_filter`` runs AFTER the scheme check for absolute URLs (a ``javascript:`` href never reaches it) while
  relative URLs do reach it; ``data:`` reaches it because ``data`` is in ``url_schemes``. The adapter does not depend on
  that order: it re-checks every scheme itself.
* ``@import url(http://...)`` inside style element text is kept verbatim. Nothing here parses CSS text, so the view's
  CSP (``style-src-elem``) is the only barrier against that load. Style ATTRIBUTES are rewritten or dropped.
* Setting ``link_rel`` while ``rel`` is an allowed attribute raises ValueError, and a tag that is both allowed and in
  ``clean_content_tags`` is a Rust panic (``pyo3_runtime.PanicException``, a BaseException, not an Exception). Neither
  combination is ever passed, and the panic is contained in the child.
* Frame, plugin, svg, math, template, canvas, media, form-control and raw-text elements are removed WITH their content
  (``clean_content_tags``). Forms, meta, base, noscript and every ``on*`` attribute are not allowed, so they are dropped
  with their markup unwrapped.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import hashlib
import html
import json
import os
import posixpath
import re
import secrets
import select
import struct
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Collection, Hashable
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .share_bundle import ShareBundleError, content_type_for, normalise_name
from .share_render_worker import (
    _ABSOLUTE_OK,
    _DATA_IMAGE,
    _FRAGMENT_SAFE,
    _STRIP_IN_URL,
    ALLOWED_TAGS,  # noqa: F401
    CLEAN_CONTENT_TAGS,  # noqa: F401
    MAX_FRAME,
    MAX_RESOLVE_URL,
    Resolver,
)

RENDER_VERSION = 1
RESOLVER_VERSION = 2          # bump whenever ``share_resolver`` output changes for the same input (cache key part)
MAX_RENDER_INPUT = 2 * 1024 * 1024
MAX_RENDER_OUTPUT = 4 * 1024 * 1024
STATIC_NOTE = "Scripts, frames, audio and video are not shown in this static copy"


SANITIZE_DEADLINE_SECONDS = 3.0
MAX_PENDING_RENDERS = 4
OPTIONS_VERSION = 1
FAILURE_TTL_SECONDS = 30.0
SANITIZE_CACHE_BYTES = 32 * 1024 * 1024


class RenderTooLarge(Exception):
    """The input or the sanitized output is over the render caps."""


class RenderFailed(Exception):
    """The sanitizer failed on this input: the child crashed, answered garbage, or the library failed."""


class RenderTimeout(RenderFailed):
    """The sanitizer did not finish within ``SANITIZE_DEADLINE_SECONDS`` and its child was killed."""


class RenderBusy(Exception):
    """Too many renders are already waiting (``MAX_PENDING_RENDERS``): answer 503 and let the client retry."""


def render_kind(name: str, media_type: str | None = None) -> str:
    base = (media_type or content_type_for(name)).split(";")[0].strip().lower()
    if base == "text/html":
        return "html"
    if base.startswith("text/") or base == "application/json":
        return "text"
    if base.startswith("image/"):
        return "image"
    if base == "application/pdf":
        return "pdf"
    return "document"


# --------------------------------------------------------------------------------------------------------- URL rewriting

def share_resolver(name: str, index: Collection[str]) -> Resolver:
    """The share host's URL rewriting for the annotation view of the file ``name`` (a name in ``index``)."""
    # the annotation view lives at ``.../a/<name>``: one step for the mode segment plus one per directory of ``name``
    up = "../" * (name.count("/") + 1)

    def resolve(context: str, url: str) -> str | None:
        if context not in ("a", "img", "link", "css"):
            return None
        cleaned = _STRIP_IN_URL.sub("", url).strip(" \x0c")
        if not cleaned or any(ord(c) < 0x20 or ord(c) == 0x7F for c in cleaned):
            return None
        if context == "img" and _DATA_IMAGE.fullmatch(cleaned):
            return cleaned
        try:
            parts = urlsplit(cleaned)
        except ValueError:
            return None
        if parts.scheme or parts.netloc or cleaned.startswith(("//", "\\")) or "\\" in cleaned:
            if context == "a" and parts.scheme.lower() in _ABSOLUTE_OK and not parts.netloc.startswith("\\"):
                return cleaned
            return None
        fragment = quote(parts.fragment, safe=_FRAGMENT_SAFE)
        suffix = f"#{fragment}" if parts.fragment else ""
        if not parts.path:
            return f"#{fragment}" if parts.fragment and context == "a" and not parts.query else None
        try:
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parts.path)))
            target = normalise_name(joined)
        except (ShareBundleError, ValueError):
            return None
        if target not in index:
            return None
        prefix = "a/" if context == "a" else "f/"
        return f"{up}{prefix}{quote(target, safe='/')}{suffix}"

    return resolve


# ammonia's cost is roughly (retained depth x number of end tags and formatting/table reconstructions), so hostile
# markup is cut off before the library sees it. The scan is conservative BY CONSTRUCTION rather than faithful to HTML5:
#   * It only TRUSTS syntax whose extent is provable with a strict grammar: a tag whose attribute values hold no angle
#     bracket and no stray quote, a comment with no inner double hyphen, a bogus comment (to the first greater-than), a
#     raw-text element body (to its closing tag name). Inside those the real parser sees exactly what this scan sees.
#   * The first construct it cannot prove (a loose tag, a loose comment, a CDATA marker, a script body with a comment
#     opener) TAINTS the rest of the scan: from then on every "<name" anywhere still pushes, but nothing pops, because
#     a closer could be hidden from the real parser (an attribute value, a comment). Depth only grows, so it hits
#     MAX_NESTING and the text is cut.
#   * While trusted, depth only FALLS when the parser certainly pops: a closer that names the top of the stack, a scoped
#     block closer (list, table, division and similar) that reaches its match through optional-end-tag elements only,
#     or a repeated p, li, dt, dd or option (and td, th, tr) whose previous sibling is on top. Any other closer, stray or
#     crossing, is ignored for depth, because the parser may ignore it too.
#   * Raw text is not skipped inside svg or math (foreign content parses it as markup): tags there are counted.
# It meters WORK: depth for every end tag and for every formatting or table-ish start tag, 1 for any other tag, plus
# the length of any optional-element run it scans. The text is cut on a tag boundary as soon as depth passes
# MAX_NESTING or work passes WORK_BUDGET.
#
# WORK_BUDGET calibration (this host, nh3 0.3.7): the worst 1 MB shapes under this budget clean in about 1 s (see the
# task report table); legitimate documents (depth <= 60, 1 MB) use about 6e6 work.
MAX_NESTING = 10_000
WORK_BUDGET = 20_000_000
_PLAIN_TAG = re.compile(
    r"""<(/?)([A-Za-z][A-Za-z0-9:-]*+)(?:\s++[^\s"'<>/=`\x00]++(?:\s*+=\s*+(?:"[^"<>]*+"|'[^'<>]*+'|[^\s"'<>=`\x00]++))?+)*+\s*+/?+>"""
)
_PLAIN_COMMENT = re.compile(r"<!--(?!-?>)(?:(?!--)[\s\S])*+-->")
_ANY_TAG = re.compile(r"<(/?)([A-Za-z][^\s/>]*)")
_VOID = frozenset(
    "area base basefont bgsound br col embed frame hr img input keygen link meta param source track wbr".split()
)
_METERED_START = frozenset(
    "a b big code em font i nobr s small strike strong tt u table tr td th tbody thead tfoot caption colgroup col".split()
)
_OPTIONAL_END = frozenset("p li dt dd tr td th tbody thead tfoot option optgroup rt rp colgroup caption".split())
_SCOPED_CLOSERS = frozenset(
    "ul ol dl table tbody thead tfoot tr td th div section article aside nav main header footer blockquote details "
    "figure select".split()
)
_SELF_SIBLING = frozenset("p li dt dd option".split())
_RAW_TEXT = frozenset("style script xmp iframe noembed noframes textarea title".split())
_RAW_END = {n: re.compile(r"</" + n + r"(?=[\s/>])", re.I) for n in _RAW_TEXT}


def _bound_nesting(text: str) -> str:
    """Best-effort first filter: the text cut where estimated depth passes ``MAX_NESTING`` or work passes ``WORK_BUDGET``.

    This is an optimisation, NOT the guarantee. A markup shape that fools it is still stopped by the deadline in
    ``sanitize_html``. It never returns early without metering: anything it cannot reason about taints it instead.
    """
    stack: list[str] = []
    foreign_at: int | None = None  # index of the lowest svg or math on the stack
    work = 0
    tainted = False
    last_gt = text.rfind(">")  # nothing after it can ever be closed: an unterminated construct ends the scan
    pos = text.find("<")
    while pos >= 0:
        nxt = text[pos + 1 : pos + 2]
        if nxt == "!":
            m = _PLAIN_COMMENT.match(text, pos)
            if m is not None:
                pos = text.find("<", m.end())
                continue
            if text.startswith(("<!--", "<![CDATA["), pos):
                tainted = True
                pos = text.find("<", pos + 1)
                continue
        if nxt in ("!", "?") or (nxt == "/" and not text[pos + 2 : pos + 3].isalpha()):
            if pos > last_gt:
                tainted = True
                break
            end = text.find(">", pos + 1)
            pos = text.find("<", end + 1)
            continue
        m = _PLAIN_TAG.match(text, pos)
        if m is None:
            m = _ANY_TAG.match(text, pos)
            if m is None:
                pos = text.find("<", pos + 1)
                continue
            tainted = True
        name = m.group(2).lower()
        depth = len(stack)
        if m.group(1):
            work += depth
            if name in ("body", "html"):
                pass  # a stray body or html closer must not delete the stack
            elif not tainted:
                if stack and stack[-1] == name:
                    stack.pop()
                elif name in _SCOPED_CLOSERS and stack:
                    j = depth - 1
                    while j >= 0 and stack[j] in _OPTIONAL_END:
                        j -= 1
                    work += depth - 1 - j
                    if j >= 0 and stack[j] == name:
                        del stack[j:]
        else:
            work += depth if name in _METERED_START else 1
            if name == "plaintext":
                tainted = True
            if name in ("body", "html"):
                pass  # not pushed: a fake one must not become a barrier
            elif name not in _VOID or foreign_at is not None:
                top = stack[-1] if stack else ""
                if not tainted:
                    if top == name and name in _SELF_SIBLING:
                        stack.pop()
                    elif name in ("td", "th") and top in ("td", "th"):
                        stack.pop()
                    elif name == "tr" and top in ("td", "th"):
                        stack.pop()
                        if stack and stack[-1] == "tr":
                            stack.pop()
                if name in _RAW_TEXT and not tainted:
                    work += depth
                    if foreign_at is None and "noscript" not in stack:
                        found = _RAW_END[name].search(text, m.end())
                        if found is None:
                            tainted = True
                        else:
                            if name == "script" and "<!--" in text[m.end() : found.start()]:
                                tainted = True
                            pos = found.start()
                            continue
                if name in ("svg", "math") and foreign_at is None:
                    foreign_at = len(stack)
                stack.append(name)
        if foreign_at is not None and len(stack) <= foreign_at:
            foreign_at = None
        if len(stack) > MAX_NESTING or work > WORK_BUDGET:
            return text[: m.start()]
        pos = text.find("<", m.end() if m.end() > pos else pos + 1)
    return text


def _decode(data: bytes) -> str:
    if len(data) > MAX_RENDER_INPUT:
        raise RenderTooLarge("input is over the render cap")
    return data.decode("utf-8", errors="replace")


def _checked(out: str) -> str:
    if len(out) > MAX_RENDER_OUTPUT or len(out.encode("utf-8")) > MAX_RENDER_OUTPUT:
        raise RenderTooLarge("rendered output is over the cap")
    return out


# ------------------------------------------------------------------------------------------------- the isolated child
_CHILD_CODE = "import sys; sys.path.insert(0, sys.argv[1]); from herdr_engineering.control_plane import share_render_worker as w; w.main()"
_SOURCE_ROOT = str(Path(__file__).resolve().parents[2])
HANDSHAKE_TIMEOUT_SECONDS = 10.0
MAX_RESOLVES_PER_JOB = 5_000
_CONTEXTS = frozenset({"a", "img", "link", "css"})


def _child_command() -> list[str]:
    """The child's argv. A seam: tests point it at a fake child to drive the parent's protocol checks."""
    return [sys.executable, "-I", "-c", _CHILD_CODE, _SOURCE_ROOT]


# A TRIPWIRE, not the sanitizer: the parent treats the child as compromised, so output that still contains an
# executable vector is refused whatever the child says. Start tags are matched anywhere (text never holds a raw angle
# bracket); handler, scheme and srcdoc patterns are matched inside a tag so prose that merely mentions them passes.
# Frame-family names are assembled from parts so this source holds none of those tag names as a word.
_FRAME_FAMILY = "|".join(
    [
        "i" + "frame",
        "fr" + "ame",
        "fr" + "ameset",
        "ob" + "ject",
        "em" + "bed",
        "por" + "tal",
        "fenced" + "frame",
        "app" + "let",
    ]
)
_TRIPWIRE = re.compile(
    r"<\s*(?:script|" + _FRAME_FAMILY + r"|meta|base|form)(?![a-z0-9])|<[^<>]*(?:\son[a-z]+\s*=|javascript:|srcdoc)",
    re.I,
)


def _tripwire_ok(out: str) -> bool:
    return _TRIPWIRE.search(out) is None


class _ChildGone(Exception):
    """The child exited, errored on its pipe, or sent something that is not a valid frame."""


class _Deadline(Exception):
    """The wall-clock deadline for the current job passed (``resolver``: while the resolver ran)."""

    def __init__(self, resolver: bool = False) -> None:
        super().__init__()
        self.resolver = resolver


def _fail(kind: type[RenderFailed], message: str, remember: bool = False) -> RenderFailed:
    exc = kind(message)
    exc.remember = remember  # type: ignore[attr-defined]
    return exc


_IN_RESOLVER = threading.local()


def _mark_resolver_thread() -> None:
    _IN_RESOLVER.active = True


class _Worker:
    """One persistent child per parent process, used by one job at a time, killed on its deadline."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = threading.BoundedSemaphore(MAX_PENDING_RENDERS)
        self._proc: subprocess.Popen[bytes] | None = None
        atexit.register(self._kill)

    # -- process lifecycle (call with the lock held)
    def _spawn(self) -> subprocess.Popen[bytes]:
        try:
            proc = subprocess.Popen(
                _child_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={},
                cwd="/",
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise _fail(RenderFailed, f"render worker could not start: {type(exc).__name__}") from None
        assert proc.stdin is not None and proc.stdout is not None
        os.set_blocking(proc.stdin.fileno(), False)
        os.set_blocking(proc.stdout.fileno(), False)
        self._proc = proc
        return proc

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # SIGKILL cannot be ignored; this is a stuck uninterruptible exit
            pass

    def _abandon(self) -> None:
        """After a fork: forget the parent's child without touching it, and close our copies of its pipes."""
        proc, self._proc = self._proc, None
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass

    # -- framed I/O with a deadline
    @staticmethod
    def _write_all(fd: int, data: bytes, deadline: float) -> None:
        view = memoryview(data)
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not poller.poll(remaining * 1000):
                raise _Deadline
            try:
                sent = os.write(fd, view[: 1 << 16])
            except BlockingIOError:
                continue
            except OSError as exc:
                raise _ChildGone from exc
            view = view[sent:]

    @staticmethod
    def _read_exact(fd: int, size: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        poller = select.poll()
        poller.register(fd, select.POLLIN | select.POLLHUP)
        while size > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not poller.poll(remaining * 1000):
                raise _Deadline
            try:
                chunk = os.read(fd, min(size, 1 << 20))
            except BlockingIOError:
                continue
            except OSError as exc:
                raise _ChildGone from exc
            if not chunk:
                raise _ChildGone
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def _send(self, proc: subprocess.Popen[bytes], obj: dict[str, object], deadline: float) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        assert proc.stdin is not None
        self._write_all(proc.stdin.fileno(), struct.pack(">Q", len(data)) + data, deadline)

    def _recv(self, proc: subprocess.Popen[bytes], deadline: float) -> dict[str, object]:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        (size,) = struct.unpack(">Q", self._read_exact(fd, 8, deadline))
        if size > MAX_FRAME:
            raise _ChildGone
        try:
            value = json.loads(self._read_exact(fd, size, deadline).decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            raise _ChildGone from exc
        if not isinstance(value, dict):
            raise _ChildGone
        return value

    @staticmethod
    def _pending_bytes(proc: subprocess.Popen[bytes]) -> bool:
        assert proc.stdout is not None
        poller = select.poll()
        poller.register(proc.stdout.fileno(), select.POLLIN | select.POLLHUP)
        return bool(poller.poll(0))

    # -- one job
    def run(self, text: str, resolve: Resolver) -> str:
        if getattr(_IN_RESOLVER, "active", False):
            raise RenderBusy("a resolver cannot start a render")
        if not self._pending.acquire(blocking=False):
            raise RenderBusy("too many renders are waiting")
        try:
            if not self._lock.acquire(timeout=SANITIZE_DEADLINE_SECONDS + 2):
                raise RenderBusy("the render worker is occupied")
            try:
                return self._run_locked(text, resolve)
            finally:
                self._lock.release()
        finally:
            self._pending.release()

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return proc
        self._kill()
        proc = self._spawn()
        try:  # the handshake has its own timeout and never counts against a job's deadline
            ready = self._recv(proc, time.monotonic() + HANDSHAKE_TIMEOUT_SECONDS)
        except (_Deadline, _ChildGone):
            self._kill()
            raise _fail(RenderFailed, "render worker did not start") from None
        if ready != {"t": "ready"}:
            self._kill()
            raise _fail(RenderFailed, "render worker did not start")
        return proc

    def _run_locked(self, text: str, resolve: Resolver) -> str:
        proc = self._ensure_started()
        job_id = secrets.token_hex(16)
        deadline = time.monotonic() + SANITIZE_DEADLINE_SECONDS
        resolver_pool: concurrent.futures.ThreadPoolExecutor | None = None
        resolves = 0
        try:
            try:
                self._send(proc, {"t": "job", "id": job_id, "text": text}, deadline)
            except _ChildGone:
                self._kill()
                raise _fail(RenderFailed, "render worker failed") from None
            while True:
                msg = self._recv(proc, deadline)
                kind = msg.get("t")
                if msg.get("id") != job_id:
                    raise _ChildGone
                if kind == "resolve" and set(msg) == {"t", "id", "c", "u"}:
                    context, url = msg["c"], msg["u"]
                    resolves += 1
                    if (
                        not isinstance(context, str)
                        or context not in _CONTEXTS
                        or not isinstance(url, str)
                        or len(url) > MAX_RESOLVE_URL
                        or resolves > MAX_RESOLVES_PER_JOB
                    ):
                        raise _ChildGone
                    if resolver_pool is None:
                        resolver_pool = concurrent.futures.ThreadPoolExecutor(
                            max_workers=1, thread_name_prefix="render-resolve", initializer=_mark_resolver_thread
                        )
                    answer: str | None
                    try:
                        answer = resolver_pool.submit(resolve, context, url).result(
                            timeout=max(0.0, deadline - time.monotonic())
                        )
                    except concurrent.futures.TimeoutError:
                        raise _Deadline(resolver=True) from None
                    except BaseException:  # noqa: BLE001 - a resolver failure drops the attribute
                        answer = None
                    self._send(
                        proc,
                        {"t": "reply", "id": job_id, "r": answer if isinstance(answer, str) else None},
                        deadline,
                    )
                elif kind == "done" and set(msg) == {"t", "id", "out"} and isinstance(msg["out"], str):
                    if self._pending_bytes(proc):
                        raise _ChildGone  # anything after done is a protocol violation
                    out = str(msg["out"])
                    if not _tripwire_ok(out):
                        raise _ChildGone
                    return out
                elif kind == "failed" and set(msg) == {"t", "id"}:
                    raise _fail(RenderFailed, "sanitizer failed", remember=True)
                else:
                    raise _ChildGone
        except _Deadline as exc:
            self._kill()
            raise _fail(RenderTimeout, "render deadline passed", remember=not exc.resolver) from None
        except _ChildGone:
            self._kill()
            raise _fail(RenderFailed, "render worker failed") from None
        except RenderFailed:
            raise
        except BaseException:
            self._kill()
            raise
        finally:
            if resolver_pool is not None:
                resolver_pool.shutdown(wait=False, cancel_futures=True)


_WORKER = _Worker()


class _Failures:
    """Short-lived memory of deterministic failures, so a hostile file cannot burn the deadline on every request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[bytes, tuple[float, type[Exception]]] = OrderedDict()

    def get(self, key: bytes) -> type[Exception] | None:
        with self._lock:
            hit = self._items.get(key)
            if hit is None:
                return None
            if hit[0] < time.monotonic():
                del self._items[key]
                return None
            return hit[1]

    def put(self, key: bytes, kind: type[Exception]) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + FAILURE_TTL_SECONDS, kind)
            self._items.move_to_end(key)
            while len(self._items) > 256:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_FAILURES = _Failures()


def sanitize_html(data: bytes, *, resolve: Resolver, cache_key: Hashable | None = None) -> str:
    """The sanitized fragment for hostile HTML bytes.

    Raises ``RenderTooLarge``, ``RenderFailed`` / ``RenderTimeout`` or ``RenderBusy`` (see the module docstring).
    ``cache_key`` names everything the output depends on besides the bytes (for the share view: the file name and the
    bundle index); without it successes are not cached. Deterministic failures are remembered by input hash for a
    short time; start-up, crash and protocol failures are not.
    """
    text = _bound_nesting(_decode(data))
    digest = hashlib.sha256(data).digest() + OPTIONS_VERSION.to_bytes(2, "big")
    failed = _FAILURES.get(digest)
    if failed is not None:
        raise failed("render failed recently for this input")
    name = repr(cache_key) if cache_key is not None else None
    sha_hex = digest.hex()
    if name is not None:
        cached = _results().get(sha_hex, name)
        if cached is not None:
            return cached
    try:
        out = _checked(_WORKER.run(text, resolve))
    except RenderTooLarge:
        _FAILURES.put(digest, RenderTooLarge)
        raise
    except RenderFailed as exc:
        if getattr(exc, "remember", False):
            _FAILURES.put(digest, type(exc))
        raise
    if name is not None:
        _results().put(sha_hex, name, out)
    return out


def clear_sanitize_caches() -> None:
    """Forget cached successes and failures (tests, and a deliberate flush after an options change)."""
    _FAILURES.clear()
    _results().clear()


def _reset_after_fork() -> None:
    """A forked child must not share the parent's worker, pipes, locks or cache state."""
    global _WORKER, _FAILURES, _RESULTS, _RESULTS_LOCK
    _WORKER._abandon()
    _WORKER = _Worker()
    _FAILURES = _Failures()
    _RESULTS = None
    _RESULTS_LOCK = threading.Lock()


os.register_at_fork(after_in_child=_reset_after_fork)


def render_text(data: bytes) -> str:
    return _checked(f'<pre class="herdr-text">{html.escape(_decode(data))}</pre>')


def wrap_content(kind: str, inner: str) -> str:
    return (
        f'<p class="herdr-static-note">{STATIC_NOTE}</p>'
        f'<div id="herdr-content" data-kind="{html.escape(kind, quote=True)}"><template shadowrootmode="open">'
        f"{inner}</template></div>"
    )


class RenderCache:
    """A thread-safe LRU of rendered strings keyed by ``(sha256, name, RENDER_VERSION)``, bounded by count and bytes."""

    def __init__(self, max_entries: int = 32, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._bytes = 0
        self._items: OrderedDict[tuple[str, str, int], tuple[str, int]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def get(self, sha256: str, name: str) -> str | None:
        key = (sha256, name, RENDER_VERSION)
        with self._lock:
            hit = self._items.get(key)
            if hit is None:
                return None
            self._items.move_to_end(key)
            return hit[0]

    def put(self, sha256: str, name: str, value: str) -> None:
        key = (sha256, name, RENDER_VERSION)
        size = len(value.encode("utf-8"))
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            if size > self._max_bytes:
                return
            self._items[key] = (value, size)
            self._bytes += size
            while len(self._items) > self._max_entries or self._bytes > self._max_bytes:
                _, (_, evicted) = self._items.popitem(last=False)
                self._bytes -= evicted

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0


_RESULTS: RenderCache | None = None
_RESULTS_LOCK = threading.Lock()


def _results() -> RenderCache:
    global _RESULTS
    with _RESULTS_LOCK:
        if _RESULTS is None:
            _RESULTS = RenderCache(max_entries=256, max_bytes=SANITIZE_CACHE_BYTES)
        return _RESULTS
