"""No frames anywhere, the dashboard included (operator 2026-10-30, Decision 1).

Three layers:

* (a) a source scan of every shipped script, page, style sheet and Python module,
* (b) every rendered response a browser can be handed (dashboard, share host, owner render routes), and
* (c) the sanitiser on a corpus of frame and plugin markup.

Product code and test fixtures name frame elements in prose without angle brackets, or build the hostile markup by
concatenation, so this scan stays quiet. A genuine exception must be a named ``ALLOWED`` entry with its reason.
Excluded by design: ``node_modules``, ``*.test.js`` (they assert the absence of frames), ``share_static/vendor/``
(pdf.js calls ``postMessage`` for its own worker, which Decision 1 allows; (b) skips ``/vendor/`` paths for the same
reason) and the raw ``f/`` and owner ``live`` routes, which serve an artifact's own bytes.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from herdr_engineering.control_plane import api as api_module
from herdr_engineering.control_plane import share_public, share_render
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane import test_artifact_render_api as owner
from tests.control_plane import test_share_annotate as sa
from tests.control_plane.test_share_lab import STATIC_DIR

ROOT = Path(__file__).resolve().parents[2]
TAG = re.compile(r"<\s*(iframe|frame|frameset|object|embed|portal|fencedframe)(?=[\s/>])", re.I)
API = re.compile(r"createElement\(\s*['\"`](iframe|frame|object|embed)|\.srcdoc\b|\bsrcdoc=|postMessage\(", re.I)
ALLOWED: dict[str, str] = {}      # "relative/path" -> reason; a new entry needs a reason a reviewer can check
SCANNED_SUFFIXES = {".js", ".mjs", ".html", ".css"}


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted((ROOT / "packages").rglob("*")):
        parts = path.relative_to(ROOT).parts
        if (path.is_file() and path.suffix in SCANNED_SUFFIXES and "node_modules" not in parts
                and not path.name.endswith(".test.js")):
            files.append(path)
    static = ROOT / "herdr_engineering/control_plane/share_static"
    files += [p for p in sorted(static.rglob("*")) if p.is_file() and "vendor" not in p.relative_to(static).parts
              and p.suffix in SCANNED_SUFFIXES]
    files += sorted((ROOT / "herdr_engineering").rglob("*.py"))
    return [p for p in dict.fromkeys(files)]


def test_the_scan_sees_the_files_it_is_supposed_to_see():
    names = {p.relative_to(ROOT).as_posix() for p in _scanned_files()}
    for expected in ("packages/artifact-share-panel.js", "packages/artifact-feedback-panel.js",
                     "herdr_engineering/control_plane/share_static/annotate.js",
                     "herdr_engineering/control_plane/share_static/view.html",
                     "herdr_engineering/control_plane/share_render.py",
                     "herdr_engineering/control_plane/intel_attachments.py"):
        assert expected in names, expected
    assert not any("/vendor/" in n or "node_modules" in n or n.endswith(".test.js") for n in names)


def test_a_source_scan_finds_no_frame_markup_or_frame_apis():
    hits = []
    for path in _scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (TAG, API):
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{rel}:{line}: {match.group(0)!r}")
    assert not hits, "frames or frame APIs in shipped sources:\n" + "\n".join(hits)


def test_the_allow_list_only_names_files_that_exist_and_give_a_reason():
    for rel, reason in ALLOWED.items():
        assert (ROOT / rel).is_file() and reason.strip(), rel


def test_the_detectors_do_match_what_they_are_for():
    for sample in ("<" + "iframe src=x>", "<" + "IFRAME src=x>", "<" + "iframe/src=x>", "<" + "frameset>", "<" + " object data=x>",
                   "<" + "embed src=x>", "<" + "portal src=x>", "<" + "fencedframe>"):
        assert TAG.search(sample), sample
    for sample in ("document.createElement('" + "iframe')", 'createElement("' + 'object")', "el." + "srcdoc = x",
                   "<div " + 'srcdoc="x">', "parent." + "postMessage(1, '*')"):
        assert API.search(sample), sample
    # prose without angle brackets, and the detector byte string in intel_attachments.py, are not tags
    for sample in ("an " + "iframe in prose", "<frames-are-not-tags>", 'b"<' + 'iframe"', "a < iframeless"):
        assert not TAG.search(sample), sample


# ------------------------------------------------------------------------------------------------ (b) rendered responses

def _frame_free(label: str, body: bytes) -> None:
    text = body.decode("utf-8", errors="replace").lower()
    assert not TAG.search(text), f"{label}: frame markup in a rendered response"
    assert not API.search(text), f"{label}: frame API in a rendered response"


def test_the_mode_switch_template_carries_neither_markup_nor_api():
    template = share_public.MODE_SWITCH_TEMPLATE
    text = template.decode() if isinstance(template, bytes) else template
    assert not TAG.search(text.lower()) and not API.search(text)


def _dashboard():
    server = api_module.ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, web_root=ROOT / "packages")
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    return server


def _fetch(base, path):
    try:
        with urllib.request.urlopen(base + path) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_dashboard_index_public_pages_and_static_assets_are_frame_free():
    server = _dashboard()
    try:
        base = f"http://127.0.0.1:{server.port}"
        status, body = _fetch(base, "/index.html")
        assert status == 200 and b"<html" in body.lower()
        _frame_free("/index.html", body)
        public = 0
        for path in sorted(api_module._PUBLIC_STATIC):
            status, body = _fetch(base, path)
            assert status in (200, 404), path        # a page whose file this checkout does not ship answers 404
            _frame_free(path, body)
            public += status == 200
        assert public > 20
        served = 0
        for path in _scanned_files():
            rel = path.relative_to(ROOT / "packages").as_posix() if path.is_relative_to(ROOT / "packages") else None
            if rel is None:
                continue
            status, body = _fetch(base, "/" + rel)
            if status == 200:
                _frame_free("/" + rel, body)
                served += 1
        assert served > 50
        # The vendored pdf.js files are the one documented exemption: served from /vendor/ paths, they call postMessage
        # for pdf.js's own worker, which Decision 1 allows. (a) skips share_static/vendor/ for the same reason.
        for rel in api_module._VENDORED_PDFJS:
            assert rel.startswith("vendor/"), rel
            assert _fetch(base, "/" + rel)[0] == 200
    finally:
        server.shutdown()


FRAMES = {
    "index.html": ("<html><body><p>top</p>" + "<" + "iframe src=https://evil.example/x></" + "iframe>"
                   + "<" + "frameset><" + "frame src=a></" + "frameset>" + "<" + "object data=x></" + "object>"
                   + "<" + "embed src=y>" + "<" + "portal src=z></" + "portal>" + "<" + "IFRAME/src=q>"
                   + "<svg><foreignObject><" + "iframe src=w></" + "iframe></foreignObject></svg>"
                   + "<" + "fencedframe></" + "fencedframe></body></html>").encode(),
    "notes.txt": ("plain " + "<" + "iframe src=a></" + "iframe> text").encode(),
    "pic.png": b"\x89PNG\r\n\x1a\n" + bytes(range(64)),
    "doc.pdf": b"%PDF-1.4\n%fake\n",
    "data/t.parquet": bytes(range(256)) * 4,
}
KINDS = {"index.html": "html", "notes.txt": "text", "pic.png": "image", "doc.pdf": "pdf", "data/t.parquet": "document"}


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    env = sa.make_env(tmp_path_factory.mktemp("noframes"), files=FRAMES)
    yield env
    env.close()


def test_share_host_pages_views_assets_and_annotations_are_frame_free(lab):
    sess = sa.session()
    pages = [f"/s/{sa.TOKEN}/", "/s/callback", "/s/callback?code=abc-123&state=xyz_9"]
    pages += [f"/s/static/{name}" for name in sorted(share_public.STATIC_FILES)]   # the served assets; /vendor/ is exempt
    for path in pages:
        status, _, body = lab.request("GET", path)
        assert status == 200, path
        _frame_free(path, body)
    for name, kind in KINDS.items():
        quoted = urllib.parse.quote(name)
        for letter in ("v", "a"):
            status, _, body = lab.request("GET", sa.base(sess, letter) + quoted)
            # an HTML entry has no v/ page: a lab link lands on the running page f/ (Decision 9), so v/ answers 404 for it
            assert status == (404 if (letter, kind) == ("v", "html") else 200), (letter, name)
            _frame_free(f"{letter}/{name} ({kind})", body)
    # the HTML bundle is full of frames, so the annotation view of it must actually have been sanitised, not skipped
    status, _, body = lab.request("GET", sa.base(sess, "a") + "index.html")
    assert status == 200 and b"top" in body
    # the annotations JSON, with a row whose text names frames: the host escapes "<", so the body is still markup-free
    comment = "see " + "<" + "iframe src=x></" + "iframe> and " + "<" + "object data=y>"
    sa.seed(lab, "alice", [sa.row(sa.AID, body=comment)])
    status, _, body = lab.request("GET", sa.api(sess))
    assert status == 200 and json.loads(body)["annotations"][0]["body"] == comment      # same value for every client
    _frame_free("annotations", body)


def test_vendored_assets_are_the_documented_exemption():
    vendor = STATIC_DIR / "vendor"
    assert vendor.is_dir()
    # (a) and (b) skip /vendor/ exactly: pdf.js posts messages to its own worker, which Decision 1 allows.
    pdfjs = [p for p in vendor.rglob("*.mjs")]
    assert pdfjs and any(API.search(p.read_text(errors="replace")) for p in pdfjs)


def _get(base, path, key="owner-key"):
    return owner.get(base, path, key)


def test_owner_render_routes_and_report_preview_are_frame_free(owner_world):
    base = owner_world
    store_paths = ["/api/v1/artifacts/h1/render", "/api/v1/artifacts/t1/render", "/api/v1/artifacts/p1/render",
                   "/api/v1/artifacts/i1/render", "/api/v1/artifacts/h1/versions/v1/outputs/0/render",
                   "/api/v1/artifacts/h1/versions/v1/preview/render"]
    for path in store_paths:
        status, body, _ = _get(base, path)
        assert status == 200, path
        _frame_free(path, body)
        if json.loads(body).get("html"):
            assert "<" in json.loads(body)["html"]      # real markup went through the check, not an empty answer


@pytest.fixture
def owner_world():
    gen = owner.world.__wrapped__() if hasattr(owner.world, "__wrapped__") else None
    assert gen is not None
    base, _ = next(gen)
    try:
        yield base
    finally:
        gen.close()


# ------------------------------------------------------------------------------------------------ (c) the sanitiser

CORPUS = [
    "<" + "IFRAME src=https://evil.example></" + "IFRAME>",
    "<" + "iframe/src=https://evil.example/x>",
    "<" + "frameset><" + "frame src=a></" + "frameset>",
    "<" + "object data=x><" + "embed src=y></" + "object>",
    "<svg><foreignObject><" + "iframe src=w></" + "iframe></foreignObject></svg>",
    "<" + "portal src=z></" + "portal>",
    "<" + "fencedframe></" + "fencedframe>",
    "<p>ok</p><" + "iframe srcdoc='<script>1</script>'></" + "iframe>",
    "<" + "IfRaMe\n src=x\n>",
]


@pytest.mark.parametrize("markup", CORPUS)
def test_the_sanitiser_removes_every_frame_and_plugin_element(markup):
    out = share_render.sanitize_html(markup.encode(), resolve=lambda context, url: None)
    text = out.decode() if isinstance(out, bytes) else out
    assert not TAG.search(text) and not API.search(text), text
