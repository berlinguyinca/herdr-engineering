import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def _start(**kw):
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer", "write": "operator"}, **kw)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    return server


def _post(server, body, key="write"):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/fabric/resolve-urls",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def server():
    calls = []

    def resolver(urls, machine):
        calls.append((urls, machine))
        return {"resolutions": [{"original": u, "canonical": u.replace("localhost", "dev.lan"), "status": "resolved"}
                                for u in urls]}
    s = _start(fabric_resolver=resolver)
    s.calls = calls
    yield s
    s.shutdown()


def test_resolves_through_gateway(server):
    code, out = _post(server, {"urls": ["http://localhost:3188/x"], "source_machine": "bender"})
    assert code == 200 and out["resolutions"][0]["canonical"] == "http://dev.lan:3188/x"
    assert server.calls == [(["http://localhost:3188/x"], "bender")]


def test_bad_bodies_rejected(server):
    assert _post(server, {"urls": "nope"})[0] == 400
    assert _post(server, {"urls": ["x"] * 201})[0] == 400
    assert _post(server, {"urls": ["http://a/"], "source_machine": 5})[0] == 400


def test_viewer_cannot_post(server):
    assert _post(server, {"urls": []}, key="read")[0] == 403


def test_no_gateway_or_failure_degrades_to_unavailable():
    s = _start()
    try:
        code, out = _post(s, {"urls": ["http://localhost:1/"]})
        assert code == 200 and out["resolutions"][0]["status"] == "unavailable" and out["resolutions"][0]["canonical"] is None
    finally:
        s.shutdown()

    def boom(urls, machine):
        raise RuntimeError("down")
    s = _start(fabric_resolver=boom)
    try:
        assert _post(s, {"urls": ["http://localhost:1/"]})[1]["resolutions"][0]["status"] == "unavailable"
    finally:
        s.shutdown()


def _get(server, path, key="read"):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/{path}", headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status, json.loads(r.read())


def test_fabric_services_lists_only_display_fields():
    s = _start(fabric_services=lambda: {"leases": [{"id": "l1", "label": "web", "external_port": 1, "secret": "x",
                                                    "target": {"host": "127.0.0.1"}}]})
    try:
        code, out = _get(s, "fabric/services")
        assert code == 200 and out["available"] and out["leases"][0]["label"] == "web" and "secret" not in out["leases"][0]
    finally:
        s.shutdown()


def test_fabric_services_unavailable_without_gateway():
    s = _start()
    try:
        assert _get(s, "fabric/services")[1] == {"leases": [], "available": False}
    finally:
        s.shutdown()
