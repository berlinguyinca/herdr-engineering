"""Same-origin dashboard proxy: injects viewer/operator keys server-side, against a REAL keyed ControlPlaneServer."""
import http.client
import json
import os
import threading
import time

import pytest
import yaml

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.dashboard_proxy import (
    DEFAULT_OPERATOR_POST_ALLOWLIST,
    DashboardProxy,
    ProxyConfigError,
    parse_dashboard_proxy,
    read_key_file,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

VIEWER, OPERATOR = "viewer-test-key", "operator-test-key"


def call(port, method, path, *, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        data = json.dumps(body).encode() if body is not None else None
        hdrs = dict(headers or {})
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


@pytest.fixture
def cp():
    svr = ControlPlaneServer(ControlPlaneRepo(), api_keys={VIEWER: "viewer", OPERATOR: "operator"})
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    yield svr
    svr.shutdown()
    thread.join(timeout=3)


def start_proxy(cp_port, *, operator_key=OPERATOR, allowlist=DEFAULT_OPERATOR_POST_ALLOWLIST):
    px = DashboardProxy(f"http://127.0.0.1:{cp_port}", viewer_key=VIEWER, operator_key=operator_key,
                        allowlist=allowlist, host="127.0.0.1", port=0)
    thread = threading.Thread(target=px.serve, daemon=True)
    thread.start()
    px.wait_until_ready()
    return px, thread


@pytest.fixture
def proxy(cp):
    px, thread = start_proxy(cp.port)
    yield px
    px.shutdown()
    thread.join(timeout=3)


def test_keyless_direct_get_is_401_but_via_proxy_is_200(cp, proxy):
    assert call(cp.port, "GET", "/api/v1/sessions")[0] == 401
    status, body = call(proxy.port, "GET", "/api/v1/sessions")
    assert status == 200 and json.loads(body) is not None


def test_static_ui_is_served_through_the_proxy(cp, proxy):
    assert call(cp.port, "GET", "/")[0] == 401
    assert call(proxy.port, "GET", "/")[0] == 200


def test_query_string_is_forwarded(proxy):
    assert call(proxy.port, "GET", "/api/v1/sessions?limit=1")[0] == 200


def test_allowlisted_post_gets_the_operator_key(cp, proxy):
    path = "/api/v1/attention/ack"
    # The viewer key alone is refused by the control plane: proof the proxy used the operator key.
    assert call(cp.port, "POST", path, headers={"X-API-Key": VIEWER}, body={})[0] == 403
    status, _ = call(proxy.port, "POST", path, body={})
    assert status not in (401, 403)


def test_non_allowlisted_post_is_403_at_the_proxy(proxy):
    status, body = call(proxy.port, "POST", "/api/v1/ingest", body={})
    assert status == 403
    assert b"proxy" in body


def test_write_methods_not_listed_are_403(proxy):
    assert call(proxy.port, "DELETE", "/api/v1/sessions/x")[0] == 403
    assert call(proxy.port, "PUT", "/api/v1/sessions/x", body={})[0] == 403


@pytest.mark.parametrize("path", [
    "/api/v1/attention/../ingest", "/api/v1/attention/%2e%2e/ingest", "/api/v1/attention%2fack"])
def test_traversal_in_a_mutating_path_is_403(proxy, path):
    assert call(proxy.port, "POST", path, body={})[0] == 403


def test_client_supplied_api_key_is_ignored_on_get(proxy):
    assert call(proxy.port, "GET", "/api/v1/sessions", headers={"X-API-Key": "garbage"})[0] == 200


def test_client_supplied_operator_key_cannot_widen_a_post(proxy):
    assert call(proxy.port, "POST", "/api/v1/ingest", headers={"X-API-Key": OPERATOR}, body={})[0] == 403


def test_client_supplied_key_is_never_forwarded_to_upstream(cp, proxy):
    # A GET carrying the OPERATOR key must still be served with the viewer key: the answer matches
    # what the control plane gives the viewer key directly.
    direct_viewer = call(cp.port, "GET", "/api/v1/artifact-members", headers={"X-API-Key": VIEWER})[0]
    direct_operator = call(cp.port, "GET", "/api/v1/artifact-members", headers={"X-API-Key": OPERATOR})[0]
    via = call(proxy.port, "GET", "/api/v1/artifact-members", headers={"X-API-Key": OPERATOR})[0]
    assert direct_viewer != direct_operator and via == direct_viewer


def test_without_operator_key_every_mutation_is_403(cp):
    px, thread = start_proxy(cp.port, operator_key=None)
    try:
        assert call(px.port, "POST", "/api/v1/attention/ack", body={})[0] == 403
        assert call(px.port, "GET", "/api/v1/sessions")[0] == 200
    finally:
        px.shutdown()
        thread.join(timeout=3)


def test_sse_streams_without_buffering(proxy):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    try:
        conn.request("GET", "/api/v1/events/stream")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type", "").startswith("text/event-stream")
        started = time.monotonic()
        first = resp.fp.readline()  # the initial frame arrives while the stream stays open
        assert first.startswith((b"data:", b"event:", b":", b"id:"))
        assert time.monotonic() - started < 5
    finally:
        conn.close()


def test_hop_by_hop_response_headers_are_stripped(proxy):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    try:
        conn.request("GET", "/api/v1/sessions")
        resp = conn.getresponse()
        resp.read()
        assert resp.getheader("Keep-Alive") is None and resp.getheader("Transfer-Encoding") is None
    finally:
        conn.close()


def test_unreachable_upstream_is_502():
    px, thread = start_proxy(1, operator_key=None, allowlist=())
    try:
        assert call(px.port, "GET", "/api/v1/sessions")[0] == 502
    finally:
        px.shutdown()
        thread.join(timeout=3)


# --------------------------------------------------------------- key files and config

def write_key(path, text=VIEWER, mode=0o600):
    path.write_text(text + "\n")
    os.chmod(path, mode)
    return str(path)


def test_key_file_is_read_and_stripped(tmp_path):
    assert read_key_file(write_key(tmp_path / "k")) == VIEWER


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_group_or_other_readable_key_file_is_refused(tmp_path, mode):
    with pytest.raises(ProxyConfigError, match="0600"):
        read_key_file(write_key(tmp_path / "k", mode=mode))


def test_missing_or_empty_key_file_is_refused(tmp_path):
    with pytest.raises(ProxyConfigError):
        read_key_file(str(tmp_path / "nope"))
    with pytest.raises(ProxyConfigError, match="empty"):
        read_key_file(write_key(tmp_path / "k", text=""))


def test_parse_absent_or_unkeyed_means_off():
    assert parse_dashboard_proxy(None) is None
    assert parse_dashboard_proxy({}) is None


def test_parse_defaults_and_overrides():
    s = parse_dashboard_proxy({"viewer_key_file": "/k/v", "upstream": "http://cp:8080"})
    assert s.operator_key_file is None and s.allowlist == DEFAULT_OPERATOR_POST_ALLOWLIST
    s = parse_dashboard_proxy({"viewer_key_file": "/k/v", "operator_key_file": "/k/o",
                               "operator_post_allowlist": ["POST /api/v1/attention/*"]})
    assert s.allowlist == ("POST /api/v1/attention/*",) and s.upstream == "http://control-plane-web:8080"


@pytest.mark.parametrize("raw", [
    "x", {"viewer_key_file": ""}, {"operator_key_file": "/k/o"}, {"viewer_key_file": "/k", "bogus": 1},
    {"viewer_key_file": "/k", "operator_post_allowlist": ["/api/v1/x"]},
    {"viewer_key_file": "/k", "operator_post_allowlist": ["GET /api/v1/x"]},
    {"viewer_key_file": "/k", "operator_post_allowlist": [1]},
    {"viewer_key_file": "/k", "upstream": "ftp://x"}])
def test_parse_rejects_bad_config(raw):
    with pytest.raises(ValueError):
        parse_dashboard_proxy(raw)


def test_fleet_config_validation_rejects_a_bad_dashboard_proxy_block(tmp_path):
    from herdr_engineering import fleet_config_cli as fcc
    from herdr_engineering.fleet_config import FleetConfigError, FleetConfigStore
    path = fcc.init_config(tmp_path / "fleet-config.yaml", {
        "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
        "llm_upstream_key": "placeholder-llm-value", "ci_url": "https://ci.example.test",
        "ci_token": "placeholder-ci-value", "web_bind": "127.0.0.1", "web_port": "8080",
        "gateway_tailnet_ip": "127.0.0.1", "hosts": {"bender": ["builder"]}})
    data = yaml.safe_load(path.read_text())
    data["sections"]["control_plane"]["values"]["dashboard_proxy"] = {"operator_key_file": "/k/o"}
    path.unlink()
    fcc.write_private_file(path, yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(FleetConfigError, match="dashboard_proxy"):
        FleetConfigStore(path).current()


# --------------------------------------------------------------- CLI wiring

def _fleet_with_proxy(tmp_path, block):
    from herdr_engineering import fleet_config_cli as fcc
    path = fcc.init_config(tmp_path / "fleet-config.yaml", {
        "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
        "llm_upstream_key": "placeholder-llm-value", "ci_url": "https://ci.example.test",
        "ci_token": "placeholder-ci-value", "web_bind": "127.0.0.1", "web_port": "8080",
        "gateway_tailnet_ip": "127.0.0.1", "hosts": {"bender": ["builder"]}})
    if block is not None:
        data = yaml.safe_load(path.read_text())
        data["sections"]["control_plane"]["values"]["dashboard_proxy"] = block
        path.unlink()
        fcc.write_private_file(path, yaml.safe_dump(data, sort_keys=False))
    return path


def test_cli_refuses_when_not_configured(tmp_path, capsys):
    from herdr_engineering import cli
    path = _fleet_with_proxy(tmp_path, None)
    assert cli.main(["control-plane", "dashboard-proxy", "--fleet-config", str(path)]) == 2
    assert "dashboard_proxy" in capsys.readouterr().err


def test_cli_refuses_a_group_readable_key_file(tmp_path, capsys):
    from herdr_engineering import cli
    key = write_key(tmp_path / "viewer.key", mode=0o640)
    path = _fleet_with_proxy(tmp_path, {"viewer_key_file": key})
    assert cli.main(["control-plane", "dashboard-proxy", "--fleet-config", str(path)]) == 2
    err = capsys.readouterr().err
    assert "0600" in err and VIEWER not in err


def test_settings_built_from_key_files_proxy_a_real_server(cp, tmp_path):
    from herdr_engineering.control_plane.dashboard_proxy import build_from_settings
    vkey, okey = write_key(tmp_path / "v.key", VIEWER), write_key(tmp_path / "o.key", OPERATOR)
    settings = parse_dashboard_proxy({"viewer_key_file": vkey, "operator_key_file": okey,
                                      "upstream": f"http://127.0.0.1:{cp.port}"})
    px = build_from_settings(settings, "127.0.0.1", 0)
    thread = threading.Thread(target=px.serve, daemon=True)
    thread.start()
    px.wait_until_ready()
    try:
        assert call(px.port, "GET", "/api/v1/sessions")[0] == 200
        assert call(px.port, "POST", "/api/v1/attention/ack", body={})[0] not in (401, 403)
    finally:
        px.shutdown()
        thread.join(timeout=3)
