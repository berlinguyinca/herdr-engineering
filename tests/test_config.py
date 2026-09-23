"""Config loading: precedence, auto-discovery, merging (spec 0050)."""
import textwrap
from pathlib import Path

from herdr_engineering.config import (
    DEFAULTS,
    _deep_merge,
    _discover_config_paths,
    load_config,
)
from herdr_engineering.errors import InvalidError


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))
    return path


def test_defaults_when_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HERDR_ENGINEERING_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = load_config()
    assert cfg["version"] == 1
    assert cfg["web"]["private_only"] is True
    assert cfg["security"]["allow_public_listeners"] is False


def test_discovery_finds_machine_local_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write(home / ".config/herdr-engineering/config.yaml", """
        version: 1
        web:
          port: 9999
    """)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERDR_ENGINEERING_CONFIG", raising=False)
    found = _discover_config_paths()
    assert [str(p) for p in found] == [str(home / ".config/herdr-engineering/config.yaml")]
    assert load_config()["web"]["port"] == 9999


def test_repo_config_overrides_machine_local(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write(home / ".config/herdr-engineering/config.yaml", """
        version: 1
        web:
          port: 9999
          private_only: true
    """)
    repo = tmp_path / "repo"
    _write(repo / "config/herdr-engineering.yaml", """
        version: 1
        web:
          port: 8787
    """)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    monkeypatch.delenv("HERDR_ENGINEERING_CONFIG", raising=False)
    cfg = load_config()
    # repo port wins; machine-local private_only preserved via merge
    assert cfg["web"]["port"] == 8787
    assert cfg["web"]["private_only"] is True


def test_env_var_highest_precedence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write(home / ".config/herdr-engineering/config.yaml", """
        version: 1
        web:
          port: 9999
    """)
    env_cfg = tmp_path / "explicit.yaml"
    _write(env_cfg, """
        version: 1
        web:
          port: 1234
    """)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", str(env_cfg))
    assert load_config()["web"]["port"] == 1234


def test_deep_merge_preserves_unrelated_keys():
    merged = _deep_merge(DEFAULTS, {"web": {"port": 8000}})
    assert merged["web"]["port"] == 8000
    assert merged["web"]["host"] == "127.0.0.1"  # unrelated key preserved
    assert merged["dev_fabric"]["enabled"] is True


def test_env_expansion(tmp_path, monkeypatch):
    p = _write(tmp_path / "env.yaml", """
        version: 1
        artifacts:
          root: "${TEST_CFG_ROOT:-/default/root}"
    """)
    monkeypatch.delenv("TEST_CFG_ROOT", raising=False)
    assert load_config([p])["artifacts"]["root"] == "/default/root"
    monkeypatch.setenv("TEST_CFG_ROOT", "/custom/root")
    assert load_config([p])["artifacts"]["root"] == "/custom/root"


def test_invalid_version_rejected(tmp_path):
    p = _write(tmp_path / "bad.yaml", "version: 999\n")
    try:
        load_config([p])
        raise AssertionError("expected InvalidError")
    except InvalidError:
        pass
