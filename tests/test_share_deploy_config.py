"""Deploy wiring of artifact share links: the share slice, the edge's /s/ location, the
share-public compose service and deploy.sh's gate (spec 2026-10-30-artifact-share-links, Task 8).

Nothing here starts a container or contacts anything: compose files are linted as YAML,
deploy.sh runs with the stub docker of test_deploy_scripts.py."""
import importlib.util
import secrets
import json
import logging
import re
import stat
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
          "public_url": "https://share.example.test/", "operator_user": "owner-test"}


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
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "max_files": 50,
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
                                    "default_public_days", "session_seconds", "signing_secret"}
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


@pytest.mark.parametrize("url", ["https://share.example.test/collab/", "https://share.example.test/x",
                                 "http://share.example.test/"])
def test_enabled_share_refuses_a_public_url_with_a_path(tmp_path, url):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET},
                       collab={**COLLAB, "public_url": url})
    with pytest.raises(FleetConfigError, match="public_url"):
        fcc.render_share(load_fleet_config(central), tmp_path / "out.json")


def test_enabled_share_refuses_unknown_keys_and_bad_caps(tmp_path):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET, "api_key": "x"})
    with pytest.raises(FleetConfigError, match="unknown keys: api_key"):
        fcc.render_share(load_fleet_config(central), tmp_path / "out.json")
    central = _central(tmp_path / "b", share={"enabled": True, "signing_secret": SECRET, "max_files": 0})
    with pytest.raises(FleetConfigError, match="max_files"):
        fcc.render_share(load_fleet_config(central), tmp_path / "out.json")


def test_slice_cap_defaults_match_the_control_plane():
    from herdr_engineering.control_plane.artifact_share_api import DEFAULTS
    assert fcc.SHARE_CAP_DEFAULTS == DEFAULTS


def test_share_public_refuses_a_file_carrying_more_than_the_slice(tmp_path):
    central = _central(tmp_path, share={"enabled": True, "signing_secret": SECRET})
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
    out = _slice(tmp_path, {"enabled": True, "signing_secret": SECRET}, collab=collab)
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
    out = _slice(tmp_path, {"enabled": True, "signing_secret": SECRET})
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
    return [m.group(1).strip() for m in re.finditer(r"^\s*location\s+([^{]+)\{", conf, re.M)]


BASE_LOCATIONS = ["= /mcp", "= /a2a", "= /.well-known/agent-card.json", "/api/v1/collab/", "= /", "= /guide",
                  None, "/", "/api/v1/collab/", "/"]   # None: the static-asset regex location


def test_disabled_share_renders_the_edge_conf_unchanged():
    template = EDGE.read_text()
    assert fcc.render_edge_conf(template, share=False) == template
    assert "share" not in "".join(ln for ln in template.splitlines() if not ln.lstrip().startswith("#"))


def test_enabled_share_adds_exactly_the_s_location():
    template = EDGE.read_text()
    conf = fcc.render_edge_conf(template, share=True)
    before, after = _locations(template), _locations(conf)
    added = list(after)
    for loc in before:
        added.remove(loc)
    assert added == ["^~ /s/"]
    assert len(before) == len(BASE_LOCATIONS)
    block = conf[conf.index("location ^~ /s/"):]
    block = block[:block.index("\n    }\n")]
    for needle in ("proxy_pass http://share;", "proxy_buffering off;", "proxy_read_timeout 600s;", "access_log off;",
                   "limit_req zone=share_req", "limit_conn share_conn", "proxy_pass_request_body off;",
                   "if ($share_method_ok = 0)", "if ($share_has_body)"):
        assert needle in block, needle
    assert "client_max_body_size 0" not in conf          # 0 = unlimited in nginx
    # its own zones and upstream, keepalive pool well under share-public's 128 connections
    assert re.search(r"limit_req_zone \S+ zone=share_req:", conf)
    assert re.search(r"limit_conn_zone \S+ zone=share_conn:", conf)
    upstream = re.search(r"upstream share \{ zone share_up \d+k; server share-public:8083 resolve max_conns=(\d+); "
                         r"keepalive (\d+); \}", conf)
    assert upstream and int(upstream.group(1)) < 128 and int(upstream.group(2)) <= 32
    # server-level timeouts the /s/ location inherits
    assert "client_header_timeout 10s;" in conf and "client_body_timeout 10s;" in conf
    # the operator listener (tailnet) never gets /s/
    operator = conf[conf.index("listen 8082;"):]
    assert "/s/" not in operator


def test_share_method_map_allows_reads_and_only_the_body_less_session_post():
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True)
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
    assert allowed("POST", f"/s/{token}/session")
    for method, uri in (("POST", f"/s/{token}/f/x"), ("POST", f"/s/{token}/session/x"), ("POST", "/s/short/session"),
                        ("POST", f"/s/{token}/session\n"),
                        ("PUT", f"/s/{token}/session"), ("DELETE", f"/s/{token}/"), ("PATCH", f"/s/{token}/")):
        assert not allowed(method, uri), (method, uri)
    b = re.search(r'map "\$http_transfer_encoding:\$http_content_length" \$share_has_body \{\n(.*?)\n  \}', conf, re.S)
    assert b and "default 1;" in b.group(1) and '":" 0;' in b.group(1) and '":0" 0;' in b.group(1)


def test_share_public_is_never_a_start_time_dependency_of_the_edge():
    """nginx resolves a plain upstream host at start and at every reload: a down share-public would take the whole
    collab edge (and its certificate reloads) down. share-public must be resolved at run time only."""
    conf = fcc.render_edge_conf(EDGE.read_text(), share=True)
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
    central = _central(tmp_path / "ok", share={"enabled": True, "signing_secret": SECRET, "shares_dir": "/shares",
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
    conf = fcc.render_edge_conf(EDGE.read_text(), share=share)
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
    _set_share(env, {"enabled": True, "signing_secret": SECRET}, collab=False)
    data = yaml.safe_load(env["central"].read_text())
    data["sections"]["control_plane"]["values"]["collab"] = dict(COLLAB)
    env["central"].unlink()
    fcc.write_private_file(env["central"], yaml.safe_dump(data, sort_keys=False))
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode != 0 and "collab-enabled" in r.stderr
    assert SECRET not in r.stdout + r.stderr


def test_dry_run_with_share_enabled_validates_and_never_prints_the_secret(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET})
    before = _ds._home_files(env)
    r = _ds.deploy(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "share-public" in r.stdout
    assert SECRET not in r.stdout + r.stderr
    assert _ds._home_files(env) == before
    calls = [ln for ln in env["log"].read_text().splitlines() if " config -q" in ln]
    assert calls and "compose.collab.yaml" in calls[0]


def test_real_deploy_with_share_enabled_installs_the_slice_and_edge_and_recreates_share_public(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET})
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
    assert "location ^~ /s/" in edge
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
    edge.write_text(fcc.render_edge_conf(EDGE.read_text(), share=with_s))
    return edge


def _log_pairs(e):
    lines = e["log"].read_text().splitlines()
    return list(zip(lines, lines[1:]))


def test_rollback_recreates_share_public_from_the_previous_commit_when_its_edge_routes_s(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET})
    full, prev_tag = _ds._seed_previous(env, _rollback_target_with_share_public())
    _seed_share_edge(env, with_s=True)
    env["env"]["FAIL_COMPOSE_TAGS"] = _ds._head()
    r = _ds.deploy(env)
    assert r.returncode == 1 and "and healthy" in r.stderr, r.stdout + r.stderr
    recreated = [nxt for cur, nxt in _log_pairs(env)
                 if cur.startswith("docker compose") and cur.endswith("--force-recreate share-public")]
    assert any(f" tag={prev_tag} " in n for n in recreated), recreated
    assert "docker stop herdr-share-public" not in env["log"].read_text()
    assert SECRET not in r.stdout + r.stderr


def test_rollback_stops_share_public_when_the_restored_edge_does_not_route_s(env):
    _set_share(env, {"enabled": True, "signing_secret": SECRET})
    _, prev_tag = _ds._seed_previous(env, _rollback_target_with_share_public())
    _seed_share_edge(env, with_s=False)
    env["env"]["FAIL_COMPOSE_TAGS"] = _ds._head()
    r = _ds.deploy(env)
    assert r.returncode == 1 and "and healthy" in r.stderr, r.stdout + r.stderr
    assert "docker stop herdr-share-public" in env["log"].read_text()
    recreated = [nxt for cur, nxt in _log_pairs(env)
                 if cur.startswith("docker compose") and cur.endswith("--force-recreate share-public")]
    assert not any(f" tag={prev_tag} " in n for n in recreated)
