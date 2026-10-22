"""API-key role 'ingest' (Spec §104): read + POST /api/v1/ingest only.

An ingest key may call the ingest endpoint and nothing else that requires
the operator role; everywhere else it is treated as viewer.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def _get(base, path, key=None):
    headers = {"X-API-Key": key} if key else {}
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _post(base, path, body, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture()
def server():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, api_keys={
        "op-1": "operator",
        "view-1": "viewer",
        "ing-1": "ingest",
    })
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    yield f"http://127.0.0.1:{svr.port}", repo
    svr.shutdown()


def _ingest_body():
    return {"entity_type": "host", "entity_id": "h-ingest",
            "event_type": "host.registered",
            "payload": {"host_name": "h-ingest", "tailnet_ip": "10.0.0.9"}}


def test_ingest_key_can_post_ingest(server):
    base, repo = server
    status = _post(base, "/api/v1/ingest", _ingest_body(), key="ing-1")
    assert status in (200, 202)
    # the ingest actually landed in the repo
    assert len(repo.list_hosts()) == 1


def test_ingest_key_cannot_call_operator_only_post(server):
    base, repo = server
    sid = repo.create_session()
    status = _post(base, f"/api/v1/sessions/{sid}/messages",
                  {"message": "x"}, key="ing-1")
    assert status == 403


def test_viewer_cannot_ingest(server):
    base, _ = server
    status = _post(base, "/api/v1/ingest", _ingest_body(), key="view-1")
    assert status == 403


def test_ingest_key_is_viewer_elsewhere(server):
    base, repo = server
    # read access is fine (viewer-equivalent)
    assert _get(base, "/api/v1/health", key="ing-1") == 200
    sid = repo.create_session()
    assert _get(base, f"/api/v1/sessions/{sid}/messages", key="ing-1") == 200
    # but any other operator-only POST is forbidden
    status = _post(base, f"/api/v1/sessions/{sid}/messages",
                  {"message": "x"}, key="ing-1")
    assert status == 403


def test_operator_and_unknown_keys(server):
    base, _ = server
    # operator can still ingest
    assert _post(base, "/api/v1/ingest", _ingest_body(),
                key="op-1") in (200, 202)
    # unknown key is unauthorized, not read-only
    assert _post(base, "/api/v1/ingest", _ingest_body(),
                key="nope") == 401
    assert _get(base, "/api/v1/missions", key="nope") == 401


@pytest.fixture()
def fail_closed_server():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, api_keys={
        "blank-role": "",
        "none-role": None,
        "admin-role": "admin",
        "ing-1": "ingest",
    })
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    yield f"http://127.0.0.1:{svr.port}", repo
    svr.shutdown()


def test_blank_and_none_roles_are_read_only(fail_closed_server):
    base, repo = fail_closed_server
    for key in ("blank-role", "none-role"):
        # never defaulted to operator: ingest is forbidden
        assert _post(base, "/api/v1/ingest", _ingest_body(), key=key) == 403
        # and an operator-only route is forbidden too
        sid = repo.create_session()
        assert _post(base, f"/api/v1/sessions/{sid}/messages",
                    {"message": "x"}, key=key) == 403
        # but reads still work (viewer-equivalent)
        assert _get(base, "/api/v1/health", key=key) == 200


def test_unknown_role_admin_is_read_only(fail_closed_server):
    base, repo = fail_closed_server
    assert _post(base, "/api/v1/ingest", _ingest_body(),
                key="admin-role") == 403
    sid = repo.create_session()
    assert _post(base, f"/api/v1/sessions/{sid}/messages",
                {"message": "x"}, key="admin-role") == 403


def test_ingest_path_prefix_is_not_ingest(fail_closed_server):
    base, _ = fail_closed_server
    # a key with role 'ingest' must not be granted ingest rights on a
    # path that merely starts with /api/v1/ingest (exact check, not
    # startswith)
    assert _post(base, "/api/v1/ingest/x", _ingest_body(),
                key="ing-1") == 403
