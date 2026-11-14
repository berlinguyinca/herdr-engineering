"""An `agent` key may POST /api/v1/token-usage (usage reporters); a viewer key may not; an open CP still works."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

RECORD = {"project": "p", "model": "m", "request_id": "r-1", "input_tokens": 1, "output_tokens": 2}


def _post(base, key, body=RECORD):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(base + "/api/v1/token-usage", data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _serve(keys):
    svr = ControlPlaneServer(ControlPlaneRepo(), api_keys=keys)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr


@pytest.fixture()
def keyed():
    svr = _serve({"ag-1": "agent", "view-1": "viewer"})
    yield f"http://127.0.0.1:{svr.port}"
    svr.shutdown()


@pytest.fixture()
def keyless():
    svr = _serve({})
    yield f"http://127.0.0.1:{svr.port}"
    svr.shutdown()


def test_agent_key_may_post_token_usage(keyed):
    assert _post(keyed, "ag-1") in (200, 201, 202)


def test_viewer_key_may_not_post_token_usage(keyed):
    assert _post(keyed, "view-1") == 403


def test_missing_key_is_refused_when_keyed(keyed):
    assert _post(keyed, None) == 401


def test_open_control_plane_still_accepts_token_usage(keyless):
    assert _post(keyless, None) in (200, 201, 202)
