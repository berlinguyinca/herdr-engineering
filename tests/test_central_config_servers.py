"""The control-plane web, worker, llm-proxy and their helpers read configuration
only from the central fleet file (control_plane / llm sections) - never from env."""
import argparse
import threading

import pytest
import yaml

from herdr_engineering import cli, fleet_config_cli as fcc
from herdr_engineering.control_plane import db
from herdr_engineering.control_plane.ci_monitor import CIMonitor
from herdr_engineering.control_plane.inspector import InspectorStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo

ANSWERS = {
    "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
    "llm_upstream_key": "llm-secret-key", "ci_url": "https://ci.example.test",
    "ci_token": "ci-secret-token", "web_bind": "127.0.0.1", "web_port": "8080", "gateway_tailnet_ip": "127.0.0.1",
    "hosts": {"bender": ["builder"]},
}

OLD_ENV = {
    "HERDR_CP_PG_DSN": "postgresql://env:env@envhost/envdb", "HERDR_CP_PG_USER": "envuser",
    "HERDR_CP_PG_PASSWORD": "envpass", "HERDR_CP_HOST_ALIASES": "a=b",
    "HERDR_CP_SELF_HOST": "env-self", "HERDR_CP_WEB_PORT": "1111",
    "HERDR_CP_RUSTFS_ENDPOINT": "http://env:9", "HERDR_CP_MINIO_USER": "envminio",
    "HERDR_CP_MINIO_PASSWORD": "envminiopw", "HERDR_ENGINEERING_CI_ACTIONS": "1",
    "HERDR_INSPECTOR_MAX_EVENTS": "7", "HERDR_INSPECTOR_STORE_REASONING": "0",
    "HERDR_INSPECTOR_REASONING_ROLE": "viewer", "HERDR_LLM_PROXY_MAX_INFLIGHT": "1",
    "HERDR_LLM_PROXY_QUEUE_WAIT_SECONDS": "0.1", "FABRIC_LLM_UPSTREAM_KEY": "env-key",
    "FABRIC_LLM_UPSTREAM_KEY_FILE": "/nonexistent",
}


@pytest.fixture
def old_env(monkeypatch):
    for k, v in OLD_ENV.items():
        monkeypatch.setenv(k, v)


def make_file(tmp_path, **cp_extra):
    path = fcc.init_config(tmp_path / "fleet-config.yaml", dict(ANSWERS))
    if cp_extra:
        data = yaml.safe_load(path.read_text())
        data["sections"]["control_plane"]["values"].update(cp_extra)
        path.unlink()
        fcc.write_private_file(path, yaml.safe_dump(data, sort_keys=False))
    return path


# ------------------------------------------------------------------ inspector

def test_inspector_reads_limits_from_mapping_not_env(old_env):
    s = InspectorStore.from_config({"max_events": 123, "max_bytes": 999999999999,
                                    "reasoning_role": "viewer", "sensitive_role": "bogus",
                                    "store_reasoning": True})
    assert s.limits["max_events_per_session"] == 123
    assert s.limits["max_bytes_per_session"] == 64_000_000
    assert s.store_reasoning is True


def test_inspector_defaults_ignore_old_env(old_env):
    s = InspectorStore.from_config(None)
    assert s.store_reasoning is True
    assert s.limits["max_events_per_session"] != 7


def test_repo_passes_inspector_limits(old_env):
    repo = ControlPlaneRepo(inspector_limits={"max_sessions": 5, "store_reasoning": False})
    assert repo.inspector.limits["max_sessions"] == 5
    assert repo.inspector.store_reasoning is False


# ----------------------------------------------------------------- ci monitor

def test_ci_actions_come_from_the_flag_not_env(old_env):
    assert CIMonitor(provider_factory=lambda: None).actions_enabled is False
    assert CIMonitor(provider_factory=lambda: None, actions_enabled=True).actions_enabled is True
    with pytest.raises(PermissionError):
        CIMonitor(provider_factory=lambda: None).action("org/app", 1, "cancel")


def test_ci_detail_strips_actions_unless_enabled(old_env):
    class P:
        available = True
        token = ""

        def pipeline(self, *_a):
            return {"can_retry": True, "can_cancel": True, "run_number": 1}
    off = CIMonitor(provider_factory=P).detail("org/app", 1)["pipeline"]
    on = CIMonitor(provider_factory=P, actions_enabled=True).detail("org/app", 1)["pipeline"]
    assert off["can_retry"] is False and on["can_retry"] is True


# ------------------------------------------------------------------------- db

def test_db_config_ignores_pg_env(old_env):
    c = db.control_plane_config({"control_plane": {"postgres_dsn": "postgresql://u:p@postgres:5432/herdr"}})
    assert c["postgres_dsn"] == "postgresql://u:p@postgres:5432/herdr"


def test_cp_config_takes_dsn_from_fleet_file(tmp_path, old_env):
    path = make_file(tmp_path)
    dsn = yaml.safe_load(path.read_text())["sections"]["control_plane"]["values"]["pg_dsn"]
    assert cli._cp_config(str(path))["postgres_dsn"] == dsn


# ------------------------------------------------------------------ web / rustfs

def test_rustfs_client_not_built_from_env(old_env, monkeypatch):
    import herdr_engineering.control_plane.rustfs as rfs
    monkeypatch.setattr(rfs, "RustFSClient", lambda **kw: pytest.fail("built from env"))
    assert cli._maybe_rustfs_client(None) is None
    assert cli._maybe_rustfs_client({}) is None


def test_host_aliases_accept_mapping_and_string():
    assert cli._host_aliases_from({"host_aliases": {"a": "b"}}) == {"a": "b"}
    assert cli._host_aliases_from({"host_aliases": "a=b,c=d"}) == {"a": "b", "c": "d"}
    assert cli._host_aliases_from({}) == {}


def _run_web(monkeypatch, path, **overrides):
    seen = {}

    class FakeServer:
        port = 8080

        def __init__(self, repo, **kw):
            seen["server_kw"] = kw
            seen["repo"] = repo

        def serve(self):
            pass

        def shutdown(self):
            pass
    import herdr_engineering.control_plane.api as api
    import herdr_engineering.control_plane.sink as sink
    monkeypatch.setattr(api, "ControlPlaneServer", FakeServer)
    monkeypatch.setattr(sink, "DurableEventSink", lambda dsn: seen.setdefault("sink_dsn", dsn))
    monkeypatch.setattr(cli, "_maybe_pg_source", lambda dsn: None)
    monkeypatch.setattr(cli, "_maybe_rustfs_client", lambda cp: seen.setdefault("rustfs_cp", cp) and None)
    monkeypatch.setattr(cli, "_self_report_services",
                        lambda repo, **kw: seen.setdefault("self_report", kw))
    args = argparse.Namespace(host="0.0.0.0", port=8080, api_key=None, api_keys=None,
                              fleet_config=str(path), pg_dsn=None, seed_demo=False,
                              web_root=None, allow_open=True, collab_cognito_pool=None,
                              collab_users_file=None, collab_tls_terminated=False,
                              collab_require_https=False, collab_cognito_client=[],
                              collab_cognito_group=None, collab_only=False,
                              collab_public_port=None, collab_public_host=None,
                              collab_log_file=None, collab_sync_dir=None,
                              collab_cognito_web_client=None, collab_cognito_domain=None,
                              collab_public_url=None)
    for k, v in overrides.items():
        setattr(args, k, v)
    assert cli._cmd_cp_web(args) == 0
    return seen


def test_web_reads_everything_from_the_fleet_file_and_ignores_env(tmp_path, monkeypatch, old_env, capsys):
    path = make_file(tmp_path, host_aliases={"x": "y"}, self_host="cp-host", ci_actions=True,
                     inspector_limits={"max_sessions": 9})
    seen = _run_web(monkeypatch, path)
    cp = yaml.safe_load(path.read_text())["sections"]["control_plane"]["values"]
    assert seen["sink_dsn"] == cp["pg_dsn"]
    assert seen["self_report"]["host_id"] == "cp-host"
    assert seen["self_report"]["web_port"] == 8080
    assert seen["self_report"]["dsn"] == cp["pg_dsn"]
    assert seen["self_report"]["rustfs_endpoint"] == cp["rustfs_endpoint"]
    assert seen["repo"]._host_aliases == {"x": "y"}
    assert seen["repo"].inspector.limits["max_sessions"] == 9
    assert seen["server_kw"]["ci_actions"] is True


def test_web_without_file_has_no_durable_config_even_with_env(tmp_path, monkeypatch, old_env, capsys):
    seen = _run_web(monkeypatch, tmp_path / "missing.yaml")
    assert "sink_dsn" not in seen and seen["self_report"]["dsn"] is None
    assert seen["self_report"]["rustfs_endpoint"] is None
    assert seen["self_report"]["host_id"] != "env-self"
    assert seen["server_kw"]["ci_actions"] is False


# ------------------------------------------------------------- collab-only (public)

COLLAB = {"cognito_pool": "us-west-2_TESTPOOL", "cognito_clients": ["client-a"],
          "cognito_group": "team.test", "cognito_web_client": "web-client",
          "cognito_domain": "https://pool.auth.example.test", "public_url": "https://collab.example.test/"}


def _collab_slice(tmp_path, collab=None):
    central = make_file(tmp_path, collab=dict(collab or COLLAB))
    out = tmp_path / "slice" / "collab-config.json"
    fcc.render_collab(fcc.load_fleet_config(central), out)
    return central, out


def _collab_only_args(tmp_path, log=None):
    return dict(collab_only=True, collab_public_port=18081, collab_public_host="127.0.0.1",
                collab_log_file=str(log or tmp_path / "collab.jsonl"),
                collab_sync_dir=None, allow_open=False)


def test_render_collab_slice_holds_only_the_collab_mapping(tmp_path):
    central, out = _collab_slice(tmp_path)
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    text = out.read_text()
    data = yaml.safe_load(text)
    assert data["sections"]["control_plane"]["values"] == {"collab": COLLAB}
    assert data["hosts"] == {}
    cp = yaml.safe_load(central.read_text())["sections"]["control_plane"]["values"]
    for secret in (cp["pg_dsn"], cp["minio_password"], *cp["api_keys"]):
        assert secret not in text


def test_render_collab_refuses_missing_or_unknown_keys(tmp_path):
    central = make_file(tmp_path)
    with pytest.raises(fcc.FleetConfigError, match="control_plane.collab is not set"):
        fcc.render_collab(fcc.load_fleet_config(central), tmp_path / "x.json")
    central.unlink()
    central = make_file(tmp_path, collab=dict(COLLAB, client_secret="s3cret"))
    with pytest.raises(fcc.FleetConfigError, match="unknown keys: client_secret"):
        fcc.render_collab(fcc.load_fleet_config(central), tmp_path / "x.json")


def test_collab_keys_match_the_cli_mapping():
    assert set(cli.COLLAB_CONFIG_KEYS) == set(fcc.COLLAB_KEYS)


def test_collab_only_reads_cognito_settings_from_the_slice(tmp_path, monkeypatch, old_env, capsys):
    import herdr_engineering.control_plane.collab_cognito as cc
    seen_auth = {}
    real = cc.CognitoAuthenticator

    def spy(**kw):
        seen_auth.update(kw)
        return real(**kw)
    monkeypatch.setattr(cc, "CognitoAuthenticator", spy)
    monkeypatch.setattr(cc, "boto_group_lookup", lambda *a: (lambda *b: None))
    _, out = _collab_slice(tmp_path)
    seen = _run_web(monkeypatch, out, **_collab_only_args(tmp_path))
    assert seen_auth["pool_id"] == "us-west-2_TESTPOOL"
    assert seen_auth["client_ids"] == ["client-a", "web-client"]
    assert seen_auth["required_group"] == "team.test"
    kw = seen["server_kw"]
    assert kw["collab_only"] is True
    assert kw["fleet_config"] is None              # no fleet endpoint on the public container
    assert kw["collab"].public_config == {"client_id": "web-client",
                                          "hosted_ui_domain": "https://pool.auth.example.test",
                                          "public_url": "https://collab.example.test/"}
    assert "self_report" not in seen and "sink_dsn" not in seen


def test_collab_only_flags_override_the_slice(tmp_path, monkeypatch, old_env, capsys):
    import herdr_engineering.control_plane.collab_cognito as cc
    seen_auth = {}
    real = cc.CognitoAuthenticator
    monkeypatch.setattr(cc, "CognitoAuthenticator", lambda **kw: seen_auth.update(kw) or real(**kw))
    monkeypatch.setattr(cc, "boto_group_lookup", lambda *a: (lambda *b: None))
    _, out = _collab_slice(tmp_path)
    _run_web(monkeypatch, out, collab_cognito_pool="us-west-2_FLAG", collab_cognito_group="flag.group",
             **_collab_only_args(tmp_path))
    assert seen_auth["pool_id"] == "us-west-2_FLAG" and seen_auth["required_group"] == "flag.group"


def test_collab_only_refuses_the_central_file(tmp_path, monkeypatch, old_env, capsys):
    central = make_file(tmp_path, collab=dict(COLLAB))
    with pytest.raises(SystemExit, match="must not be given database credentials"):
        _run_web(monkeypatch, central, **_collab_only_args(tmp_path))


def test_collab_only_without_a_pool_fails_and_ignores_env(tmp_path, monkeypatch, old_env, capsys):
    monkeypatch.setenv("HERDR_COLLAB_POOL", "us-west-2_ENV")
    with pytest.raises(SystemExit, match="Cognito pool"):
        _run_web(monkeypatch, tmp_path / "missing.json", **_collab_only_args(tmp_path))


def test_dashboard_ignores_control_plane_collab(tmp_path, monkeypatch, old_env, capsys):
    central = make_file(tmp_path, collab=dict(COLLAB))
    seen = _run_web(monkeypatch, central)
    assert seen["server_kw"]["collab"] is None


# ------------------------------------------------------------------- llm proxy

def test_proxy_limits_from_arguments_not_env(old_env):
    from herdr_engineering.llm_proxy import DEFAULT_MAX_INFLIGHT, ControlPlaneClient, ProxyState
    st = ProxyState(ControlPlaneClient("http://x"), "http://u", "k")
    assert st.max_inflight == DEFAULT_MAX_INFLIGHT
    st = ProxyState(ControlPlaneClient("http://x"), "http://u", "k", max_inflight=3, wait_seconds=2.5)
    assert (st.max_inflight, st.wait_seconds) == (3, 2.5)


def _proxy_args(path, **kw):
    base = dict(host="127.0.0.1", port=0, control_plane="http://cp", upstream=None,
                fleet_config=str(path))
    base.update(kw)
    return argparse.Namespace(**base)


def test_llm_proxy_command_reads_key_url_and_tuning_from_fleet_file(tmp_path, monkeypatch, old_env):
    path = make_file(tmp_path, llm_proxy={"max_inflight": 4, "queue_wait_seconds": 1.5})
    seen = {}

    class Httpd:
        server_address = ("127.0.0.1", 1)

        def serve_forever(self):
            raise KeyboardInterrupt
    import herdr_engineering.llm_proxy as lp
    monkeypatch.setattr(lp, "serve", lambda *a, **kw: (seen.update(a=a, kw=kw), Httpd())[1])
    assert cli._cmd_cp_llm_proxy(_proxy_args(path)) == 0
    assert seen["a"][3] == "https://llm.example.test" and seen["a"][4] == "llm-secret-key"
    assert seen["kw"] == {"max_inflight": 4, "wait_seconds": 1.5}


def test_llm_proxy_command_requires_a_fleet_file(tmp_path, capsys, old_env):
    assert cli._cmd_cp_llm_proxy(_proxy_args(tmp_path / "missing.yaml")) == 2
    assert "env-key" not in capsys.readouterr().err
