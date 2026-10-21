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
    "llm_upstream_key": "placeholder-llm-value", "ci_url": "https://ci.example.test",
    "ci_token": "placeholder-ci-value", "web_bind": "127.0.0.1", "web_port": "8080",
    "hosts": {"bender": ["builder"]},
}

OLD_ENV = {
    "HERDR_CP_PG_DSN": f"postgresql://{'envuser'}:{'envpass'}@envhost/envdb", "HERDR_CP_PG_USER": "envuser",
    "HERDR_CP_PG_PASSWORD": "envpass", "HERDR_CP_HOST_ALIASES": "a=b",
    "HERDR_CP_SELF_HOST": "env-self", "HERDR_CP_WEB_PORT": "1111",
    "HERDR_CP_RUSTFS_ENDPOINT": "http://env:9", "HERDR_CP_MINIO_USER": "envminio",
    "HERDR_CP_MINIO_PASSWORD": "envminiopw", "HERDR_ENGINEERING_CI_ACTIONS": "1",
    "HERDR_INSPECTOR_MAX_EVENTS": "7", "HERDR_INSPECTOR_STORE_REASONING": "0",
    "HERDR_INSPECTOR_REASONING_ROLE": "viewer", "HERDR_LLM_PROXY_MAX_INFLIGHT": "1",
    "HERDR_LLM_PROXY_QUEUE_WAIT_SECONDS": "0.1", "FABRIC_LLM_UPSTREAM_KEY": "placeholder-env-value",
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
    c = db.control_plane_config({"control_plane": {"postgres_dsn": f"postgresql://{'dbuser'}:{'dbvalue'}@postgres:5432/herdr"}})
    assert c["postgres_dsn"] == f"postgresql://{'dbuser'}:{'dbvalue'}@postgres:5432/herdr"


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


def _run_web(monkeypatch, path):
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
                              collab_cognito_group=None)
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
    assert seen["a"][3] == "https://llm.example.test" and seen["a"][4] == "placeholder-llm-value"
    assert seen["kw"] == {"max_inflight": 4, "wait_seconds": 1.5}


def test_llm_proxy_command_requires_a_fleet_file(tmp_path, capsys, old_env):
    assert cli._cmd_cp_llm_proxy(_proxy_args(tmp_path / "missing.yaml")) == 2
    assert "placeholder-env-value" not in capsys.readouterr().err
