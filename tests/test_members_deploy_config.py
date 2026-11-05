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
_central, COLLAB, SECRET = _sd._central, _sd.COLLAB, _sd.SECRET

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
