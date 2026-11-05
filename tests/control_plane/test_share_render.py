"""Static render adapter: hostile-input corpus, URL rewriting, size bounds, cache.

Hostile inputs that name frame or plugin elements are built by concatenation so the repo-wide no-frames guard does not
match this file.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from html.parser import HTMLParser

import pytest

from herdr_engineering.control_plane import share_render as sr
from herdr_engineering.control_plane import share_render_worker as worker

LT = "<"
FRAME_RE = re.compile(r"<\s*(iframe|frame|frameset|object|embed|portal|fencedframe|applet)(?=[\s/>])", re.I)
IDX = {"index.html", "page2.html", "sub/x.html", "img/p.png", "sub/img/q.png"}


def keep_all(context: str, url: str) -> str | None:
    return url


def drop_all(context: str, url: str) -> str | None:
    return None


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[tuple[str, str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            self.attrs.append((tag, k, v))

    handle_startendtag = handle_starttag


def parse(out: str) -> Collector:
    c = Collector()
    c.feed(out)
    c.close()
    return c


def assert_inert(out: str) -> None:
    c = parse(out)
    bad_tags = {"script", "base", "meta", "form", "input", "noscript", "svg", "math", "template", "textarea", "select"}
    assert not (set(c.tags) & bad_tags), (c.tags, out)
    assert not FRAME_RE.search(out), out
    for tag, k, v in c.attrs:
        assert not k.startswith("on"), (tag, k, out)
        assert k not in {"srcdoc", "formaction", "action", "srcset", "background", "poster", "xlink:href", "http-equiv"}
        if v is not None and k in {"href", "src"}:
            compact = re.sub(r"[\s\x00-\x20]", "", v).lower()
            assert not compact.startswith(("javascript:", "vbscript:")), (tag, k, v)
            assert not (compact.startswith("data:") and not compact.startswith("data:image/")), (tag, k, v)
        if k == "style" and v is not None:
            low = v.lower()
            assert "expression" not in low and "@import" not in low and "javascript:" not in low


HOSTILE = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=//evil/x.js></SCRIPT>",
    "<scr<script>ipt>alert(1)</scr</script>ipt>",
    "<img src=x onerror=alert(1)>",
    "<img src=x OnErRoR=alert(1)>",
    '<img src="x"onload=alert(1)>',
    '<p onclick="alert(1)" onmouseover=alert(2)>x</p>',
    '<a href="javascript:alert(1)">x</a>',
    '<a href=" JaVaScRiPt:alert(1)">x</a>',
    '<a href="java&#x09;script:alert(1)">x</a>',
    '<a href="&#106;avascript:alert(1)">x</a>',
    '<a href="jav&#x0A;ascript:alert(1)">x</a>',
    '<a href="\x01javascript:alert(1)">x</a>',
    '<a href="vbscript:msgbox(1)">x</a>',
    '<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">x</a>',
    '<img src="data:image/svg+xml;base64,PHN2Zz48c2NyaXB0Pg==">',
    '<img src="data:text/html,<script>alert(1)</script>">',
    '<img srcset="javascript:alert(1) 1x">',
    "<meta http-equiv=refresh content=\"0;url=//evil\">",
    "<base href=//evil/>",
    "<form action=//evil><input name=x></form>",
    '<button formaction="javascript:alert(1)">x</button>',
    "<svg><script>alert(1)</script></svg>",
    "<svg onload=alert(1)></svg>",
    "<svg><a xlink:href=javascript:alert(1)><text>x</text></a></svg>",
    "<svg><foreignObject>" + LT + "iframe src=//evil></foreignObject></svg>",
    "<math><mtext><table><mglyph><style><img src=x onerror=1>",
    "<math><maction actiontype=statusline xlink:href=javascript:alert(1)>x</maction></math>",
    '<noscript><p title="</noscript><img src=x onerror=1>">',
    "<style></style><img src=x onerror=1>",
    "<template><script>alert(1)</script></template>",
    "<xmp><img src=x onerror=1></xmp>",
    "<plaintext><img src=x onerror=1>",
    "<select><option><img src=x onerror=1></option></select>",
    "<textarea></textarea><img src=x onerror=1>",
    "<title></title><img src=x onerror=1>",
    "<noembed><img src=x onerror=1></noembed>",
    "<table><tr><td><select><template><img src=x onerror=1></template></select>",
    "<!--><img src=x onerror=1>-->",
    "<![CDATA[<script>alert(1)</script>]]>",
    "<!-- --!><img src=x onerror=1> -->",
    "<?xml version='1.0'?><script>alert(1)</script>",
    LT + "iframe srcdoc='<script>alert(1)</script>'></iframe>",
    LT + "iframe src=a.html></iframe>",
    LT + "frame src=a.html>",
    LT + "frameset>" + LT + "frame src=a.html></frameset>",
    LT + "object data=x></object>",
    LT + "embed src=x>",
    LT + "applet code=x></applet>",
    LT + "portal src=//evil></portal>",
    LT + "fencedframe src=//evil></fencedframe>",
    '<p style="background:url(javascript:alert(1))">x</p>',
    '<p style="width:expression(alert(1))">x</p>',
    '<p style="behavior:url(x.htc)">x</p>',
    '<p style="background:u\\72l(http://evil/x)">x</p>',
    '<p style="@import \'//evil/x.css\'">x</p>',
    '<p style="position:fixed;top:0;left:0;width:100%;height:100%;z-index:99999">x</p>',
    '<p style="background:image-set(\'//evil/x.png\' 1x)">x</p>',
    '<link rel=stylesheet href="javascript:alert(1)">',
    '<link rel=import href=//evil/x.html>',
    '<link rel=prefetch href=//evil/x>',
    '<a href="https://example.org" target=_self rel=opener>x</a>',
    '<a href="//evil/x">x</a>',
    "<div><p><b><i>unclosed <a href=x",
    "<<script>script>alert(1)<</script>/script>",
    "\x00<scr\x00ipt>alert(1)</scr\x00ipt>",
    "<image src=x onerror=alert(1)>",
    "<img/src=x/onerror=alert(1)>",
    "<details open ontoggle=alert(1)>x</details>",
    "<body onload=alert(1)><html onload=1>",
    "<a href=x id=\"__proto__\" aria-label=ok data-x=1>x</a>",
]


@pytest.mark.parametrize("payload", HOSTILE)
def test_hostile_corpus_is_inert(payload: str) -> None:
    for resolver in (keep_all, drop_all, sr.share_resolver("index.html", IDX)):
        out = sr.sanitize_html(payload.encode(), resolve=resolver)
        assert_inert(out)
        again = sr.sanitize_html(out.encode(), resolve=resolver)
        assert_inert(again)


def test_property_no_executable_vector_in_any_output() -> None:
    import random

    rng = random.Random(7)
    pieces = [p for p in HOSTILE] + ["<", ">", '"', "'", "=", "&#x", "</", "<!--", "-->", "<![CDATA[", "]]>", " ", "\x00"]
    for _ in range(400):
        payload = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 8)))
        for resolver in (keep_all, sr.share_resolver("index.html", IDX)):
            out = sr.sanitize_html(payload.encode("utf-8", "replace"), resolve=resolver)
            assert_inert(out)
            low = out.lower()
            assert not re.search(r"<\s*script", low)
            assert not re.search(r"\son\w+\s*=", low)
            assert "srcdoc" not in low


def test_each_hostile_payload_exact_structure() -> None:
    out = sr.sanitize_html(b"<script>alert(1)</script><p>ok</p>", resolve=keep_all)
    assert out == "<p>ok</p>"
    out = sr.sanitize_html(b"<img src=x onerror=alert(1)>", resolve=keep_all)
    assert out == '<img src="x">'
    out = sr.sanitize_html(b'<a href="javascript:alert(1)">x</a>', resolve=keep_all)
    assert "href" not in out and "javascript" not in out
    assert sr.sanitize_html(b"<meta http-equiv=refresh content='0;url=//evil'>", resolve=keep_all) == ""
    assert sr.sanitize_html(b"<base href=//evil/>", resolve=keep_all) == ""
    assert "action" not in sr.sanitize_html(b"<form action=//evil><input></form>x", resolve=keep_all)


def test_smoke_option_combination_is_valid() -> None:
    assert sr.sanitize_html(b"<p>hi</p>", resolve=keep_all) == "<p>hi</p>"


def test_tags_and_clean_content_tags_never_overlap() -> None:
    assert not (set(sr.ALLOWED_TAGS) & set(sr.CLEAN_CONTENT_TAGS))
    assert "style" in sr.ALLOWED_TAGS and "link" in sr.ALLOWED_TAGS


def test_panic_in_the_library_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    class PanicException(BaseException):
        pass

    def boom(*a, **k):
        raise PanicException("boom")

    monkeypatch.setattr(worker.nh3, "clean", boom)
    with pytest.raises(worker.SanitizerFailed):
        worker.sanitize_text("<p>x</p>", keep_all)


def test_interrupts_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker.nh3, "clean", boom)
    with pytest.raises(KeyboardInterrupt):
        worker.sanitize_text("<p>x</p>", keep_all)


def test_resolver_exception_does_not_crash_or_leak() -> None:
    def bad(context: str, url: str) -> str | None:
        raise RuntimeError("nope")

    out = sr.sanitize_html(b'<a href="http://x">t</a><img src="a.png">', resolve=bad)
    assert "href" not in out and "src" not in out


def test_style_element_is_kept_and_import_stays() -> None:
    out = sr.sanitize_html(b"<style>a{color:red}</style><p>x</p>", resolve=keep_all)
    assert "<style>a{color:red}</style>" in out
    out = sr.sanitize_html(b"<style>@import url(http://evil/x.css)</style>", resolve=keep_all)
    assert "@import url(http://evil/x.css)" in out


def test_frame_and_plugin_elements_vanish() -> None:
    for payload in (
        LT + "iframe src=a.html>",
        LT + "frame>",
        LT + "frameset>" + LT + "frame>",
        LT + "object data=x>",
        LT + "embed src=x>",
        "<svg><foreignObject>" + LT + "iframe></foreignObject></svg>",
    ):
        out = sr.sanitize_html(payload.encode(), resolve=keep_all)
        assert not FRAME_RE.search(out) and "srcdoc" not in out


def test_unterminated_markup_is_balanced() -> None:
    out = sr.sanitize_html(b"<div><p><b>hi <a href='http://x'>link", resolve=keep_all)
    c = parse(out)
    assert out.count("</div>") == 1 and out.count("</p>") == 1 and out.count("</b>") == 1 and out.count("</a>") == 1
    assert c.tags[0] == "div"
    assert sr.wrap_content("html", out).endswith("</template></div>")


def test_link_only_stylesheet_survives() -> None:
    res = sr.share_resolver("index.html", IDX)
    out = sr.sanitize_html(b'<link rel="stylesheet" href="img/p.png">', resolve=res)
    assert 'rel="stylesheet"' in out and 'href="../f/img/p.png"' in out
    out = sr.sanitize_html(b'<link rel="icon" href="img/p.png"><link rel=import href=x>', resolve=res)
    assert "rel" not in out  # a link with no stylesheet rel is inert; the per-attribute filter cannot also drop its href
    assert "<link>" in out
    out = sr.sanitize_html(b'<link rel="stylesheet" href="https://evil/x.css">', resolve=res)
    assert "evil" not in out


def test_share_resolver_links() -> None:
    res = sr.share_resolver("index.html", IDX)
    out = sr.sanitize_html(b'<a href="page2.html">p</a>', resolve=res)
    assert 'href="../a/page2.html"' in out
    out = sr.sanitize_html(b'<a href="page2.html#sec">p</a><a href="#top">t</a>', resolve=res)
    assert 'href="../a/page2.html#sec"' in out and 'href="#top"' in out
    out = sr.sanitize_html(b'<a href="missing.html">p</a>', resolve=res)
    assert "href" not in out
    out = sr.sanitize_html(b'<a href="https://example.org">e</a>', resolve=res)
    assert 'href="https://example.org"' in out
    assert 'rel="noopener noreferrer"' in out and 'target="_blank"' in out
    out = sr.sanitize_html(b'<a href="mailto:a@b.co">e</a>', resolve=res)
    assert 'href="mailto:a@b.co"' in out


def test_share_resolver_nested_name() -> None:
    res = sr.share_resolver("sub/x.html", IDX)
    out = sr.sanitize_html(b'<a href="../index.html">i</a><img src="img/q.png"><img src="../img/p.png">', resolve=res)
    assert 'href="../../a/index.html"' in out
    assert 'src="../../f/sub/img/q.png"' in out
    assert 'src="../../f/img/p.png"' in out
    out = sr.sanitize_html(b'<a href="../../index.html">i</a><a href="../../../etc/passwd">j</a>', resolve=res)
    assert "href" not in out


def test_share_resolver_images() -> None:
    res = sr.share_resolver("index.html", IDX)
    out = sr.sanitize_html(b'<img src="img/p.png">', resolve=res)
    assert 'src="../f/img/p.png"' in out
    out = sr.sanitize_html(b'<img src="data:image/svg+xml;base64,PHN2Zz4=">', resolve=res)
    assert "src" not in out
    out = sr.sanitize_html(b'<img src="data:image/png;base64,iVBORw0KGgo=">', resolve=res)
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in out
    out = sr.sanitize_html(b'<img src="https://evil/x.png"><img src="//evil/x.png">', resolve=res)
    assert "src" not in out


def test_share_resolver_direct_cases() -> None:
    res = sr.share_resolver("index.html", IDX)
    assert res("a", "javascript:alert(1)") is None
    assert res("a", "vbscript:x") is None
    assert res("a", "//evil/x") is None
    assert res("a", "data:text/html,x") is None
    assert res("a", "page2.html?x=1") == "../a/page2.html"
    assert res("a", "") is None
    assert res("a", "pa\tge2.html") == "../a/page2.html"
    assert res("a", " JaVaScRiPt:1") is None
    assert res("a", "java\nscript:1") is None
    assert res("css", "img/p.png") == "../f/img/p.png"
    assert res("css", "https://evil/x.png") is None
    assert res("link", "https://evil/x.css") is None
    assert res("img", "..\\index.html") is None
    assert res("nope", "img/p.png") is None


def test_style_attribute_urls_are_rewritten() -> None:
    res = sr.share_resolver("index.html", IDX)
    out = sr.sanitize_html(b'<p style="color:red;background:url(img/p.png)">x</p>', resolve=res)
    assert 'url(../f/img/p.png)' in out or 'url("../f/img/p.png")' in out
    assert "color:red" in out
    out = sr.sanitize_html(b"<p style=\"background:url('img/p.png') no-repeat\">x</p>", resolve=res)
    assert "f/img/p.png" in out
    out = sr.sanitize_html(b'<p style="background:url(https://evil/x.png)">x</p>', resolve=res)
    assert "evil" not in out and "url(" not in out.lower()


def test_style_attribute_hostile_forms_are_dropped_or_neutral() -> None:
    for style in (
        "width:expression(alert(1))",
        "behavior:url(x.htc)",
        "background:u\\72l(http://evil/x)",
        "background:url(a.png) /* c */",
        "position:fixed;top:0",
        "position : sticky",
        "-moz-binding:url(x)",
        "background:image-set('//evil/x.png' 1x)",
        "background:URL( 'a.png' ) , url(b.png",
        "x:" + "url(" * 50,
        "color:red;" * 2000,
    ):
        out = sr.sanitize_html(f'<p style="{style}">x</p>'.encode(), resolve=sr.share_resolver("index.html", IDX))
        assert "evil" not in out
        assert not re.search(r"expression|behavior|binding|image-set|position\s*:\s*(fixed|sticky)|\\", out, re.I), out


def test_style_attribute_regex_is_linear() -> None:
    start = time.monotonic()
    for payload in ("url(" + " " * 200000, "url(" * 50000, "url('" + "a" * 300000, "url(" + "\\" * 100000):
        sr.sanitize_html(f'<p style="{payload}">x</p>'.encode(), resolve=keep_all)
    assert time.monotonic() - start < 5


def test_input_and_output_bounds() -> None:
    with pytest.raises(sr.RenderTooLarge):
        sr.sanitize_html(b"a" * (sr.MAX_RENDER_INPUT + 1), resolve=keep_all)
    with pytest.raises(sr.RenderTooLarge):
        sr.render_text(b"a" * (sr.MAX_RENDER_INPUT + 1))
    assert sr.sanitize_html(b"a" * sr.MAX_RENDER_INPUT, resolve=keep_all)
    # output expansion over the cap: every "&" in text escapes to "&amp;"
    with pytest.raises(sr.RenderTooLarge):
        sr.render_text(b"&" * sr.MAX_RENDER_INPUT)
    with pytest.raises(sr.RenderTooLarge):
        sr.sanitize_html(b"&" * sr.MAX_RENDER_INPUT, resolve=keep_all)


def test_deep_nesting_is_bounded() -> None:
    start = time.monotonic()
    out = sr.sanitize_html(b"<div>" * 100_000, resolve=keep_all)
    assert time.monotonic() - start < 5
    assert len(out.encode()) <= sr.MAX_RENDER_OUTPUT
    assert out.count("<div>") == sr.MAX_NESTING and out.count("</div>") == sr.MAX_NESTING
    start = time.monotonic()
    sr.sanitize_html(b"<b><i>" * 50_000 + b"</b>" * 50_000, resolve=keep_all)
    sr.sanitize_html(b"<table>" * 50_000, resolve=keep_all)
    sr.sanitize_html(b"<a href=x " * 50_000, resolve=keep_all)
    sr.sanitize_html(b"<!--" * 200_000, resolve=keep_all)
    assert time.monotonic() - start < 15


def test_non_utf8_bytes_are_replaced() -> None:
    out = sr.sanitize_html(b"<p>caf\xe9 \xff\xfe</p><p onclick=x>\xc3\x28</p>", resolve=keep_all)
    assert "�" in out
    assert_inert(out)
    assert sr.sanitize_html("<p>café</p>".encode(), resolve=keep_all) == "<p>café</p>"
    assert sr.sanitize_html(b"\xff\xfe<\x00s\x00c\x00r\x00i\x00p\x00t\x00>", resolve=keep_all) is not None


def test_comments_and_cdata_are_dropped() -> None:
    out = sr.sanitize_html(b"<p>a<!-- secret -->b</p><![CDATA[x]]>", resolve=keep_all)
    assert "secret" not in out and "<!--" not in out


def test_global_attributes_survive() -> None:
    out = sr.sanitize_html(b'<p class="c" id="i" title="t" lang="en" dir="ltr" role="note" aria-label="l" data-x="1">x</p>', resolve=keep_all)
    for needle in ('class="c"', 'id="i"', 'title="t"', 'lang="en"', 'dir="ltr"', 'role="note"', 'aria-label="l"'):
        assert needle in out
    assert "data-x" not in out


def test_render_text_escapes() -> None:
    out = sr.render_text(b"a,b\n<script>alert(1)</script> & \xff")
    assert out.startswith('<pre class="herdr-text">') and out.endswith("</pre>")
    assert "<script" not in out and "&lt;script&gt;" in out and "&amp;" in out and "�" in out


def test_wrap_content() -> None:
    out = sr.wrap_content("html", "<p>x</p>")
    assert out == (
        f'<p class="herdr-static-note">{sr.STATIC_NOTE}</p><div id="herdr-content" data-kind="html">'
        '<template shadowrootmode="open"><p>x</p></template></div>'
    )
    hostile = sr.wrap_content('x"><script>', "")
    assert "<script" not in hostile
    sanitized = sr.sanitize_html(b"&lt;/template&gt;<p>x</p></template><b>y", resolve=keep_all)
    assert "</template>" not in sanitized


def test_render_kind() -> None:
    assert sr.render_kind("report.html") == "html"
    assert sr.render_kind("data.csv") == "text"
    assert sr.render_kind("a.svg") == "image"
    assert sr.render_kind("a.pdf") == "pdf"
    assert sr.render_kind("t.parquet") == "document"
    assert sr.render_kind("x.bin", "text/plain; charset=utf-8") == "text"
    assert sr.render_kind("x.bin", "application/json") == "text"
    assert sr.render_kind("x.bin", "text/html") == "html"
    assert sr.render_kind("x.html", "image/png") == "image"
    assert sr.render_kind("x.bin", "application/pdf") == "pdf"
    assert sr.render_kind("x.bin", "application/zip") == "document"


def test_constants() -> None:
    assert sr.RENDER_VERSION == 1
    assert sr.MAX_RENDER_INPUT == 2 * 1024 * 1024
    assert sr.MAX_RENDER_OUTPUT == 4 * 1024 * 1024
    assert sr.STATIC_NOTE == "Scripts, frames and embedded media are not shown in this static copy"


def test_cache_hit_and_eviction_by_count() -> None:
    cache = sr.RenderCache(max_entries=2, max_bytes=1000)
    assert cache.get("h1", "a") is None
    cache.put("h1", "a", "AAA")
    assert cache.get("h1", "a") == "AAA"
    assert cache.get("h1", "b") is None
    cache.put("h2", "a", "BBB")
    assert cache.get("h1", "a") == "AAA"  # refresh h1
    cache.put("h3", "a", "CCC")  # evicts h2 (least recent)
    assert cache.get("h2", "a") is None
    assert cache.get("h1", "a") == "AAA" and cache.get("h3", "a") == "CCC"
    assert len(cache) == 2


def test_cache_eviction_by_bytes_and_oversize() -> None:
    cache = sr.RenderCache(max_entries=10, max_bytes=10)
    cache.put("h1", "a", "12345")
    cache.put("h2", "a", "12345")
    cache.put("h3", "a", "12345")
    assert cache.get("h1", "a") is None
    assert cache.get("h2", "a") == "12345" and cache.get("h3", "a") == "12345"
    cache.put("h4", "a", "x" * 11)
    assert cache.get("h4", "a") is None and len(cache) == 2
    cache.put("h2", "a", "123456789")  # replacing recounts bytes
    assert cache.get("h3", "a") is None and cache.get("h2", "a") == "123456789"
    cache.put("é", "a", "é" * 5)  # 10 bytes in UTF-8
    assert cache.get("é", "a") == "é" * 5


def test_cache_is_thread_safe() -> None:
    cache = sr.RenderCache(max_entries=8, max_bytes=10_000)
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(500):
                cache.put(f"h{(n * 7 + i) % 20}", "a", "v" * (i % 50))
                cache.get(f"h{i % 20}", "a")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(cache) <= 8


def test_style_element_with_markup_inside_is_dropped() -> None:
    payload = b"<style></template><script>alert(1)</script></style><p>ok</p>"
    out = sr.sanitize_html(payload, resolve=keep_all)
    assert "<style" not in out and "script" not in out.lower() and out.endswith("<p>ok</p>")
    out = sr.sanitize_html(b"<style>a>b{color:red}</style>", resolve=keep_all)
    assert "<style>" not in out or "<" not in out.split("<style>")[1].split("</style>")[0]


_REPO = str(__import__("pathlib").Path(__file__).resolve().parents[2])
_CHILD = """
import sys, time
sys.path.insert(0, sys.argv[1])
from herdr_engineering.control_plane import share_render as sr
data = sys.stdin.buffer.read()
start = time.monotonic()
try:
    sr.sanitize_html(data, resolve=lambda c, u: u)
except sr.RenderTooLarge:
    pass
print(time.monotonic() - start)
"""


def _timed(payload: bytes, limit: float = 3.0) -> float:
    """Sanitize in a child process under a hard kill, so a regression FAILS instead of hanging the suite."""
    try:
        done = subprocess.run(
            [sys.executable, "-c", _CHILD, _REPO], input=payload, capture_output=True, timeout=30, check=True
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"sanitizing took over 30 s: {payload[:40]!r}")
    elapsed = float(done.stdout.decode().strip().splitlines()[-1])
    assert elapsed < limit, (elapsed, payload[:40])
    return elapsed


def _fill(unit: str, size: int = 1_000_000) -> bytes:
    return (unit * (size // len(unit) + 1)).encode()[:size]


_REPRODUCERS = [
    "<b></i>",
    "<div></i>",
    "<span></p>",
    "<DIV></I>",
    "<div></span>",
    '<a title="</b>">',
    '<div title="</div>"><div>',
    "<div><!-- </div> -->",
    "<div><![CDATA[</div>]]>",
    "<div><![if x]></div>",
    "<div><?pi </div> ?>",
    '<div title="x',
    "<a><b></a></b>",
    "<b><div></b>",
    "<div><b></div>",
    "<div></div></div></div><div>",
    "<style></style><div>",
    "<div/><div / >",
    "<table><div>",
    "<table><b></table>",
    "<table><b><tr></table>",
    "<svg><div>",
    "<div></ div><div>",
    "<div></>",
    "<optgroup>",
    "<rt>",
    "<rp>",
    "<form><div></form>",
    "<span><div></span>",
    "<b><i><u></b></i></u>",
    "<td>",
    "<tr>",
    "<table><tr><td>",
    "<p>",
    "<li><li>",
    "<dl><dt><dd>",
    "<select><option>",
    "<a><div></a>",
]


@pytest.mark.parametrize("unit", _REPRODUCERS)
def test_nesting_shapes_stay_fast(unit: str) -> None:
    _timed(_fill(unit))


@pytest.mark.parametrize(
    "prefix,fill,suffix",
    [
        ("<b a==' >", "<div>", "'>"),
        ("<b a=\" >", "<div>", '">'),
        ('<b ="x>', "<div>", '">'),
        ("<b a='x>", "<div>", "'>"),
        ("<b a=`x>", "<div>", "`>"),
        ("<b a=<>", "<div>", "<>"),
        ("<b a b='", "<div>", "'>"),
    ],
)
def test_quote_desync_cannot_hide_depth(prefix: str, fill: str, suffix: str) -> None:
    _timed((prefix + fill * 200_000 + suffix).encode())
    _timed(((prefix + fill * 5 + suffix) * 40_000).encode())


def test_plain_nested_divs_are_cut_at_max_nesting() -> None:
    _timed(b"<div>" * 200_000)
    out = sr.sanitize_html(b"<div>" * 100_000, resolve=keep_all)
    assert out.count("<div>") == sr.MAX_NESTING


def test_nesting_scan_semantics() -> None:
    assert sr._bound_nesting("<b></i>" * 5) == "<b></i>" * 5
    deep = "<b></i>" * (sr.MAX_NESTING + 5)
    assert sr._bound_nesting(deep).count("<b>") <= sr.MAX_NESTING  # cut by depth or by the work budget, never whole
    assert len(sr._bound_nesting(deep)) < len(deep)
    ok = "<div>" * 100 + "</div>" * 100 + "<div>" * 100
    assert sr._bound_nesting(ok) == ok
    wrong_order = "<a><b></a></b>" * 20_000
    assert sr._bound_nesting(wrong_order).count("<a>") < 20_000  # crossing closers are ignored for depth: over-count
    assert sr._bound_nesting("<b><div></b>" * 20_000).count("<b>") < 20_000
    quote = "<b a==' >" + "<div>" * 20_000 + "'>"
    assert sr._bound_nesting(quote).count("<div>") == sr.MAX_NESTING - 1  # the loose tag taints; nothing pops after
    assert sr._bound_nesting("<div></i>" * 20_000).count("<div>") < 20_000
    assert sr._bound_nesting("<rt>" * 20_000).count("<rt>") == sr.MAX_NESTING
    hidden = "<div><!-- </div> -->" * 20_000
    assert sr._bound_nesting(hidden).count("<div>") == sr.MAX_NESTING  # a closer inside a comment pops nothing
    attr = '<div title="</div>"><div>' * 20_000
    assert sr._bound_nesting(attr).count("<div") < 20_000
    assert sr._bound_nesting("<optgroup>" * 20_000).count("<optgroup>") == sr.MAX_NESTING


def test_work_budget_cuts_quadratic_shapes_on_a_tag_boundary() -> None:
    soup = "<b>" * 3_000 + "</i>" * 2_000_000
    cut = sr._bound_nesting(soup)
    assert len(cut) < len(soup) and cut.endswith("</i>") and cut.count("</i>") * 3_000 <= sr.WORK_BUDGET + 3_000


def _realistic_document(size: int = 1_000_000) -> tuple[str, str]:
    row = (
        "<h2 id=\"s{n}\">Section {n}</h2><p>Paragraph {n} one<p>Paragraph {n} two with <b>bold</b> and <a href=\"https://example.org/{n}\">link</a>"
        "<ul><li>item {n}a<li>item {n}b<li>item {n}c</ul>"
        "<table><tr><td>c{n}1<td>c{n}2<tr><td>c{n}3<td>c{n}4</table>"
        "<dl><dt>term {n}<dd>def {n}</dl><div><div><div><span>deep {n}</span></div></div></div><!-- note {n} -->"
    )
    parts: list[str] = []
    n = 0
    total = 0
    while total < size:
        piece = row.format(n=n)
        parts.append(piece)
        total += len(piece)
        n += 1
    return "".join(parts), f"Paragraph {n - 1} two with"


def test_legitimate_large_documents_survive_untruncated() -> None:
    doc, tail = _realistic_document()
    assert len(doc) >= 1_000_000
    assert sr._bound_nesting(doc) == doc
    out = sr.sanitize_html(doc.encode(), resolve=keep_all)
    assert tail in out and "Section 0<" in out
    assert "deep 0" in out and f"deep {doc.count('<h2')-1}" in out


def test_tag_soup_fuzz_is_fast() -> None:
    import random

    rng = random.Random(1234)
    frags = [
        "<b>", "</b>", "<i>", "</i>", "<div>", "</div>", "<span>", "</p>", "<a href=x>", "</a>", "<table>", "<td>",
        "<DIV>", "</Div>", "<!--", "-->", "<![CDATA[", "]]>", "<![", "<style>", "</style>", "<svg>", "</svg>",
        '<a title="', '">', "'", '"', "=", "<", ">", "</", "<br>", "<template>", "</template>", "<b></i>",
        "`", "==", "<b a==' >", '<b ="x>', "<rt>", "<optgroup>", "<form>", "</form>", "<li>", "<p>", "</ul>", "<tr>",
    ]
    for _ in range(12):
        parts: list[str] = []
        total = 0
        while total < 500_000:
            f = rng.choice(frags)
            parts.append(f)
            total += len(f)
        _timed("".join(parts).encode())


# ------------------------------------------------------------------------------------------ the isolated child's deadline
@pytest.fixture
def guard():
    """A hard alarm so a regression that hangs the parent FAILS the test instead of hanging the suite."""
    import signal

    def fire(signum, frame):
        raise AssertionError("test exceeded its hard guard")

    previous = signal.signal(signal.SIGALRM, fire)
    signal.alarm(120)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _reviewer_shapes() -> dict[str, str]:
    return {
        "svg-plaintext": "<svg><plaintext>" + "<g>" * 80_000 + "</x>" * 80_000,
        "svg-input": "<svg>" + "<input>" * 80_000 + "</x>" * 80_000,
        "noscript-frame": "<noscript><" + "iframe></noscript><div>" + "<x>" * 80_000 + "</y>" * 80_000,
        "body-optgroup": ("<body>" + "<optgroup>" * 9_000 + "</body>" + "</x>" * 100_000) * 3,
    }


@pytest.mark.parametrize("name", sorted(_reviewer_shapes()))
@pytest.mark.parametrize("estimator", [True, False])
def test_reviewer_reproducers_return_within_the_deadline(
    name: str, estimator: bool, monkeypatch: pytest.MonkeyPatch, guard: None
) -> None:
    if not estimator:
        monkeypatch.setattr(sr, "_bound_nesting", lambda text: text)
    sr.clear_sanitize_caches()
    payload = _reviewer_shapes()[name].encode()
    start = time.monotonic()
    try:
        out: str | None = sr.sanitize_html(payload, resolve=keep_all)
    except (sr.RenderFailed, sr.RenderTooLarge):
        out = None
    elapsed = time.monotonic() - start
    assert elapsed < sr.SANITIZE_DEADLINE_SECONDS + 1.5, (name, estimator, elapsed)
    if out is not None:
        assert_inert(out)
    # the child was restarted if it was killed: the next call works
    assert sr.sanitize_html(b"<p>after</p>", resolve=keep_all) == "<p>after</p>"


def test_deadline_kills_the_child_and_a_failure_is_remembered(monkeypatch: pytest.MonkeyPatch, guard: None) -> None:
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 0.05)
    sr.clear_sanitize_caches()
    payload = b"<div>" * 9_000 + b"remembered"
    with pytest.raises(sr.RenderTimeout):
        sr.sanitize_html(payload, resolve=keep_all)
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 30.0)
    start = time.monotonic()
    with pytest.raises(sr.RenderTimeout):  # served from the failure memory, not re-run
        sr.sanitize_html(payload, resolve=keep_all)
    assert time.monotonic() - start < 0.5
    sr.clear_sanitize_caches()
    assert sr.sanitize_html(payload, resolve=keep_all).count("<div>") == 9_000
    assert sr.sanitize_html(b"<p>fine</p>", resolve=keep_all) == "<p>fine</p>"


def _children_of_self() -> list[int]:
    me = os.getpid()
    kids: list[int] = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = open(f"/proc/{entry}/stat").read()
            except OSError:
                continue
            if int(stat.rsplit(")", 1)[1].split()[1]) == me:
                kids.append(int(entry))
    return kids


def test_two_hundred_timeouts_leak_no_fds_or_processes(monkeypatch: pytest.MonkeyPatch, guard: None) -> None:
    import signal

    signal.alarm(0)
    signal.alarm(300)
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 0.03)
    sr.clear_sanitize_caches()
    assert sr.sanitize_html(b"<p>warm</p>", resolve=keep_all) == "<p>warm</p>"
    fds_before = len(os.listdir("/proc/self/fd"))
    # Other fixtures in a full run (a session Postgres, say) own children of this process; count only ours.
    children_before = set(_children_of_self())
    for i in range(200):
        with pytest.raises(sr.RenderFailed):
            sr.sanitize_html(b"<div>" * 9_000 + str(i).encode(), resolve=keep_all)
    sr._WORKER._kill()
    assert len(os.listdir("/proc/self/fd")) <= fds_before + 1
    zombies = [p for p in _children_of_self() if open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[0] == "Z"]
    assert not zombies
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 3.0)
    assert sr.sanitize_html(b"<p>again</p>", resolve=keep_all) == "<p>again</p>"
    assert len(set(_children_of_self()) - children_before) <= 1
    sr.clear_sanitize_caches()


def test_a_crashing_child_is_a_render_failure_and_recovers(guard: None) -> None:
    assert sr.sanitize_html(b"<p>x</p>", resolve=keep_all) == "<p>x</p>"
    proc = sr._WORKER._proc
    assert proc is not None
    proc.kill()
    proc.wait()
    assert sr.sanitize_html(b"<p>y</p>", resolve=keep_all) == "<p>y</p>"  # noticed dead, respawned


def test_concurrent_good_and_hostile_inputs(monkeypatch: pytest.MonkeyPatch, guard: None) -> None:
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 0.4)
    sr.clear_sanitize_caches()
    results: dict[int, object] = {}

    def work(i: int) -> None:
        payload = (b"<div>" * 9_000 + str(i).encode()) if i % 2 else f"<p>good {i}</p>".encode()
        try:
            results[i] = sr.sanitize_html(payload, resolve=keep_all)
        except (sr.RenderFailed, sr.RenderBusy) as exc:
            results[i] = exc

    start = time.monotonic()
    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert time.monotonic() - start < 30
    for i in range(0, 8, 2):  # a good input may only lose a race for a slot, never be wrong
        assert results[i] == f"<p>good {i}</p>" or isinstance(results[i], sr.RenderBusy)
    assert sum(results[i] == f"<p>good {i}</p>" for i in range(0, 8, 2)) >= 1
    monkeypatch.setattr(sr, "SANITIZE_DEADLINE_SECONDS", 3.0)
    sr.clear_sanitize_caches()
    assert sr.sanitize_html(b"<p>after</p>", resolve=keep_all) == "<p>after</p>"


def test_busy_when_too_many_are_waiting(monkeypatch: pytest.MonkeyPatch, guard: None) -> None:
    sem = sr._WORKER._pending
    held = [sem.acquire(blocking=False) for _ in range(sr.MAX_PENDING_RENDERS)]
    try:
        assert all(held)
        with pytest.raises(sr.RenderBusy):
            sr.sanitize_html(b"<p>x</p>", resolve=keep_all)
    finally:
        for _ in held:
            sem.release()
    assert sr.sanitize_html(b"<p>x</p>", resolve=keep_all) == "<p>x</p>"


def test_success_cache_needs_a_cache_key_and_honours_it(guard: None) -> None:
    sr.clear_sanitize_caches()
    calls: list[str] = []

    def counting(context: str, url: str) -> str | None:
        calls.append(url)
        return url

    data = b'<a href="https://example.org/x">t</a>'
    first = sr.sanitize_html(data, resolve=counting, cache_key=("n", 1))
    assert calls == ["https://example.org/x"]
    assert sr.sanitize_html(data, resolve=counting, cache_key=("n", 1)) == first
    assert calls == ["https://example.org/x"]  # served from the cache
    sr.sanitize_html(data, resolve=counting, cache_key=("n", 2))
    assert len(calls) == 2  # a different key re-renders
    sr.sanitize_html(data, resolve=counting)
    sr.sanitize_html(data, resolve=counting)
    assert len(calls) == 4  # no key, no caching


def test_child_module_imports_only_nh3_and_stdlib() -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from herdr_engineering.control_plane import share_render_worker;"
        "bad = [m for m in sys.modules if m.startswith(('boto3','asyncpg','psycopg2','yaml','herdr_engineering.control_plane.share_'))];"
        "print(sorted(bad))"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code, _REPO], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "['herdr_engineering.control_plane.share_render_worker']"
