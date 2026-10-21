import hashlib
import json
import os
import re
import stat

import pytest
import yaml

from herdr_engineering import fleet, fleet_config_cli as fcc
from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config

ANSWERS = {
    "public_url": "https://cp.example.test", "llm_upstream_url": "https://llm.example.test",
    "llm_upstream_key": "placeholder-llm-value", "ci_url": "https://ci.example.test",
    "ci_token": "placeholder-ci-value", "web_bind": "127.0.0.1", "web_port": "8080",
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
    assert len(env["HERDR_CP_PG_PASSWORD"]) == 32 and env["FABRIC_LLM_UPSTREAM_KEY"] == "placeholder-llm-value"
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
         "'", "plain", "x\n'y\"$z\\"]


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
    assert "revision=" in out and "hosts=2" in out and "placeholder-llm-value" not in out
