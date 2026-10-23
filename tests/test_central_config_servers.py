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



def _url(hostport, database, user, pw):
    """A postgres URL assembled from parts so no credential-shaped literal sits in the source."""
    return f"postgresql://{user}:{pw}@{hostport}/{database}"


ANSWERS = {
    "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
    "llm_upstream_key": "placeholder-llm-value", "ci_url": "https://ci.example.test",
    "ci_token": "placeholder-ci-value", "web_bind": "127.0.0.1", "web_port": "8080", "gateway_tailnet_ip": "127.0.0.1",
    "hosts": {"bender": ["builder"]},
}

OLD_ENV = {
    "HERDR_CP_PG_DSN": _url("envhost", "envdb", "envuser", "envpass"), "HERDR_CP_PG_USER": "envuser",
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
    c = db.control_plane_config({"control_plane": {"postgres_dsn": _url("postgres:5432", "herdr", "dbuser", "dbvalue")}})
    assert c["postgres_dsn"] == _url("postgres:5432", "herdr", "dbuser", "dbvalue")


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
                              collab_public_url=None, collab_operator_port=None,
                              collab_operator_user=None, collab_operator_email=None,
                              collab_operator_host=[], collab_operator_url=None,
                              collab_dashboard_host=[], collab_open_signup=False,
                              collab_approver_group=[], collab_delegation_enabled=False)
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
          "cognito_domain": "https://pool.auth.example.test", "public_url": "https://collab.example.test/",
          "operator_user": "owner-test", "operator_email": "owner@example.test"}


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


def test_collab_only_reads_the_operator_user_from_the_slice(tmp_path, monkeypatch, old_env, capsys):
    # #111 (second origin/main merge): the operator identity is configuration, so it comes from
    # control_plane.collab via the slice, not from HERDR_COLLAB_OPERATOR_USER in an env file.
    import herdr_engineering.control_plane.collab_cognito as cc
    monkeypatch.setattr(cc, "boto_group_lookup", lambda *a: (lambda *b: None))
    monkeypatch.setenv("HERDR_COLLAB_OPERATOR_USER", "env-user")
    _, out = _collab_slice(tmp_path)
    seen = _run_web(monkeypatch, out, collab_operator_port=0,
                    collab_operator_host=["100.64.0.1:9482"], **_collab_only_args(tmp_path))
    kw = seen["server_kw"]
    assert kw["operator_user"] == "owner-test" and kw["operator_email"] == "owner@example.test"
    assert kw["operator_hosts"] == ["100.64.0.1:9482"]


def test_render_collab_requires_the_operator_user(tmp_path):
    central = make_file(tmp_path, collab={k: v for k, v in COLLAB.items() if k != "operator_user"})
    with pytest.raises(fcc.FleetConfigError, match="operator_user is required"):
        fcc.render_collab(fcc.load_fleet_config(central), tmp_path / "x.json")


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
    slice_ = tmp_path / "llm-proxy" / "llm-config.json"
    fcc.render_llm(fcc.load_fleet_config(path), slice_)
    assert cli._cmd_cp_llm_proxy(_proxy_args(slice_)) == 0
    assert seen["a"][3] == "https://llm.example.test" and seen["a"][4] == "placeholder-llm-value"
    assert seen["kw"] == {"max_inflight": 4, "wait_seconds": 1.5}


def test_llm_proxy_command_requires_a_fleet_file(tmp_path, capsys, old_env):
    assert cli._cmd_cp_llm_proxy(_proxy_args(tmp_path / "missing.yaml")) == 2
    assert "placeholder-env-value" not in capsys.readouterr().err


# ------------------------------------------- C1: init output keeps the open posture

from herdr_engineering.control_plane.api import ControlPlaneServer as _RealServer  # noqa: E402


def _serve_from_init(monkeypatch, tmp_path, **cp_extra):
    """Build the web server exactly as `control-plane web --fleet-config` would
    from an `init` file (the CLI's api_keys merge included), then serve it for real."""
    import json as _json
    import urllib.error
    import urllib.request
    path = make_file(tmp_path, **cp_extra)
    seen = _run_web(monkeypatch, path, host="127.0.0.1", port=0)
    svr = _RealServer(seen["repo"], **seen["server_kw"])
    t = threading.Thread(target=svr.serve, daemon=True)
    t.start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"

    def call(method, route, body=None, headers=None):
        data = _json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(base + route, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    return svr, call


def test_server_built_from_init_output_needs_no_api_key(tmp_path, monkeypatch, old_env, capsys):
    svr, call = _serve_from_init(monkeypatch, tmp_path)
    try:
        assert call("GET", "/api/v1/health")[0] == 200
        assert call("GET", "/")[0] == 200
        assert call("GET", "/api/v1/llm/config")[0] == 200
        status, _ = call("POST", "/api/v1/token-usage",
                         {"project": "p", "model": "m", "input_tokens": 1, "output_tokens": 1})
        assert status == 200
    finally:
        svr.shutdown()


def test_keyed_mode_still_serves_a_minimal_health_without_a_key(tmp_path, monkeypatch, old_env, capsys):
    import json as _json
    key = "placeholder-operator-one"
    svr, call = _serve_from_init(monkeypatch, tmp_path, api_keys={key: "operator"})
    try:
        status, body = call("GET", "/api/v1/health")
        assert status == 200
        assert set(_json.loads(body)) == {"status", "version", "commit"}
        assert call("GET", "/api/v1/missions")[0] == 401
        status, body = call("GET", "/api/v1/health", headers={"X-API-Key": key})
        assert status == 200 and "capabilities" in _json.loads(body)
        # a wrong key gets the minimal view, not an error and not the full payload
        status, body = call("GET", "/api/v1/health", headers={"X-API-Key": "wrong"})
        assert status == 200 and set(_json.loads(body)) == {"status", "version", "commit"}
    finally:
        svr.shutdown()


# ------------------------------------------------- I4: the llm-proxy gets a slice

def _all_strings(node):
    """Values plus mapping keys that are values (api_keys maps key -> role)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and v in ("operator", "ingest", "agent", "viewer"):
                yield k
            yield from _all_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _all_strings(v)
    elif isinstance(node, str):
        yield node


def test_llm_slice_carries_no_dsn_api_keys_or_tokens(tmp_path):
    import os
    import stat
    path = fcc.init_config(tmp_path / "fleet" / "fleet-config.yaml",
                           dict(ANSWERS, github_token="placeholder-gh-value", fabric_token="placeholder-fab-value"),
                           with_api_keys=True)
    data = yaml.safe_load(path.read_text())
    data["sections"]["control_plane"]["values"]["llm_proxy"] = {"max_inflight": 4}
    path.unlink()
    fcc.write_private_file(path, yaml.safe_dump(data, sort_keys=False))
    out = tmp_path / "llm-proxy" / "llm-config.json"
    fcc.render_llm(fcc.load_fleet_config(path), out)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    sl = yaml.safe_load(out.read_text())
    assert set(sl["sections"]) == {"llm", "control_plane"} and sl["hosts"] == {}
    assert sl["sections"]["control_plane"]["values"] == {"llm_proxy": {"max_inflight": 4}}
    assert sl["sections"]["llm"]["values"] == data["sections"]["llm"]["values"]
    text = out.read_text()
    llm_values = set(_all_strings(data["sections"]["llm"]["values"]))
    secrets = {s for name, sec in data["sections"].items() if name != "llm"
               for s in _all_strings(sec["values"]) if len(s) >= 12 and s not in llm_values}
    secrets |= {h["token_sha256"] for h in data["hosts"].values()}
    assert data["sections"]["control_plane"]["values"]["pg_dsn"] in secrets
    assert any(k in secrets for k in data["sections"]["control_plane"]["values"]["api_keys"])
    for s in secrets:
        assert s not in text


def test_llm_proxy_refuses_the_central_file(tmp_path, capsys, old_env):
    path = make_file(tmp_path)
    assert cli._cmd_cp_llm_proxy(_proxy_args(path)) == 2
    err = capsys.readouterr().err
    assert "render-llm" in err and "placeholder-llm-value" not in err


def test_llm_proxy_refuses_a_slice_with_extra_control_plane_keys(tmp_path, capsys, old_env):
    import json as _json
    path = make_file(tmp_path)
    out = tmp_path / "s" / "llm-config.json"
    fcc.render_llm(fcc.load_fleet_config(path), out)
    data = _json.loads(out.read_text())
    data["sections"]["control_plane"] = {"roles": ["control-plane"],
                                         "values": {"llm_proxy": {}, "pg_dsn": "postgresql://x"}}
    out.unlink()
    fcc.write_private_file(out, _json.dumps(data))
    assert cli._cmd_cp_llm_proxy(_proxy_args(out)) == 2


def test_approval_settings_come_from_the_collab_slice():
    import argparse
    args = argparse.Namespace(collab_open_signup=False, collab_approver_group=[])
    cli._apply_collab_config(args, {"collab": {"open_signup": True, "approver_groups": ["Administrators", "Managers"]}})
    assert args.collab_open_signup is True and args.collab_approver_group == ["Administrators", "Managers"]
    args = argparse.Namespace(collab_open_signup=False, collab_approver_group=["Flag"])
    cli._apply_collab_config(args, {"collab": {"approver_groups": ["Administrators"]}})
    assert args.collab_approver_group == ["Flag"]                    # flags win
    with pytest.raises(SystemExit):
        cli._apply_collab_config(argparse.Namespace(collab_open_signup=False), {"collab": {"open_signup": "yes"}})


def test_dashboard_host_names_are_a_collab_config_key():
    import argparse
    args = argparse.Namespace(collab_dashboard_host=[])
    cli._apply_collab_config(args, {"collab": {"dashboard_hosts": ["dev.lan"]}})
    assert args.collab_dashboard_host == ["dev.lan"]
    assert "dashboard_hosts" in fcc.COLLAB_KEYS


def test_lineage_key_scopes_reach_the_server_and_bad_entries_stop_startup(tmp_path, monkeypatch, old_env):
    ing, op = "in-" + "k1", "op-" + "k1"
    keys = {ing: "ingest", op: "operator"}
    ok = make_file(tmp_path, api_keys=keys, lineage_key_scopes={ing: {"rooms": ["r1"], "missions": ["m1"]}})
    seen = _run_web(monkeypatch, ok)
    assert seen["server_kw"]["lineage_key_scopes"] == {ing: {"rooms": ["r1"], "missions": ["m1"]}}
    for name, bad in (("operator", {op: {"rooms": ["r1"]}}), ("unknown", {"no-" + "such": {"rooms": ["r1"]}}),
                      ("shape", {ing: {"rooms": "r1"}}), ("extra", {ing: {"admin": True}})):
        d = tmp_path / name
        d.mkdir()
        with pytest.raises(SystemExit) as e:
            _run_web(monkeypatch, make_file(d, api_keys=keys, lineage_key_scopes=bad))
        assert ing not in str(e.value) and op not in str(e.value), "the key is never echoed"
    d = tmp_path / "opmsg"
    d.mkdir()
    with pytest.raises(SystemExit) as e:
        _run_web(monkeypatch, make_file(d, api_keys=keys, lineage_key_scopes={op: {"rooms": ["r1"]}}))
    assert "operator" in str(e.value)


# ------------------------------------------------- T11 delegation kill switch (YAML only, default off)
def test_delegation_kill_switch_is_off_by_default_and_a_collab_yaml_key(tmp_path, monkeypatch, old_env):
    import herdr_engineering.control_plane.collab_cognito as cc
    monkeypatch.setattr(cc, "boto_group_lookup", lambda *a: (lambda *b: None))
    assert "delegation_enabled" in fcc.COLLAB_KEYS and "delegation_enabled" in cli.COLLAB_CONFIG_KEYS
    _, off = _collab_slice(tmp_path)
    assert _run_web(monkeypatch, off, **_collab_only_args(tmp_path))["server_kw"]["collab"].delegation_enabled is False
    d = tmp_path / "on"
    d.mkdir()
    _, on = _collab_slice(d, dict(COLLAB, delegation_enabled=True))
    assert _run_web(monkeypatch, on, **_collab_only_args(d))["server_kw"]["collab"].delegation_enabled is True
    d = tmp_path / "bad"
    d.mkdir()
    _, bad = _collab_slice(d, dict(COLLAB, delegation_enabled="yes"))
    with pytest.raises(SystemExit) as e:
        _run_web(monkeypatch, bad, **_collab_only_args(d))
    assert "delegation_enabled" in str(e.value)


def test_delegation_kill_switch_reaches_the_combined_server_too(tmp_path, monkeypatch, old_env):
    import json as _json
    users = tmp_path / "users.json"
    users.write_text(_json.dumps({"alice": "0" * 64}))
    for name, collab, want in (("unset", None, False), ("off", {"delegation_enabled": False}, False),
                               ("on", {"delegation_enabled": True}, True)):
        d = tmp_path / name
        d.mkdir()
        path = make_file(d, pg_dsn="", **({"collab": collab} if collab else {}))
        seen = _run_web(monkeypatch, path, collab_users_file=str(users))
        assert seen["server_kw"]["collab"].delegation_enabled is want, name
    d = tmp_path / "typo"
    d.mkdir()
    with pytest.raises(SystemExit) as e:
        _run_web(monkeypatch, make_file(d, pg_dsn="", collab={"delegation_enabled": "true"}), collab_users_file=str(users))
    assert "delegation_enabled" in str(e.value)
