"""The isolated half of the static render: option choice, URL filtering and the ``nh3`` call, plus the worker loop.

This module imports only ``nh3`` and the standard library, so the child process that runs it never pulls in the rest of
the service. The parent (``share_render``) starts it with ``python -m`` style isolation, sends one framed JSON job per
request and answers the child's URL-resolution callbacks, because the resolver stays in the parent. A frame is an
8-byte big-endian length followed by UTF-8 JSON.

Child to parent: ``{"t": "resolve", "c": context, "u": url}`` (the parent answers ``{"r": url-or-null}``),
``{"t": "done", "out": html}``, ``{"t": "failed"}`` (the library failed, including a Rust panic).
Parent to child: ``{"t": "job", "text": html}``.

Backstops set in the child: a CPU limit re-armed before every job and an address-space limit. The parent's wall-clock
deadline and SIGKILL are the guarantee; these only stop a runaway child that the parent has not killed yet.
"""

from __future__ import annotations

import json
import os
import re
import resource
import struct
import sys
import time
from collections.abc import Callable
from typing import BinaryIO
from urllib.parse import urlsplit

import nh3

Resolver = Callable[[str, str], str | None]

MAX_FRAME = 16 * 1024 * 1024
CHILD_ADDRESS_SPACE = 1 << 30


class SanitizerFailed(Exception):
    """The library failed on this input (including a Rust panic, which is a BaseException)."""


_STRIP_IN_URL = re.compile(r"[\t\r\n]")
_DATA_IMAGE = re.compile(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]{0,262144}", re.I)
_ABSOLUTE_OK = {"http", "https", "mailto"}
_FRAGMENT_SAFE = "-._~!$&'()*+,;=:@/?"


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


# Raw text ends only at the closing style tag, so a "<" inside a kept style element is suspicious: drop the element.
_STYLE_ELEMENT = re.compile(r"<style(?:\s[^>]*)?>(.*?)</style>", re.I | re.S)


def _drop_suspicious_style(out: str) -> str:
    return _STYLE_ELEMENT.sub(lambda m: "" if "<" in m.group(1) else m.group(0), out)


def sanitize_text(text: str, resolve: Resolver) -> str:
    """One ``nh3.clean`` call with the fixed options. Raises ``SanitizerFailed`` on any library failure."""
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
        raise SanitizerFailed(type(exc).__name__) from None
    return _drop_suspicious_style(out)


# ----------------------------------------------------------------------------------------------------------- the worker
def write_frame(out: BinaryIO, obj: dict[str, object]) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    out.write(struct.pack(">Q", len(data)) + data)
    out.flush()


def read_frame(inp: BinaryIO) -> dict[str, object] | None:
    head = inp.read(8)
    if len(head) < 8:
        return None
    (size,) = struct.unpack(">Q", head)
    if size > MAX_FRAME:
        return None
    body = inp.read(size)
    if len(body) < size:
        return None
    value = json.loads(body.decode("utf-8"))
    return value if isinstance(value, dict) else None


def serve(inp: BinaryIO, out: BinaryIO) -> None:
    """The child loop: read a job, sanitize it asking the parent to resolve URLs, answer, repeat until EOF."""
    cache: dict[tuple[str, str], str | None] = {}

    def resolve(context: str, url: str) -> str | None:
        key = (context, url)
        if key in cache:
            return cache[key]
        write_frame(out, {"t": "resolve", "c": context, "u": url})
        reply = read_frame(inp)
        if reply is None:
            os._exit(3)  # the parent went away mid-job
        value = reply.get("r")
        cache[key] = value if isinstance(value, str) else None
        return cache[key]

    while True:
        job = read_frame(inp)
        if job is None or job.get("t") != "job" or not isinstance(job.get("text"), str):
            return
        cache.clear()
        used = int(time.process_time())
        soft = used + int(SAFETY_CPU_SECONDS)
        resource.setrlimit(resource.RLIMIT_CPU, (soft, resource.RLIM_INFINITY))
        try:
            write_frame(out, {"t": "done", "out": sanitize_text(job["text"], resolve)})  # type: ignore[arg-type]
        except SanitizerFailed:
            write_frame(out, {"t": "failed"})


SAFETY_CPU_SECONDS = 5.0


def main() -> None:
    """Entry point of the child. Standard output is reclaimed for the protocol; the library never writes to it."""
    inp = os.fdopen(os.dup(0), "rb", buffering=1 << 20)
    out = os.fdopen(os.dup(1), "wb", buffering=1 << 20)
    os.dup2(2, 1)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (CHILD_ADDRESS_SPACE, CHILD_ADDRESS_SPACE))
    except (ValueError, OSError):
        pass
    serve(inp, out)
    sys.exit(0)
