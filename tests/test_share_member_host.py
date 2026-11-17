"""control_plane.share.member_host: the member web UI on its own origin through the legacy share path.

Config validation and slices, the edge's extra server block (only when set), nginx -t on both renders and deploy.sh's
certificate refusal and probe.  Real files, a real nginx and the real deploy script; the MemberWeb behaviour itself
is in tests/control_plane/test_member_web.py."""
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_sd = _load("_smh_share_deploy", "tests/test_share_deploy_config.py")
_md = _load("_smh_members_deploy", "tests/test_members_deploy_config.py")
env = _sd.env
candidate_ref = _sd.candidate_ref

HOST = _md.HOST                      # the share host (control_plane.share.public_host)
MEMBER = "my.artifacts.example.test"  # control_plane.share.member_host
MCP = "mcp.example.test"             # control_plane.collab.public_url's host
EDGE_DIR = _md.EDGE_DIR


def _cfg(tmp_path, **share_over):
    return _md.cfg_of(tmp_path, sh=_md.share(**share_over), mem=_md.members())


def _edge(**kw):
    return _md._edge(share=True, share_host=HOST, members=True, **kw)


def _top_locations(block: str) -> set[str]:
    return set(re.findall(r"^    location ([^{]+?) \{", block, re.M))


# ---------------------------------------------------------------------------------------------- config
def test_member_host_is_off_by_default_and_no_slice_names_it(tmp_path):
    cfg = _cfg(tmp_path)
    assert "member_host" not in fcc.share_values(cfg)["share"]
    out = tmp_path / "collab.json"
    fcc.render_collab(cfg, out)
    assert "member_host" not in json.loads(out.read_text())["sections"]["control_plane"]["values"]["collab"]["members"]


def test_member_host_reaches_the_share_slice_and_the_collab_slice(tmp_path):
    cfg = _cfg(tmp_path, member_host=MEMBER)
    assert fcc.share_values(cfg)["share"]["member_host"] == MEMBER
    share_out, collab_out = tmp_path / "share.json", tmp_path / "collab.json"
    fcc.render_share(cfg, share_out)
    fcc.check_share_slice(load_fleet_config(share_out))
    fcc.render_collab(cfg, collab_out)
    members = json.loads(collab_out.read_text())["sections"]["control_plane"]["values"]["collab"]["members"]
    assert members["member_host"] == MEMBER and members["public_host"] == HOST
    assert "member_host" in fcc.SHARE_KEYS and "member_host" in fcc.SHARE_SLICE_KEYS


@pytest.mark.parametrize("bad", [HOST, MCP, "My.Artifacts.Example.Test", "https://my.example.test",
                                 "my.example.test:8443", "10.0.0.1", "", 7, ["a.example.test"]])
def test_a_bad_member_host_is_refused_naming_the_key(tmp_path, bad):
    with pytest.raises(FleetConfigError, match=r"control_plane\.share\.member_host"):
        fcc.share_values(_cfg(tmp_path, member_host=bad))


def test_a_bad_member_host_is_refused_even_while_share_is_disabled(tmp_path):
    cfg = _md.cfg_of(tmp_path, sh={"enabled": False, "public_host": HOST, "member_host": MCP})
    with pytest.raises(FleetConfigError, match=r"control_plane\.share\.member_host"):
        fcc.share_values(cfg)


def test_member_host_needs_members_enabled(tmp_path):
    cfg = _md.cfg_of(tmp_path, sh=_md.share(member_host=MEMBER))
    with pytest.raises(FleetConfigError, match=r"member_host"):
        fcc.collab_values(cfg)


def test_member_host_is_not_authored_under_members(tmp_path):
    cfg = _md.cfg_of(tmp_path, sh=_md.share(), mem=_md.members(member_host=MEMBER))
    with pytest.raises(FleetConfigError, match=r"control_plane\.share\.member_host"):
        fcc.collab_values(cfg)


def test_the_slice_form_of_members_accepts_member_host_and_validates_it():
    from herdr_engineering.control_plane.members_config import parse_members
    raw = {"enabled": True, "group": "g", "session_secret": "s" * 40, "public_host": HOST, "member_host": MEMBER}
    assert parse_members(raw)["member_host"] == MEMBER
    assert parse_members({k: v for k, v in raw.items() if k != "member_host"})["member_host"] is None
    for bad in (HOST, "Bad.Example.Test", 3):
        with pytest.raises(ValueError, match="member_host"):
            parse_members({**raw, "member_host": bad})


# ---------------------------------------------------------------------------------------------- the edge
def test_unset_the_edge_is_the_share_render_byte_for_byte():
    assert _edge(share_member_host=None) == _edge()
    assert "@share_member_host@" not in _edge() and MEMBER not in _edge()


def test_one_extra_server_serves_the_member_ui_alone():
    off, on = _edge(), _edge(share_member_host=MEMBER)
    assert "@share_member_host@" not in on and on.count("server_name ") == off.count("server_name ") + 1
    block = _md._server(on, MEMBER)
    assert _top_locations(block) == {"/api/member/", "= /", "/my/", "/"}
    assert "proxy_pass http://share;" not in block and "/s/" not in re.sub(r"#.*", "", block)
    assert set(re.findall(r"proxy_pass (\S+);", block)) == {"http://collab"}
    # the same listener, TLS, limits and headers as the share host's own server
    main = _md._server(on, HOST)
    for needle in ("listen 8443 ssl proxy_protocol;", "ssl_certificate     /etc/acme/fullchain.pem;",
                   "ssl_protocols TLSv1.2 TLSv1.3;", "if ($hcn_from_edge = 0) { return 444; }",
                   "limit_req_status 429;", "proxy_set_header X-Forwarded-For $remote_addr;",
                   "proxy_set_header X-Forwarded-Host \"\";", 'add_header Strict-Transport-Security "max-age=31536000" always;'):
        assert needle in main and needle in block, needle
    for loc in ("= /", "/my/", "/api/member/"):
        assert f"location {loc} {{" in main and f"location {loc} {{" in block
    # the member locations are the SAME text on both names
    member_text = (EDGE_DIR / "member-http.conf").read_text()
    assert member_text.strip() in main and member_text.strip() in block
    # the share host keeps serving /s/ and member routes unchanged (overlap phase)
    assert _md._server(off, HOST) == main and "location /s/ {" in main


def test_the_member_server_name_is_listed_once_and_the_share_one_unchanged():
    on = _edge(share_member_host=MEMBER)
    assert on.count(f"server_name {MEMBER};") == 1 and on.count(f"server_name {HOST};") == 1


@pytest.mark.parametrize("bad", [HOST, "Bad_Host", "https://x.example.test", "10.0.0.1"])
def test_the_edge_refuses_a_bad_member_host(bad):
    with pytest.raises(FleetConfigError, match="member_host"):
        _edge(share_member_host=bad)


def test_the_edge_refuses_a_member_host_without_members_or_share():
    with pytest.raises(FleetConfigError, match="member_host"):
        _md._edge(share=True, share_host=HOST, members=False, share_member_host=MEMBER)
    with pytest.raises(FleetConfigError, match="member_host"):
        _md._edge(share=False, share_member_host=MEMBER)


def test_render_edge_adds_the_member_server_only_when_the_config_sets_it(tmp_path):
    for sub, over, present in (("a", {}, False), ("b", {"member_host": MEMBER}, True)):
        out = tmp_path / sub / "nginx.conf"
        fcc.render_edge(_cfg(tmp_path / sub, **over), _sd.EDGE, out)
        assert (f"server_name {MEMBER};" in out.read_text()) is present
    assert (tmp_path / "a" / "nginx.conf").read_text() == _edge()
    assert (tmp_path / "b" / "nginx.conf").read_text() == _edge(share_member_host=MEMBER)


def test_member_host_while_share_is_disabled_adds_no_server(tmp_path):
    cfg = _md.cfg_of(tmp_path, sh={"enabled": False, "public_host": HOST, "member_host": MEMBER}, mem=_md.members())
    out = tmp_path / "nginx.conf"
    fcc.render_edge(cfg, _sd.EDGE, out)
    assert MEMBER not in out.read_text()


@pytest.mark.skipif(_md._docker_nginx() is None, reason="no docker nginx image: nginx -t not run")
@pytest.mark.parametrize("member_host", [None, MEMBER])
def test_both_renders_pass_nginx_t(tmp_path, member_host):
    (tmp_path / "acme").mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "acme/privkey.pem"),
                    "-out", str(tmp_path / "acme/fullchain.pem"), "-days", "1", "-subj", "/CN=t"],
                   check=True, capture_output=True)
    (tmp_path / "nginx.conf").write_text(_edge(share_member_host=member_host))
    out = subprocess.run(["docker", "run", "--rm", "--add-host", "collab-public:127.0.0.1", "--add-host",
                          "share-public:127.0.0.1", "-v", f"{tmp_path}/nginx.conf:/etc/nginx/nginx.conf:ro",
                          "-v", f"{tmp_path}/acme:/etc/acme:ro", _md._docker_nginx(), "nginx", "-t"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "test is successful" in out.stderr


# ---------------------------------------------------------------------------------------------- deploy.sh
def _set(e, member_host):
    """Share + members enabled in the deploy fixture's central file (member_host optional)."""
    _sd._set_share(e, {"enabled": True, "signing_secret": _sd.SECRET, "public_host": _sd.SHARE_HOST,
                       **({"member_host": member_host} if member_host else {})})
    data = yaml.safe_load(e["central"].read_text())
    cpv = data["sections"]["control_plane"]["values"]
    cpv["collab"]["members"] = _md.members()
    cpv["collab_artifacts"] = {"enabled": True}
    cpv["api_keys"] = {"placeholder-api-key-value": "operator"}      # members need API keys (the prerequisite)
    e["central"].unlink()
    fcc.write_private_file(e["central"], yaml.safe_dump(data, sort_keys=False))


def test_the_certificate_must_name_the_share_member_host(env):
    _set(env, MEMBER)
    _sd._live_cert(env, MCP, _sd.SHARE_HOST)          # names the share host, not the member host
    r = _sd._ds.deploy(env)
    assert r.returncode != 0
    assert MEMBER in r.stderr and "certificate" in r.stderr
    assert not (env["tmp"] / "edge-certs" / "fullchain.pem").exists()
    assert _md.MEMBER_SECRET not in r.stdout + r.stderr and _sd.SECRET not in r.stdout + r.stderr


def test_a_deploy_with_the_member_host_checks_renewals_and_probes_it(env):
    _set(env, MEMBER)
    _sd._live_cert(env, MCP, _sd.SHARE_HOST, MEMBER)
    r = _sd._ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    unit = (env["home"] / ".config/systemd/user/herdr-collab-sync.service").read_text()
    assert f"--share-host {_sd.SHARE_HOST}" in unit and f"--member-host {MEMBER}" in unit
    assert f"curl https://{MEMBER}/s/probe/" in env["log"].read_text()
    edge = (env["home"] / ".config/herdr-engineering/collab-edge/nginx.conf").read_text()
    assert f"server_name {MEMBER};" in edge and "location /s/ {" in edge


def test_a_deploy_without_the_member_host_neither_checks_nor_probes_it(env):
    _set(env, None)
    _sd._live_cert(env, MCP, _sd.SHARE_HOST)
    r = _sd._ds.deploy(env)
    assert r.returncode == 0, r.stdout + r.stderr
    unit = (env["home"] / ".config/systemd/user/herdr-collab-sync.service").read_text()
    assert "--member-host" not in unit and MEMBER not in env["log"].read_text()
    assert MEMBER not in (env["home"] / ".config/herdr-engineering/collab-edge/nginx.conf").read_text()


# ------------------------------------------------------------------- the Host header reaches the app as the client sent it
AH = "artifacts2.example.test"
AH_MEMBER = "my.artifacts2.example.test"


def _host_lines(block: str) -> list[str]:
    return re.findall(r"^\s*proxy_set_header Host (\S+);", block, re.M)


def test_both_member_blocks_forward_the_clients_own_host_header():
    """nginx's $host is lowercased and loses the port, which would defeat the app's exact Host match."""
    share_on = _edge(share_member_host=MEMBER)
    ah_on = _md._edge(share=False, artifact_host=AH, member_host=AH_MEMBER, mcp_hosts=[])
    for block in (_md._server(share_on, MEMBER), _md._server(ah_on, AH_MEMBER)):
        assert _host_lines(block) == ["$http_host"]
        assert "$host" not in re.sub(r"#.*", "", block)


def test_the_public_blocks_still_forward_the_normalised_host():
    share_on = _edge(share_member_host=MEMBER)
    ah_on = _md._edge(share=False, artifact_host=AH, member_host=AH_MEMBER, mcp_hosts=[])
    assert _host_lines(_md._server(share_on, HOST)) == ["$host"]
    assert _host_lines(_md._server(ah_on, AH)) == ["$host"]
