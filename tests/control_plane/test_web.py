import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


@pytest.fixture()
def web():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    yield repo, base
    svr.shutdown()


def get(base, path):
    try:
        with urllib.request.urlopen(base + path) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_index_served(web):
    _, base = web
    status, headers, body = get(base, "/")
    assert status == 200
    assert headers["Content-Type"] == "text/html"
    assert b"herdr-app-shell" in body
    assert b"view" in body


def test_app_and_assets_served(web):
    _, base = web
    assert get(base, "/app.js")[0] == 200
    assert get(base, "/herdr-web-components/index.js")[0] == 200
    assert get(base, "/herdr-design-system/theme.css")[0] == 200
    assert get(base, "/herdr-web-client/client.js")[0] == 200


def test_path_traversal_blocked(web):
    _, base = web
    status, _, _ = get(base, "/../../etc/passwd")
    assert status == 403 or status == 404


def test_unknown_asset_404(web):
    _, base = web
    assert get(base, "/nope.js")[0] == 404


def test_api_and_static_together(web):
    repo, base = web
    repo.create_mission("web-mission")
    status, _, body = get(base, "/api/v1/missions")
    assert status == 200
    assert b"web-mission" in body
