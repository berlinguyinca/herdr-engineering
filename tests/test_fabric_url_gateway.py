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


def test_dev_register_reports_a_gateway_route_rejection_without_falling_back(tmp_path, monkeypatch, capsys):
    """A real gateway answers 400 for a bad route table: `dev register` says so and exits 1; it must not claim the
    gateway is unreachable or register the lease in the local registry."""
    import herdr_engineering.cli as cli
    from herdr_engineering.fabric import GatewayRejected

    gw, registry, _ = _gateway(tmp_path)
    state = tmp_path / "local-state"
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(state))
    monkeypatch.setattr(cli, "_local_fabric_client", lambda: _client(gw))
    try:
        with pytest.raises(GatewayRejected) as exc:
            _client(gw).register(target_machine_id="m", target_host="127.0.0.1", target_port=80,
                                 routes=[{"prefix": "/a", "host": "127.0.0.1", "port": 0}])
        assert isinstance(exc.value, UnreachableError) and "400" in str(exc.value)
        args = type("A", (), {"machine": "m", "host": "127.0.0.1", "port": 80, "protocol": "http", "session": None,
                              "repo": None, "worktree": None, "process": None, "label": None,
                              "route": ["/a=127.0.0.1:0"], "preferred_port": 80})()
        assert cli._cmd_dev_register(args) == 1
        out = capsys.readouterr().out
        assert "rejected" in out and "fell back" not in out and "unreachable" not in out
        assert registry.active_leases() == []
        assert not (state / "leases.json").exists() or not json.loads((state / "leases.json").read_text()).get("leases")
    finally:
        gw.stop()
