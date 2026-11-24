"""Gateway control-API auth: documented token sources, fail-closed default, open health probe, rotation."""
import json
import os
import urllib.error
import urllib.request

import pytest

from herdr_engineering import cli
from herdr_engineering import host_config as _hc
from herdr_engineering.devfabric import DevServiceRegistry
from herdr_engineering.errors import UnreachableError
from herdr_engineering.fabric import FabricClient, FabricGateway, GatewayRouter, read_fabric_token
from tests.test_fabric import _port_range

_REAL_HOST_CONFIG = _hc.host_config   # captured at import, before the autouse fixture replaces it


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    old_umask = os.umask(0o077)                       # token files the tests write are 0600, as the gateway requires
    # Real process environment (not internals): point the file source at an empty temp dir.
    monkeypatch.delenv("HERDR_ENGINEERING_FABRIC_TOKEN", raising=False)
    monkeypatch.setenv("HERDR_ENGINEERING_FABRIC_TOKEN_FILE", str(tmp_path / "fabric-token"))
    monkeypatch.setenv("HOME", str(tmp_path))
    from herdr_engineering.fabric import reset_fabric_token_cache
    reset_fabric_token_cache()
    yield
    os.umask(old_umask)


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


# -- review fixes ---------------------------------------------------------------------------------------------------

def test_healthz_probe_rejects_a_wrong_token_instead_of_reading_the_minimal_payload_as_healthy(tmp_path):
    gw = _gw(tmp_path, token="sekrit")
    url = f"http://127.0.0.1:{gw.control_port}"
    try:
        for bad in ("wrong", ""):
            with pytest.raises(UnreachableError):
                FabricClient(url, token=bad).healthz()
            assert cli._gateway_bound_ports(FabricClient(url, token=bad)) is None
        assert "bound_ports" in FabricClient(url, token="sekrit").healthz()
        assert cli._gateway_bound_ports(FabricClient(url, token="sekrit")) == set()
    finally:
        gw.stop()


def test_ha_peer_health_needs_the_right_token_and_then_carries_role_and_epoch(tmp_path):
    peer = _gw(tmp_path / "peer", token="sekrit")
    mine = _gw(tmp_path / "mine", token="sekrit", peer_url=f"http://127.0.0.1:{peer.control_port}", auto_ha=False)
    wrong = _gw(tmp_path / "wrong", token="other", peer_url=f"http://127.0.0.1:{peer.control_port}", auto_ha=False)
    try:
        health = mine._peer_health()
        assert health and health.get("role") and "epoch" in health and health.get("node_id")
        assert wrong._peer_health() is None
    finally:
        for g in (peer, mine, wrong):
            g.stop()


def test_herdr_web_gets_the_same_read_only_token_mount_as_the_gateway():
    import pathlib

    import yaml
    doc = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "deploy/control-plane/compose.yaml").read_text())
    mounts = {}
    for name in ("fabric-gateway", "herdr-web"):
        svc = doc["services"][name]
        assert svc["environment"]["HERDR_ENGINEERING_FABRIC_TOKEN_FILE"] == "/run/fabric/token", name
        mounts[name] = [v for v in svc["volumes"] if v.endswith(":/run/fabric:ro")]
        assert len(mounts[name]) == 1, name
    assert mounts["fabric-gateway"] == mounts["herdr-web"]


def test_a_typo_then_the_fix_does_not_evict_the_real_previous_token(tmp_path):
    f = tmp_path / "fabric-token"
    f.write_text("one")
    gw = _gw(tmp_path, rotation_grace=60)
    try:
        assert _raw(gw, "/list", "one")[0] == 200
        f.write_text("tw")                             # torn / mistyped
        assert _raw(gw, "/list", "tw")[0] == 200
        f.write_text("two")
        assert _raw(gw, "/list", "two")[0] == 200
        assert _raw(gw, "/list", "one")[0] == 200      # still inside its own grace
        assert _raw(gw, "/list", "tw")[0] == 200
        assert _raw(gw, "/list", "bogus")[0] == 401
    finally:
        gw.stop()


def test_previous_tokens_expire_after_the_grace(tmp_path):
    f = tmp_path / "fabric-token"
    f.write_text("one")
    gw = _gw(tmp_path, rotation_grace=0)
    try:
        assert _raw(gw, "/list", "one")[0] == 200
        f.write_text("two")
        assert _raw(gw, "/list", "two")[0] == 200
        assert _raw(gw, "/list", "one")[0] == 401
    finally:
        gw.stop()


def test_default_grace_leaves_a_wide_margin_over_the_agent_config_refresh_interval(tmp_path):
    from herdr_engineering.host_config import REFRESH_INTERVAL
    gw = _gw(tmp_path, allow_open=True)
    try:
        assert gw.rotation_grace >= 4 * REFRESH_INTERVAL
    finally:
        gw.stop()


def test_an_unreadable_token_file_fails_closed_even_with_allow_open(tmp_path):
    (tmp_path / "fabric-token").mkdir()               # docker creates a directory for a missing file bind
    with pytest.raises(OSError):
        read_fabric_token()
    gw = _gw(tmp_path, allow_open=True)
    try:
        assert _raw(gw, "/list")[0] == 401
        assert _raw(gw, "/healthz")[1] == {"ok": True}
    finally:
        gw.stop()


def test_a_token_file_that_becomes_unreadable_keeps_the_last_known_token(tmp_path):
    f = tmp_path / "fabric-token"
    f.write_text("one")
    gw = _gw(tmp_path, allow_open=True)
    try:
        assert _raw(gw, "/list", "one")[0] == 200
        f.unlink()
        f.mkdir()
        assert _raw(gw, "/list")[0] == 401
        assert _raw(gw, "/list", "one")[0] == 200
    finally:
        gw.stop()


def test_gateway_command_refuses_to_start_with_an_unreadable_token_file(tmp_path):
    (tmp_path / "fabric-token").mkdir()
    with pytest.raises(SystemExit, match="unreadable"):
        cli.main(["devfabric", "gateway", "--bind", "127.0.0.1", "--control-port", "0", "--url-base", "dev.test"])


def test_a_long_lived_client_follows_a_rotated_token_file_after_the_grace(tmp_path):
    f = tmp_path / "fabric-token"
    f.write_text("one")
    gw = _gw(tmp_path, rotation_grace=0)
    try:
        client = FabricClient(f"http://127.0.0.1:{gw.control_port}")      # built before the rotation
        assert client.list() == {"leases": []} or "leases" in client.list()
        f.write_text("two")
        assert "leases" in client.list()
        f.write_text("three")
        assert "leases" in client.list()
        pinned = FabricClient(f"http://127.0.0.1:{gw.control_port}", token="one")   # explicit token stays pinned
        with pytest.raises(UnreachableError):
            pinned.list()
    finally:
        gw.stop()


# -- review round 2 ---------------------------------------------------------------------------------------------------

def _post(gw, path, payload, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{gw.control_port}{path}", data=json.dumps(payload).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_an_open_standby_never_promotes_over_a_peer_that_only_rejects_its_token(tmp_path):
    """The token was written on one node of an HA pair only: the other node sees a minimal /healthz. That peer is
    alive, so it must not count as a miss, and the pair must not flip."""
    tokened = _gw(tmp_path / "t", token="sekrit", node_id="zz-tokened", promote_after=1, auto_ha=False)
    open_node = _gw(tmp_path / "o", allow_open=True, node_id="aa-open", promote_after=1, auto_ha=False,
                    peer_url=f"http://127.0.0.1:{tokened.control_port}")
    try:
        assert tokened.role == "active"
        open_node.demote()
        for _ in range(4):
            open_node._ha_step()
        assert open_node.role == "standby" and open_node.peer_unauthorised
        assert tokened.role == "active"
    finally:
        tokened.stop()
        open_node.stop()


def test_a_dead_peer_still_promotes_after_the_misses(tmp_path):
    gw = _gw(tmp_path, allow_open=True, promote_after=2, auto_ha=False, peer_url="http://127.0.0.1:1")
    try:
        gw.demote()
        gw._ha_step()
        assert gw.role == "standby"
        gw._ha_step()
        assert gw.role == "active" and not gw.peer_unauthorised
    finally:
        gw.stop()


def test_a_node_starting_beside_a_peer_that_rejects_its_token_waits_as_standby(tmp_path):
    tokened = _gw(tmp_path / "t", token="sekrit", auto_ha=False)
    try:
        gw = _gw(tmp_path / "o", allow_open=True, auto_ha=False, peer_url=f"http://127.0.0.1:{tokened.control_port}")
        try:
            assert gw.role == "standby"
        finally:
            gw.stop()
    finally:
        tokened.stop()


def test_sessions_resolve_and_rewrite_loopback_without_a_token_but_see_no_lease_record(tmp_path):
    gw = _gw(tmp_path, token="sekrit")
    try:
        lease = FabricClient(f"http://127.0.0.1:{gw.control_port}", token="sekrit").register(
            target_machine_id="box", target_host="127.0.0.1", target_port=4321, label="svc")
        port = lease["lease"]["external_port"] if "lease" in lease else lease["external_port"]
        code, body = _raw(gw, "/resolve-loopback/4321?from=other")
        assert code == 200 and body["url"] == f"http://dev.test:{port}" and "lease" not in body
        assert "target_host" not in json.dumps(body) and "box" not in json.dumps(body)
        code, body = _post(gw, "/rewrite", {"text": "see http://localhost:4321/x", "from": "other"})
        assert code == 200 and f"dev.test:{port}" in body["text"]
        assert _raw(gw, "/resolve-loopback/4321?from=other", token="sekrit")[1].get("lease")
        assert _post(gw, "/register", {})[0] == 401
        assert _post(gw, "/close", {"lease_id": "x"})[0] == 401
        assert _raw(gw, "/list")[0] == 401
        assert _post(gw, "/rewrite", {"text": "x" * (70 * 1024)})[0] == 413
        assert _post(gw, "/rewrite", {"text": "x" * (70 * 1024)}, token="sekrit")[0] == 200
    finally:
        gw.stop()


def test_a_non_utf8_token_file_fails_closed_everywhere(tmp_path):
    f = tmp_path / "fabric-token"
    f.write_bytes(b"\xff\xfe\x80")
    f.chmod(0o600)
    with pytest.raises(OSError):
        read_fabric_token()
    gw = _gw(tmp_path, allow_open=True)
    try:
        assert _raw(gw, "/list")[0] == 401
        assert gw.control_auth == "unreadable"
        with pytest.raises(UnreachableError):
            FabricClient(f"http://127.0.0.1:{gw.control_port}").list()
    finally:
        gw.stop()


@pytest.mark.parametrize("make", ["wide", "symlink"])
def test_a_token_file_with_a_wide_mode_or_a_symlink_is_refused(tmp_path, make):
    f = tmp_path / "fabric-token"
    if make == "wide":
        f.write_text("tok")
        f.chmod(0o644)
    else:
        (tmp_path / "real").write_text("tok")
        (tmp_path / "real").chmod(0o600)
        f.symlink_to(tmp_path / "real")
    with pytest.raises(OSError):
        read_fabric_token()


def test_an_empty_or_missing_token_file_is_no_token(tmp_path):
    assert read_fabric_token() is None
    f = tmp_path / "fabric-token"
    f.write_text("")
    f.chmod(0o600)
    assert read_fabric_token() is None


def test_an_open_gateway_reports_open_and_a_tokened_one_token(tmp_path):
    gw = _gw(tmp_path, allow_open=True)
    try:
        assert gw.control_auth == "open"
        (tmp_path / "fabric-token").write_text("t")
        (tmp_path / "fabric-token").chmod(0o600)
        assert gw.control_auth == "token"
    finally:
        gw.stop()


def _enrol_host(tmp_path, url):
    cfg = tmp_path / ".config" / "herdr-engineering"
    cfg.mkdir(parents=True)
    (cfg / "host.json").write_text(json.dumps({"control_plane_url": url, "host_id": "h1", "token": "host-token"}))


def test_a_failed_host_config_fetch_is_not_read_as_no_token_and_is_retried(tmp_path, monkeypatch):
    """Control plane down and nothing cached: the host config says nothing, which must not read as "no token"."""
    from herdr_engineering import host_config as hc
    from herdr_engineering.fabric import reset_fabric_token_cache
    monkeypatch.setattr(hc, "host_config", _REAL_HOST_CONFIG)
    reset_fabric_token_cache()
    _enrol_host(tmp_path, "http://127.0.0.1:1")
    with pytest.raises(OSError):
        read_fabric_token()
    gw = _gw(tmp_path, allow_open=True)
    try:
        assert _raw(gw, "/list")[0] == 401 and gw.control_auth == "unreadable"
    finally:
        gw.stop()
        reset_fabric_token_cache()


def test_the_token_file_wins_over_a_pinned_host_config_token_and_digits_in_yaml_are_text(tmp_path, fake_host_config):
    from herdr_engineering.fabric import reset_fabric_token_cache
    reset_fabric_token_cache()
    fake_host_config({"fabric": {"token": 123456}})
    assert read_fabric_token() == "123456"                    # an int from YAML must not crash accepts()
    f = tmp_path / "fabric-token"
    f.write_text("from-file")
    f.chmod(0o600)
    assert read_fabric_token() == "from-file"
    gw = _gw(tmp_path)
    try:
        assert _raw(gw, "/list", "from-file")[0] == 200
        f.write_text("rotated")
        assert _raw(gw, "/list", "rotated")[0] == 200
    finally:
        gw.stop()
        reset_fabric_token_cache()


def test_a_numeric_host_config_token_is_accepted_not_a_crash(tmp_path, fake_host_config):
    from herdr_engineering.fabric import reset_fabric_token_cache
    reset_fabric_token_cache()
    fake_host_config({"fabric": {"token": 987654}})
    gw = _gw(tmp_path)
    try:
        assert _raw(gw, "/list", "987654")[0] == 200
        assert _raw(gw, "/list", "nope")[0] == 401
    finally:
        gw.stop()
        reset_fabric_token_cache()
