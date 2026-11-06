"""Owner dashboard render routes over real HTTP: static copies, the sandboxed live route and the route order.

The registry is an in-memory store with the adapter surface the routes use (``sync_call``); the sanitiser is the real
``nh3`` child process. Hostile frame markup is built by concatenation so the repo-wide frame guards stay quiet.
"""
import asyncio
import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane import share_render
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifacts import ArtifactError
from herdr_engineering.control_plane.repo import ControlPlaneRepo

OWNER = hashlib.sha256(b"owner-key").hexdigest()[:16]
FRAME = "<" + "iframe src=https://evil.example/x></" + "iframe>"
HOSTILE = ("<h1>Findings</h1><script>window.__x=1</script>" + FRAME +
           '<img src=x onerror=alert(1)><a href="#/settings">settings</a>'
           '<a href="https://example.org/ok">ok</a><a href="relative.html">rel</a>'
           '<img src="data:image/png;base64,iVBORw0KGgo=">')
REFUSED = {"error": "render refused"}
UNAVAILABLE = {"error": "render unavailable"}


def key_of(data):
    digest = hashlib.sha256(data).hexdigest()
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}", digest


class Blobs:
    def __init__(self):
        self.objects = {}
        self.objects_by_sha = {}

    def add(self, data):
        key, digest = key_of(data)
        self.objects[key] = data
        self.objects_by_sha[digest] = data
        return {"storage_key": key, "sha256": digest, "size_bytes": len(data)}

    def get(self, key, digest):
        return self.objects[key]

    def iter_get(self, key, digest, chunk_size=1024 * 1024):
        data = self.objects[key]
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]


class Store:
    def __init__(self, blobs):
        self.blobs = blobs
        self.records = {}
        self.outputs = {}
        self.descriptors = {}       # (artifact, version) -> descriptor overriding the generic one
        self.dead = False

    def add(self, aid, name, mime, data):
        self.records[aid] = {"artifact_id": aid, "original_filename": name, "mime_type": mime,
                             "artifact_type": "other", "status": "available", **self.blobs.add(data)}

    def sync_call(self, method, *args, **kwargs):
        if self.dead:
            raise ConnectionResetError("password authentication failed for user secret_db_user")
        return asyncio.run(getattr(self, method)(*args, **kwargs))

    async def get_record(self, aid, **_):
        return self.records.get(aid)

    async def tombstoned_ids(self):
        return []

    async def lifecycle_of(self, aid):
        return "active"

    async def report_permission(self, aid, principal, permission, **_):
        return aid in self.records and OWNER in principal

    async def operator_member_access(self, aid, principal, permission):
        return False    # no member-owned artifacts in this store

    async def get_version(self, vid, aid=None):
        outputs = self.outputs.get((aid, vid))
        return None if outputs is None else {"version_id": vid, "artifact_id": aid, "output_manifest": outputs}

    figure_assets = ()    # (id, media_type, bytes) shown as figures of version "vfig"

    async def get_report_asset(self, aid, vid, asset_id):
        for name, media, data in self.figure_assets:
            if name == asset_id:
                return {"id": name, "media_type": media, **self.blobs.add(data)}
        descriptor = await self.get_report_descriptor(aid, vid)
        asset = next((a for a in descriptor["assets"] if a["id"] == asset_id), None)
        if asset is None:
            raise ArtifactError("no artifact report asset")
        key, _ = key_of(self.blobs.objects_by_sha[asset["sha256"]])
        return {**asset, "storage_key": key}

    async def get_report_descriptor(self, aid, vid):
        if (aid, vid) in self.descriptors:
            return self.descriptors[(aid, vid)]
        if vid == "vfig":
            base = await self.get_report_descriptor(aid, "v1")
            assets = [{"id": n, "name": f"{n}.bin", "role": "figure", "media_type": m,
                       "sha256": hashlib.sha256(d).hexdigest(), "size_bytes": len(d)}
                      for n, m, d in self.figure_assets]
            figures = [{"id": f"fig-{n}", "title": f"Figure {n}", "asset_id": n, "caption": f"cap {n}"}
                       for n, _, _ in self.figure_assets]
            return {**base, "figures": figures, "assets": base["assets"] + assets}
        return {"schema_version": 1, "title": "Generic investigation", "summary": "Open evidence",
                "questions": [{"id": "q1", "text": "What is known?"}],
                "evidence": [{"id": "e1", "title": "Account", "description": "Observed information.",
                              "asset_ids": ["data1"]}],
                "findings": [{"id": "f1", "title": "Uncertainty", "statement": "Findings here.",
                              "question_ids": ["q1"], "evidence_ids": ["e1"]}],
                "figures": [], "assets": [{"id": "data1", "name": "data.csv", "role": "data",
                    "media_type": "text/csv", "sha256": hashlib.sha256(b"evidence").hexdigest(), "size_bytes": 8}]}


@pytest.fixture
def world():
    blobs = Blobs()
    store = Store(blobs)
    acl = ArtifactAcl()
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator", "other-key": "operator"},
                                artifact_store=store, rustfs=blobs, artifact_acl=acl)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    store.add("h1", "page.html", "text/html", HOSTILE.encode())
    store.add("t1", "notes.txt", "text/plain", b"<b>plain</b> & text")
    store.add("p1", "doc.pdf", "application/pdf", b"%PDF-1.4 fake")
    store.add("i1", "pic.png", "image/png", b"\x89PNG")
    store.add("big", "big.html", "text/html", b"<p>" + b"a" * (share_render.MAX_RENDER_INPUT + 1))
    for aid in list(store.records):
        acl.grant(aid, f"api-key:{OWNER}:operator", {"read"})
    index = b'<h1>Legacy</h1><img src="fig.png"><img src="../escape.png"><a href="other.html">o</a><a href="#/x">x</a>'
    index += b'<link rel="stylesheet" href="style.css"><link rel="stylesheet" href="./style.css">'
    sheet = b"body{background:url(https://tracker.example/pixel.gif)}@import url(https://tracker.example/i.css);"
    store.outputs[("h1", "v1")] = [
        {"name": "index.html", "media_type": "text/html", **blobs.add(index)},
        {"name": "fig.png", "media_type": "image/png", **blobs.add(b"\x89PNG")},
        {"name": "style.css", "media_type": "text/css", **blobs.add(sheet)},
    ]
    store.blobs = blobs
    store.acl = acl
    store.outputs[("h1", "v2")] = []
    try:
        yield f"http://127.0.0.1:{server.port}", store
    finally:
        server.shutdown()


def get(base, path, key="owner-key"):
    req = urllib.request.Request(base + path, headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


def envelope(base, path, key="owner-key"):
    status, body, headers = get(base, path, key)
    assert headers["Content-Type"] == "application/json"
    return status, json.loads(body), headers


def assert_inert(html):
    lowered = html.lower()
    for bad in ("<script", "<iframe", "onerror", "alert(1)", "#/settings", "evil.example", "window.__x"):
        assert bad not in lowered, bad


ROUTES = ["/api/v1/artifacts/h1/render", "/api/v1/artifacts/h1/versions/v1/outputs/0/render",
          "/api/v1/artifacts/h1/versions/v1/preview/render", "/api/v1/artifacts/h1/live"]


def test_render_strips_active_content_and_keeps_safe_links(world):
    base, _ = world
    status, body, headers = envelope(base, "/api/v1/artifacts/h1/render")
    assert status == 200 and body["kind"] == "html" and body["too_large"] is False
    assert_inert(body["html"])
    assert "Findings" in body["html"] and "https://example.org/ok" in body["html"]
    assert "data:image/png;base64,iVBORw0KGgo=" in body["html"]
    assert "relative.html" not in body["html"]
    assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"


def test_text_pdf_and_image_kinds(world):
    base, _ = world
    status, body, _ = envelope(base, "/api/v1/artifacts/t1/render")
    assert status == 200 and body["kind"] == "text" and "&lt;b&gt;plain&lt;/b&gt; &amp; text" in body["html"]
    for aid, kind in (("p1", "pdf"), ("i1", "image")):
        status, body, _ = envelope(base, f"/api/v1/artifacts/{aid}/render")
        assert (status, body) == (200, {"kind": kind, "html": None, "too_large": False})


def test_over_the_input_cap_is_too_large_without_html(world):
    base, _ = world
    status, body, _ = envelope(base, "/api/v1/artifacts/big/render")
    assert status == 200 and body == {"kind": "html", "html": None, "too_large": True}


def test_legacy_output_resolver_maps_siblings_and_drops_the_rest(world):
    base, _ = world
    status, body, _ = envelope(base, "/api/v1/artifacts/h1/versions/v1/outputs/0/render")
    assert status == 200
    html = body["html"]
    assert 'src="/api/v1/artifacts/h1/versions/v1/outputs/1"' in html
    assert "escape.png" not in html and "other.html" not in html and "#/x" not in html
    assert "Legacy" in html
    # a sibling stylesheet is raw CSS (url() / @import beacons) and the dashboard has no CSP: never mapped or served here
    assert "style.css" not in html and "/outputs/2" not in html and "tracker.example" not in html
    status, body, _ = envelope(base, "/api/v1/artifacts/h1/versions/v1/outputs/2/render")
    assert status == 200 and body["kind"] == "text" and "tracker.example" in body["html"]   # escaped text, never a stylesheet
    assert "<style" not in body["html"].lower() and "<link" not in body["html"].lower()


def test_pdf_and_image_renders_do_not_read_the_blob(world):
    base, store = world
    reads = []
    original = store.blobs.get
    store.blobs.get = lambda key, digest: (reads.append(key), original(key, digest))[1]
    for path in ("/api/v1/artifacts/p1/render", "/api/v1/artifacts/i1/render",
                 "/api/v1/artifacts/h1/versions/v1/outputs/1/render"):
        assert envelope(base, path)[0] == 200
    assert reads == []
    assert envelope(base, "/api/v1/artifacts/h1/render")[1]["kind"] == "html" and len(reads) == 1


SCRIPTED_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>window.__x=1</script>'
                b'<foreignObject><iframe src="http://evil.example"/></foreignObject>'
                b'<image href="http://evil.example/p.png"/><use href="http://evil.example/s.svg#a"/>'
                b'<style>@import url(http://evil.example/x.css);</style></svg>')


def test_dashboard_report_preview_shows_safe_figure_images_through_the_real_sanitiser(world):
    import re
    base, store = world
    store.outputs[("h1", "vfig")] = []
    store.figure_assets = (("svg", "image/svg+xml", SCRIPTED_SVG), ("png", "image/png", b"\x89PNG\r\n\x1a\n"),
                           ("html", "text/html", b"<script>alert(1)</script>"),
                           ("blob", "application/octet-stream", b"\x00\x01"))
    status, body, _ = envelope(base, "/api/v1/artifacts/h1/versions/vfig/preview/render")
    assert status == 200 and body["kind"] == "html"
    html = body["html"]
    images = re.findall(r'<img[^>]*>', html)
    assert len(images) == 2
    for tag in images:
        assert re.fullmatch(r'<img class="fig-img" alt="Figure (svg|png)" src="data:image/(svg\+xml|png);base64,[A-Za-z0-9+/=]+">', tag), tag
    for needle in ("<svg", "<script", "onload", "evil.example", "javascript:", "window.__x", "<iframe", "foreignobject"):
        assert needle not in html.lower(), needle
    assert "cap html" in html and "cap blob" in html and "Download figure" in html


def test_dashboard_report_preview_without_figure_assets_or_permission_embeds_nothing(world):
    base, store = world
    store.outputs[("h1", "vfig")] = []
    store.figure_assets = ()
    assert "<img" not in envelope(base, "/api/v1/artifacts/h1/versions/vfig/preview/render")[1]["html"]
    store.figure_assets = (("svg", "image/svg+xml", SCRIPTED_SVG),)
    assert get(base, "/api/v1/artifacts/h1/versions/vfig/preview/render", key="other-key")[0] == 404


def test_preview_and_outputs_render_win_over_the_report_routes(world):
    """Regression: route_report would serve raw preview HTML on ``.../preview/render`` and 404 ``.../outputs/{i}/render``."""
    base, _ = world
    status, body, headers = envelope(base, "/api/v1/artifacts/h1/versions/v1/preview/render")
    assert status == 200 and set(body) == {"kind", "html", "too_large"} and body["kind"] == "html"
    assert "Findings here." in body["html"] and "<script" not in body["html"].lower()
    assert headers["Content-Type"] == "application/json"
    status, body, _ = envelope(base, "/api/v1/artifacts/h1/versions/v1/outputs/1/render")
    assert status == 200 and body["kind"] == "image"
    assert get(base, "/api/v1/artifacts/h1/versions/v1/outputs/7/render")[0] == 404
    assert get(base, "/api/v1/artifacts/h1/versions/zzz/outputs/0/render")[0] == 404
    assert get(base, "/api/v1/artifacts/h1/versions/zzz/preview/render")[0] == 404


@pytest.mark.parametrize("path", ROUTES)
def test_without_permission_every_route_is_a_plain_404(world, path):
    base, _ = world
    status, body, _ = get(base, path, key="other-key")
    assert status == 404 and json.loads(body) == {"error": "not found"}
    assert get(base, path.replace("/h1/", "/nope/"), key="other-key")[0] == 404


def test_live_route_headers_for_html_and_pdf(world):
    base, _ = world
    status, body, headers = get(base, "/api/v1/artifacts/h1/live")
    assert status == 200 and body == HOSTILE.encode()
    csp = headers["Content-Security-Policy"]
    assert csp.startswith("sandbox allow-scripts allow-downloads; default-src 'none'")
    assert "allow-same-origin" not in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    for directive in ("img-src data: blob:", "font-src data:", "style-src 'unsafe-inline'", "script-src 'unsafe-inline'",
                      "base-uri 'none'", "form-action 'none'"):
        assert directive in csp, directive
    for absent in ("connect-src", "worker-src", "frame-src", "https:", "'self'"):
        assert absent not in csp, absent    # default-src 'none' covers fetch, workers and frames
    assert headers["X-Frame-Options"] == "DENY" and headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer" and headers["Cache-Control"] == "no-store"
    assert headers["Access-Control-Allow-Origin"] is None
    assert headers["Content-Type"].startswith("text/html")
    status, body, headers = get(base, "/api/v1/artifacts/p1/live")
    assert status == 200 and headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert headers["X-Frame-Options"] == "DENY" and headers["Content-Type"] == "application/pdf"


def test_download_is_attachment_and_sandboxed_but_other_bytes_callers_are_unchanged(world):
    base, _ = world
    status, body, headers = get(base, "/api/v1/artifacts/h1/download")
    assert status == 200 and body == HOSTILE.encode()
    assert headers["Content-Disposition"].startswith("attachment;") and "page.html" in headers["Content-Disposition"]
    assert headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    # /download is the only _bytes caller; the headers parameter is optional so any other caller sends what it did before
    from herdr_engineering.control_plane import api as api_module
    with open(api_module.__file__, encoding="utf-8") as handle:
        assert handle.read().count("self._bytes(") == 1
    status, _, headers = get(base, "/api/v1/artifacts/i1/download")
    assert status == 200 and headers["Content-Disposition"].startswith("attachment;")


def test_report_preview_text_sends_frame_ancestors_none(world):
    base, _ = world
    status, _, headers = get(base, "/api/v1/artifacts/h1/versions/v1/preview")
    assert status == 200 and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" not in headers["Content-Security-Policy"]


def test_busy_maps_to_a_fixed_503(world, monkeypatch):
    base, _ = world

    def busy(*a, **k):
        raise share_render.RenderBusy("lanes secret detail")
    monkeypatch.setattr("herdr_engineering.control_plane.artifact_render_api.sanitize_html", busy)
    status, body, _ = envelope(base, "/api/v1/artifacts/h1/render")
    assert status == 503 and body == UNAVAILABLE


@pytest.mark.parametrize("error", [share_render.RenderTooLarge, share_render.RenderFailed, share_render.RenderTimeout])
def test_other_render_errors_are_one_uniform_refusal(world, monkeypatch, error):
    base, _ = world

    def boom(*a, **k):
        raise error("child said /secret/path")
    monkeypatch.setattr("herdr_engineering.control_plane.artifact_render_api.sanitize_html", boom)
    for path in ROUTES[:3]:
        status, body, _ = envelope(base, path)
        assert status == 422 and body == REFUSED


def test_dead_store_connection_is_a_fixed_503_without_driver_text(world):
    base, store = world
    store.dead = True
    for path in ROUTES:
        status, body, headers = get(base, path)
        assert status == 503, path
        assert json.loads(body) == UNAVAILABLE and b"secret_db_user" not in body and b"password" not in body


STYLE_PAYLOADS = [
    b"<p>keep</p><style>@import 'http://192.168.1.1/reboot'; p{color:red}</style>",
    b"<p>keep</p><style>p{background:url(http://127.0.0.1:9/x)}</style>",
    b"<p>keep</p><style>p{background:u\\72l(http://127.0.0.1:9/x)}</style>",
    b"<p>keep</p><STYLE type=text/css>@\\69mport url(//evil.example/a.css)</STYLE >",
    b"<p>keep</p><svg><style>p{background:url(http://127.0.0.1:9/x)}</style></svg>",
    b"<p>keep</p><style>a{}</style><style>b{background:url(http://127.0.0.1:9/x)}",
]


@pytest.mark.parametrize("payload", STYLE_PAYLOADS)
def test_dashboard_render_drops_every_style_element(world, payload):
    """No dashboard CSP exists, so a style body (url(), @import, escapes) would let the author make the owner's browser
    fetch LAN or loopback URLs. The dashboard path keeps no style at all."""
    base, store = world
    store.add("s1", "s.html", "text/html", payload)
    store.acl.grant("s1", f"api-key:{OWNER}:operator", {"read"})
    for path in ("/api/v1/artifacts/s1/render",):
        status, body, _ = envelope(base, path)
        assert status == 200
        html = body["html"].lower()
        assert "keep" in html
        for bad in ("<style", "</style", "@import", "url(", "\\72", "\\69", "127.0.0.1", "192.168", "evil.example"):
            assert bad not in html, (bad, html)


@pytest.mark.parametrize("mime", ["text/html\r\nX-Evil: 1\r\nContent-Security-Policy: none", "text/plain\nx", "a/b\x00"])
def test_stored_media_type_with_control_characters_never_reaches_a_header(world, mime):
    """Response splitting through a stored mime (``?mime=`` upload): /live and /download fall back to octet-stream."""
    base, store = world
    store.add("m1", "evil.html", mime, b"<p>x</p>")
    store.acl.grant("m1", f"api-key:{OWNER}:operator", {"read"})
    for path in ("/api/v1/artifacts/m1/live", "/api/v1/artifacts/m1/download"):
        status, _, headers = get(base, path)
        assert status == 200 and headers["Content-Type"].startswith("application/octet-stream"), path
        assert headers.get("X-Evil") is None
        assert headers.get("Content-Security-Policy", "none") != "none"


def test_mime_upload_query_with_control_characters_is_refused(world):
    base, _ = world
    req = urllib.request.Request(base + "/api/v1/artifacts?filename=a.txt&mime=text%2Fhtml%0D%0AX-Evil%3A1", data=b"x",
                                 method="POST", headers={"X-API-Key": "owner-key"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req)
    assert caught.value.code == 400


# --- X1: a stored media type's parameters are never sent -------------------------------------------------------
HOSTILE_MIMES = [
    "application/pdf;a=1,text/html", "application/pdf, text/html", "application/pdf;,text/html",
    'application/pdf;a="x,text/html', "application/pdf;a=\\,text/html", "application/pdf,text/html",
    "application/pdf;a=1", "APPLICATION/PDF;a=1,TEXT/HTML", "text/html\r\nX-Evil: 1", "text/html\nx", "", "   ",
    "text/html;charset=utf-8,application/pdf", "application/pdf /html", "pdf", "application/", "/pdf",
]


@pytest.mark.parametrize("mime", HOSTILE_MIMES)
def test_live_never_sends_stored_parameters(world, mime):
    """Every variant at send time: the type is a bare token pair; only a literal application/pdf escapes the sandbox."""
    from herdr_engineering.control_plane.artifact_render_api import _live_headers
    headers = dict(_live_headers(mime))
    content_type = headers["Content-Type"]
    assert not any(c in content_type for c in ',"\\\r\n') and content_type.count("/") == 1
    if content_type == "application/pdf":
        assert headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert mime.split(";")[0].strip().lower() == "application/pdf" and "," not in mime.split(";")[0]
    else:
        assert headers["Content-Security-Policy"].startswith("sandbox allow-scripts allow-downloads")


@pytest.mark.parametrize("mime", HOSTILE_MIMES)
def test_live_and_download_over_http_never_send_stored_parameters(world, mime):
    base, store = world
    store.add("x1", "evil.pdf", mime, b"<script>1</script>")
    store.acl.grant("x1", f"api-key:{OWNER}:operator", {"read"})
    for path in ("/api/v1/artifacts/x1/live", "/api/v1/artifacts/x1/download"):
        status, _, headers = get(base, path)
        assert status == 200, path
        content_type = headers["Content-Type"]
        assert content_type.split(";")[0] == content_type.split(";")[0].strip().lower()
        assert not any(c in content_type for c in ',"\\') and content_type.count("/") == 1, (path, content_type)
        assert headers.get("X-Evil") is None
    live = get(base, "/api/v1/artifacts/x1/live")[2]
    if live["Content-Type"] != "application/pdf":
        assert live["Content-Security-Policy"].startswith("sandbox")


def test_a_bare_uppercase_or_spaced_pdf_type_is_still_a_pdf(world):
    from herdr_engineering.control_plane.artifact_render_api import _live_headers
    for mime in ("application/pdf", "Application/PDF", " application/pdf ; a=1"):
        assert dict(_live_headers(mime))["Content-Type"] == "application/pdf"
    assert dict(_live_headers("text/plain"))["Content-Type"] == "text/plain; charset=utf-8"
    assert dict(_live_headers("text/plain; charset=latin-1"))["Content-Type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize("mime", ["application/pdf;a=1,text/html", "application/pdf, text/html", 'text/html;a="b',
                                  "text/html;a=\\b", "text/html\r\nX: 1", "nonsense", "", "a/b/c", "text /html"])
def test_upload_query_refuses_hostile_media_types(world, mime):
    from urllib.parse import quote
    base, _ = world
    req = urllib.request.Request(base + "/api/v1/artifacts?filename=a.pdf&mime=" + quote(mime, safe=""), data=b"x",
                                 method="POST", headers={"X-API-Key": "owner-key", "Content-Type": "application/pdf"})
    if mime == "":      # an empty ?mime= falls back to the request Content-Type, which is valid here
        return
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req)
    assert caught.value.code == 400


def test_upload_content_type_header_with_a_comma_is_refused_and_plain_parameters_are_dropped(world):
    base, _ = world
    bad = urllib.request.Request(base + "/api/v1/artifacts?filename=a.pdf", data=b"x", method="POST", headers={
        "X-API-Key": "owner-key", "Content-Type": "application/pdf;a=1,text/html"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(bad)
    assert caught.value.code == 400


def test_plain_parameters_are_dropped_by_the_shared_upload_validator():
    from herdr_engineering.media_type import canonical_upload_media_type
    assert canonical_upload_media_type("Text/Plain; charset=utf-8") == "text/plain"
    assert canonical_upload_media_type("application/pdf") == "application/pdf"


@pytest.mark.parametrize("mime", ["application/pdf;a=1,text/html", 'x/y;a="b', "x/y;a=\\", "x/y,z/w", "x y/z", "", "x/y\r\n"])
def test_the_one_shared_upload_validator(mime):
    from herdr_engineering.media_type import canonical_upload_media_type
    assert canonical_upload_media_type(mime) is None
    assert canonical_upload_media_type(None) is None and canonical_upload_media_type("a/" + "b" * 300) is None


@pytest.mark.parametrize("unit", ["<style>", "<style ", "<style></style", "<style x='>", "</style "])
def test_drop_style_elements_is_linear_on_raw_text(unit):
    import time

    from herdr_engineering.control_plane.artifact_render_api import drop_style_elements
    text = unit * (400_000 // len(unit))
    started = time.monotonic()
    out = drop_style_elements(text)
    assert time.monotonic() - started < 3.0
    assert "<style" not in out.lower().replace("&lt;style", "")


def test_drop_style_elements_still_cuts_balanced_elements_and_defangs_leftovers():
    from herdr_engineering.control_plane.artifact_render_api import drop_style_elements
    assert drop_style_elements("a<style>b{}</style>c<STYLE x>d</style >e") == "ace"
    assert drop_style_elements("a<style>b") == "a&lt;style>b"
