import hashlib
import json
import os
import re
import stat
from pathlib import Path

import pytest
import yaml

from herdr_engineering import fleet, fleet_config_cli as fcc
from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config

ANSWERS = {
    "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
    "llm_upstream_key": "llm-secret-key", "ci_url": "https://ci.example.test",
    "ci_token": "ci-secret-token", "web_bind": "127.0.0.1", "web_port": "8080", "gateway_tailnet_ip": "127.0.0.1",
    "hosts": {"bender": ["builder"], "mac": ["host"]},
}


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def no_prompt(*_a):
    raise AssertionError("prompted although answers were complete")


def make(tmp_path):
    return fcc.init_config(tmp_path / "fleet-config.yaml", dict(ANSWERS), prompt=no_prompt,
                           secret_prompt=no_prompt)


def parse_env_file(text):
    """Reference parser of compose env_file semantics (quoted values only)."""
    out, i = {}, 0
    while i < len(text):
        if text[i] == "\n":
            i += 1
            continue
        m = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=").match(text, i)
        assert m, text[i:i + 20]
        key, i = m.group(1), m.end()
        q = text[i]
        assert q in "'\""
        i += 1
        val = []
        while True:
            c = text[i]
            if q == "'":
                if c == "'":
                    assert not (val and val[-1] == "\\"), "compose rejects a single-quoted value ending in a backslash"
                    i += 1
                    break
                val.append(c)
                i += 1
            else:
                if c == '"':
                    i += 1
                    break
                if c == "\\":
                    n = text[i + 1]
                    val.append({"n": "\n"}.get(n, n))
                    i += 2
                    continue
                assert c != "$", "unescaped $ in double quotes would interpolate"
                val.append(c)
                i += 1
        assert text[i] == "\n"
        out[key] = "".join(val)
    return out


def test_init_writes_0600_and_validates(tmp_path):
    p = make(tmp_path)
    assert mode(p) == 0o600
    cfg = load_fleet_config(p)
    assert set(cfg.status()["hosts"]) == {"bender", "mac"}
    env = fcc.compose_env(cfg)
    assert len(env["HERDR_CP_PG_PASSWORD"]) == 32 and cfg.section("llm")["upstream_key"] == "llm-secret-key"
    assert env["HERDR_CP_WEB_PUBLIC_PORT"] == "8080"
    assert fcc.validate_config(p)["hosts"] == 2


def test_init_prompts_secrets_via_secret_prompt(tmp_path):
    asked, secret_asked = [], []
    partial = {k: v for k, v in ANSWERS.items() if k not in ("llm_upstream_key", "ci_token")}
    fcc.init_config(tmp_path / "f.yaml", partial, prompt=lambda m: asked.append(m) or "x",
                    secret_prompt=lambda m: secret_asked.append(m) or "sekret")
    assert len(secret_asked) == 2 and not any("key" in m or "token" in m for m in asked)


def test_init_refuses_overwrite(tmp_path):
    p = make(tmp_path)
    before = p.read_text()
    with pytest.raises(FleetConfigError):
        fcc.init_config(p, dict(ANSWERS), prompt=no_prompt, secret_prompt=no_prompt)
    assert p.read_text() == before


def test_mint_token_hash_roundtrip():
    tok, dig = fcc.mint_host_token()
    assert len(tok) >= 43 and dig == hashlib.sha256(tok.encode()).hexdigest()
    assert fcc.mint_host_token()[0] != tok


NASTY = ["pa$$word", "a'b\"c", "line1\nline2", "back\\slash # not comment", "$(rm -rf) ${HOME}",
         "'", "plain", "abc\\", "\\", "x\n'y\"$z\\"]


@pytest.mark.parametrize("value", NASTY)
def test_render_compose_preserves_dollar_quote_newline_values(tmp_path, value):
    p = make(tmp_path)
    data = yaml.safe_load(p.read_text())
    data["sections"]["control_plane"]["values"]["compose"]["FABRIC_LLM_UPSTREAM_KEY"] = value
    p.write_text(yaml.safe_dump(data))
    out = tmp_path / "sub" / "compose.env"
    fcc.render_compose(load_fleet_config(p), out)
    assert mode(out) == 0o600
    parsed = parse_env_file(out.read_text())
    assert parsed["FABRIC_LLM_UPSTREAM_KEY"] == value
    assert parsed["HERDR_CP_WEB_BIND"] == "127.0.0.1"


def test_render_rejects_bad_values_naming_key_only(tmp_path):
    p = make(tmp_path)
    data = yaml.safe_load(p.read_text())
    data["sections"]["control_plane"]["values"]["compose"]["FABRIC_LLM_UPSTREAM_KEY"] = "top\rsecret"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(FleetConfigError) as e:
        fcc.render_compose(load_fleet_config(p), tmp_path / "o.env")
    assert "FABRIC_LLM_UPSTREAM_KEY" in str(e.value) and "topsecret" not in str(e.value)
    assert not (tmp_path / "o.env").exists()


class FakeSSH:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return type("P", (), {"returncode": self.rc, "stderr": "", "stdout": ""})()


def test_join_adds_host_and_never_prints_token_in_report(tmp_path, monkeypatch):
    p = make(tmp_path)
    ssh = FakeSSH()
    monkeypatch.setattr(fcc.subprocess, "run", ssh)
    monkeypatch.setattr(fleet, "_run", lambda *a, **k: (0, "", ""))
    inv = tmp_path / "hosts.yml"
    rep = fleet.fleet_join(ssh_target="newbox", label="newbox", attach=False, provision=False,
                           inventory=inv, host_vars_dir=tmp_path / "hv", fleet_config=p)
    argv, kw = ssh.calls[0]
    token = json.loads(kw["input"])["token"]
    assert rep["steps"]["config"]["token"] == "<written to host>"
    assert token not in json.dumps(rep) and not any(token in a for a in argv)
    payload = json.loads(kw["input"])
    assert payload["host_id"] == "newbox" and payload["control_plane_url"] == "https://cp.example.test"
    cfg = load_fleet_config(p)
    assert cfg.host_for_token(token) == "newbox"
    assert cfg.status()["hosts"]["newbox"] == ["host"]
    assert mode(p) == 0o600
    baks = list(tmp_path.glob("fleet-config.yaml.bak-*"))
    assert len(baks) == 1 and mode(baks[0]) == 0o600


def test_failed_ssh_leaves_file_unchanged(tmp_path, monkeypatch):
    p = make(tmp_path)
    before = p.read_bytes()
    monkeypatch.setattr(fcc.subprocess, "run", FakeSSH(rc=255))
    with pytest.raises(FleetConfigError):
        fcc.issue_host_credentials(p, "bender", "bender")
    assert p.read_bytes() == before and not list(tmp_path.glob("*.bak-*"))


def test_token_rotates_existing_host_and_local_target(tmp_path, monkeypatch):
    p = make(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    old = load_fleet_config(p)
    rep = fcc.issue_host_credentials(p, "bender", "local", must_exist=True)
    assert rep["token"] == "<written to host>"
    hj = home / ".config/herdr-engineering/host.json"
    assert mode(hj) == 0o600
    tok = json.loads(hj.read_text())["token"]
    new = load_fleet_config(p)
    assert new.host_for_token(tok) == "bender" and new.status()["hosts"]["bender"] == ["builder"]
    assert new.revision != old.revision
    with pytest.raises(FleetConfigError):
        fcc.issue_host_credentials(p, "ghost", "local", must_exist=True)


def test_cli_validate_prints_no_values(tmp_path, capsys):
    from herdr_engineering.cli import main
    p = make(tmp_path)
    assert main(["fleet", "config", "validate", "--path", str(p)]) == 0
    out = capsys.readouterr().out
    assert "revision=" in out and "hosts=2" in out and "llm-secret-key" not in out


def test_init_generates_ingest_key_in_both_places_and_host_sections(tmp_path):
    path = make(tmp_path)
    cfg = load_fleet_config(path)
    cp = cfg.section("control_plane")
    ingest_key = cfg.section("ingest")["api_key"]
    assert cp["api_keys"][ingest_key] == "ingest"
    agent_key = cfg.section("agent")["api_key"]
    assert cp["api_keys"][agent_key] == "agent" and agent_key != ingest_key
    assert sorted(cp["api_keys"].values()) == ["agent", "ingest", "operator"]
    assert cfg.section("github") == {"token": ""}
    assert cfg.section("fabric") == {"url": "", "token": ""}
    assert cfg.section("inspector") == {"enabled": False, "reasoning": "off"}
    assert "host" not in cfg._sections
    served = cfg.slice_for("mac")  # a roleless-ish host still gets the "all" sections
    assert {"ingest", "agent", "fabric", "inspector"} <= set(served)
    assert "github" not in served and "control_plane" not in served


# ----------------------------------------------------- gateway ip / compose map / in place

def _answers_without_gateway():
    a = dict(ANSWERS)
    del a["gateway_tailnet_ip"]
    return a


def test_init_prompts_for_gateway_ip_defaulting_to_tailscale(tmp_path, monkeypatch):
    monkeypatch.setattr(fcc, "_tailscale_ip", lambda: "100.64.0.7")
    seen = []

    def prompt(label):
        seen.append(label)
        return ""  # accept the default
    p = fcc.init_config(tmp_path / "f.yaml", _answers_without_gateway(), prompt=prompt,
                        secret_prompt=no_prompt)
    assert any("gateway tailnet IP" in s and "[100.64.0.7]" in s for s in seen)
    assert fcc.compose_env(load_fleet_config(p))["GATEWAY_TAILNET_IP"] == "100.64.0.7"


def test_init_asks_explicitly_when_tailscale_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(fcc, "_tailscale_ip", lambda: None)
    with pytest.raises(FleetConfigError, match="gateway_tailnet_ip is required"):
        fcc.init_config(tmp_path / "a.yaml", _answers_without_gateway(), prompt=lambda _l: "",
                        secret_prompt=no_prompt)
    p = fcc.init_config(tmp_path / "b.yaml", _answers_without_gateway(), prompt=lambda _l: "100.9.9.9",
                        secret_prompt=no_prompt)
    assert fcc.compose_env(load_fleet_config(p))["GATEWAY_TAILNET_IP"] == "100.9.9.9"


def test_tailscale_ip_takes_first_line_and_tolerates_failure(monkeypatch):
    import subprocess as sp

    class R:
        returncode, stdout = 0, "100.1.1.1\n100.1.1.2\n"
    monkeypatch.setattr(sp, "run", lambda *a, **k: R())
    assert fcc._tailscale_ip() == "100.1.1.1"
    R.returncode = 1
    assert fcc._tailscale_ip() is None

    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(sp, "run", boom)
    assert fcc._tailscale_ip() is None


def test_gateway_bind_has_no_duplicate_for_loopback(tmp_path):
    p = make(tmp_path)
    assert fcc.compose_env(load_fleet_config(p))["GATEWAY_BIND"] == "127.0.0.1"
    a = dict(ANSWERS, gateway_tailnet_ip="100.64.0.7")
    q = fcc.init_config(tmp_path / "t.yaml", a, prompt=no_prompt, secret_prompt=no_prompt)
    assert fcc.compose_env(load_fleet_config(q))["GATEWAY_BIND"] == "127.0.0.1,100.64.0.7"


def test_init_compose_map_holds_only_what_compose_substitutes(tmp_path):
    p = make(tmp_path)
    keys = set(yaml.safe_load(p.read_text())["sections"]["control_plane"]["values"]["compose"])
    assert keys == set(fcc.COMPOSE_REQUIRED)


def _rewrite(path, mutate):
    data = yaml.safe_load(path.read_text())
    mutate(data["sections"]["control_plane"]["values"])
    path.unlink()
    fcc.write_private_file(path, yaml.safe_dump(data, sort_keys=False))


def test_validate_requires_compose_map_and_names_missing_keys_only(tmp_path):
    p = make(tmp_path)
    secret = yaml.safe_load(p.read_text())["sections"]["control_plane"]["values"]["compose"]["HERDR_CP_PG_PASSWORD"]
    _rewrite(p, lambda v: v["compose"].pop("HERDR_CP_WEB_BIND"))
    with pytest.raises(FleetConfigError) as e:
        fcc.validate_config(p)
    assert "HERDR_CP_WEB_BIND" in str(e.value) and secret not in str(e.value)
    _rewrite(p, lambda v: v.pop("compose"))
    with pytest.raises(FleetConfigError, match="compose"):
        fcc.validate_config(p)


def test_example_file_matches_what_the_code_requires():
    ex = yaml.safe_load((Path(__file__).resolve().parents[1] / "deploy/control-plane/fleet-config.example.yaml").read_text())
    vals = ex["sections"]["control_plane"]["values"]
    assert set(vals["compose"]) >= set(fcc.COMPOSE_REQUIRED)
    assert "minio_user" in vals and "minio_password" in vals and "ci_actions" in vals
    assert "rustfs_access_key" not in vals and "rustfs_secret_key" not in vals


def test_set_host_writes_in_place_keeping_inode_and_mode(tmp_path):
    p = make(tmp_path)
    before = os.stat(p)
    fcc.set_host(p, "bender", "a" * 64)
    after = os.stat(p)
    assert (after.st_ino, after.st_dev) == (before.st_ino, before.st_dev)
    assert mode(p) == 0o600
    assert load_fleet_config(p).status()["hosts"]["bender"] == ["builder"]
    assert yaml.safe_load(p.read_text())["hosts"]["bender"]["token_sha256"] == "a" * 64
    backups = list(tmp_path.glob("fleet-config.yaml.bak-*"))
    assert len(backups) == 1 and mode(backups[0]) == 0o600
    # a second write still keeps the inode
    fcc.set_host(p, "mac", "b" * 64)
    assert os.stat(p).st_ino == before.st_ino
