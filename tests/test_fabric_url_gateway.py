"""Gateway wiring for canonical URL resolution and target-host (SSRF) validation."""
import json
import urllib.request

import pytest

from herdr_engineering.errors import UnreachableError
from tests.test_fabric import _client, _gateway, _http_target


def _post(gw, path, payload):
    req = urllib.request.Request(f"http://127.0.0.1:{gw.control_port}{path}", data=json.dumps(payload).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


@pytest.mark.parametrize("host", ["169.254.169.254", "10.0.0.5", "evil.example", "0.0.0.0", "fe80::1"])
def test_register_rejects_ssrf_targets(tmp_path, host):
    gw, registry, router = _gateway(tmp_path)
    try:
        with pytest.raises(UnreachableError) as exc:
            _client(gw).register(target_machine_id="beast", target_host=host, target_port=80)
        assert "400" in str(exc.value)
        assert registry.active_leases() == [] and router.bound_ports() == []
    finally:
        gw.stop()


def test_operator_allowlist_permits_declared_network(tmp_path):
    server, tport = _http_target(tmp_path)
    gw, _, _ = _gateway(tmp_path)
    gw.allowed_target_networks = ("10.0.0.0/8",)
    try:
        res = _client(gw).register(target_machine_id="beast", target_host="10.1.2.3", target_port=tport)
        assert res["lease"]["target"]["host"] == "10.1.2.3"
    finally:
        gw.stop()
        server.shutdown()


def test_resolve_urls_batch_keeps_originals(tmp_path):
    server, tport = _http_target(tmp_path)
    gw, _, _ = _gateway(tmp_path)
    try:
        lease = _client(gw).register(target_machine_id="bender", target_host="127.0.0.1",
                                     target_port=tport, label="web")["lease"]
        urls = [f"http://localhost:{tport}/benchmarks?x=1#f", "https://example.com/", "http://localhost:1/"]
        out = _post(gw, "/resolve-urls", {"urls": urls, "source_machine": "bender"})["resolutions"]
        assert [o["original"] for o in out] == urls
        assert out[0]["canonical"] == f"http://dev.test:{lease['external_port']}/benchmarks?x=1#f"
        assert out[0]["service"] == "web" and out[0]["status"] == "resolved"
        assert out[1]["status"] == "not_loopback" and out[1]["canonical"] is None
        assert out[2]["status"] == "unmapped"
    finally:
        gw.stop()
        server.shutdown()


def test_resolve_urls_rejects_bad_body(tmp_path):
    gw, _, _ = _gateway(tmp_path)
    try:
        with pytest.raises(Exception) as exc:
            _post(gw, "/resolve-urls", {"urls": "http://localhost:1/"})
        assert "400" in str(exc.value)
    finally:
        gw.stop()
