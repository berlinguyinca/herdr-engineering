"""Deploy wiring of artifact share links: the share slice, the edge's /s/ location, the
share-public compose service and deploy.sh's gate (spec 2026-10-30-artifact-share-links, Task 8).

Nothing here starts a container or contacts anything: compose files are linted as YAML,
deploy.sh runs with the stub docker of test_deploy_scripts.py."""
import importlib.util
import json
import logging
import re
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config

ROOT = Path(__file__).resolve().parents[1]
CP = ROOT / "deploy" / "control-plane"
EDGE = CP / "collab-edge" / "nginx.conf"

# deploy.sh fixtures (stub docker/curl, throwaway HOME and central file) are shared with the
# existing deploy tests; loaded by path because the suite runs with --import-mode=importlib.
_spec = importlib.util.spec_from_file_location("_share_deploy_scripts", ROOT / "tests" / "test_deploy_scripts.py")
_ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ds)
env = _ds.env
candidate_ref = _ds.candidate_ref

SECRET = secrets.token_hex(32)   # generated per run, 64 hex chars
COLLAB = {"cognito_pool": "us-west-2_TESTPOOL", "cognito_clients": ["client-a"],
          "cognito_group": "team.test", "cognito_web_client": "webclient1",
          "cognito_domain": "https://pool.auth.us-west-2.amazoncognito.com",
          "public_url": "https://mcp.example.test/", "operator_user": "owner-test"}
SHARE_URL = "https://artifacts.example.test/"
SHARE_HOST = "artifacts.example.test"


def _central(tmp_path, share=None, collab=COLLAB):
    central = tmp_path / "fleet" / "fleet-config.yaml"
    central.parent.mkdir(parents=True)
    fcc.init_config(central, dict(_ds.ANSWERS))
    data = yaml.safe_load(central.read_text())
    cpv = data["sections"]["control_plane"]["values"]
    cpv["api_keys"] = {"placeholder-api-key-value": "operator"}
    if collab is not None:
        cpv["collab"] = dict(collab)
    if share is not None:
        cpv["share"] = dict(share)
    central.unlink()
    fcc.write_private_file(central, yaml.safe_dump(data, sort_keys=False))
    return central


# ------------------------------------------------------------------------------------ (b) the share slice

def test_slice_holds_only_the_share_and_cognito_keys(tmp_path):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL, "max_files": 50,
                                        "session_seconds": 3600, "shares_dir": "/shares"})
    out = tmp_path / "share" / "share-config.json"
    fcc.render_share(load_fleet_config(central), out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    data = json.loads(out.read_text())
    assert set(data["sections"]) == {"control_plane"}
    assert data["hosts"] == {}
    values = data["sections"]["control_plane"]["values"]
    assert set(values) == {"collab", "share"}
    assert set(values["collab"]) == {"cognito_pool", "cognito_clients", "cognito_web_client",
                                     "cognito_domain", "public_url"}
    assert set(values["share"]) <= {"enabled", "max_share_bytes", "max_total_bytes", "max_files",
                                    "default_public_days", "session_seconds", "signing_secret", "public_url"}
    assert values["share"]["public_url"] == SHARE_URL
    assert values["share"]["signing_secret"] == SECRET and values["share"]["max_files"] == 50
    text = out.read_text()
    full = yaml.safe_load(central.read_text())
    cpv = full["sections"]["control_plane"]["values"]
    for value in (cpv["pg_dsn"], cpv["minio_password"], "placeholder-api-key-value", "owner-test", "team.test",
                  full["sections"]["llm"]["values"]["upstream_key"]):
        assert value not in text
    fcc.check_share_slice(load_fleet_config(out))      # the container accepts exactly this


def test_disabled_share_renders_without_a_secret_or_collab(tmp_path):
    central = _central(tmp_path, share={"enabled": False}, collab=None)
    out = tmp_path / "share-config.json"
    fcc.render_share(load_fleet_config(central), out)
    values = json.loads(out.read_text())["sections"]["control_plane"]["values"]
    assert values["share"]["enabled"] is False
    assert fcc.share_enabled(load_fleet_config(central)) is False
    central2 = _central(tmp_path / "none", share=None, collab=None)
    assert fcc.share_enabled(load_fleet_config(central2)) is False
    fcc.render_share(load_fleet_config(central2), tmp_path / "none" / "out.json")


@pytest.mark.parametrize("secret", [None, "", "too-short"])
def test_enabled_share_needs_a_long_signing_secret_and_says_how(tmp_path, secret):
    share = {"enabled": True}
    if secret is not None:
        share["signing_secret"] = secret
    central = _central(tmp_path, share=share)
    with pytest.raises(FleetConfigError) as info:
        fcc.render_share(load_fleet_config(central), tmp_path / "out.json")
    msg = str(info.value)
    assert "control_plane.share.signing_secret" in msg and "openssl rand -hex 32" in msg
    if secret:
        assert secret not in msg
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("url", ["https://artifacts.example.test/x/", "https://artifacts.example.test/x",
                                 "http://artifacts.example.test/", "https://artifacts.example.test/?a=1",
                                 "https://artifacts.example.test/#f", "https://mcp.example.test/", "", None,
                                 "https://mcp.example.test:443/", "https://mcp.example.test:8443", "https://MCP.Example.Test/",
                                 "https://mcp.example.test./", "https://user@artifacts.example.test/",
                                 "https://user:pw@mcp.example.test/", "https://artifacts.example.test./",
                                 "https://Artifacts.example.test/", "https://artifacts.example.test:99999x/",
                                 "https://ärtifacts.example.test/", "https://artifacts.exämple.test/", 7,
                                 "https://[::1]/", "https://artifacts .example.test/"])
def test_enabled_share_refuses_a_bad_share_public_url(tmp_path, url):
    share = {"enabled": True, "signing_secret": SECRET}
    if url is not None:
        share["public_url"] = url
    central = _central(tmp_path, share=share)
    with pytest.raises(FleetConfigError, match=r"control_plane\.share\.public_url") as info:
        fcc.render_share(load_fleet_config(central), tmp_path / "out.json")
    assert "/s/callback" in str(info.value) and "control_plane.collab.public_url" in str(info.value)
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("collab_url", [None, 5, "", "not a url"])
def test_a_missing_or_odd_collab_url_does_not_block_a_valid_share_host(tmp_path, collab_url):
    collab = {k: v for k, v in COLLAB.items() if k != "public_url"}
    if collab_url is not None:
        collab["public_url"] = collab_url
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL},
                       collab=collab)
    fcc.render_share(load_fleet_config(central), tmp_path / "out.json")


def test_share_host_may_be_a_subdomain_or_have_its_own_port(tmp_path):
    for url in ("https://share.mcp.example.test/", "https://artifacts.example.test:8443"):
        central = _central(tmp_path / url[8:12], share={"enabled": True, "signing_secret": SECRET, "public_url": url})
        fcc.render_share(load_fleet_config(central), tmp_path / "out.json")


def test_every_accepted_public_url_is_also_accepted_by_the_share_server():
    from herdr_engineering.https_origin import https_origin
    assert https_origin("https://Artifacts.Example.test:8443/") == "https://artifacts.example.test:8443"
    for bad in ("https://a.test./", "https://u@a.test/", "http://a.test/", "https://ä.test/", "https://a.test/x"):
        assert https_origin(bad) is None


def test_disabled_share_needs_no_share_public_url(tmp_path):
    central = _central(tmp_path, share={"enabled": False})
    fcc.render_share(load_fleet_config(central), tmp_path / "out.json")


def test_share_public_builds_lab_and_origin_from_the_share_host(tmp_path):
    from herdr_engineering.cli import _share_public_server
    out = _slice(tmp_path, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    server = _share_public_server(out, shares_dir=tmp_path / "shares", events_dir=tmp_path / "events",
                                  sync_dir=tmp_path / "sync", host="127.0.0.1", port=0)
    try:
        assert server.config.public_url == SHARE_URL
        assert server.lab is not None
        assert json.loads(server.lab.config_json)["redirect_uri"] == "https://artifacts.example.test/s/callback"
        assert server.public_origin == "https://artifacts.example.test"
        from herdr_engineering.control_plane.share_public import sandbox_csp
        assert "https://artifacts.example.test/s/" + "a" * 43 + "/" in sandbox_csp(server.public_origin, "a" * 43)
    finally:
        server.server_close()


def test_enabled_share_refuses_unknown_keys_and_bad_caps(tmp_path):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL, "api_key": "x"})
    with pytest.raises(FleetConfigError, match="unknown keys: api_key"):
        fcc.render_share(load_fleet_config(central), tmp_path / "out.json")
    central = _central(tmp_path / "b", share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL, "max_files": 0})
    with pytest.raises(FleetConfigError, match="max_files"):
        fcc.render_share(load_fleet_config(central), tmp_path / "out.json")


def test_slice_cap_defaults_match_the_control_plane():
    from herdr_engineering.control_plane.artifact_share_api import DEFAULTS
    assert fcc.SHARE_CAP_DEFAULTS == DEFAULTS


def test_share_public_refuses_a_file_carrying_more_than_the_slice(tmp_path):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    with pytest.raises(FleetConfigError, match="render-share"):
        fcc.check_share_slice(load_fleet_config(central))


# ------------------------------------------------------------------- share-public start-up (cli helper)

def _slice(tmp_path, share, collab=COLLAB):
    central = _central(tmp_path, share=share, collab=collab)
    out = tmp_path / "share" / "share-config.json"
    fcc.render_share(load_fleet_config(central), out)
    return out


def test_share_public_logs_once_when_lab_settings_are_incomplete(tmp_path, caplog):
    from herdr_engineering.cli import _share_public_server
    collab = {k: v for k, v in COLLAB.items() if k != "cognito_web_client"}
    out = _slice(tmp_path, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL}, collab=collab)
    caplog.set_level(logging.WARNING)
    server = _share_public_server(out, shares_dir=tmp_path / "shares", events_dir=tmp_path / "events",
                                  sync_dir=tmp_path / "sync", host="127.0.0.1", port=0)
    try:
        assert server.lab is None
        lines = [r.getMessage() for r in caplog.records if "lab" in r.getMessage()]
        assert len(lines) == 1 and "cognito_web_client" in lines[0]
        assert SECRET not in caplog.text
    finally:
        server.server_close()


def test_share_public_with_complete_lab_settings_logs_nothing(tmp_path, caplog):
    from herdr_engineering.cli import _share_public_server
    out = _slice(tmp_path, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    caplog.set_level(logging.WARNING)
    server = _share_public_server(out, shares_dir=tmp_path / "shares", events_dir=tmp_path / "events",
                                  sync_dir=tmp_path / "sync", host="127.0.0.1", port=0)
    try:
        assert server.lab is not None
        assert server.config.shares_root == tmp_path / "shares"
        assert server.config.events_path == tmp_path / "events" / "events.jsonl"
        assert not [r for r in caplog.records if "lab" in r.getMessage()]
    finally:
        server.server_close()


def test_share_public_refuses_a_disabled_slice(tmp_path):
    from herdr_engineering.cli import _share_public_server
    out = _slice(tmp_path, {"enabled": False})
    with pytest.raises(SystemExit, match="enabled"):
        _share_public_server(out, shares_dir=tmp_path, events_dir=tmp_path, sync_dir=tmp_path,
                             host="127.0.0.1", port=0)


# ------------------------------------------------------------------------------- (a) the rendered edge

def _locations(conf: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r'^\s*location\s+((?:"[^"]*"|[^{"])+)\{', conf, re.M)]


BASE_LOCATIONS = ["= /mcp", "= /a2a", "= /.well-known/agent-card.json", "/api/v1/collab/", "= /", "= /guide",
                  None, "/", "/api/v1/collab/", "/"]   # None: the static-asset regex location


def test_disabled_share_renders_the_edge_conf_unchanged():
    template = EDGE.read_text()
    assert fcc.render_edge_conf(template, share=False) == template
    assert "share" not in "".join(ln for ln in template.splitlines() if not ln.lstrip().startswith("#"))


def test_enabled_share_adds_a_share_host_server_with_only_s_locations():
    template = EDGE.read_text()
    conf = fcc.render_edge_conf(template, share=True, share_host=SHARE_HOST)
    mcp = _server_block(conf, "server_name localdev.mcp.metabolomics.us;")
    assert "/s/" not in mcp and "proxy_pass http://share" not in mcp and "return 308" not in mcp
    assert "location ^~ /s/" not in conf
    share = _server_block(conf, f"server_name {SHARE_HOST};")
    assert _locations(share) == ["/s/", '~ "^/s/[A-Za-z0-9_-]{43}/k/[A-Za-z0-9_.-]{1,768}/annotations'
                                 '(/ann_[A-Za-z0-9_-]{22}/(edit|delete))?$"', "/"]
    assert "location / { return 404; }" in share
    block = _brace_block(share, share.index("location /s/"))
    for needle in ("proxy_pass http://share;", "proxy_buffering off;", "proxy_read_timeout 600s;", "access_log off;",
                   "error_log /dev/stderr crit;", "limit_req zone=share_req burst=300 nodelay;",
                   "limit_conn share_conn 16;", "proxy_pass_request_body off;", "client_max_body_size 1k;",
                   "if ($share_method_ok = 0)", "if ($share_has_body)"):
        assert needle in block, needle
    assert "client_max_body_size 0" not in conf          # 0 = unlimited in nginx
    assert re.search(r"limit_req_zone \S+ zone=share_req:", conf)
    assert re.search(r"limit_conn_zone \S+ zone=share_conn:", conf)
    upstream = re.search(r"upstream share \{ zone share_up \d+k; server share-public:8083 resolve max_conns=(\d+); "
                         r"keepalive (\d+); \}", conf)
    assert upstream and int(upstream.group(1)) < 128 and int(upstream.group(2)) <= 32
    assert "client_header_timeout 10s;" in share and "client_body_timeout 10s;" in share
    operator = conf[conf.index("listen 8082;"):]
    assert "/s/" not in operator


def _depth_at(conf: str, needle: str) -> int:
    pos = conf.index(needle)
    return conf.count("{", 0, pos) - conf.count("}", 0, pos)


def test_annotations_location_is_flat_and_the_only_one_that_takes_a_body():
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True, share_host=SHARE_HOST)
    share = _server_block(conf, f"server_name {SHARE_HOST};")
    assert _depth_at(conf, "location ~ \"^/s/") == 2 == _depth_at(conf, "location /s/") == _depth_at(conf, "location / {")
    ann = _brace_block(share, share.index('$" {'))
    for needle in ("proxy_pass_request_body on;", "client_max_body_size 32k;", "limit_req zone=share_annotate burst=30 nodelay;",
                   "limit_conn share_conn 16;", "proxy_pass http://share;", "access_log off;",
                   "error_log /dev/stderr crit;", "if ($request_method !~ ^(GET|POST)$) { return 404; }"):
        assert needle in ann, needle
    assert share.count("proxy_pass_request_body on") == 1
    assert re.search(r"limit_req_zone \$remote_addr zone=share_annotate:10m rate=5r/s;", conf)
    assert "share_annotate" not in "".join(ln for ln in conf.splitlines() if "hcn_" in ln or "/mcp" in ln)
    assert "zone=share_annotate" not in _server_block(conf, "server_name localdev.mcp.metabolomics.us;")


def test_share_host_proxy_hygiene_and_headers():
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True, share_host=SHARE_HOST)
    share = _server_block(conf, f"server_name {SHARE_HOST};")
    for needle in ("proxy_set_header Host $host;", "proxy_set_header Origin $http_origin;",
                   "proxy_set_header Sec-Fetch-Dest $http_sec_fetch_dest;", "proxy_set_header X-Forwarded-Proto https;",
                   "proxy_set_header X-Forwarded-For $remote_addr;", 'proxy_set_header X-Forwarded-Host "";',
                   'proxy_set_header Forwarded "";', "proxy_http_version 1.1;", 'proxy_set_header Connection "";',
                   'add_header Strict-Transport-Security "max-age=31536000" always;',
                   "if ($hcn_from_edge = 0) { return 444; }", "limit_req_status 429;", "limit_conn_status 429;",
                   "proxy_connect_timeout 3s;", "send_timeout 30s;", "ssl_protocols TLSv1.2 TLSv1.3;"):
        assert needle in share, needle
    # what share-public sets must reach the browser untouched; the client's Range / Content-Length are not stripped
    for forbidden in ("proxy_hide_header", "add_header Content-Security", "add_header Cache-Control", "expires",
                      "proxy_cache", "proxy_set_header Range", "proxy_set_header Content-Length",
                      "proxy_set_header Content-Type", "proxy_set_header Sec-Fetch-Mode", "proxy_set_header Sec-Fetch-Site"):
        assert forbidden not in share, forbidden
    assert share.count("add_header") == 1


def test_share_and_mcp_servers_present_the_same_certificate():
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True, share_host=SHARE_HOST)
    paths = []
    for listen in ("server_name localdev.mcp.metabolomics.us;", f"server_name {SHARE_HOST};"):
        blk = _server_block(conf, listen)
        paths.append((re.search(r"ssl_certificate\s+(\S+);", blk).group(1),
                      re.search(r"ssl_certificate_key\s+(\S+);", blk).group(1)))
    assert paths[0] == paths[1] == ("/etc/acme/fullchain.pem", "/etc/acme/privkey.pem")
    assert conf.count("listen 8443 ssl proxy_protocol") == 3     # default reject, MCP, share


def test_mcp_server_keeps_its_mcp_limits_when_shares_are_on():
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True, share_host=SHARE_HOST)
    mcp = _server_block(conf, "server_name localdev.mcp.metabolomics.us;")
    for needle in ("limit_conn mcpconn 32;", "limit_conn mcptotal 128;", "limit_conn_status 429;",
                   "limit_req zone=hcn_mcp burst=100 nodelay;", "limit_req_status 429;"):
        assert needle in mcp, needle


def test_share_location_snippet_is_gone_and_unreferenced():
    assert not (CP / "collab-edge" / "share-location.conf").exists()
    assert "@share-location@" not in EDGE.read_text()
    assert "share-location" not in (CP / "collab-edge" / "share-http.conf").read_text()


def test_share_host_is_required_validated_and_replaced():
    template = EDGE.read_text()
    with pytest.raises(FleetConfigError, match="share host"):
        fcc.render_edge_conf(template, share=True)
    for bad in ("Artifacts.example.test", "a b", "x.test/", "", "https://x.test"):
        with pytest.raises(FleetConfigError):
            fcc.render_edge_conf(template, share=True, share_host=bad)
    conf = fcc.render_edge_conf(template, share=True, share_host="artifacts.example.test:8443")
    assert "server_name artifacts.example.test;" in conf and "@share_host@" not in conf


def test_render_edge_uses_the_configured_share_origin(tmp_path):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    out = tmp_path / "out" / "nginx.conf"
    fcc.render_edge(load_fleet_config(central), EDGE, out)
    assert "server_name artifacts.example.test;" in out.read_text()
    off = _central(tmp_path / "off", share={"enabled": False})
    out2 = tmp_path / "out2" / "nginx.conf"
    fcc.render_edge(load_fleet_config(off), EDGE, out2)
    assert out2.read_text() == EDGE.read_text()


NGINX_IMAGE = "nginx:1.27-alpine"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", NGINX_IMAGE], capture_output=True).returncode == 0


needs_nginx = pytest.mark.skipif(shutil.which("nginx") is None and not _docker_ready(),
                                 reason="neither nginx nor docker with the nginx image is available")
needs_docker = pytest.mark.skipif(not _docker_ready(), reason="docker with the nginx image is not available")


def _dummy_certs(acme: Path) -> None:
    acme.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=t",
                    "-keyout", str(acme / "privkey.pem"), "-out", str(acme / "fullchain.pem")],
                   check=True, capture_output=True)


def _testable_conf(acme_dir: str, pid: str, upstream: str = "127.0.0.1:9") -> str:
    """The rendered edge, with only what a test box cannot provide swapped: cert dir, pid, upstream addresses."""
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True, share_host=SHARE_HOST)
    conf = conf.replace("/etc/acme", acme_dir).replace("pid /run/nginx.pid;", f"pid {pid};")
    conf = re.sub(r"server (collab-public|share-public):\d+", f"server {upstream}", conf)
    return conf.replace("resolver 127.0.0.11 valid=10s ipv6=off;", "").replace(" resolve max_conns", " max_conns")


@needs_nginx
def test_rendered_edge_passes_nginx_t(tmp_path):
    _dummy_certs(tmp_path / "acme")
    if shutil.which("nginx"):
        (tmp_path / "nginx.conf").write_text(_testable_conf(str(tmp_path / "acme"), f"{tmp_path}/n.pid"))
        cmd = ["nginx", "-t", "-c", str(tmp_path / "nginx.conf"), "-e", "/dev/stderr"]
    else:
        (tmp_path / "nginx.conf").write_text(_testable_conf("/c", "/tmp/n.pid"))
        cmd = ["docker", "run", "--rm", "-v", f"{tmp_path}/nginx.conf:/etc/nginx/nginx.conf:ro",
               "-v", f"{tmp_path}/acme:/c:ro", NGINX_IMAGE, "nginx", "-t"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---- behaviour against a real nginx (docker, host network) and a tiny echo upstream

TOKEN = "A" * 43
SESSION = "sess-1"
ANN = f"/s/{TOKEN}/k/{SESSION}/annotations"
MCP_HOST = "localdev.mcp.metabolomics.us"


@pytest.fixture(scope="module")
def live_edge(tmp_path_factory):
    import http.server
    import socket
    import threading
    import time
    if not _docker_ready():
        pytest.skip("docker with the nginx image is not available")
    tmp = tmp_path_factory.mktemp("edge")
    _dummy_certs(tmp / "acme")

    class Echo(http.server.BaseHTTPRequestHandler):
        def do(self):
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(n) if n else b""
            out = json.dumps({"m": self.command, "path": self.path, "blen": len(body)}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(out)
        do_GET = do_POST = do_HEAD = do_PUT = do_OPTIONS = do

        def log_message(self, *a):
            pass

    def free_port():
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            return sk.getsockname()[1]
    up_port, edge_port = free_port(), free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", up_port), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    conf = _testable_conf("/c", "/tmp/n.pid", upstream=f"127.0.0.1:{up_port}")
    # what a test box has no equivalent of: the PROXY protocol header and the LAN edge address
    conf = conf.replace("listen 8443 ssl proxy_protocol", f"listen {edge_port} ssl")
    conf = conf.replace("real_ip_header proxy_protocol;", "").replace("set_real_ip_from 192.168.137.250;", "")
    conf = conf.replace("map $realip_remote_addr $hcn_from_edge { default 0; 192.168.137.250 1; }",
                        "map $remote_addr $hcn_from_edge { default 1; }")
    conf = conf.replace("listen 8082;", f"listen {free_port()};")
    (tmp / "nginx.conf").write_text(conf)
    name = f"herdr-edge-test-{secrets.token_hex(4)}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
                    "-v", f"{tmp}/nginx.conf:/etc/nginx/nginx.conf:ro", "-v", f"{tmp}/acme:/c:ro",
                    NGINX_IMAGE, "nginx", "-g", "daemon off;"], check=True, capture_output=True)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", edge_port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        yield edge_port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        server.shutdown()


def _request(port, method, path, *, sni=SHARE_HOST, host=None, body=b"", headers=None):
    """(status, body) or (None, b"") when the server closed the connection; the path is sent raw."""
    import http.client
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
    raw.sock = ctx.wrap_socket(__import__("socket").create_connection(("127.0.0.1", port), timeout=10),
                               server_hostname=sni)
    try:
        raw.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        raw.putheader("Host", host or sni)
        for k, v in (headers or {}).items():
            raw.putheader(k, v)
        if body or method in ("POST", "PUT"):
            raw.putheader("Content-Length", str(len(body)))
        raw.endheaders(body)
        resp = raw.getresponse()
        return resp.status, resp.read()
    except (ConnectionError, http.client.RemoteDisconnected):
        return None, b""
    finally:
        raw.close()


@needs_docker
@pytest.mark.parametrize("method,path,kw,expect", [
    ("GET", f"/s/{TOKEN}/f/index.html", {}, 200),
    ("GET", f"/s/{TOKEN}/f/my%20file.csv", {}, 200),
    ("GET", "/s/static/vendor/pdfjs/pdf.min.mjs", {}, 200),
    ("GET", ANN, {}, 200),
    ("POST", ANN, {"body": b"x" * 31 * 1024, "headers": {"Content-Type": "application/json"}}, 200),
    ("POST", ANN, {"body": b"x" * 33 * 1024, "headers": {"Content-Type": "application/json"}}, 413),
    ("PUT", ANN, {"body": b"{}"}, 404),
    ("DELETE", ANN, {}, 404),
    ("GET", ANN, {"headers": {"Content-Length": "5"}, "body": b"hello"}, 404),
    ("POST", f"/s/{TOKEN}/f/x", {"body": b"{}"}, 404),
    ("GET", "/", {}, 404),
    ("GET", "/api/v1/collab/config", {}, 404),
    ("GET", f"/s/{TOKEN}/k/{SESSION}/y/../annotations", {}, 404),
    ("POST", f"/s/{TOKEN}/k/{SESSION}/y/../annotations", {"body": b"{}"}, 404),
    ("POST", f"//s/{TOKEN}/k/{SESSION}/annotations", {"body": b"{}"}, 404),
    ("GET", f"/s/{TOKEN}/f/./index.html", {}, 404),
    ("GET", f"/s/{TOKEN}/f/a%2fb", {}, 404),
    ("GET", f"/s/{TOKEN}/f/a%2Eb", {}, 404),
    ("GET", f"/s/{TOKEN}/f/a%5cb", {}, 404),
    ("GET", f"/s/{TOKEN}/f/a\\b", {}, 404),
    ("GET", f"/s/{TOKEN}/f/x/..", {}, 404),
])
def test_share_host_behaviour(live_edge, method, path, kw, expect):
    body = kw.get("body", b"")
    headers = dict(kw.get("headers") or {})
    headers.pop("Content-Length", None)
    status, _ = _request(live_edge, method, path, body=body, headers=headers)
    assert status == expect


@needs_docker
def test_mcp_host_serves_no_s_and_unknown_host_is_dropped(live_edge):
    assert _request(live_edge, "GET", f"/s/{TOKEN}/f/index.html", sni=MCP_HOST)[0] == 404
    assert _request(live_edge, "POST", ANN, sni=MCP_HOST, body=b"{}")[0] == 404
    assert _request(live_edge, "GET", "/", host="unknown.example.test")[0] in (None, 444)


@needs_docker
def test_share_host_forwards_the_raw_path_and_keeps_upstream_headers(live_edge):
    import http.client
    import ssl
    status, body = _request(live_edge, "GET", f"/s/{TOKEN}/f/my%20file.csv")
    assert status == 200 and json.loads(body)["path"] == f"/s/{TOKEN}/f/my%20file.csv"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection("127.0.0.1", live_edge, context=ctx)
    conn.sock = ctx.wrap_socket(__import__("socket").create_connection(("127.0.0.1", live_edge)),
                                server_hostname=SHARE_HOST)
    conn.request("GET", ANN, headers={"Host": SHARE_HOST})
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("Cache-Control") == "no-store"          # no caching layer defeats it
    assert resp.getheader("Strict-Transport-Security")
    conn.close()


def test_share_method_map_allows_reads_and_only_the_body_less_session_post():
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True, share_host=SHARE_HOST)
    m = re.search(r'map "\$request_method:\$uri" \$share_method_ok \{\n(.*?)\n  \}', conf, re.S)
    assert m, "method map missing"
    rules = re.findall(r'"~([^"]+)"\s+1;', m.group(1))
    assert "default 0;" in m.group(1)

    def allowed(method, uri):
        # nginx's PCRE \z (end of subject) is Python's \Z
        return any(re.search(r.replace("\\z", "\\Z"), f"{method}:{uri}") for r in rules)
    token = "A" * 43
    for method in ("GET", "HEAD", "OPTIONS"):
        assert allowed(method, f"/s/{token}/f/index.html")
        assert allowed(method, f"/s/{token}")             # no trailing slash: share-public redirects it
    assert allowed("POST", f"/s/{token}/session")
    for method, uri in (("POST", f"/s/{token}/f/x"), ("POST", f"/s/{token}/session/x"), ("POST", "/s/short/session"),
                        ("POST", f"/s/{token}/session\n"),
                        ("PUT", f"/s/{token}/session"), ("DELETE", f"/s/{token}/"), ("PATCH", f"/s/{token}/")):
        assert not allowed(method, uri), (method, uri)
    b = re.search(r'map "\$http_transfer_encoding:\$http_content_length" \$share_has_body \{\n(.*?)\n  \}', conf, re.S)
    assert b and "default 1;" in b.group(1) and '":" 0;' in b.group(1) and '":0" 0;' in b.group(1)


def test_share_rate_limit_tolerates_range_reads_and_campus_nat():
    """A parquet viewer issues many small range requests, and a campus NAT puts many people behind one address."""
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True, share_host=SHARE_HOST)
    rate = re.search(r"limit_req_zone \$remote_addr zone=share_req:\d+m rate=(\d+)r/s;", conf)
    burst = re.search(r"limit_req zone=share_req burst=(\d+) nodelay;", conf)
    assert rate and int(rate.group(1)) >= 100
    assert burst and int(burst.group(1)) >= 300
    conn = re.search(r"limit_conn share_conn (\d+);", conf)
    assert conn and int(conn.group(1)) == 16                 # per address; the upstream's max_conns caps the total


def test_share_public_is_never_a_start_time_dependency_of_the_edge():
    """nginx resolves a plain upstream host at start and at every reload: a down share-public would take the whole
    collab edge (and its certificate reloads) down. share-public must be resolved at run time only."""
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True, share_host=SHARE_HOST)
    names = re.findall(r"server\s+share-public:\d+([^;]*);", conf)
    assert names and all(" resolve" in rest for rest in names)
    assert re.search(r"^\s*resolver 127\.0\.0\.11\b", conf, re.M)
    assert re.search(r"upstream share \{ zone \S+ \d+k;", conf)          # resolve needs a shared zone
    without_upstream = re.sub(r"upstream share \{[^}]*\}", "", conf)
    assert "share-public" not in "".join(ln for ln in without_upstream.splitlines() if not ln.lstrip().startswith("#"))


def test_share_dirs_are_fixed_container_paths(tmp_path):
    for key, bad in (("shares_dir", "/data/shares"), ("events_dir", "/events"), ("marker_dir", "/tmp/m")):
        central = _central(tmp_path / key, share={"enabled": False, key: bad})
        with pytest.raises(FleetConfigError, match=f"control_plane.share.{key} must be"):
            fcc.render_share(load_fleet_config(central), tmp_path / "out.json")
    central = _central(tmp_path / "ok", share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL, "shares_dir": "/shares",
                                              "events_dir": "/share-events",
                                              "marker_dir": "/var/lib/herdr/share-markers"})
    fcc.render_share(load_fleet_config(central), tmp_path / "ok.json")


def _share_subnet() -> str:
    ipam = yaml.safe_load((CP / "compose.collab.yaml").read_text())["networks"]["share"]["ipam"]
    (entry,) = ipam["config"]
    return entry["subnet"]


def _server_block(conf: str, listen: str) -> str:
    start = conf.index(listen)
    start = conf.rindex("server {", 0, start)
    depth, i = 0, start
    while True:
        ch = conf[i]
        depth += ch == "{"
        depth -= ch == "}"
        i += 1
        if depth == 0 and ch == "}":
            return conf[start:i]


@pytest.mark.parametrize("share", [False, True])
def test_operator_listener_refuses_the_share_network(share):
    """share-public sits on the share network with the edge: a compromised share-public must not reach the
    no-sign-in operator listener (8082) by setting Host itself. The share subnet is fixed so the edge can deny it."""
    import ipaddress
    subnet = ipaddress.ip_network(_share_subnet())
    assert subnet.is_private and subnet.prefixlen >= 24
    assert not subnet.overlaps(ipaddress.ip_network("192.168.137.0/24"))     # the LAN the SNI edge lives on
    assert not subnet.overlaps(ipaddress.ip_network("100.64.0.0/10"))        # tailnet
    conf = fcc.render_edge_conf(EDGE.read_text(), share=share, share_host=SHARE_HOST if share else None)
    operator = _server_block(conf, "listen 8082;")
    rules = re.findall(r"^\s*(allow|deny)\s+([^;]+);", operator, re.M)
    assert rules and rules[0] == ("deny", str(subnet)), rules
    # the public listener: only the LAN SNI edge's TCP address passes (never a PROXY-header claim)
    public = _server_block(conf, "server_name localdev.mcp.metabolomics.us;")
    assert "if ($hcn_from_edge = 0) { return 444; }" in public
    assert re.search(r"map \$realip_remote_addr \$hcn_from_edge \{ default 0; 192\.168\.137\.250 1; \}", conf)
    assert re.findall(r"set_real_ip_from\s+([^;]+);", conf) == ["192.168.137.250"]
    assert ipaddress.ip_address("192.168.137.250") not in subnet


# --------------------------------------------------------------------------------------- (c) compose lint

def _collab_doc():
    return yaml.safe_load((CP / "compose.collab.yaml").read_text())


def test_share_public_is_isolated_like_collab_public():
    doc = _collab_doc()
    pub = doc["services"]["share-public"]
    assert pub["networks"] == ["share"]
    net = doc["networks"]["share"]
    assert net["internal"] is True and not net.get("enable_ipv6")
    assert set(net) == {"internal", "ipam"} and _share_subnet()
    assert pub["read_only"] is True and pub["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in pub["security_opt"]
    assert "env_file" not in pub and "environment" not in pub
    assert pub["image"] == "herdr-engineering:${HERDR_CP_IMAGE_TAG:?run deploy.sh}"
    assert "ports" not in pub
    targets = {}
    for v in pub["volumes"]:
        target, *mode = v.split("}:", 1)[1].split(":")
        targets[target] = mode
    assert targets == {"/shares": ["ro"], "/share-events": [], "/sync": ["ro"], "/etc/herdr/share": ["ro"]}
    assert not any("HERDR_CP_FLEET" in v or "herdr-cp-pg" in v or "rustfs" in v for v in pub["volumes"])
    cmd = pub["command"]
    assert cmd[:2] == ["control-plane", "share-public"]
    assert cmd[cmd.index("--fleet-config") + 1] == "/etc/herdr/share/share-config.json"
    assert cmd[cmd.index("--port") + 1] == "8083"
    assert "--signing-secret" not in cmd and not any(SECRET in a for a in cmd)
    # the edge reaches it on the shared internal network and lists it as a dependency
    edge = doc["services"]["collab-edge"]
    assert "share" in edge["networks"] and "collab" in edge["networks"]
    assert "share-public" in edge["depends_on"]
    assert "share" not in doc["services"]["collab-public"]["networks"]


def test_web_and_worker_mount_the_shares_volume_and_the_command_stays_in_step():
    base = yaml.safe_load((CP / "compose.yaml").read_text())
    web, worker = base["services"]["control-plane-web"], base["services"]["control-plane-worker"]
    assert "${HERDR_SHARES_DIR:?run deploy.sh}:/shares" in web["volumes"]
    assert "${HERDR_SHARES_DIR:?run deploy.sh}:/shares" in worker["volumes"]
    # read-write: the worker rotates share-public's events file (rename to .1, delete once drained)
    assert "${HERDR_SHARE_EVENTS_DIR:?run deploy.sh}:/share-events" in worker["volumes"]
    assert not any(v.startswith("${HERDR_SHARE_EVENTS_DIR") and v.endswith(":ro") for v in worker["volumes"])
    assert "herdr-cp-share-markers:/var/lib/herdr/share-markers" in worker["volumes"]
    assert "herdr-cp-share-markers" in base["volumes"]
    assert not any("/share-events" in v for v in web["volumes"])
    for name in ("postgres", "rustfs", "llm-proxy"):
        assert not any("/shares" in str(v) for v in base["services"][name].get("volumes", [])), name
    overlay = _collab_doc()["services"]["control-plane-web"]["command"]
    i = overlay.index("--collab-operator-url")
    assert overlay[:i] + overlay[i + 2:] == web["command"]


# ------------------------------------------------------------------------------------- (d) deploy.sh gate

def _set_share(e, share, collab=True):
    if collab:
        _ds._with_collab(e, collab=COLLAB)
    data = yaml.safe_load(e["central"].read_text())
    if share is None:
        data["sections"]["control_plane"]["values"].pop("share", None)
    else:
        data["sections"]["control_plane"]["values"]["share"] = dict(share)
    e["central"].unlink()
    fcc.write_private_file(e["central"], yaml.safe_dump(data, sort_keys=False))


def _dry(e):
    r = _ds.deploy(e, "--dry-run")
    return r, re.sub(r"herdr-cp-env\.\w+", "<tmp>", r.stdout)


def test_dry_run_with_share_disabled_is_unchanged(env):
    _set_share(env, None)
    r0, out0 = _dry(env)
    assert r0.returncode == 0, r0.stderr
    _set_share(env, {"enabled": False}, collab=False)
    r1, out1 = _dry(env)
    assert r1.returncode == 0, r1.stderr
    assert out1 == out0
    assert "share-public" not in out1


def test_dry_run_refuses_enabled_share_without_a_signing_secret(env):
    _set_share(env, {"enabled": True})
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode != 0
    assert "control_plane.share.signing_secret" in r.stderr and "openssl rand -hex 32" in r.stderr
    assert not (env["home"] / ".config/herdr-engineering/share").exists()


def test_dry_run_refuses_enabled_share_without_the_collab_marker(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL}, collab=False)
    data = yaml.safe_load(env["central"].read_text())
    data["sections"]["control_plane"]["values"]["collab"] = dict(COLLAB)
    env["central"].unlink()
    fcc.write_private_file(env["central"], yaml.safe_dump(data, sort_keys=False))
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode != 0 and "collab-enabled" in r.stderr
    assert SECRET not in r.stdout + r.stderr


def test_dry_run_with_share_enabled_validates_and_never_prints_the_secret(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    before = _ds._home_files(env)
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "share-public" in r.stdout
    assert SECRET not in r.stdout + r.stderr
    assert _ds._home_files(env) == before
    calls = [ln for ln in env["log"].read_text().splitlines() if " config -q" in ln]
    assert calls and "compose.collab.yaml" in calls[0]


def test_real_deploy_with_share_enabled_installs_the_slice_and_edge_and_recreates_share_public(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    _live_cert(env, "mcp.example.test", SHARE_HOST)
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert SECRET not in r.stdout + r.stderr
    home = env["home"]
    slice_ = home / ".config/herdr-engineering/share/share-config.json"
    assert stat.S_IMODE(slice_.stat().st_mode) == 0o600
    assert sorted(p.name for p in slice_.parent.iterdir()) == ["share-config.json"]
    assert (home / ".local/share/herdr-engineering/shares").is_dir()
    assert (home / ".local/share/herdr-engineering/share-events").is_dir()
    edge = (home / ".config/herdr-engineering/collab-edge/nginx.conf").read_text()
    assert "location /s/" in edge and "server_name artifacts.example.test;" in edge
    log = env["log"].read_text().splitlines()
    ups = [ln for ln in log if ln.startswith("docker compose") and " up " in ln]
    share_ups = [ln for ln in ups if "share-public" in ln]
    assert len(share_ups) == 1 and share_ups[0].endswith("--force-recreate share-public")
    assert ups.index(share_ups[0]) < next(i for i, ln in enumerate(ups) if "collab-edge" in ln)


def test_real_deploy_with_share_disabled_leaves_the_edge_conf_as_the_repo_file(env):
    _set_share(env, {"enabled": False})
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    edge = (env["home"] / ".config/herdr-engineering/collab-edge/nginx.conf").read_text()
    assert edge == EDGE.read_text()
    ups = [ln for ln in env["log"].read_text().splitlines() if ln.startswith("docker compose") and " up " in ln]
    assert not any("share-public" in ln for ln in ups)
    assert not (env["home"] / ".config/herdr-engineering/share").exists()


def test_disabling_shares_stops_share_public_even_without_collab(env):
    _set_share(env, {"enabled": False}, collab=False)
    env["env"]["STUB_RUNNING_IMAGE"] = "herdr-engineering:test"
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker stop herdr-share-public" in env["log"].read_text()


def _rollback_target_with_share_public():
    """A commit with HEAD's tree (so its compose.collab.yaml defines share-public) that is not HEAD: a dangling
    commit object, never on a branch."""
    tree = _ds._git("rev-parse", "HEAD^{tree}")
    return _ds._git("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-p", "HEAD",
                    "-m", "rollback target (test)")


def _seed_share_edge(e, with_s):
    edge = e["home"] / ".config/herdr-engineering/collab-edge/nginx.conf"
    edge.parent.mkdir(parents=True, exist_ok=True)
    edge.write_text(fcc.render_edge_conf(EDGE.read_text(), share=with_s, share_host=SHARE_HOST if with_s else None))
    return edge


def _log_pairs(e):
    lines = e["log"].read_text().splitlines()
    return list(zip(lines, lines[1:]))


def test_rollback_recreates_share_public_from_the_previous_commit_when_its_edge_routes_s(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    full, prev_tag = _ds._seed_previous(env, _rollback_target_with_share_public())
    _seed_share_edge(env, with_s=True)
    env["env"]["FAIL_COMPOSE_TAGS"] = _ds._head()
    # The synthetic deployed commit is a child of HEAD; intentionally allow this
    # older/sibling candidate so the failure exercises protected automatic rollback.
    r = _ds.deploy(env, "--allow-divergent")
    assert r.returncode == 1 and "and healthy" in r.stderr, r.stdout + r.stderr
    recreated = [nxt for cur, nxt in _log_pairs(env)
                 if cur.startswith("docker compose") and cur.endswith("--force-recreate share-public")]
    assert any(f" tag={prev_tag} " in n for n in recreated), recreated
    assert "docker stop herdr-share-public" not in env["log"].read_text()
    assert SECRET not in r.stdout + r.stderr


def test_rollback_stops_share_public_when_the_restored_edge_does_not_route_s(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    _, prev_tag = _ds._seed_previous(env, _rollback_target_with_share_public())
    _seed_share_edge(env, with_s=False)
    env["env"]["FAIL_COMPOSE_TAGS"] = _ds._head()
    # The synthetic deployed commit is a child of HEAD; intentionally allow this
    # older/sibling candidate so the failure exercises protected automatic rollback.
    r = _ds.deploy(env, "--allow-divergent")
    assert r.returncode == 1 and "and healthy" in r.stderr, r.stdout + r.stderr
    assert "docker stop herdr-share-public" in env["log"].read_text()
    recreated = [nxt for cur, nxt in _log_pairs(env)
                 if cur.startswith("docker compose") and cur.endswith("--force-recreate share-public")]
    assert not any(f" tag={prev_tag} " in n for n in recreated)


def _brace_block(text, start):
    """The text of the {...} block whose opening brace is the first one at/after ``start``."""
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        depth += (text[j] == "{") - (text[j] == "}")
        if depth == 0:
            return text[i:j + 1]
    raise AssertionError("unbalanced braces")


def test_edge_mcp_location_alone_has_the_chunk_body_cap_and_its_own_rate_zone():
    conf = EDGE.read_text()
    mcp = _brace_block(conf, conf.index("location = /mcp"))
    assert "client_max_body_size 768k;" in mcp and "limit_req zone=hcn_mcp burst=100 nodelay;" in mcp
    # the 768k cap and the hcn_mcp zone appear nowhere else
    assert conf.count("client_max_body_size 768k") == 1 and conf.count("768k;") == 1 and conf.count("zone=hcn_mcp") == 2        # the definition + the one use
    assert len(re.findall(r"zone=hcn_mcp:10m rate=25r/s;", conf)) == 1
    http_head = conf[:conf.index("server {")]
    assert "limit_req_zone $remote_addr zone=hcn_mcp:10m rate=25r/s;" in http_head
    # everything else keeps the 64k body cap and the shared 10 r/s zone
    assert "limit_req_zone $remote_addr zone=hcn_req:10m rate=10r/s;" in http_head
    assert conf.count("client_max_body_size 64k;") == 2
    assert conf.count("limit_req zone=hcn_req burst=40 nodelay;") == 2
    assert "zone=hcn_req" not in mcp
    other = conf.replace(mcp, "")
    assert "hcn_mcp" not in other.replace("limit_req_zone $remote_addr zone=hcn_mcp:10m rate=25r/s;", "")


def test_edge_mcp_body_spool_is_bounded_by_connection_limits_and_an_explicit_tmpfs():
    conf = EDGE.read_text()
    mcp = _brace_block(conf, conf.index("location = /mcp"))
    assert "limit_conn mcpconn 32;" in mcp and "limit_conn mcptotal 128;" in mcp
    assert "limit_conn_status 429;" in mcp
    assert "limit_conn_zone $remote_addr zone=mcpconn:" in conf
    assert "limit_conn_zone $server_name zone=mcptotal:" in conf
    edge = yaml.safe_load((CP / "compose.collab.yaml").read_text())["services"]["collab-edge"]
    sizes = {t.split(":")[0]: t for t in edge["tmpfs"]}
    assert "size=96m" in sizes["/var/cache/nginx"] and "size=8m" in sizes["/run"]
    spool_mib = 128 * 768 / 1024                                # every in-flight /mcp body, worst case
    assert spool_mib <= 96
    assert edge["mem_limit"] == "384m"


# ------------------------------------------------------------------ annotations and owner emails

def test_share_values_accept_annotations_and_owner_emails_and_the_slice_drops_the_owners(tmp_path):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL,
                                        "annotations": {"max_per_share": 7, "per_user_per_minute": 5},
                                        "owner_emails": ["Owner@Lab.test"]})
    cfg = load_fleet_config(central)
    values = fcc.share_values(cfg)
    assert values["share"]["annotations"] == {"max_per_share": 7, "per_user_per_minute": 5}
    assert "owner_emails" not in values["share"]
    out = tmp_path / "share" / "share-config.json"
    fcc.render_share(cfg, out)
    assert "owner@lab.test" not in out.read_text().lower()
    fcc.check_share_slice(load_fleet_config(out))
    assert "owner_emails" in fcc.SHARE_KEYS and "owner_emails" not in fcc.SHARE_SLICE_KEYS
    assert "annotations" in fcc.SHARE_SLICE_KEYS


@pytest.mark.parametrize("annotations", [{"enabled": "yes"}, {"per_user_per_minute": 0}, {"max_comment_chars": 4001},
                                         {"nope": 1}, "on"])
def test_a_bad_annotations_block_is_a_fleet_config_error_not_a_value_error(tmp_path, annotations):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL, "annotations": annotations})
    with pytest.raises(FleetConfigError, match="annotations"):
        fcc.share_values(load_fleet_config(central))


@pytest.mark.parametrize("owners", ["a@b.test", ["not-an-email"], ["a@b.test", 5], ["é@b.test"], [""]])
def test_invalid_owner_emails_are_refused(tmp_path, owners):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL, "owner_emails": owners})
    with pytest.raises(FleetConfigError, match="owner_emails"):
        fcc.share_values(load_fleet_config(central))


@pytest.mark.parametrize("url", ["https://u@artifacts.example.test/", "http://artifacts.example.test/", None, "x"])
def test_lab_problems_names_a_share_url_the_server_would_reject(url):
    from herdr_engineering.cli import _share_lab_problems
    share = {"signing_secret": SECRET, "public_url": url}
    assert any("control_plane.share.public_url" in p for p in _share_lab_problems(COLLAB, share))
    assert not any("public_url" in p for p in _share_lab_problems(COLLAB, {**share, "public_url": SHARE_URL}))


def test_share_locations_refuse_raw_paths_that_differ_from_the_normalised_one_and_reads_with_a_body():
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True, share_host=SHARE_HOST)
    assert "map $request_uri $share_raw_path_ok {" in conf
    share = _server_block(conf, f"server_name {SHARE_HOST};")
    assert share.count("if ($share_raw_path_ok = 0) { return 404; }") == 2       # /s/ and the annotation location
    ann = _brace_block(share, share.index('$" {'))
    assert "if ($share_read_with_body) { return 404; }" in ann


# ------------------------------------------------------------- the edge certificate must name the share host

def _make_cert(directory: Path, *names: str) -> Path:
    """A real self-signed certificate (openssl) whose subjectAltName lists ``names``; returns fullchain.pem."""
    directory.mkdir(parents=True, exist_ok=True)
    pem, key = directory / "fullchain.pem", directory / "privkey.pem"
    san = ",".join(f"DNS:{n}" for n in names)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "60", "-subj",
                    f"/CN={names[0]}", "-addext", f"subjectAltName={san}", "-keyout", str(key), "-out", str(pem)],
                   check=True, capture_output=True)
    return pem


SYNC = CP / "collab-sync.sh"


def _cert_host(env_, config, cert_dir, host):
    return subprocess.run([str(SYNC), "cert-host", "--config", str(config), "--cert-dir", str(cert_dir),
                           "--share-host", host], env=env_["env"], capture_output=True, text=True)


@pytest.mark.parametrize(("sans", "ok"), [
    (("mcp.example.test", "artifacts.example.test"), True),
    (("mcp.example.test", "*.example.test"), True),
    (("mcp.example.test",), False),
    (("mcp.example.test", "*.other.test"), False),
    (("mcp.example.test", "*.example.test"), True),
])
def test_collab_sync_cert_host_checks_the_subject_alternative_names(env, tmp_path, sans, ok):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    config = tmp_path / "collab-config.json"
    fcc.render_collab(fcc.load_fleet_config(env["central"]), config)
    certs = tmp_path / "certs"
    _make_cert(certs, *sans)
    r = _cert_host(env, config, certs, SHARE_HOST)
    assert (r.returncode == 0) is ok, r.stderr
    if not ok:
        assert SHARE_HOST in r.stderr and "certbot" in r.stderr


def test_a_wildcard_does_not_cover_a_deeper_name(env, tmp_path):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    config = tmp_path / "collab-config.json"
    fcc.render_collab(fcc.load_fleet_config(env["central"]), config)
    _make_cert(tmp_path / "certs", "*.example.test")
    assert _cert_host(env, config, tmp_path / "certs", "a.b.example.test").returncode != 0


def _live_cert(e, *names):
    live = e["home"] / ".local/share/herdr-engineering/collab-acme/config/live/localdev.mcp.metabolomics.us"
    _make_cert(live, *names)


def test_deploy_with_shares_fails_when_the_edge_certificate_lacks_the_share_host(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    _live_cert(env, "mcp.example.test")
    r = _ds.deploy(env)
    assert r.returncode != 0
    assert SHARE_HOST in r.stderr and "certificate" in r.stderr
    assert not (env["tmp"] / "edge-certs" / "fullchain.pem").exists()   # the unusable cert never reached the edge


def test_deploy_with_shares_passes_when_the_certificate_names_the_share_host(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    _live_cert(env, "mcp.example.test", SHARE_HOST)
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    unit = (env["home"] / ".config/systemd/user/herdr-collab-sync.service").read_text()
    assert f'--share-host {SHARE_HOST}' in unit          # the minute timer keeps checking renewals
    assert (env["tmp"] / "edge-certs" / "fullchain.pem").is_file()


def test_deploy_without_shares_does_not_look_at_the_certificate_names(env):
    _set_share(env, {"enabled": False})
    _live_cert(env, "mcp.example.test")
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--share-host" not in (env["home"] / ".config/systemd/user/herdr-collab-sync.service").read_text()


def test_deploy_with_shares_probes_the_share_host_and_only_warns(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET, "public_url": SHARE_URL})
    _live_cert(env, "mcp.example.test", SHARE_HOST)
    r = _ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr          # the stub curl answers no 404: a warning, not a failure
    assert f"curl https://{SHARE_HOST}/s/probe/" in env["log"].read_text()
    assert f"probe of https://{SHARE_HOST}/s/probe/" in r.stderr
