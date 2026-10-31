"""The vendored pdf.js: pinned, hash-checked, packaged, and served by both servers (dashboard and share-public)."""
import hashlib
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from herdr_engineering.control_plane import share_public
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab import Api
from tests.control_plane.test_share_lab import LAB_ALLOW, lab, lab_config, reference  # noqa: F401
from tests.control_plane.test_share_public import Env

ROOT = Path(__file__).resolve().parents[2]
VENDOR = Path(share_public.__file__).parent / "share_static" / "vendor" / "pdfjs"
MAIN_FILES = ("pdf.min.mjs", "pdf.worker.min.mjs")
EXPECTED = {*MAIN_FILES, "LICENSE", "SHA256SUMS"}
SHA_LINE_PARTS = 2


def _etag(name):
    return f'"{_sums()[name]}"'          # the strong ETag is the SHA256SUMS digest


def _sums():
    out = {}
    for line in (VENDOR / "SHA256SUMS").read_text().splitlines():
        parts = line.split()
        assert len(parts) == SHA_LINE_PARTS, line
        out[parts[1]] = parts[0]
    return out


def test_every_vendored_file_matches_sha256sums_and_nothing_else_is_there():
    assert {p.name for p in VENDOR.iterdir()} == EXPECTED
    sums = _sums()
    assert set(sums) == EXPECTED - {"SHA256SUMS"}
    for name, digest in sums.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_licence_is_apache_2_and_the_script_pins_the_release():
    text = (VENDOR / "LICENSE").read_text()
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    script = (ROOT / "scripts" / "vendor-pdfjs.sh").read_text()
    assert "PDFJS=4.10.38" in script and "--ignore-scripts" in script and "dist.integrity" in script
    assert "pdfjs-dist 4.10.38" in (ROOT / "THIRD_PARTY_NOTICES.md").read_text()


def test_wheel_ships_the_vendored_files(tmp_path):
    if subprocess.run(["python3", "-m", "pip", "--version"], capture_output=True).returncode:
        pytest.skip("no system pip to build a wheel with")
    out, site = tmp_path / "whl", tmp_path / "site"
    subprocess.run(["python3", "-m", "pip", "wheel", str(ROOT), "--no-deps", "-q", "-w", str(out)],
                   check=True, timeout=300, capture_output=True)
    wheel = next(out.glob("*.whl"))
    subprocess.run(["python3", "-m", "pip", "install", "--no-deps", "--no-compile", "-q", "--target", str(site),
                    str(wheel)], check=True, timeout=300, capture_output=True)
    code = ("import importlib.resources as r;"
            "d=r.files('herdr_engineering.control_plane').joinpath('share_static/vendor/pdfjs');"
            "print(' '.join(sorted(p.name for p in d.iterdir())))")
    got = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True,
                         env={"PYTHONPATH": str(site), "PATH": "/usr/bin"}, cwd=str(tmp_path))
    assert set(got.stdout.split()) == EXPECTED


@pytest.fixture
def dashboard(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "app.js").write_text("// app")
    (web / "index.html").write_text("<p>x</p>")
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, web_root=web, allow_open=True)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    api = Api(srv.port)
    api.web = web
    yield api
    srv.shutdown()
    t.join(timeout=5)


def _raw_get(port, path, headers=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("GET", path, skip_accept_encoding=True)  # putrequest keeps the path byte for byte
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    conn.endheaders()
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body


@pytest.mark.parametrize("name", MAIN_FILES)
def test_dashboard_serves_vendored_modules_as_javascript(dashboard, name):
    status, h, body = _raw_get(dashboard.port, f"/vendor/pdfjs/{name}")
    assert status == 200 and body == (VENDOR / name).read_bytes()
    assert h["content-type"].startswith("text/javascript") and h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-cache" and h["etag"] == _etag(name)
    assert "content-security-policy" not in h          # the dashboard has no CSP (Task 13)


@pytest.mark.parametrize("name", MAIN_FILES)
def test_dashboard_vendored_modules_revalidate_with_if_none_match(dashboard, name):
    path, tag = f"/vendor/pdfjs/{name}", _etag(name)
    status, h, body = _raw_get(dashboard.port, path, {"If-None-Match": tag})
    assert status == 304 and body == b"" and h["etag"] == tag and h["cache-control"] == "no-cache"
    assert h["x-content-type-options"] == "nosniff"
    for header in (f"W/{tag}", f'"other", {tag}', "*"):
        status, _, body = _raw_get(dashboard.port, path, {"If-None-Match": header})
        assert status == 304 and body == b"", header
    status, h, body = _raw_get(dashboard.port, path, {"If-None-Match": '"0000"'})
    assert status == 200 and body == (VENDOR / name).read_bytes() and h["etag"] == tag


def test_dashboard_static_files_get_nosniff_and_no_store(dashboard):
    status, h, _ = _raw_get(dashboard.port, "/app.js")
    assert status == 200 and h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"
    assert "etag" not in h


def test_dashboard_serves_mjs_as_javascript(dashboard):
    (dashboard.web / "m.mjs").write_text("export const a = 1;")
    status, h, _ = _raw_get(dashboard.port, "/m.mjs")
    assert status == 200 and h["content-type"] == "text/javascript" and h["x-content-type-options"] == "nosniff"


def test_dashboard_vendor_allow_list_is_exact(dashboard):
    for path in ("/vendor/pdfjs/../x", "/vendor/pdfjs/../app.js"):
        assert _raw_get(dashboard.port, path)[0] == 403, path        # the existing ".." rule
    not_found = ("/vendor/pdfjs/%2e%2e/app.js", "/vendor/pdfjs/LICENSE", "/vendor/pdfjs/SHA256SUMS", "/vendor/pdfjs/",
                 "/vendor/pdfjs/nope.mjs", "/vendor/pdfjs/pdf.min.mjs/x", "/vendor/pdfjs/Pdf.min.mjs")
    for path in not_found:
        assert _raw_get(dashboard.port, path)[0] == 404, path


@pytest.mark.parametrize("name", MAIN_FILES)
def test_share_public_serves_vendored_modules(lab, name):  # noqa: F811
    status, h, body = lab.request("GET", f"/s/static/vendor/pdfjs/{name}")
    assert status == 200 and body == (VENDOR / name).read_bytes()
    assert h["content-type"].startswith("text/javascript") and h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-cache" and h["etag"] == _etag(name)
    assert h["content-security-policy"]


@pytest.mark.parametrize("name", MAIN_FILES)
def test_share_public_vendored_modules_revalidate_with_if_none_match(lab, name):  # noqa: F811
    path, tag = f"/s/static/vendor/pdfjs/{name}", _etag(name)
    status, h, body = lab.request("GET", path, {"If-None-Match": tag})
    assert status == 304 and body == b"" and h["etag"] == tag and h["cache-control"] == "no-cache"
    assert h["x-content-type-options"] == "nosniff" and h["content-security-policy"]
    for header in (f"W/{tag}", f'"other", {tag}', "*"):
        assert lab.request("GET", path, {"If-None-Match": header})[0] == 304, header
    status, h, body = lab.request("GET", path, {"If-None-Match": '"0000"'})
    assert status == 200 and body == (VENDOR / name).read_bytes() and h["etag"] == tag


def test_share_public_vendor_allow_list_is_exact(lab):  # noqa: F811
    ref = reference(lab)
    for path in ("/s/static/vendor/pdfjs/../share.js", "/s/static/vendor/pdfjs/%2e%2e/share.js",
                 "/s/static/vendor/pdfjs/LICENSE", "/s/static/vendor/pdfjs/SHA256SUMS", "/s/static/vendor/pdfjs/",
                 "/s/static/vendor/pdfjs", "/s/static/vendor/pdfjs/pdf.min.mjs/", "/s/static/vendor/other/pdf.min.mjs",
                 "/s/static/vendor/pdfjs/sub/pdf.min.mjs", "/s/static/vendor/pdfjs/Pdf.min.mjs", "/s/static/vendor"):
        status, _, body = lab.request("GET", path)
        assert status == 404 and body == ref, path
    for method in ("POST", "PUT", "OPTIONS"):
        status, _, body = lab.request(method, "/s/static/vendor/pdfjs/pdf.min.mjs")
        assert status == 404 and body == ref, method


def test_share_public_refuses_an_oversized_vendor_file(tmp_path):
    static = tmp_path / "static-big"
    shutil.copytree(share_public.DEFAULT_STATIC_DIR, static)
    (static / "vendor" / "pdfjs" / "pdf.min.mjs").write_bytes(b"x" * (share_public.VENDOR_MAX_BYTES + 1))
    assert share_public.VENDOR_MAX_BYTES == 4 * 1024 * 1024 and share_public.STATIC_MAX_BYTES == 1024 * 1024
    e = Env(tmp_path, audience="lab", expires_in=None, config_extra=lab_config(tmp_path, static_dir=static),
            state_extra=LAB_ALLOW)
    try:
        ref = reference(e)
        status, _, body = e.request("GET", "/s/static/vendor/pdfjs/pdf.min.mjs")
        assert status == 404 and body == ref
        status, _, _ = e.request("GET", "/s/static/vendor/pdfjs/pdf.worker.min.mjs")
        assert status == 200                      # larger than STATIC_MAX_BYTES, within VENDOR_MAX_BYTES
    finally:
        e.close()
