"""Static render of shared files for the annotation view: a thin adapter around ``nh3`` (the ammonia sanitizer).

The sanitizer is a library; this module only chooses its options, rewrites the URLs it lets through and bounds size.
There is no hand-written tokenizer or serializer here.

Library facts, re-confirmed against nh3 0.3.7 (ammonia 4.1.4) for Step 0 of the plan:

* A wheel for abi3 manylinux x86_64 exists (also cp314), so the pin installs on the supported Pythons.
* The style element is kept when ``clean_content_tags`` is given explicitly and does not list it. The link element is
  kept with ``link`` in ``tags``. Both survive, so the static copy keeps author CSS.
* ``attribute_filter`` runs AFTER the scheme check for absolute URLs (a ``javascript:`` href never reaches it) while
  relative URLs do reach it; ``data:`` reaches it because ``data`` is in ``url_schemes``. This module does not depend on
  that order: the resolver re-checks every scheme itself.
* ``@import url(http://...)`` inside style element text is kept verbatim. Nothing here parses CSS text, so the view's
  CSP (``style-src-elem``) is the only barrier against that load. Style ATTRIBUTES are different: they are rewritten or
  dropped here (see ``_filter_style``).
* Setting ``link_rel`` while ``rel`` is an allowed attribute raises ValueError, and a tag that is both allowed and in
  ``clean_content_tags`` is a Rust panic (``pyo3_runtime.PanicException``, a BaseException, not an Exception). Neither
  combination is ever passed, and the panic is additionally contained at the call boundary so hostile input can never
  take the process down.
* Frame, plugin, svg, math, template, canvas, media, form-control and raw-text elements are removed WITH their content
  (``clean_content_tags``). Forms, meta, base, noscript and every ``on*`` attribute are not allowed, so they are dropped
  with their markup unwrapped.
"""

from __future__ import annotations

import html
import posixpath
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection
from urllib.parse import quote, unquote, urlsplit

import nh3

from .share_bundle import ShareBundleError, content_type_for, normalise_name

RENDER_VERSION = 1
MAX_RENDER_INPUT = 2 * 1024 * 1024
MAX_RENDER_OUTPUT = 4 * 1024 * 1024
STATIC_NOTE = "Scripts, frames and embedded media are not shown in this static copy"

Resolver = Callable[[str, str], str | None]


class RenderTooLarge(Exception):
    """The input or the sanitized output is over the render caps."""


class RenderFailed(Exception):
    """The sanitizer library failed (including a Rust panic) on this input."""


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
_STRIP_IN_URL = re.compile(r"[\t\r\n]")
_DATA_IMAGE = re.compile(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]{0,262144}", re.I)
_ABSOLUTE_OK = {"http", "https", "mailto"}
_FRAGMENT_SAFE = "-._~!$&'()*+,;=:@/?"


def share_resolver(name: str, index: Collection[str]) -> Resolver:
    """The share host's URL rewriting for the annotation view of the file ``name`` (a name in ``index``)."""
    depth = name.count("/")
    up = "../" * depth

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


# ----------------------------------------------------------------------------------------------------------- CSS in style
_MAX_STYLE = 8192
# One pass, no nesting, every quantifier bounded and over a class disjoint from its delimiters: failed starts are O(1).
_CSS_URL = re.compile(
    r"""url\(\s*(?:"([^"\\()]{0,2048})"|'([^'\\()]{0,2048})'|([^\s"'()\\]{0,2048}))\s*\)""",
    re.I,
)
_CSS_REJECT = re.compile(
    r"expression|@import|behaviou?r|binding|image-set|cross-fade|\bimage\s*\(|\bsrc\s*\(|\belement\s*\(|"
    r"javascript:|vbscript:|/\*|\\|position\s*:\s*(?:fixed|sticky)",
    re.I,
)


def _filter_style(value: str, resolve: Resolver) -> str | None:
    """Rewrite url() through the resolver; drop the whole attribute for anything this cannot prove harmless."""
    if len(value) > _MAX_STYLE or _CSS_REJECT.search(value):
        return None

    def sub(match: re.Match[str]) -> str:
        target = next(g for g in match.groups() if g is not None) if any(g is not None for g in match.groups()) else ""
        new = resolve("css", target)
        if new is None or not _scheme_ok("css", new) or any(c in new for c in "()\"'\\ \n\r\t"):
            return "none"
        return f"url({new})"

    out = _CSS_URL.sub(sub, value)
    if re.search(r"url\s*\(", out, re.I) and out.lower().count("url(") != len(_CSS_URL.findall(value)):
        return None  # a url( the single pass did not understand
    return out


# --------------------------------------------------------------------------------------------------------- sanitization
ALLOWED_TAGS: frozenset[str] = frozenset(
    "a abbr address article aside b bdi bdo blockquote br caption cite code col colgroup data dd del details dfn div dl dt "
    "em figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li link main mark nav ol p pre q rp rt ruby s "
    "samp section small span strong style sub summary sup table tbody td tfoot th thead time tr u ul var wbr".split()
)
CLEAN_CONTENT_TAGS: frozenset[str] = frozenset(
    "script template iframe frame frameset noframes object embed applet portal fencedframe svg math canvas video audio "
    "textarea select title noembed xmp plaintext".split()
)
_ATTRIBUTES: dict[str, set[str]] = {
    "*": {"class", "id", "title", "lang", "dir", "role", "style"},
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "headers", "scope"},
    "th": {"colspan", "rowspan", "headers", "scope"},
    "ol": {"start", "reversed", "type"},
    "li": {"value"},
    "col": {"span"},
    "colgroup": {"span"},
    "time": {"datetime", "value"},
    "data": {"datetime", "value"},
    "link": {"rel", "href"},
}
_SET_ATTRS: dict[str, dict[str, str]] = {"a": {"target": "_blank", "rel": "noopener noreferrer"}}
_URL_CONTEXT = {("a", "href"): "a", ("img", "src"): "img", ("link", "href"): "link"}

# An overlap is a Rust panic inside the library; fail at import instead.
assert not ALLOWED_TAGS & CLEAN_CONTENT_TAGS


def _scheme_ok(context: str, url: str) -> bool:
    """Defence in depth: whatever a resolver returns, only these shapes reach the output."""
    compact = _STRIP_IN_URL.sub("", url).strip(" \x0c")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in compact):
        return False
    if context == "img" and _DATA_IMAGE.fullmatch(compact):
        return True
    scheme = urlsplit(compact).scheme.lower() if ":" in compact.split("/")[0].split("?")[0].split("#")[0] else ""
    if scheme:
        return context == "a" and scheme in _ABSOLUTE_OK
    return not compact.startswith(("//", "\\")) and "\\" not in compact


def _make_filter(resolve: Resolver) -> Callable[[str, str, str], str | None]:
    def attribute_filter(tag: str, attr: str, value: str) -> str | None:
        try:
            context = _URL_CONTEXT.get((tag, attr))
            if context is not None:
                new = resolve(context, value)
                return new if new is not None and _scheme_ok(context, new) else None
            if tag == "link" and attr == "rel":
                return "stylesheet" if value.strip().lower() == "stylesheet" else None
            if attr == "style":
                return _filter_style(value, resolve)
            return value
        except Exception:  # noqa: BLE001 - a resolver bug must drop the attribute, never break or leak
            return None

    return attribute_filter


# ammonia's cost grows with the square of the retained nesting depth (about 3 s at 40 000 levels, 30 s at 100 000), so
# hostile depth is cut off before the library sees it. This is a linear over-estimate from tag names only (it parses
# nothing and builds nothing): void and optional-end-tag elements do not count, any closing tag pops one level.
MAX_NESTING = 10_000
_TAG_NAME = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)")
_NOT_NESTING = frozenset(
    "area base br col embed hr img input link meta source track wbr p li dt dd tr td th tbody thead tfoot option optgroup "
    "colgroup rp rt".split()
)


def _bound_nesting(text: str) -> str:
    """The text cut at the point where estimated nesting would pass ``MAX_NESTING`` (the library closes what is open)."""
    depth = 0
    for match in _TAG_NAME.finditer(text):
        if match.group(1):
            depth = max(0, depth - 1)
        elif match.group(2).lower() not in _NOT_NESTING:
            depth += 1
            if depth > MAX_NESTING:
                return text[: match.start()]
    return text


def _decode(data: bytes) -> str:
    if len(data) > MAX_RENDER_INPUT:
        raise RenderTooLarge("input is over the render cap")
    return data.decode("utf-8", errors="replace")


def _checked(out: str) -> str:
    if len(out) > MAX_RENDER_OUTPUT or len(out.encode("utf-8")) > MAX_RENDER_OUTPUT:
        raise RenderTooLarge("rendered output is over the cap")
    return out


def sanitize_html(data: bytes, *, resolve: Resolver) -> str:
    """The sanitized fragment for hostile HTML bytes. Raises ``RenderTooLarge`` or ``RenderFailed``."""
    text = _bound_nesting(_decode(data))
    try:
        out = nh3.clean(
            text,
            tags=set(ALLOWED_TAGS),
            clean_content_tags=set(CLEAN_CONTENT_TAGS),
            attributes={k: set(v) for k, v in _ATTRIBUTES.items()},
            attribute_filter=_make_filter(resolve),
            strip_comments=True,
            link_rel=None,
            generic_attribute_prefixes={"aria-"},
            set_tag_attribute_values={k: dict(v) for k, v in _SET_ATTRS.items()},
            url_schemes={"http", "https", "mailto", "data"},
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - the library's Rust panic is a BaseException
        raise RenderFailed(f"sanitizer failed: {type(exc).__name__}") from None
    return _checked(out)


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
