"""The annotation view ``a/<path>`` of ``share-public``: markup, CSP, CSRF delivery and hostile content.

Real sockets, real frozen bundles, the real isolated sanitiser child. Helpers come from ``test_share_annotate``.
"""
import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_engineering.control_plane import share_public, share_render
from herdr_engineering.control_plane.share_session import csrf_token
from tests.control_plane.test_share_annotate import (
    FRAME_RE,
    ORIGIN,
    SECRET,
    TOKEN,
    base,
    make_env,
    session,
)
from tests.control_plane.test_share_lab import NOW0

CLOSE_TEMPLATE = "</" + "template>"
CLOSE_STYLE = "</" + "style>"
FRAME_OPEN = "<" + "iframe src=a></" + "iframe>"
HOSTILE_NAME = "we&ird 'q' <x>.pdf"
HOSTILE = (
    b'<html><head><style>:host{position:fixed!important;inset:0;z-index:2147483647}</style></head><body>'
    b'<script>window.x=1</script>' + FRAME_OPEN.encode() + b'<img src="pic.png" onerror="alert(1)">'
    b'<a href="javascript:alert(1)">js</a><a href="page2.html">next</a>'
    b'<div id="annotate-panel" name="herdr-view-data"><a id="herdr-view-data" name="herdr-annotate-csrf">c</a></div>'
    b'<p>' + CLOSE_TEMPLATE.encode() + b'<b>escaped?</b></p>'
    b'<style>p{color:red}' + CLOSE_TEMPLATE.encode() + b'<b id=pwn>x</b></style>'
    b'<style>p{color:blue}' + CLOSE_STYLE.encode() + b'<b id=pwn2>y</b></body></html>')
FILES = {
    "index.html": HOSTILE,
    "page2.html": b"<html><body><p>two</p></body></html>",
    "sub/deep.html": b"<html><body><a href='../page2.html'>up</a><img src='x.png'></body></html>",
    "sub/x.png": b"\x89PNG\r\n\x1a\n" + bytes(40),
    "pic.png": b"\x89PNG\r\n\x1a\n" + bytes(range(64)),
    "notes.txt": b"hello <script>alert(1)</script> & more",
    "doc.pdf": b"%PDF-1.4\n%fake\n",
    HOSTILE_NAME: b"%PDF-1.4\n%fake\n",
    "data/t.parquet": bytes(range(256)) * 4,
}


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    env = make_env(tmp_path_factory.mktemp("annview"), files=FILES)
    yield env
    env.close()


def a(env, path, sess=None, method="GET", **headers):
    return env.request(method, base(sess, "a") + urllib.parse.quote(path), headers)


def parse_csp(csp):
    return {d.split()[0]: d.split()[1:] for d in csp.split("; ")}


def split_shadow(page):
    """(before, inside, after) of the one declarative shadow root."""
    opener = '<template shadowrootmode="open">'
    assert page.count(opener) == 1 and page.count(CLOSE_TEMPLATE) == 1
    before, rest = page.split(opener)
    inside, after = rest.split(CLOSE_TEMPLATE)
    return before, inside, after


def data_of(page):
    return json.loads(re.search(r'id="herdr-view-data"[^>]*>(.*?)</script>', page, re.S).group(1))


@pytest.mark.parametrize("name,kind", [("index.html", "html"), ("notes.txt", "text"), ("pic.png", "image"),
                                       ("doc.pdf", "pdf"), ("data/t.parquet", "document")])
def test_annotation_view_for_each_kind(lab, name, kind):
    status, h, body = a(lab, name)
    page = body.decode()
    assert status == 200 and h["content-type"] == "text/html; charset=utf-8"
    assert "access-control-allow-origin" not in h and "access-control-expose-headers" not in h
    nonce = re.search(r"script-src 'nonce-([^']*)' 'strict-dynamic'", h["content-security-policy"]).group(1)
    assert len(nonce) == 24 and f'nonce="{nonce}"' in page
    assert a(lab, name)[1]["content-security-policy"] != h["content-security-policy"]
    for key, value in (("x-frame-options", "DENY"), ("cache-control", "no-store"), ("x-content-type-options", "nosniff"),
                       ("referrer-policy", "no-referrer"), ("x-robots-tag", "noindex"),
                       ("cross-origin-opener-policy", "same-origin"), ("cross-origin-resource-policy", "same-origin")):
        assert h[key] == value
    data = data_of(page)
    depth = name.count("/") + 1
    assert data["mode"] == "annotate" and data["kind"] == kind and data["file"] == name
    assert data["raw_href"] == "../" * depth + "f/" + urllib.parse.quote(name)
    assert page.count('<a data-mode="annotate"') == 1 and page.count('aria-current="page"') == 1
    page_url = f"/s/{TOKEN}/k/S/a/{urllib.parse.quote(name)}"
    cur = re.search(r'<a data-mode="annotate" href="([^"]*)" aria-current="page"', page).group(1)
    assert urllib.parse.urljoin(page_url, cur) == page_url
    live = re.search(r'<a data-mode="live" href="([^"]*)"', page).group(1)
    mode = "f" if kind == "html" else "v"
    assert urllib.parse.urljoin(page_url, live) == f"/s/{TOKEN}/k/S/{mode}/{urllib.parse.quote(name)}"
    assert "<aside data-annotate-panel" in page and "{{" not in page
    assert not FRAME_RE.search(page.replace(FRAME_OPEN, ""))


def test_csp_header_by_header(lab):
    csp = parse_csp(a(lab, "notes.txt")[1]["content-security-policy"])
    script = csp["script-src"]
    assert len(script) == 2 and script[0].startswith("'nonce-") and script[1] == "'strict-dynamic'"
    share = f"{ORIGIN}/s/{TOKEN}/"
    expected = {"default-src": ["'none'"], "style-src-elem": [f"{ORIGIN}/s/static/", share, "'unsafe-inline'"],
                "style-src-attr": ["'unsafe-inline'"], "img-src": [share, "data:"],
                "font-src": [share, f"{ORIGIN}/s/static/", "data:"], "connect-src": [share],
                "worker-src": [f"{ORIGIN}/s/static/vendor/pdfjs/"], "frame-src": ["'none'"], "child-src": ["'none'"],
                "object-src": ["'none'"], "base-uri": ["'none'"], "form-action": ["'none'"],
                "frame-ancestors": ["'none'"]}
    for name, value in expected.items():
        assert csp[name] == value, name
    assert set(csp) == set(expected) | {"script-src"}
    assert not any(t in ("'self'", "blob:", "*") for ds in csp.values() for t in ds)
    assert not any(t.startswith("http") and not t.startswith(ORIGIN) for ds in csp.values() for t in ds)


def test_csrf_is_delivered_only_in_our_markup(lab):
    sess = session()
    page = a(lab, "index.html", sess)[2].decode()
    token = csrf_token(SECRET, "shr-1", sess)
    before, inside, after = split_shadow(page)
    assert page.count(token) == 1 and token not in inside and token not in after
    assert re.search(rf'<meta name="herdr-annotate-csrf" content="{token}">', before)
    assert token not in json.dumps(data_of(page))
    live = lab.request("GET", base(sess, "v") + "notes.txt")[2].decode()
    assert "herdr-annotate-csrf" not in live
    other = a(lab, "notes.txt", session(identity="bob", email="bob@lab.example"))[2].decode()
    assert token not in other


def test_hostile_html_is_sanitised_and_contained_in_the_shadow_root(lab):
    page = a(lab, "index.html")[2].decode()
    before, inside, after = split_shadow(page)
    assert after.lstrip().startswith("</div>")
    for bad in ("<script", "onerror", "javascript:", "iframe", "window.x"):
        assert bad not in inside.lower(), bad
    assert 'href="../a/page2.html"' in inside and 'src="../f/pic.png"' in inside
    # our own elements are outside the shadow root and exist once; the clobbering ids stay inside it
    assert page.count('id="herdr-view-data"') == 2 and before.count('id="herdr-view-data"') == 1
    assert after.count('id="herdr-view-data"') == 0 and "data-annotate-panel" in after
    assert 'id="annotate-panel"' in inside and 'id="annotate-panel"' not in before + after
    assert "pwn" not in before + after
    # a style whose text holds a closer for the wrapper or the style element is dropped, never kept
    assert "color:red" not in inside and "pwn" not in inside.replace('id="pwn2"', "")
    assert "color:blue" in inside and inside.count("<style>") == inside.count(CLOSE_STYLE) == 2     # blue is a whole style
    # a host rule is allowed through (by design) but the page contains it: wrapper outside the host
    assert ":host{position:fixed!important" in inside
    outer = before.rindex('id="herdr-viewport"')
    assert outer < before.index('id="herdr-content"') and after.lstrip().startswith("</div>")


def test_stylesheet_contains_the_content_box(lab):
    css = lab.request("GET", "/s/static/annotate.css")[2].decode()
    rule = re.search(r"#herdr-viewport\s*\{([^}]*)\}", css).group(1)
    for decl in ("contain: paint", "overflow: auto", "isolation: isolate", "position: relative", "z-index: 0"):
        assert decl in rule
    assert re.search(r"nav\[data-mode-switch\]\s*\{[^}]*z-index: 20", css)


def test_nested_html_links_resolve_from_the_annotation_url(lab):
    page = a(lab, "sub/deep.html")[2].decode()
    where = f"/s/{TOKEN}/k/S/a/sub/deep.html"
    href = re.search(r'<a href="([^"]*)"', split_shadow(page)[1]).group(1)
    img = re.search(r'<img src="([^"]*)"', split_shadow(page)[1]).group(1)
    assert urllib.parse.urljoin(where, href) == f"/s/{TOKEN}/k/S/a/page2.html"
    assert urllib.parse.urljoin(where, img) == f"/s/{TOKEN}/k/S/f/sub/x.png"


def test_text_is_an_escaped_pre_in_the_shadow_root(lab):
    page = a(lab, "notes.txt")[2].decode()
    inside = split_shadow(page)[1]
    assert inside.startswith('<pre class="herdr-text">') and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in inside


def test_pdf_page_carries_the_pdfjs_options_for_the_script(lab):
    data = data_of(a(lab, "doc.pdf")[2].decode())
    assert data["pdfjs"] == {"module": "/s/static/vendor/pdfjs/pdf.min.mjs",
                             "worker": "/s/static/vendor/pdfjs/pdf.worker.min.mjs",
                             "options": {"isEvalSupported": False, "enableXfa": False}}
    assert lab.request("GET", data["pdfjs"]["module"])[0] == 200
    assert lab.request("GET", data["pdfjs"]["worker"])[0] == 200
    assert "pdfjs" not in data_of(a(lab, "pic.png")[2].decode())
    assert "pdfjs" not in data_of(lab.request("GET", base(kind="v") + "doc.pdf")[2].decode())


def test_hostile_file_name_is_escaped_everywhere(lab):
    status, h, body = a(lab, HOSTILE_NAME)
    page = body.decode()
    assert status == 200
    assert HOSTILE_NAME not in page.replace(json.dumps(HOSTILE_NAME, ensure_ascii=True)[1:-1], "")
    assert "<x>" not in page and "'q'" not in re.sub(r"<script.*?</script>", "", page, flags=re.S)
    assert data_of(page)["file"] == HOSTILE_NAME
    for href in re.findall(r'<a data-mode="[a-z]+" href="([^"]*)"', page):
        assert "<" not in href and '"' not in href and " " not in href
    assert HOSTILE_NAME not in h["content-security-policy"]


def test_a_huge_html_gets_the_too_large_note(tmp_path):
    huge = b"<html><body>" + b"<p>x</p>" * 300_000 + b"</body></html>"
    assert len(huge) > share_render.MAX_RENDER_INPUT
    env = make_env(tmp_path, files={"index.html": huge})
    try:
        status, _, body = a(env, "index.html")
        page = body.decode()
        assert status == 200 and "too large to show as a static copy" in page
        assert data_of(page)["kind"] == "document" and data_of(page)["raw_href"] == "../f/index.html"
        assert "shadowrootmode" not in page
    finally:
        env.close()


def test_uniform_404s(lab, tmp_path):
    ref = lab.request("GET", "/nothing-here")[2]
    for sess in ("alice.garbage", session(exp=NOW0 - 1), session(share_id="shr-2")):
        assert a(lab, "notes.txt", sess)[0:3:2] == (404, ref)
    assert a(lab, "missing.txt")[0:3:2] == (404, ref)
    assert lab.request("OPTIONS", base(kind="a") + "notes.txt", {"Origin": "null"})[0] == 404
    assert lab.request("POST", base(kind="a") + "notes.txt", {"Content-Length": "0"})[0] == 404
    assert lab.request("GET", f"/s/{TOKEN}/k/{session()}/a/")[0] == 404


def test_disabled_annotations_and_public_shares_have_no_annotation_view(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "pub").mkdir()
    env = make_env(tmp_path / "off", files=FILES, annotations={"enabled": False})
    pub = make_env(tmp_path / "pub", files=FILES, audience="public")
    try:
        assert a(env, "notes.txt")[0] == 404
        assert a(pub, "notes.txt")[0] == 404
    finally:
        env.close()
        pub.close()


def test_a_view_records_an_annotate_opened_event_for_the_entry(lab):
    assert a(lab, "index.html")[0] == 200
    events = [json.loads(x) for x in lab.event_lines()]
    assert {"file": "index.html", "mode": "annotate"} in [e["detail"] for e in events if e["kind"] == "opened"]


def test_render_failures_map_to_uniform_responses(tmp_path, monkeypatch):
    """Every case is provoked through the real isolated child; nothing is stubbed."""
    files = {"index.html": b"<p>i</p>",
             "tripwire.html": b'<p title="' + CLOSE_TEMPLATE.encode() + b'<img src=x onerror=1>">x</p>',
             "huge.html": b"< " * 1_000_000, "slowish.html": b"<div>" * 9000 + b"slow-one"}
    for k in range(8):
        files[f"busy{k}.html"] = b"<div>" * 9000 + b"distinct-%d" % k
    env = make_env(tmp_path, files=files)
    try:
        share_render.clear_sanitize_caches()
        ref = env.request("GET", "/nothing-here")[2]
        # RenderFailed: the output tripwire refuses the page, uniformly and without detail
        status, _, body = a(env, "tripwire.html")
        assert (status, body) == (404, ref)
        # RenderTooLarge: the 2 MiB input is within the cap but parses to more than the output cap
        status, _, body = a(env, "huge.html")
        assert status == 200 and b"too large to show" in body
        # RenderTimeout: a legitimate module constant, then a payload that cannot finish in time
        monkeypatch.setattr(share_render, "SANITIZE_DEADLINE_SECONDS", 0.05)
        status, _, body = a(env, "slowish.html")
        assert (status, body) == (404, ref)
        # RenderBusy: more concurrent renders than the pending cap
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda k: a(env, f"busy{k}.html"), range(8)))
        busy = [r for r in results if r[0] == 503]
        assert busy, [r[0] for r in results]
        for _, h, body in busy:
            assert body == share_public.UNAVAILABLE_BODY and h["retry-after"] == "5"
        assert all(r[0] in (200, 404, 503) for r in results)
    finally:
        env.close()


def test_back_to_back_renders_from_short_lived_threads_all_succeed(tmp_path):
    files = {f"p{k}.html": b"<p>page %d</p>" % k for k in range(12)} | {"index.html": b"<p>i</p>"}
    env = make_env(tmp_path, files=files)
    try:
        share_render.clear_sanitize_caches()
        statuses = [a(env, f"p{k}.html")[0] for k in range(12)]       # each on a fresh server thread, no gaps
        assert statuses == [200] * 12
    finally:
        env.close()


def test_sanitised_output_is_cached_per_share_file_hash_and_resolver(lab, monkeypatch):
    seen = []
    real = share_public.sanitize_html

    def spy(data, *, resolve, cache_key=None):
        seen.append(cache_key)
        return real(data, resolve=resolve, cache_key=cache_key)
    monkeypatch.setattr(share_public, "sanitize_html", spy)
    first = a(lab, "page2.html")[2]
    assert a(lab, "page2.html")[0] == 200 and len(seen) == 2 and seen[0] == seen[1]
    share_id, name, sha, version, fingerprint = seen[0]
    assert (share_id, name, version) == ("shr-1", "page2.html", share_render.RESOLVER_VERSION)
    assert re.fullmatch(r"[0-9a-f]{64}", sha) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    assert first
