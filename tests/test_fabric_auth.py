"""Gateway control-API auth: documented token sources, fail-closed default, open health probe, rotation."""
import json
import urllib.error
import urllib.request

import pytest

from herdr_engineering import cli
from herdr_engineering.devfabric import DevServiceRegistry
from herdr_engineering.errors import UnreachableError
from herdr_engineering.fabric import FabricClient, FabricGateway, GatewayRouter, read_fabric_token
from tests.test_fabric import _port_range


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    # Real process environment (not internals): point the file source at an empty temp dir.
    monkeypatch.delenv("HERDR_ENGINEERING_FABRIC_TOKEN", raising=False)
    monkeypatch.setenv("HERDR_ENGINEERING_FABRIC_TOKEN_FILE", str(tmp_path / "fabric-token"))
    monkeypatch.setenv("HOME", str(tmp_path))


def _gw(tmp_path, **kw):
    registry = DevServiceRegistry(store_path=tmp_path / "leases.json", port_range=_port_range())
    gw = FabricGateway(registry, GatewayRouter(["127.0.0.1"]), control_host="127.0.0.1", control_port=0,
                       url_base="dev.test", probe_interval=30, **kw)
    gw.start()
    return gw


def _raw(gw, path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{gw.control_port}{path}")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_token_env_var_is_read(monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_FABRIC_TOKEN", "from-env")
    assert read_fabric_token() == "from-env"


def test_token_file_path_env_is_read(monkeypatch, tmp_path):
    (tmp_path / "fabric-token").write_text("from-file\n")
    assert read_fabric_token() == "from-file"


def test_no_token_and_no_opt_in_rejects_control_requests(tmp_path):
    gw = _gw(tmp_path)
    try:
        assert _raw(gw, "/list")[0] == 401
        with pytest.raises(UnreachableError):
            FabricClient(f"http://127.0.0.1:{gw.control_port}").register(
                target_machine_id="m", target_host="127.0.0.1", target_port=1)
    finally:
        gw.stop()


def test_allow_open_serves_control_api_without_token(tmp_path):
    gw = _gw(tmp_path, allow_open=True)
    try:
        assert _raw(gw, "/list")[0] == 200
    finally:
        gw.stop()


def test_healthz_needs_no_token_and_is_minimal_without_one(tmp_path):
    gw = _gw(tmp_path, token="sekrit")
    try:
        code, body = _raw(gw, "/healthz")
        assert code == 200 and body == {"ok": True}
        code, body = _raw(gw, "/healthz", token="sekrit")
        assert code == 200 and "bound_ports" in body
        assert _raw(gw, "/list")[0] == 401
    finally:
        gw.stop()


def test_gateway_follows_token_file_rotation_without_restart(tmp_path):
    f = tmp_path / "fabric-token"
    f.write_text("one")
    gw = _gw(tmp_path)
    try:
        assert _raw(gw, "/list", "one")[0] == 200
        f.write_text("two")
        assert _raw(gw, "/list", "two")[0] == 200
        assert _raw(gw, "/list", "one")[0] == 200   # previous token accepted for the grace window
        assert _raw(gw, "/list", "bogus")[0] == 401
    finally:
        gw.stop()


def test_token_file_appearing_later_closes_an_allow_open_gateway(tmp_path):
    gw = _gw(tmp_path, allow_open=True)
    try:
        assert _raw(gw, "/list")[0] == 200
        (tmp_path / "fabric-token").write_text("late")
        assert _raw(gw, "/list")[0] == 401
        assert _raw(gw, "/list", "late")[0] == 200
        (tmp_path / "fabric-token").unlink()          # a vanished file must not reopen the API
        assert _raw(gw, "/list")[0] == 401
    finally:
        gw.stop()


def test_gateway_command_refuses_to_start_without_token_or_allow_open(tmp_path):
    with pytest.raises(SystemExit, match="allow-open"):
        cli.main(["devfabric", "gateway", "--bind", "127.0.0.1", "--control-port", "0", "--url-base", "dev.test"])
