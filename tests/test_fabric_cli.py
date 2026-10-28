"""CLI wiring for the fabric URL / HTTPS / access / failover features (the part unit tests of the library miss)."""
import json
import signal
import socket
import ssl

import pytest

from herdr_engineering import cli
from herdr_engineering.fabric_tls import PrivateCA


def _run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr().out
    return rc, json.loads(out[out.index("{"):])


# ---- helpers the dashboard server is built from ------------------------------------------------
@pytest.fixture
def config(monkeypatch):
    holder = {"cfg": {}}
    monkeypatch.setattr("herdr_engineering.config.load_config", lambda *a, **k: holder["cfg"])
    monkeypatch.setattr("herdr_engineering.host_config.host_value", lambda key, default=None: None)
    return holder


def test_resolver_and_lister_are_none_without_a_gateway(config):
    assert cli._fabric_url_resolver() is None
    assert cli._fabric_service_lister() is None


def test_resolver_and_lister_bind_to_the_configured_gateway(config):
    config["cfg"] = {"fabric": {"gateway_url": "http://127.0.0.1:29999"}}
    resolve, lister = cli._fabric_url_resolver(), cli._fabric_service_lister()
    assert resolve.__name__ == "resolve_urls" and lister.__name__ == "list"
    assert resolve.__self__.gateway_url == "http://127.0.0.1:29999"


# ---- devfabric ca ------------------------------------------------------------------------------
def test_ca_commands_end_to_end(tmp_path, capsys):
    d = str(tmp_path / "ca")
    rc, init = _run(capsys, "devfabric", "ca", "init", "--dir", d)
    assert rc == 0 and init["domains"] == ["dev.lan"] and len(init["sha256"]) == 64
    rc, issued = _run(capsys, "devfabric", "ca", "issue", "--dir", d)
    assert rc == 0 and issued["names"] == ["dev.lan", "*.dev.lan"]
    rc, exported = _run(capsys, "devfabric", "ca", "export", "--dir", d, "--out", str(tmp_path / "out"))
    assert rc == 0 and exported["sha256"] == init["sha256"]
    assert (tmp_path / "out" / "fabric-ca.pem").read_text().startswith("-----BEGIN CERTIFICATE")
    assert not list((tmp_path / "out").glob("*.key"))                      # the CA key never leaves its directory
    rc, status = _run(capsys, "devfabric", "ca", "status", "--dir", d)
    assert status["ca"] is True and status["renewal_due"] is False and status["sha256"] == init["sha256"]


def test_ca_init_refuses_to_overwrite_and_reports_it(tmp_path, capsys):
    d = str(tmp_path / "ca")
    assert _run(capsys, "devfabric", "ca", "init", "--dir", d)[0] == 0
    rc, out = _run(capsys, "devfabric", "ca", "init", "--dir", d)
    assert rc == 1 and out["ok"] is False and "already exists" in out["error"]


def test_ca_issue_without_a_ca_and_outside_constraints_fail_cleanly(tmp_path, capsys):
    d = str(tmp_path / "ca")
    rc, out = _run(capsys, "devfabric", "ca", "issue", "--dir", d)
    assert rc == 1 and "no CA" in out["error"]
    _run(capsys, "devfabric", "ca", "init", "--dir", d)
    rc, out = _run(capsys, "devfabric", "ca", "issue", "--dir", d, "--name", "example.com")
    assert rc == 1 and "outside" in out["error"]


def test_ca_status_when_nothing_exists(tmp_path, capsys):
    rc, out = _run(capsys, "devfabric", "ca", "status", "--dir", str(tmp_path / "none"))
    assert rc == 0 and out["ca"] is False and out["sha256"] is None


# ---- devfabric gateway -------------------------------------------------------------------------
def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _run_gateway(monkeypatch, capsys, tmp_path, *extra):
    """Start the real gateway command, let it print its banner, then deliver SIGTERM to stop it."""
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    monkeypatch.setattr("herdr_engineering.config.load_config", lambda *a, **k: {})
    handlers, seen = {}, {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))

    def fake_sleep(_):
        out = capsys.readouterr().out
        seen["banner"] = json.loads(out[out.index("{"):out.index("}\n") + 2])
        seen["hook"]()
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    seen["hook"] = lambda: None
    monkeypatch.setattr("time.sleep", fake_sleep)
    port = _free_port()
    argv = ["devfabric", "gateway", "--bind", "127.0.0.1", "--control-port", str(port), "--url-base", "dev.test", *extra]
    return argv, seen, port


def test_gateway_plain_banner_reports_http_and_no_access_control(monkeypatch, capsys, tmp_path):
    argv, seen, _ = _run_gateway(monkeypatch, capsys, tmp_path)
    assert cli.main(argv) == 0
    b = seen["banner"]
    assert b["https"] is False and b["access_enforced"] is False and b["role"] == "active" and b["peer"] is None
    assert "http://dev.test:<port>" in b["note"]


def test_gateway_tls_flags_serve_https_and_both_flags_are_required(monkeypatch, capsys, tmp_path):
    import http.server
    import threading

    from herdr_engineering.fabric import FabricClient
    ca = PrivateCA(tmp_path / "ca"); ca.init(["dev.test"]); cert, key = ca.issue(["dev.test"])
    origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    argv, seen, port = _run_gateway(monkeypatch, capsys, tmp_path, "--tls-cert", str(cert), "--tls-key", str(key))
    results = {}

    def probe():
        lease = FabricClient(f"http://127.0.0.1:{port}", token="").register(
            target_machine_id="bender", target_host="127.0.0.1", target_port=origin.server_address[1])["lease"]
        ctx = ssl.create_default_context(cafile=str(ca.cert_path))
        with socket.create_connection(("127.0.0.1", lease["external_port"]), timeout=5) as raw, \
                ctx.wrap_socket(raw, server_hostname="dev.test") as tls:
            tls.sendall(b"GET / HTTP/1.1\r\nHost: dev.test\r\nConnection: close\r\n\r\n")
            results["status"] = tls.recv(12)
        results["url"] = lease.get("url")
    seen["hook"] = probe
    try:
        assert cli.main(argv) == 0
    finally:
        origin.shutdown()
    assert seen["banner"]["https"] is True and "plain http also accepted" in seen["banner"]["note"]
    assert results["status"].startswith(b"HTTP/1.")                       # the gateway command really terminates TLS
    with pytest.raises(SystemExit, match="together"):
        cli.main(["devfabric", "gateway", "--bind", "127.0.0.1", "--tls-cert", str(cert)])


def test_gateway_allow_flags_and_config_enable_enforcement(monkeypatch, capsys, tmp_path):
    argv, seen, _ = _run_gateway(monkeypatch, capsys, tmp_path, "--allow-user", "a@example.com", "--allow-tag", "tag:dev")
    assert cli.main(argv) == 0 and seen["banner"]["access_enforced"] is True


def test_gateway_peer_flags_reach_the_gateway(monkeypatch, capsys, tmp_path):
    argv, seen, _ = _run_gateway(monkeypatch, capsys, tmp_path, "--peer", "http://127.0.0.1:1", "--node-id", "n1",
                                 "--promote-after", "2")
    assert cli.main(argv) == 0
    # the peer is unreachable at start, so this node takes over as the active gateway
    assert seen["banner"]["peer"] == "http://127.0.0.1:1" and seen["banner"]["role"] == "active"


# ---- the control-plane server is actually handed the fabric helpers -----------------------------
def test_cp_web_passes_the_fabric_helpers_to_the_server(monkeypatch, config, tmp_path, capsys):
    from herdr_engineering.control_plane import api
    config["cfg"] = {"fabric": {"gateway_url": "http://127.0.0.1:29999"}}
    captured = {}

    class Recorder(api.ControlPlaneServer):
        def __init__(self, repo, **kw):
            captured.update(kw)
            super().__init__(repo, **kw)

        def serve(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(api, "ControlPlaneServer", Recorder)
    monkeypatch.setattr("herdr_engineering.host_config.host_config_or_none", lambda: None)
    rc = cli.main(["control-plane", "web", "--host", "127.0.0.1", "--port", "0", "--api-key", "k",
                   "--fleet-config", str(tmp_path / "none.yaml")])
    capsys.readouterr()
    assert rc == 0
    assert callable(captured["fabric_resolver"]) and captured["fabric_resolver"].__name__ == "resolve_urls"
    assert callable(captured["fabric_services"]) and captured["fabric_services"].__name__ == "list"
