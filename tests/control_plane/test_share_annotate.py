"""Lab live views (``v/``), the HTML mode-switch link on ``f/`` and the annotation HTTP API of ``share-public``.

Real sockets, real frozen bundles, sessions minted with the real signing code. The raw request path helpers live in
``test_share_public`` (``Env``) and ``test_share_lab`` (``lab_config``).
"""
import hashlib
import functools
import json
import re
import socket
import urllib.parse

import pytest

from herdr_engineering.control_plane import share_public
from herdr_engineering.control_plane.annotation_anchor import ANNOTATION_ID_RE, annotation_settings
from herdr_engineering.control_plane.share_bundle import (
    remove_state,
    write_own_projection,
    write_state,
)
from herdr_engineering.control_plane.share_session import (
    csrf_token,
    mint_session,
    write_cookie_name,
    write_cookie_value,
)
from tests.control_plane.test_share_lab import LAB_ALLOW, NOW0, SECRET, lab_config
from tests.control_plane.test_share_public import OTHER_TOKEN, TOKEN, Env

ORIGIN = "https://share.example"
EMAIL = "alice@lab.example"
FRAME_RE = re.compile(r"<\s*(iframe|frame|frameset|object|embed|portal|fencedframe)(?=[\s/>])", re.I)
HTML = b"<html><body><!-- </body> --><p>hello</p></BODY></html>"
FILES = {
    "index.html": HTML,
    "bare.html": b"<html><p>no body tag</p>",
    "sub/x.html": b"<html><body>x</body></html>",
    "a&b.html": b"<html><body>amp</body></html>",
    "notes.txt": b"hello <script>alert(1)</script> & more",
    "pic.png": b"\x89PNG\r\n\x1a\n" + bytes(range(64)),
    "doc.pdf": b"%PDF-1.4\n%fake\n",
    "data/t.parquet": bytes(range(256)) * 4,
    "{{CONTENT}}.txt": b"template placeholder in a file name",
}


def make_env(tmp_path, *, annotations=None, files=FILES, audience="lab", state_extra=None, entry=None, **over):
    cfg = lab_config(tmp_path, **over)
    if annotations is not None:
        cfg["annotations"] = annotation_settings(annotations)
    extra = dict(LAB_ALLOW) if audience == "lab" else {}
    extra.update(state_extra or {})
    return Env(tmp_path, audience=audience, expires_in=None, files=files, entry=entry, config_extra=cfg,
               state_extra=extra)


@pytest.fixture
def lab(tmp_path):
    e = make_env(tmp_path)
    yield e
    e.close()


@functools.lru_cache(maxsize=None)
def session(identity="alice", email=EMAIL, exp=NOW0 + 3600, share_id="shr-1", basis="e"):
    return mint_session(SECRET, share_id, identity, exp, email=email, basis=basis)


def base(sess=None, kind="f"):
    return f"/s/{TOKEN}/k/{sess or session()}/{kind}/"


def api(sess=None):
    return f"/s/{TOKEN}/k/{sess or session()}/annotations"


def write_cookie(sess=None, share_id="shr-1"):
    """The ``Cookie`` header value the session exchange would have set for ``sess`` (see test_share_write_cookie)."""
    sess = sess or session()
    return f"{write_cookie_name(share_id, sess)}={write_cookie_value(SECRET, share_id, sess)}"


def good_headers(sess=None, body=b"{}", **over):
    sess = sess or session()
    headers = {"Origin": ORIGIN, "X-Herdr-Annotate": csrf_token(SECRET, "shr-1", sess), "Cookie": write_cookie(sess),
               "Content-Type": "application/json", "Content-Length": str(len(body))}
    headers.update(over)
    return {k: v for k, v in headers.items() if v is not None}


def aget(env, sess=None, **over):
    """GET of the annotations route as the viewer's browser sends it: with the write cookie."""
    return env.request("GET", api(sess), {"Cookie": write_cookie(sess), **over})


def create_body(file="index.html", comment="Looks wrong", **extra):
    return json.dumps({"anchor": {"kind": "document", "file": file}, "comment": comment, **extra}).encode()


def post(env, path, body, sess=None, **over):
    return env.request("POST", path, good_headers(sess, body, **over), body)


def lines(env):
    path = env.config.annotations_path
    return [json.loads(x) for x in path.read_bytes().splitlines()] if path.exists() else []


# ----------------------------------------------------------------------------- the mode-switch link on f/

def doc_get(env, path, **headers):
    return env.request("GET", base() + path, {"Sec-Fetch-Dest": "document", **headers})


def link_for(depth, path):
    return share_public.mode_switch_link(depth, path)


def test_link_is_inserted_right_after_the_body_start_tag_and_nothing_else_changes(lab):
    status, h, body = doc_get(lab, "index.html")
    assert status == 200
    link = link_for(1, "index.html").encode()
    cut = HTML.index(b"<body>") + len(b"<body>")
    assert body == HTML[:cut] + link + HTML[cut:]
    assert h["etag"] == f'"{hashlib.sha256(HTML).hexdigest()}-m1"'
    assert h["content-length"] == str(len(body))
    assert h["vary"] == "Sec-Fetch-Dest"
    assert "accept-ranges" not in h
    assert h["content-security-policy"] == share_public.sandbox_csp(ORIGIN, TOKEN)
    assert h["access-control-allow-origin"] == "*"
    assert h["cache-control"] == "private, no-cache"


@pytest.mark.parametrize("name,html", [
    ("fb1.html", b"<!doctype html><html lang=en><head><title>t</title></head><p>x</p>"),
    ("fb2.html", b"<!DOCTYPE html><p>just a doctype</p>"),
])
def test_fallback_anchors_are_html_then_doctype(tmp_path, name, html):
    env = make_env(tmp_path, files={**FILES, name: html})
    try:
        status, _, body = doc_get(env, name)
        link = link_for(1, name).encode()
        cut = html.index(b">", html.lower().find(b"<html")) + 1 if b"<html" in html.lower() else html.index(b">") + 1
        assert status == 200 and body == html[:cut] + link + html[cut:]
    finally:
        env.close()


SCRIPTY = b'<html><head><script>document.write("<body>")</script><style>/* <body> */</style></head><!-- <body> --><textarea><body></textarea><body class="a>b" data-x=\'>\'><p>real</p></body></html>'
UTF16 = "<html><body>hi</body></html>".encode("utf-16")
NO_ANCHOR = b"<p>no html, no body, no doctype</p>"
WRITE = b'<html><head><script>document.write("</body>")</script></head><body><p>x</p><script>document.write("<body>")</script></body></html>'


def test_injection_ignores_comments_scripts_and_quoted_angle_brackets(tmp_path):
    env = make_env(tmp_path, files={**FILES, "scripty.html": SCRIPTY, "write.html": WRITE})
    try:
        status, _, body = doc_get(env, "scripty.html")
        link = link_for(1, "scripty.html").encode()
        start = SCRIPTY.index(b'<body class=')
        cut = SCRIPTY.index(b"><p>real", start) + 1
        assert body == SCRIPTY[:cut] + link + SCRIPTY[cut:]
        status, _, body = doc_get(env, "write.html")
        link = link_for(1, "write.html").encode()
        cut = WRITE.index(b"<body><p>x") + len(b"<body>")
        assert body == WRITE[:cut] + link + WRITE[cut:]
        assert body.count(b'document.write("</body>")') == 1 and b'document.write("<body>")' in body
    finally:
        env.close()


def test_unsafe_or_anchorless_html_is_served_untouched(tmp_path):
    env = make_env(tmp_path, files={**FILES, "wide.html": UTF16, "none.html": NO_ANCHOR,
                                    "open.html": b"<html><body class='x", "cmt.html": b"<!-- <body> --><p>x</p>"})
    try:
        for name, data in (("wide.html", UTF16), ("none.html", NO_ANCHOR), ("open.html", b"<html><body class='x"),
                           ("cmt.html", b"<!-- <body> --><p>x</p>")):
            assert_raw(env, base() + name, {"Sec-Fetch-Dest": "document"}, name, data)
    finally:
        env.close()


def test_non_utf8_charset_is_served_untouched(lab, monkeypatch):
    monkeypatch.setattr(share_public, "content_type_for", lambda n: "text/html; charset=shift_jis"
                        if n == "index.html" else "application/octet-stream")
    status, h, body = doc_get(lab, "index.html")
    assert status == 200 and body == HTML and "vary" not in h
    assert share_public._ascii_compatible("text/html; charset=utf-8", b"x")
    assert share_public._ascii_compatible("text/html; charset=US-ASCII", b"x")
    assert not share_public._ascii_compatible("text/html; charset=utf-16", b"x")
    assert not share_public._ascii_compatible("text/html; charset=utf-8", b"\xff\xfea\x00")
    assert not share_public._ascii_compatible("text/html", b"<\x00h\x00")


def test_link_text_is_script_free_and_frame_free(lab):
    link = link_for(1, "index.html")
    assert link.startswith("<a data-herdr-mode-switch ") and 'rel="noopener"' in link and link.endswith(">Annotate</a>")
    assert "<script" not in link.lower() and not re.search(r"\son[a-z]+\s*=", link, re.I)
    assert not FRAME_RE.search(link)


@pytest.mark.parametrize("name", ["index.html", "sub/x.html"])
def test_link_resolves_to_the_annotation_view_of_the_same_file(lab, name):
    status, _, body = doc_get(lab, name)
    href = re.search(rb'data-herdr-mode-switch href="([^"]*)"', body).group(1).decode()
    page = f"/s/{TOKEN}/k/SESS/f/{name}"
    assert urllib.parse.urljoin(page, href) == f"/s/{TOKEN}/k/SESS/a/{name}"


def test_link_path_is_html_escaped():
    link = share_public.mode_switch_link(1, 'a&b"c<d>.html')
    assert 'href="../a/a&amp;b&quot;c&lt;d&gt;.html"' in link


def test_amp_file_gets_an_escaped_href(lab):
    status, _, body = doc_get(lab, "a&b.html")
    assert status == 200 and b'href="../a/a&amp;b.html"' in body


def test_inject_helper_edge_cases():
    link = b"<a>L</a>"
    assert share_public.inject_mode_switch(b"x<BoDy id=1>y</body>z", link) == b"x<BoDy id=1>" + link + b"y</body>z"
    assert share_public.inject_mode_switch(b"abc", link) is None
    assert share_public.inject_mode_switch(b"<bodyguard>x", link) is None
    assert share_public.inject_mode_switch(b"<html><body>", link) == b"<html><body>" + link


def test_conditional_and_range_on_the_modified_response(lab):
    tag = f'"{hashlib.sha256(HTML).hexdigest()}-m1"'
    status, h, body = doc_get(lab, "index.html", **{"If-None-Match": tag})
    assert status == 304 and body == b"" and h["etag"] == tag
    raw_tag = f'"{hashlib.sha256(HTML).hexdigest()}"'
    status, _, body = doc_get(lab, "index.html", **{"If-None-Match": raw_tag})
    assert status == 200 and b"data-herdr-mode-switch" in body
    status, h, body = doc_get(lab, "index.html", Range="bytes=0-4")
    assert status == 200 and len(body) == int(h["content-length"]) > 5 and "content-range" not in h
    status, h, body = lab.request("HEAD", base() + "index.html", {"Sec-Fetch-Dest": "document"})
    assert status == 200 and body == b"" and h["content-length"] == str(len(HTML) + len(link_for(1, "index.html")))
    status, _, _ = doc_get(lab, "index.html", **{"If-Match": raw_tag})
    assert status == 412


def raw_expectation(name, data, lab=True):
    etag = f'"{hashlib.sha256(data).hexdigest()}"'
    expected = dict((k.lower(), v) for k, v in share_public._content_headers(
        name, etag, share_public.sandbox_csp(ORIGIN, TOKEN), sandbox_pdf=lab))
    expected.update({k.lower(): v for k, v in share_public.CORS_HEADERS})
    expected["content-length"] = str(len(data))
    return expected


def assert_raw(env, path, headers, name, data):
    status, h, body = env.request("GET", path, headers)
    assert status == 200 and body == data
    for key, value in raw_expectation(name, data, env.state.get("audience") == "lab").items():
        assert h[key] == value, key
    assert "vary" not in h


@pytest.mark.parametrize("dest", ["empty", "script", "image", "iframe", "Document", "document, empty", None])
def test_other_destinations_get_the_untouched_file(lab, dest):
    assert_raw(lab, base() + "index.html", {} if dest is None else {"Sec-Fetch-Dest": dest}, "index.html", HTML)


def test_range_without_document_destination_is_untouched(lab):
    status, h, body = lab.request("GET", base() + "index.html", {"Range": "bytes=0-4"})
    assert status == 206 and body == HTML[:5] and h["accept-ranges"] == "bytes"


def test_non_html_files_are_untouched_even_for_documents(lab):
    for name in ("doc.pdf", "pic.png"):
        assert_raw(lab, base() + name, {"Sec-Fetch-Dest": "document"}, name, FILES[name])


def test_public_share_and_disabled_annotations_are_untouched(tmp_path):
    pub_dir = tmp_path / "pub"
    pub_dir.mkdir()
    pub = Env(pub_dir, audience="public", files=FILES, config_extra=lab_config(pub_dir))
    try:
        assert_raw(pub, f"/s/{TOKEN}/f/index.html", {"Sec-Fetch-Dest": "document"}, "index.html", HTML)
    finally:
        pub.close()
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    off = make_env(off_dir, annotations={"enabled": False})
    try:
        assert_raw(off, base() + "index.html", {"Sec-Fetch-Dest": "document"}, "index.html", HTML)
    finally:
        off.close()


def test_the_cap_is_two_mebibytes():
    assert share_public.MODE_SWITCH_MAX_BYTES == 2 * 1024 * 1024


def test_oversize_html_is_served_untouched(lab, monkeypatch):
    monkeypatch.setattr(share_public, "MODE_SWITCH_MAX_BYTES", 10)
    assert_raw(lab, base() + "index.html", {"Sec-Fetch-Dest": "document"}, "index.html", HTML)


# ----------------------------------------------------------------------------- v/ live view

def v(env, path, method="GET", sess=None, **headers):
    return env.request(method, base(sess, "v") + path, headers)


def attr(page, name):
    return re.search(rf'{name}="([^"]*)"', page).group(1)


@pytest.mark.parametrize("name,kind", [("notes.txt", "text"), ("pic.png", "image"), ("doc.pdf", "pdf"),
                                       ("data/t.parquet", "document")])
def test_live_view_for_each_kind(lab, name, kind):
    status, h, body = v(lab, name)
    assert status == 200
    page = body.decode()
    assert h["content-type"] == "text/html; charset=utf-8"
    assert "access-control-allow-origin" not in h and "access-control-expose-headers" not in h
    nonce = re.search(r"script-src 'nonce-([^']*)' 'strict-dynamic'", h["content-security-policy"]).group(1)
    assert len(nonce) == 24 and f'nonce="{nonce}"' in page
    assert v(lab, name)[1]["content-security-policy"] != h["content-security-policy"]       # fresh nonce
    for key, value in (("x-frame-options", "DENY"), ("cache-control", "no-store"), ("x-content-type-options", "nosniff"),
                       ("referrer-policy", "no-referrer"), ("x-robots-tag", "noindex")):
        assert h[key] == value
    assert "herdr-annotate-csrf" not in page
    data = json.loads(re.search(r'id="herdr-view-data"[^>]*>(.*?)</script>', page, re.S).group(1))
    depth = name.count("/") + 1
    assert data == {"file": name, "kind": kind, "mode": "live", "entry": "index.html", "expires_at": NOW0 + 3600,
                    "max_comment_chars": 4000, "raw_href": "../" * depth + "f/" + urllib.parse.quote(name)}
    assert urllib.parse.urljoin(f"/s/{TOKEN}/k/S/v/{name}", data["raw_href"]) == f"/s/{TOKEN}/k/S/f/{name}"
    assert "{{" not in page.replace(r"{{CONTENT}}.txt", "")
    assert 'data-mode-switch' in page and page.count('aria-current="page"') == 1
    live = re.search(r'<a [^>]*href="([^"]*)"[^>]*aria-current="page"', page).group(1)
    assert urllib.parse.urljoin(f"/s/{TOKEN}/k/S/v/{name}", live) == f"/s/{TOKEN}/k/S/v/{name}"
    annotate = re.search(r'<a [^>]*data-mode="annotate"[^>]*href="([^"]*)"', page).group(1)
    assert urllib.parse.urljoin(f"/s/{TOKEN}/k/S/v/{name}", annotate) == f"/s/{TOKEN}/k/S/a/{name}"
    assert not FRAME_RE.search(page)


def test_text_view_is_escaped_and_wrapped(lab):
    page = v(lab, "notes.txt")[2].decode()
    assert "<script>alert" not in page.split('id="herdr-view-data"')[1].split("</script>", 1)[1]
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in page
    assert '<template shadowrootmode="open">' in page


def test_html_has_no_live_view(lab):
    ref = lab.request("GET", "/nothing-here")[2]
    assert v(lab, "index.html")[0] == 404 and v(lab, "index.html")[2] == ref


def test_view_csp_has_no_remote_style_origin_and_no_frames(lab):
    csp = v(lab, "notes.txt")[1]["content-security-policy"]
    directives = {d.split()[0]: d.split()[1:] for d in csp.split("; ")}
    assert directives["default-src"] == ["'none'"] and directives["frame-src"] == ["'none'"]
    assert directives["child-src"] == ["'none'"] and directives["base-uri"] == ["'none'"]
    assert "'self'" not in directives["script-src"] and "'unsafe-inline'" not in directives["script-src"]
    own = f"{ORIGIN}/s/{TOKEN}/"
    assert directives["connect-src"] == [own] and directives["img-src"] == [own, "data:", "blob:"]
    assert directives["style-src-elem"] == [f"{ORIGIN}/s/static/", own, "'unsafe-inline'"]
    assert not any(tok.startswith("http") and not tok.startswith(ORIGIN) for ds in directives.values() for tok in ds)


def test_placeholder_in_a_file_name_does_not_double_substitute(lab):
    status, _, body = v(lab, "%7B%7BCONTENT%7D%7D.txt")
    page = body.decode()
    assert status == 200 and page.count("template placeholder in a file name") == 1


def test_mode_bar_without_annotations_has_live_only(tmp_path):
    env = make_env(tmp_path, annotations={"enabled": False})
    try:
        status, _, body = v(env, "notes.txt")
        assert status == 200 and b'data-mode="annotate"' not in body and b"Annotate" not in body
        assert body.count(b'<a data-mode="live"') == 1
        assert env.request("POST", api(), good_headers(body=create_body()), create_body())[0] == 404
        assert env.request("GET", api())[0] == 404
    finally:
        env.close()


def test_live_view_404s(lab, tmp_path):
    ref = lab.request("GET", "/nothing-here")[2]
    expired = session(exp=NOW0 - 1)
    other = session(share_id="shr-2")
    for sess in ("alice.garbage", expired, other, "x" * 40):
        assert v(lab, "notes.txt", sess=sess)[0:3:2] == (404, ref)
    assert v(lab, "missing.txt")[2] == ref and v(lab, "missing.txt")[0] == 404
    assert lab.request("GET", f"/s/{OTHER_TOKEN}/k/{session()}/v/notes.txt")[2] == ref
    assert lab.request("OPTIONS", base(kind="v") + "notes.txt", {"Origin": "null"})[0] == 404
    assert lab.request("POST", base(kind="v") + "notes.txt", good_headers(body=b"{}"), b"{}")[0] == 404
    write_state(lab.root, lab.token_hash, dict(lab.state, state="revoked"))
    assert v(lab, "notes.txt")[0] == 404
    remove_state(lab.root, lab.token_hash)
    assert v(lab, "notes.txt")[0] == 404


def test_public_share_has_no_v_or_annotation_routes(tmp_path):
    env = Env(tmp_path, audience="public", files=FILES, config_extra=lab_config(tmp_path))
    try:
        ref = env.request("GET", "/nothing-here")[2]
        for path in (f"/s/{TOKEN}/k/{session()}/v/notes.txt", f"/s/{TOKEN}/k/{session()}/annotations"):
            status, _, body = env.request("GET", path)
            assert (status, body) == (404, ref)
        assert env.request("POST", api(), good_headers(body=create_body()), create_body())[0] == 404
        assert not env.config.annotations_path.exists()
    finally:
        env.close()


def test_view_records_an_opened_event_for_the_entry_only(tmp_path):
    env = make_env(tmp_path, files={"index.txt": b"hi", "other.txt": b"yo"}, entry="index.txt")
    try:
        assert env.request("GET", base(kind="v") + "other.txt")[0] == 200
        assert env.request("GET", base(kind="v") + "index.txt")[0] == 200
        events = [json.loads(x) for x in env.event_lines()]
        opened = [e for e in events if e["kind"] == "opened"]
        assert [e["detail"] for e in opened] == [{"file": "index.txt", "mode": "live"}]
    finally:
        env.close()


def test_view_refuses_a_file_changed_underneath(lab):
    victim = lab.root / "shr-1" / "files" / "notes.txt"
    victim.write_bytes(b"x" * len(FILES["notes.txt"]))          # same size, other bytes
    assert v(lab, "notes.txt")[0] == 404
    victim.write_bytes(b"short")
    assert v(lab, "notes.txt")[0] == 404


# ----------------------------------------------------------------------------- POST create

def test_create_writes_exactly_one_line_with_the_session_identity(lab):
    body = create_body(email="mallory@evil.example", identity="mallory")
    status, h, out = post(lab, api(), body.replace(b'"comment"', b'"x":1,"comment"'))
    assert status == 400 and json.loads(out) == {"error": "bad_anchor"} and lines(lab) == []   # unknown key
    body = create_body(comment="Looks wrong")
    status, h, out = post(lab, api(), body)
    assert status == 201, out
    assert "access-control-allow-origin" not in h and h["cache-control"] == "no-store"
    got = json.loads(out)["annotation"]
    assert ANNOTATION_ID_RE.fullmatch(got["annotation_id"])
    assert got["anchor"] == {"kind": "document", "file": "index.html"} and got["body"] == "Looks wrong"
    assert got["status"] == "submitted" and got["created_at"]
    (line,) = lines(lab)
    assert line["op"] == "create" and line["v"] == 1 and line["share_id"] == "shr-1"
    assert line["author_identity"] == "alice" and line["author_email"] == EMAIL
    assert line["annotation_id"] == got["annotation_id"] and line["body"] == "Looks wrong"
    assert line["anchor"] == {"kind": "document", "file": "index.html"} and line["at"]
    assert "identity" not in line and "email" not in line


def test_create_refuses_a_file_outside_the_index(lab):
    status, _, out = post(lab, api(), create_body(file="nope.html"))
    assert status == 400 and json.loads(out) == {"error": "bad_file"} and lines(lab) == []


def test_comment_limit_follows_the_configuration(tmp_path):
    env = make_env(tmp_path, annotations={"max_comment_chars": 5})
    try:
        assert post(env, api(), create_body(comment="123456"))[0] == 400
        assert post(env, api(), create_body(comment="12345"))[0] == 201
    finally:
        env.close()


def refused(env, path, body, expect_lines=0, sess=None, **over):
    status, h, out = env.request("POST", path, good_headers(sess, body, **over), body)
    assert status == 404, out
    assert out == env.request("GET", "/nothing-here")[2]
    assert len(lines(env)) == expect_lines


@pytest.mark.parametrize("over", [
    {"Origin": "null"}, {"Origin": None}, {"Origin": "https://evil.example"}, {"Origin": ORIGIN + "/"},
    {"Origin": "http://share.example"}, {"X-Herdr-Annotate": None}, {"X-Herdr-Annotate": "x" * 43},
    {"X-Herdr-Annotate": ""}, {"Content-Type": "text/plain"}, {"Content-Type": None},
    {"Content-Type": "application/x-www-form-urlencoded"}, {"Content-Type": "application/json; charset=utf-8"},
    {"Content-Type": "APPLICATION/JSON"}, {"Content-Length": None}, {"Content-Length": "0"},
])
def test_header_gate_refusals(lab, over):
    refused(lab, api(), create_body(), **over)


def test_another_sessions_csrf_token_is_refused(lab):
    other = session("bob", "bob@lab.example")
    refused(lab, api(), create_body(), **{"X-Herdr-Annotate": csrf_token(SECRET, "shr-1", other)})
    refused(lab, api(), create_body(), **{"X-Herdr-Annotate": csrf_token(SECRET, "shr-2", session())})


def raw_post(env, headers: list[str], body: bytes) -> bytes:
    head = f"POST {api()} HTTP/1.1\r\nHost: x\r\n" + "".join(h + "\r\n" for h in headers) + "\r\n"
    return env.raw(head.encode() + body)


def test_framing_refusals_write_nothing(lab):
    body = create_body()
    base_headers = [f"Origin: {ORIGIN}", "Content-Type: application/json",
                    f"X-Herdr-Annotate: {csrf_token(SECRET, 'shr-1', session())}", f"Cookie: {write_cookie()}"]
    out = raw_post(lab, base_headers + [f"Content-Length: {len(body)}", f"Content-Length: {len(body)}"], body)
    assert out.startswith(b"HTTP/1.1 404") and lines(lab) == []
    out = raw_post(lab, base_headers + ["Transfer-Encoding: chunked"], b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))
    assert out.startswith(b"HTTP/1.1 404") and lines(lab) == []
    big = json.dumps({"anchor": {"kind": "document", "file": "index.html"}, "comment": "a" * 32769}).encode()
    status, _, _ = lab.request("POST", api(), good_headers(body=big), big)
    assert status == 404 and lines(lab) == []
    pad = b" " * (32768 - len(body))                    # exactly at the limit is accepted
    status, _, _ = lab.request("POST", api(), good_headers(body=body + pad), body + pad)
    assert status == 201


def test_short_body_is_refused(lab):
    body = create_body()
    headers = [f"Origin: {ORIGIN}", "Content-Type: application/json",
               f"X-Herdr-Annotate: {csrf_token(SECRET, 'shr-1', session())}", f"Cookie: {write_cookie()}", f"Content-Length: {len(body) + 50}"]
    head = f"POST {api()} HTTP/1.1\r\nHost: x\r\n" + "".join(h + "\r\n" for h in headers) + "\r\n"
    with socket.create_connection(("127.0.0.1", lab.port), timeout=10) as conn:
        conn.sendall(head.encode() + body)
        conn.shutdown(socket.SHUT_WR)                      # the promised bytes never arrive
        out = conn.recv(65536)
    assert out.startswith(b"HTTP/1.1 404") and lines(lab) == []


def test_session_state_refusals(lab, tmp_path):
    expired = session(exp=NOW0 - 1)
    refused(lab, api(expired), create_body(), **{"X-Herdr-Annotate": csrf_token(SECRET, "shr-1", expired)})
    refused(lab, api("alice.junk.1.abc"), create_body())
    write_state(lab.root, lab.token_hash, dict(lab.state, state="revoked"))
    refused(lab, api(), create_body())
    write_state(lab.root, lab.token_hash, lab.state)
    assert post(lab, api(), create_body())[0] == 201


def test_annotations_disabled_refuses_every_route(tmp_path):
    env = make_env(tmp_path, annotations={"enabled": False})
    try:
        refused(env, api(), create_body())
        assert env.request("GET", api())[0] == 404
    finally:
        env.close()


def test_missing_public_url_means_no_origin_and_no_writes(tmp_path):
    env = make_env(tmp_path)
    try:
        env.server.annotate_origin = None
        refused(env, api(), create_body())
    finally:
        env.close()


def test_allow_list_is_rechecked_on_every_write(lab):
    write_state(lab.root, lab.token_hash, dict(lab.state, allow_emails=["someone@else.example"], allow_groups=[]))
    refused(lab, api(), create_body())
    write_state(lab.root, lab.token_hash, dict(lab.state, allow_emails=[EMAIL.upper()], allow_groups=[]))
    assert post(lab, api(), create_body())[0] == 201


def test_email_removal_is_honoured_even_when_the_share_has_groups(lab):
    assert lab.state["allow_groups"]                                      # the fixture share has groups
    write_state(lab.root, lab.token_hash, dict(lab.state, allow_emails=["someone@else.example"]))
    refused(lab, api(), create_body())                                    # alice (basis e) was removed: no write
    write_state(lab.root, lab.token_hash, dict(lab.state, allow_emails=[EMAIL]))
    assert post(lab, api(), create_body())[0] == 201


def test_group_admitted_sessions_may_write(lab):
    bob = session("bob", "bob@lab.example", basis="g")
    assert post(lab, api(bob), create_body(), bob)[0] == 201             # allow_groups is non-empty
    write_state(lab.root, lab.token_hash, dict(lab.state, allow_emails=[]))
    assert post(lab, api(bob), create_body(), bob)[0] == 201             # a group session never depended on the list
    assert [x["author_identity"] for x in lines(lab)] == ["bob", "bob"]
    write_state(lab.root, lab.token_hash, dict(lab.state, allow_groups=[]))
    refused(lab, api(bob), create_body(), sess=bob, expect_lines=2)


def test_a_forged_basis_is_an_invalid_session(lab):
    bob = session("bob", "bob@lab.example", basis="g")
    _, a_exp, a_blob = bob.split(".")
    forged = ".".join(("e", a_exp, a_blob))
    refused(lab, api(forged), create_body(), sess=forged)


def test_rate_limits_user_share_and_global(tmp_path):
    env = make_env(tmp_path)
    try:
        codes = [post(env, api(), create_body())[0] for _ in range(61)]
        assert codes[:60] == [201] * 60 and codes[60] == 429
        status, h, out = post(env, api(), create_body())
        assert status == 429 and json.loads(out) == {"error": "rate_limited"} and "access-control-allow-origin" not in h
        assert len(lines(env)) == 60
        bob = session("bob", "bob@lab.example", basis="g")
        assert post(env, api(bob), create_body(), bob)[0] == 201          # another identity has its own budget
        env.clock[0] += 61
        assert post(env, api(), create_body())[0] == 201
    finally:
        env.close()


def test_share_and_global_budgets(tmp_path):
    env = make_env(tmp_path, annotations={"per_share_per_minute": 2})
    try:
        sessions = [session(f"u{i}", f"u{i}@lab.example", basis="g") for i in range(3)]
        codes = [post(env, api(s), create_body(), s)[0] for s in sessions]
        assert codes == [201, 201, 429]
    finally:
        env.close()
    glob = tmp_path / "g"
    glob.mkdir()
    env = make_env(glob, annotations={"global_per_minute": 1})
    try:
        assert [post(env, api(), create_body())[0] for _ in range(2)] == [201, 429]
    finally:
        env.close()


def test_full_annotations_file_is_503(lab, monkeypatch):
    monkeypatch.setattr(share_public, "ANNOTATIONS_MAX_BYTES", 10)
    status, h, out = post(lab, api(), create_body())
    assert status == 503 and json.loads(out) == {"error": "unavailable"} and "access-control-allow-origin" not in h
    assert lines(lab) == []


def test_annotations_file_is_world_readable_and_each_line_is_ascii(lab):
    assert post(lab, api(), create_body(comment="café \U0001f600"))[0] == 201
    data = lab.config.annotations_path.read_bytes()
    assert data.isascii() and data.endswith(b"\n") and data.count(b"\n") == 1
    assert (lab.config.annotations_path.stat().st_mode & 0o777) == 0o644
    assert lines(lab)[0]["body"] == "café \U0001f600"


def test_line_cap_is_the_workers_limit_and_refuses_longer_lines(lab, monkeypatch, tmp_path):
    assert share_public.ANNOTATION_LINE_MAX == 65536
    log = share_public.AnnotationLog(tmp_path / "x.jsonl")
    assert not log.append({"body": "a" * 70000}) and not (tmp_path / "x.jsonl").exists()
    just_under = {"body": "a" * (65536 - len('{"body": ""}\n') - 5)}
    assert log.append(just_under)
    assert len((tmp_path / "x.jsonl").read_bytes()) <= 65536
    monkeypatch.setattr(share_public, "ANNOTATION_LINE_MAX", 200)          # an HTTP-level refusal: 503, nothing written
    status, _, out = post(lab, api(), create_body(comment="c" * 300))
    assert status == 503 and json.loads(out) == {"error": "unavailable"} and lines(lab) == []
    assert post(lab, api(), create_body(comment="short"))[0] == 503       # the whole envelope is over 200 bytes
    monkeypatch.setattr(share_public, "ANNOTATION_LINE_MAX", 65536)
    assert post(lab, api(), create_body(comment="c" * 300))[0] == 201


def test_worst_case_line_fits_the_worker_limit(lab):
    anchor = {"kind": "pdf_page", "file": "doc.pdf", "page": 99999, "exact": "\U0001f600" * 1000,
              "region": {"x": 0.123456789012345, "y": 0.5, "w": 0.1, "h": 0.1}}
    body = json.dumps({"anchor": anchor, "comment": "\U0001f600" * 4000},
                      ensure_ascii=False).encode()
    assert len(body) <= 32768
    status, _, _ = post(lab, api(), body)
    assert status == 201
    assert len(lab.config.annotations_path.read_bytes()) <= share_public.ANNOTATION_LINE_MAX


# ----------------------------------------------------------------------------- GET own annotations / edit / delete

def seed(env, identity, rows):
    write_own_projection(env.root, "shr-1", identity, rows)


def row(aid, status="open", file="index.html", body="mine"):
    return {"annotation_id": aid, "file": file, "anchor": {"kind": "document", "file": file}, "body": body,
            "status": status, "created_at": "2026-10-31T00:00:00+00:00"}


AID = "ann_" + "A" * 22
BID = "ann_" + "B" * 22
CID = "ann_" + "C" * 22


def test_get_returns_only_the_callers_own_rows(lab):
    status, h, out = aget(lab)
    assert status == 200 and json.loads(out) == {"annotations": []}
    seed(lab, "alice", [row(AID)])
    seed(lab, "bob", [row(BID, body="bobs secret")])
    status, h, out = aget(lab)
    assert status == 200 and b"bobs secret" not in out
    assert [r["annotation_id"] for r in json.loads(out)["annotations"]] == [AID]
    for key, value in (("cache-control", "no-store"), ("x-content-type-options", "nosniff")):
        assert h[key] == value
    assert "access-control-allow-origin" not in h and h["content-type"] == "application/json"
    bob = session("bob", "bob@lab.example")
    assert [r["annotation_id"] for r in json.loads(aget(lab, bob)[2])["annotations"]] == [BID]


def test_get_with_a_corrupt_projection_is_unavailable(lab):
    seed(lab, "alice", [row(AID)])
    name = hashlib.sha256(b"alice").hexdigest() + ".json"
    (lab.root / "shr-1" / "annotations" / name).write_bytes(b"not json")
    assert aget(lab)[0] == 503


def item(aid, verb, sess=None):
    return f"{api(sess)}/{aid}/{verb}"


def test_edit_and_delete_an_open_own_row(lab):
    seed(lab, "alice", [row(AID), row(BID, status="addressed"), row(CID)])
    status, h, out = post(lab, item(AID, "edit"), json.dumps({"comment": "better"}).encode())
    assert status == 202 and json.loads(out) == {"annotation_id": AID, "status": "pending"}
    assert "access-control-allow-origin" not in h
    status, _, out = post(lab, item(CID, "delete"), b"{}")
    assert status == 202 and json.loads(out) == {"annotation_id": CID, "status": "pending"}
    edit, delete = lines(lab)
    assert edit["op"] == "edit" and edit["annotation_id"] == AID and edit["body"] == "better"
    assert edit["author_identity"] == "alice" and edit["share_id"] == "shr-1" and edit["v"] == 1 and edit["at"]
    assert delete["op"] == "delete" and delete["annotation_id"] == CID and "body" not in delete
    assert delete["author_identity"] == "alice"
    assert edit["basis"] == delete["basis"] == "e"          # the worker re-checks admission by this basis (plan 4 C3)


def test_edit_refusals(lab):
    seed(lab, "alice", [row(AID), row(BID, status="addressed")])
    seed(lab, "bob", [row(CID)])
    body = json.dumps({"comment": "x"}).encode()
    for aid in (BID, CID, "ann_" + "Z" * 22):
        status, _, out = post(lab, item(aid, "edit"), body)
        assert status == 409 and json.loads(out) == {"error": "not_editable"}
    assert lines(lab) == []
    for bad in ("ann_short", "nope", "ann_" + "A" * 23, "ann_" + "A" * 21 + "!"):
        status, _, out = post(lab, item(bad, "edit"), body)
        assert status == 404 and out == lab.request("GET", "/nothing-here")[2]
    assert post(lab, item(AID, "edit"), json.dumps({"comment": "x", "anchor": {}}).encode())[0] == 400
    assert post(lab, item(AID, "edit"), json.dumps({"comment": ""}).encode())[0] == 400
    assert post(lab, item(AID, "delete"), json.dumps({"comment": "x"}).encode())[0] == 400
    assert lines(lab) == []
    assert lab.request("POST", item(AID, "frobnicate"), good_headers(body=body), body)[0] == 404
    assert lab.request("GET", item(AID, "edit"))[0] == 404


def test_edit_needs_the_gate_too(lab):
    seed(lab, "alice", [row(AID)])
    body = json.dumps({"comment": "x"}).encode()
    for over in ({"Origin": "null"}, {"X-Herdr-Annotate": "x" * 43}, {"Content-Type": "text/plain"}):
        refused(lab, item(AID, "edit"), body, **over)


def test_options_and_other_methods_on_annotation_paths_are_404(lab):
    ref = lab.request("GET", "/nothing-here")[2]
    paths = [api(), item(AID, "edit"), item(AID, "delete"), base(kind="v") + "notes.txt", base(kind="a") + "notes.txt"]
    for path in paths:
        status, _, out = lab.request("OPTIONS", path, {"Origin": "null", "Access-Control-Request-Method": "POST",
                                                      "Access-Control-Request-Headers": "x-herdr-annotate"})
        assert (status, out) == (404, ref), path
    for method in ("HEAD", "PUT", "DELETE", "PATCH"):
        assert lab.request(method, api())[0] == 404
    assert lab.request("OPTIONS", f"/s/{TOKEN}/session", {"Origin": "null"})[0] == 404
    assert lab.request("OPTIONS", base() + "index.html", {"Origin": "null"})[0] == 204
    assert lab.request("POST", base() + "index.html", good_headers(body=b"{}"), b"{}")[0] == 404
    assert lab.request("GET", api() + "/x")[0] == 404


def test_logs_and_events_never_hold_secrets(lab, capfd):
    sess = session()
    token = csrf_token(SECRET, "shr-1", sess)
    post(lab, api(sess), create_body(), sess)
    post(lab, api(sess), create_body(), sess, Origin="null")
    nonce_csp = v(lab, "notes.txt", sess=sess)[1]["content-security-policy"]
    nonce = re.search(r"'nonce-([^']*)'", nonce_csp).group(1)
    captured = capfd.readouterr()
    text = lab.events.read_text() + captured.out + captured.err
    for secret in (sess, token, nonce, TOKEN):
        assert secret not in text, secret
    for name in ("notes.txt", "Looks wrong"):
        assert name not in captured.out + captured.err and name not in text
    kinds = {json.loads(x)["kind"] for x in lab.event_lines()}
    assert "denied" in kinds
    assert not any("Looks wrong" in x.decode() for x in lab.event_lines())
