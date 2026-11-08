"""Deploy wiring of the member artifacts host (Task 11): share.public_host, collab.members, the slices, redaction."""
import importlib.util
import json
import secrets
from pathlib import Path

import pytest
import yaml

from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("_members_share_deploy", ROOT / "tests" / "test_share_deploy_config.py")
_sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sd)
COLLAB, SECRET = _sd.COLLAB, _sd.SECRET
BRIDGE = {"collab_artifacts": {"enabled": True}}


def _central(tmp_path, share=None, collab=COLLAB, extra=BRIDGE):
    return _sd._central(tmp_path, share=share, collab=collab, extra=extra)

HOST = "artifacts.example.test"
MEMBER_SECRET = "m-" + secrets.token_hex(32)


def share(**over):
    return {"enabled": True, "signing_secret": SECRET, "public_host": HOST, **over}


def members(**over):
    return {"enabled": True, "group": "staff", "session_secret": MEMBER_SECRET, **over}


def cfg_of(tmp_path, *, sh=None, mem=None):
    collab = dict(COLLAB)
    if mem is not None:
        collab["members"] = mem
    return load_fleet_config(_central(tmp_path, share=sh, collab=collab))


def test_public_host_derives_the_share_origin_and_the_slice_carries_both(tmp_path):
    cfg = cfg_of(tmp_path, sh=share())
    values = fcc.share_values(cfg)
    assert values["share"]["public_host"] == HOST and values["share"]["public_url"] == f"https://{HOST}"
    out = tmp_path / "share.json"
    fcc.render_share(cfg, out)
    fcc.check_share_slice(load_fleet_config(out))
    assert "public_host" in fcc.SHARE_KEYS and "public_host" in fcc.SHARE_SLICE_KEYS


def test_a_public_url_that_disagrees_with_public_host_is_refused_and_an_equal_one_accepted(tmp_path):
    ok = cfg_of(tmp_path / "a", sh=share(public_url=f"https://{HOST}/"))
    assert fcc.share_values(ok)["share"]["public_url"] == f"https://{HOST}"
    with pytest.raises(FleetConfigError, match="public_url"):
        fcc.share_values(cfg_of(tmp_path / "b", sh=share(public_url="https://old.example.test/")))


@pytest.mark.parametrize("bad", ["Artifacts.example.test", "https://a.example.test", "a.example.test:8443",
                                 "a.example.test/x", "a.example.test.", "localhost", "", 5, "é.example.test"])
def test_bad_public_host_forms_are_refused(tmp_path, bad):
    with pytest.raises(FleetConfigError, match="public_host"):
        fcc.share_values(cfg_of(tmp_path, sh=share(public_host=bad)))


def test_the_share_host_may_not_be_the_collab_host(tmp_path):
    with pytest.raises(FleetConfigError, match="other than"):
        fcc.share_values(cfg_of(tmp_path, sh=share(public_host="mcp.example.test")))


def test_the_edge_serves_the_public_host(tmp_path):
    cfg = cfg_of(tmp_path, sh=share())
    out = tmp_path / "edge" / "nginx.conf"
    fcc.render_edge(cfg, _sd.EDGE, out)
    assert f"server_name {HOST};" in out.read_text()


# ---------------------------------------------------------------------------- collab.members


def test_enabled_members_validate_and_the_collab_slice_carries_the_host(tmp_path):
    cfg = cfg_of(tmp_path, sh=share(), mem=members(request_limits={"http_chunks_per_request": 4}))
    out = tmp_path / "collab.json"
    fcc.render_collab(cfg, out)
    rendered = json.loads(out.read_text())["sections"]["control_plane"]["values"]["collab"]["members"]
    assert rendered["public_host"] == HOST and rendered["session_secret"] == MEMBER_SECRET
    assert rendered["request_limits"] == {"http_chunks_per_request": 4}


def test_a_disabled_members_section_renders_unchanged_without_a_host(tmp_path):
    cfg = cfg_of(tmp_path, mem={"enabled": False, "group": "g"})
    values = fcc.collab_values(cfg)
    assert values["members"] == {"enabled": False, "group": "g"}


@pytest.mark.parametrize("bad", [{"session_secret": None}, {"session_secret": "short"}, {"limits": {}},
                                 {"nonsense": 1}, {"public_host": "other.example.test"},
                                 {"share_creates_per_hour": 0}, {"rate_limits": {"writes_per_minute": 0}}])
def test_bad_members_sections_are_refused_without_echoing_the_secret(tmp_path, bad):
    with pytest.raises(FleetConfigError, match="collab.members") as err:
        fcc.collab_values(cfg_of(tmp_path, sh=share(), mem=members(**bad)))
    assert MEMBER_SECRET not in str(err.value)


def test_enabled_members_without_a_share_host_are_refused(tmp_path):
    with pytest.raises(FleetConfigError, match="public_host"):
        fcc.collab_values(cfg_of(tmp_path, mem=members()))


def test_the_share_slice_never_carries_the_member_secret(tmp_path):
    cfg = cfg_of(tmp_path, sh=share(), mem=members())
    out = tmp_path / "share.json"
    fcc.render_share(cfg, out)
    assert MEMBER_SECRET not in out.read_text()


def test_fleet_config_commands_never_print_the_member_secret(tmp_path, capsys):
    from herdr_engineering import cli
    central = _central(tmp_path, share=share(), collab={**COLLAB, "members": members()})
    import argparse
    for cmd, extra in (("validate", {}), ("render-collab", {"out": str(tmp_path / "c.json")}),
                       ("render-share", {"out": str(tmp_path / "s.json")})):
        rc = cli._cmd_fleet_config(argparse.Namespace(fleet_config_command=cmd, path=str(central), **extra))
        assert rc == 0
    captured = capsys.readouterr()
    assert MEMBER_SECRET not in captured.out + captured.err
    assert SECRET not in captured.out + captured.err
    # and a refused file's error never echoes it
    bad = _central(tmp_path / "bad", share=share(), collab={**COLLAB, "members": members(nonsense=MEMBER_SECRET)})
    rc = cli._cmd_fleet_config(argparse.Namespace(fleet_config_command="render-collab", path=str(bad),
                                                  out=str(tmp_path / "x.json")))
    captured = capsys.readouterr()
    assert rc == 1 and MEMBER_SECRET not in captured.out + captured.err


def test_central_file_yaml_round_trips(tmp_path):
    central = _central(tmp_path, share=share(), collab={**COLLAB, "members": members()})
    assert yaml.safe_load(central.read_text())


def test_member_locations_are_in_the_edge_only_when_members_are_enabled():
    from pathlib import Path

    from herdr_engineering import fleet_config_cli as fcc
    edge = Path(fcc.__file__).resolve().parents[1] / "deploy" / "control-plane" / "collab-edge" / "nginx.conf"
    off = fcc.render_edge_conf(edge.read_text(), share=True, share_host="artifacts.example.test")
    on = fcc.render_edge_conf(edge.read_text(), share=True, share_host="artifacts.example.test", members=True)
    assert "/api/member/" not in off and "@member-locations@" not in off
    assert "location /api/member/ {" in on and "location = / {" in on and "location /my/ {" in on
    assert "proxy_request_buffering off;" in on and "client_max_body_size 17m;" in on
    assert on.count("location / { return 404; }") == off.count("location / { return 404; }")


# ---------------------------------------------------------------------------- fix round 1: config validation


def test_the_slice_carries_the_session_lifetime_only_when_the_share_sets_it(tmp_path):
    out = tmp_path / "a.json"
    fcc.render_collab(cfg_of(tmp_path / "a", sh=share(session_seconds=900), mem=members()), out)
    assert json.loads(out.read_text())["sections"]["control_plane"]["values"]["collab"]["members"]["session_seconds"] == 900
    out2 = tmp_path / "b.json"
    fcc.render_collab(cfg_of(tmp_path / "b", sh=share(), mem=members()), out2)
    assert "session_seconds" not in json.loads(out2.read_text())["sections"]["control_plane"]["values"]["collab"]["members"]


@pytest.mark.parametrize("bad", [{"session_seconds": 900}, {"public_host": HOST}])
def test_the_slice_only_keys_are_not_authored_under_members(tmp_path, bad):
    with pytest.raises(FleetConfigError, match="not authored here"):
        fcc.collab_values(cfg_of(tmp_path, sh=share(), mem=members(**bad)))


@pytest.mark.parametrize("bad", [59, True, "900", 10**9])
def test_a_bad_session_lifetime_is_refused_for_members(tmp_path, bad):
    with pytest.raises(FleetConfigError, match="session_seconds"):
        fcc.collab_values(cfg_of(tmp_path, sh=share(session_seconds=bad), mem=members()))


def test_a_disabled_members_section_is_validated_too(tmp_path):
    for bad in ({"enabled": False, "nonsense": 1}, {"enabled": False, "session_secret": "short"},
                {"enabled": False, "group": 5}, {"enabled": False, "rate_limits": {"writes_per_minute": 0}},
                {"enabled": False, "request_limits": {"http_chunks_per_request": 33}}):
        with pytest.raises(FleetConfigError, match="collab.members"):
            fcc.collab_values(cfg_of(tmp_path / str(len(str(bad))), sh=share(), mem=bad))
    ok = {"enabled": False, "group": "g", "request_limits": {"http_chunks_per_request": 32}}
    assert fcc.collab_values(cfg_of(tmp_path / "ok", mem=ok))["members"] == ok


@pytest.mark.parametrize("key,sect,value", [("http_chunks_per_request", "request_limits", 33),
                                            ("max_publish_files", "request_limits", 10**6),
                                            ("max_message_chars", "request_limits", 10**6),
                                            ("chunks_per_minute", "rate_limits", 10**7),
                                            ("writes_per_minute", "rate_limits", 10**6),
                                            ("session_exchanges_per_minute", "rate_limits", 10**6),
                                            ("max_upload_bytes", "request_limits", 1 << 41),
                                            ("staging_bytes_per_member", "staging", 1 << 41)])
def test_member_limits_have_upper_bounds_at_config_load(tmp_path, key, sect, value):
    with pytest.raises(FleetConfigError, match=key):
        fcc.collab_values(cfg_of(tmp_path, sh=share(), mem=members(**{sect: {key: value}})))


def test_share_creates_per_hour_has_an_upper_bound(tmp_path):
    with pytest.raises(FleetConfigError, match="share_creates_per_hour"):
        fcc.collab_values(cfg_of(tmp_path, sh=share(), mem=members(share_creates_per_hour=10**6)))


def test_the_member_host_may_not_be_the_mcp_host_even_with_share_disabled(tmp_path):
    for sh in ({"enabled": False, "public_host": "mcp.example.test"}, share(public_host="mcp.example.test")):
        with pytest.raises(FleetConfigError, match="other than"):
            fcc.collab_values(cfg_of(tmp_path / str(len(sh)), sh=sh, mem=members()))
    with pytest.raises(FleetConfigError, match="other than"):                  # members off, host still set
        fcc.collab_values(cfg_of(tmp_path / "off", sh={"enabled": False, "public_host": "mcp.example.test"}))


@pytest.mark.parametrize("bad", ["10.0.0.1", "127.0.0.1", "a.b.7"])
def test_an_ip_literal_is_not_a_public_host(tmp_path, bad):
    with pytest.raises(FleetConfigError, match="public_host"):
        fcc.share_values(cfg_of(tmp_path, sh=share(public_host=bad)))


# ---------------------------------------------------------------------------- Task 13: the artifacts host on the edge

EDGE_DIR = ROOT / "deploy" / "control-plane" / "collab-edge"


def _edge(**kw):
    return fcc.render_edge_conf((EDGE_DIR / "nginx.conf").read_text(), snippets_dir=EDGE_DIR, **kw)


def _block(text, header):
    """The brace-balanced block that starts at ``header`` (a line prefix, or an index), as text."""
    start = header if isinstance(header, int) else text.index(header)
    depth, i = 0, text.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i + 1]
        i += 1


def _server(text, name):
    at = text.index(f"server_name {name};")
    return _block(text, text.rfind("  server {", 0, at))


def test_without_a_public_host_the_edge_is_the_template_byte_for_byte():
    template = (EDGE_DIR / "nginx.conf").read_text()
    assert _edge(share=False) == template


def test_with_a_public_host_exactly_one_server_name_is_added():
    off = _edge(share=False)
    on = _edge(share=True, share_host=HOST, members=True)
    assert on.count("server_name ") == off.count("server_name ") + 1
    assert on.count(f"server_name {HOST};") == 1


def test_the_old_host_has_no_share_location_and_the_mcp_host_keeps_mcp():
    on = _edge(share=True, share_host=HOST, members=True)
    mcp_servers = [s for s in (_server(on, n) for n in _server_names(on) if n != HOST)]
    assert mcp_servers
    for s in mcp_servers:
        assert "/s/" not in s
    assert any("location = /mcp" in s for s in mcp_servers)
    artifacts = _server(on, HOST)
    assert "/mcp" not in artifacts and "/a2a" not in artifacts and "/api/v1/collab" not in artifacts


def _server_names(text):
    import re
    return re.findall(r"^\s+server_name ([^;]+);", text, re.M)


def test_the_artifacts_host_locations_limits_and_logs():
    on = _edge(share=True, share_host=HOST, members=True)
    s = _server(on, HOST)
    for loc in ("location /s/ {", "location /api/member/ {", "location = / {", "location /my/ {",
                "location / { return 404; }"):
        assert loc in s
    api = _block(s, "    location /api/member/ {")
    run = _block(api, '      location ~ "^/api/member/artifacts/')
    # only the run upload is large and streamed; everything else under the API is 1 MiB and buffered
    assert "client_max_body_size 17m;" in run and "proxy_request_buffering off;" in run
    assert "client_max_body_size 1m;" in api.replace(run, "") and "17m" not in api.replace(run, "")
    assert "proxy_request_buffering off" not in api.replace(run, "")
    assert "PUT" in run
    # no access log where the token or the session travels
    for block in (_block(s, "    location /s/ {"), api.replace(run, ""), run, _block(s, "    location /my/ {"),
                  _block(s, "    location = / {")):
        assert "access_log off;" in block
    # its own request and connection zones, not the share ones
    assert "zone=member_req" in api and "limit_conn member_conn" in api
    assert "zone=member_req" in run and "limit_conn member_conn" in run
    assert "limit_req_zone $remote_addr zone=member_req:" in on and "limit_conn_zone $remote_addr zone=member_conn:" in on
    # framing and HSTS reach the browser from the API and the page
    for block in (api, _block(s, "    location /my/ {"), _block(s, "    location = / {")):
        assert "proxy_hide_header X-Frame-Options;" in block and 'add_header X-Frame-Options "DENY" always;' in block
        assert 'add_header Strict-Transport-Security "max-age=31536000" always;' in block


def test_member_zones_exist_only_with_members_and_the_share_listener_deny_is_unchanged():
    off = _edge(share=True, share_host=HOST, members=False)
    on = _edge(share=True, share_host=HOST, members=True)
    assert "member_req" not in off and "member_conn" not in off and "/api/member/" not in off
    # the operator listener (the share network is refused there) is identical in every rendering
    deny = [line for line in (EDGE_DIR / "nginx.conf").read_text().splitlines() if "deny" in line]
    assert deny
    for text in (off, on):
        for line in deny:
            assert line in text.splitlines()
    assert on.count("deny ") == off.count("deny ") == (EDGE_DIR / "nginx.conf").read_text().count("deny ")


def _docker_nginx():
    import shutil
    import subprocess
    if not shutil.which("docker"):
        return None
    for image in ("nginx:1.29-alpine", "nginx:alpine"):
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
            return image
    return None


@pytest.mark.skipif(_docker_nginx() is None, reason="no docker nginx image: nginx -t not run")
@pytest.mark.parametrize("members", [False, True])
def test_the_rendered_edge_passes_nginx_t(tmp_path, members):
    import subprocess
    (tmp_path / "acme").mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "acme/privkey.pem"),
                    "-out", str(tmp_path / "acme/fullchain.pem"), "-days", "1", "-subj", "/CN=t"],
                   check=True, capture_output=True)
    (tmp_path / "nginx.conf").write_text(_edge(share=True, share_host=HOST, members=members))
    out = subprocess.run(["docker", "run", "--rm", "--add-host", "collab-public:127.0.0.1", "--add-host",
                          "share-public:127.0.0.1", "-v", f"{tmp_path}/nginx.conf:/etc/nginx/nginx.conf:ro",
                          "-v", f"{tmp_path}/acme:/etc/acme:ro", _docker_nginx(), "nginx", "-t"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "test is successful" in out.stderr


def test_the_run_upload_location_has_the_raw_path_guard_and_a_strict_anchor():
    run = _block(_block(_server(_edge(share=True, share_host=HOST, members=True), HOST), "    location /api/member/ {"),
                 '      location ~ "^/api/member/artifacts/')
    assert "if ($share_raw_path_ok = 0) { return 404; }" in run
    assert '\\z"' in run.splitlines()[0] and "$\"" not in run.splitlines()[0]


@pytest.mark.skipif(_docker_nginx() is None, reason="no docker nginx image")
@pytest.mark.parametrize("path,status", [
    ("/api/member/x/../artifacts/a/uploads/b/runs/1", 404),
    ("/api/member//artifacts/a/uploads/b/runs/1", 404),
    ("/api/member/artifacts/a/uploads/b/runs/1%0A", 413),       # not the run location: the 1 MiB parent limit applies
    ("/api/member/artifacts/a/uploads/b/runs/1", 200)])         # a declared 2 MiB body passes only on the run location
def test_run_upload_raw_path_shapes_on_a_real_nginx(tmp_path, path, status):
    import subprocess
    import time
    (tmp_path / "acme").mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "acme/privkey.pem"),
                    "-out", str(tmp_path / "acme/fullchain.pem"), "-days", "1", "-subj", "/CN=t"],
                   check=True, capture_output=True)
    conf = _edge(share=True, share_host=HOST, members=True)
    # a plain listener standing in for the edge's proxy_protocol/SNI front, and a stub upstream that answers 200
    conf = conf.replace("ssl proxy_protocol", "ssl").replace(
        "if ($hcn_from_edge = 0) { return 444; }", "")
    conf = conf.replace("server collab-public:8081;", "server 127.0.0.1:9;")
    conf = conf.replace("proxy_pass http://collab;", "return 200;")
    (tmp_path / "nginx.conf").write_text(conf)
    name = f"nginx-t13-{tmp_path.name[-8:]}"
    try:
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::8443", "--add-host",
                        "collab-public:127.0.0.1", "--add-host", "share-public:127.0.0.1",
                        "-v", f"{tmp_path}/nginx.conf:/etc/nginx/nginx.conf:ro",
                        "-v", f"{tmp_path}/acme:/etc/acme:ro", _docker_nginx()], check=True, capture_output=True)
        port = int(subprocess.run(["docker", "port", name, "8443"], capture_output=True, text=True,
                                  check=True).stdout.split(":")[-1])
        import socket
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        deadline = time.monotonic() + 15           # wait for nginx to answer a TLS handshake rather than sleeping a fixed time
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2) as probe, \
                        ctx.wrap_socket(probe, server_hostname=HOST):
                    break
            except OSError:
                assert time.monotonic() < deadline, "nginx never started listening"
                time.sleep(0.1)
        raw = (f"PUT {path} HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 2097152\r\nConnection: close\r\n\r\nx").encode()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock, \
                ctx.wrap_socket(sock, server_hostname=HOST) as tls:
            tls.sendall(raw)
            got = tls.recv(200).decode("latin-1")
        assert got.startswith(f"HTTP/1.1 {status} "), got
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# ----------------------------------------------------- members.enabled prerequisites (final review A2, security F5)
def test_enabled_members_need_the_share_the_bridge_and_api_keys_with_the_keys_named(tmp_path):
    def load(sub, **kw):
        return load_fleet_config(_central(tmp_path / sub, collab={**COLLAB, "members": members()}, **kw))

    with pytest.raises(FleetConfigError, match=r"control_plane\.share\.enabled"):
        fcc.collab_values(load("a", share={**share(), "enabled": False}))
    with pytest.raises(FleetConfigError, match=r"control_plane\.collab_artifacts\.enabled"):
        fcc.collab_values(load("b", share=share(), extra={}))
    with pytest.raises(FleetConfigError, match=r"control_plane\.collab_artifacts\.enabled"):
        fcc.collab_values(load("c", share=share(), extra={"collab_artifacts": {"enabled": "true"}}))
    with pytest.raises(FleetConfigError, match=r"control_plane\.api_keys"):
        fcc.collab_values(load("d", share=share(), extra={**BRIDGE, "api_keys": {}}))
    assert fcc.collab_values(load("ok", share=share()))["members"]["enabled"] is True


def test_the_artifact_host_path_needs_no_legacy_member_prerequisites(tmp_path):
    """With control_plane.artifact_host enabled the host owns the member web: no share, bridge or api_keys needed."""
    host_on = {"artifact_host": {"enabled": True}, "api_keys": {}}
    cfg = load_fleet_config(_central(tmp_path / "host", collab={**COLLAB, "members": members()}, extra=host_on))
    assert fcc.collab_values(cfg)["members"]["enabled"] is True      # no share, no bridge, no api_keys: accepted
    out = tmp_path / "collab.json"
    fcc.render_collab(cfg, out)
    assert "members" not in json.loads(out.read_text())["sections"]["control_plane"]["values"]["collab"]
    # the legacy (share-public) path keeps every prerequisite
    legacy = load_fleet_config(_central(tmp_path / "legacy", collab={**COLLAB, "members": members()},
                                        share=share(), extra={**BRIDGE, "api_keys": {}}))
    with pytest.raises(FleetConfigError, match=r"control_plane\.api_keys"):
        fcc.collab_values(legacy)


def test_startup_refuses_members_without_any_api_key_but_accepts_keys_from_the_command_line():
    from herdr_engineering.cli import _refuse_keyless_members
    cp = {"collab": {"members": members()}, "share": share()}
    with pytest.raises(SystemExit, match="API key"):
        _refuse_keyless_members(cp, None)
    with pytest.raises(SystemExit, match="API key"):
        _refuse_keyless_members(cp, {})
    _refuse_keyless_members(cp, {"k" * 20: "operator"})              # e.g. --api-keys only: the merged set is what counts
    _refuse_keyless_members({"collab": {"members": {"enabled": False}}}, None)    # members off: nothing to refuse
    _refuse_keyless_members({}, None)
