"""The report Rich view route over real HTTP: the author's HTML asset under a no-script sandbox CSP."""
import hashlib

import pytest

from herdr_engineering.control_plane import artifact_rich_view as rich
from tests.control_plane.test_artifact_render_api import get, world  # noqa: F401  (fixture)

PAGE = (b"<!doctype html><html><body><h1 id=t>Pretty report</h1><script>window.top.document.title='x'</script>"
        b"<img src=https://evil.example/p.gif><style>h1{color:#c00}</style></body></html>")
URL = "/api/v1/artifacts/h1/versions/v1/rich"


def descriptor(*assets):
    return {"schema_version": 1, "title": "R", "summary": "S", "questions": [], "evidence": [], "findings": [],
            "figures": [], "assets": list(assets)}


def asset(store, name, data, mime="text/html", role="preview", aid=None):
    meta = store.blobs.add(data)
    return {"id": aid or name, "name": name, "role": role, "media_type": mime, "sha256": meta["sha256"],
            "size_bytes": len(data)}


@pytest.fixture
def rich_world(world):  # noqa: F811
    base, store = world
    store.descriptors[("h1", "v1")] = descriptor(asset(store, "report.html", PAGE),
                                                 asset(store, "d.csv", b"a,b", "text/csv", "data"))
    return base, store


def test_serves_the_html_asset_with_exact_headers(rich_world):
    base, _ = rich_world
    status, body, headers = get(base, URL)
    assert status == 200 and body == PAGE
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Security-Policy"] == rich.RICH_CSP == (
        "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "private, no-store"


def test_csp_never_grants_scripts_or_origin(rich_world):
    base, _ = rich_world
    csp = get(base, URL)[2]["Content-Security-Policy"]
    assert "allow-" not in csp and "script-src" not in csp
    assert csp.startswith("sandbox;")           # the bare directive: every capability off


def test_hostile_html_gets_the_same_sandbox_csp(rich_world):
    base, store = rich_world
    hostile = b"<script>fetch('/api/v1/artifacts')</script><body onload=alert(1)><form action=/x><button>go"
    store.descriptors[("h1", "v1")] = descriptor(asset(store, "evil.html", hostile))
    status, body, headers = get(base, URL)
    assert status == 200 and body == hostile
    assert headers["Content-Security-Policy"] == rich.RICH_CSP


def test_live_is_still_not_frameable(rich_world):
    base, _ = rich_world
    status, _, headers = get(base, "/api/v1/artifacts/h1/live")
    assert status == 200 and headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_no_html_asset_is_404(rich_world):
    base, store = rich_world
    store.descriptors[("h1", "v1")] = descriptor(asset(store, "d.csv", b"a,b", "text/csv", "data"))
    assert get(base, URL)[0] == 404


def test_ambiguous_html_assets_are_404_unless_one_is_the_preview(rich_world):
    base, store = rich_world
    one = asset(store, "a.html", b"<p>a", role="source")
    two = asset(store, "b.html", b"<p>b", role="data")
    store.descriptors[("h1", "v1")] = descriptor(one, two)
    assert get(base, URL)[0] == 404
    two["role"] = "preview"
    status, body, _ = get(base, URL)
    assert status == 200 and body == b"<p>b"


def test_unknown_version_and_non_report_artifact_are_404(rich_world):
    base, _ = rich_world
    assert get(base, "/api/v1/artifacts/h1/versions/zzz/rich")[0] == 404
    assert get(base, "/api/v1/artifacts/t1/versions/v1/rich")[0] == 404      # t1 has no versions at all
    assert get(base, "/api/v1/artifacts/nope/versions/v1/rich")[0] == 404


def test_other_principal_cannot_read(rich_world):
    base, _ = rich_world
    assert get(base, URL, key="other-key")[0] == 404
    assert get(base, URL, key="wrong")[0] in (401, 403)


def test_oversize_is_refused(rich_world, monkeypatch):
    base, store = rich_world
    monkeypatch.setattr(rich, "MAX_RICH_BYTES", 64)
    store.descriptors[("h1", "v1")] = descriptor(asset(store, "big.html", b"<p>" + b"a" * 100))
    assert get(base, URL)[0] == 413


def test_lying_descriptor_size_cannot_bypass_the_cap(rich_world, monkeypatch):
    base, store = rich_world
    monkeypatch.setattr(rich, "MAX_RICH_BYTES", 64)
    big = asset(store, "big.html", b"<p>" + b"a" * 100)
    big["size_bytes"] = 10
    store.descriptors[("h1", "v1")] = descriptor(big)
    assert get(base, URL)[0] == 413


def test_corrupt_blob_is_not_delivered(rich_world):
    base, store = rich_world
    page = asset(store, "x.html", b"<p>ok")
    page["sha256"] = hashlib.sha256(b"other").hexdigest()
    store.blobs.objects_by_sha[page["sha256"]] = b"<p>ok"
    store.blobs.objects[f"sha256/{page['sha256'][:2]}/{page['sha256'][2:4]}/{page['sha256']}"] = b"<p>ok"
    store.descriptors[("h1", "v1")] = descriptor(page)
    status, body, _ = get(base, URL)
    assert status == 503 and b"ok" not in body
