"""End-to-end CLI tests for the herdr-eng console entry point."""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering import cli
from herdr_engineering.cli import _parse_api_keys, _seed_demo, main
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def test_seed_demo_populates_repo():
    repo = ControlPlaneRepo()
    _seed_demo(repo)
    assert len(repo.list_missions()) == 2
    assert len(repo.list_sessions()) == 2
    assert len(repo.list_hosts()) == 2
    assert len(repo.list_services()) == 3
    assert len(repo.recent_events(limit=100)) > 0


def test_parse_api_keys_rbac_pairs():
    assert _parse_api_keys(None, "k1=viewer,k2=operator") == {
        "k1": "viewer", "k2": "operator"}
    assert _parse_api_keys("sk", None) == {"sk": "operator"}
    assert _parse_api_keys("sk", "k=viewer") == {"sk": "operator", "k": "viewer"}
    assert _parse_api_keys(None, None) is None


def test_version_flag(capsys):
    with pytest_raises_SystemExit():
        main(["--version"])
    out = capsys.readouterr().out
    assert "herdr-eng" in out


def test_name_command_json(capsys):
    rc = main(["name", "--spec-id", "0110", "--spec-title", "dev fabric", "--json"])
    assert rc == 0
    d = json.loads(capsys.readouterr().out)
    assert d["name"] == "0110-dev-fabric"
    assert d["name_source"] == "auto_spec"


def test_name_issue_identity():
    import io
    old = sys.stdout; sys.stdout = io.StringIO()
    try:
        rc = main(["name", "--issue-number", "421", "--issue-title", "test tree"])
    finally:
        out = sys.stdout.getvalue(); sys.stdout = old
    assert rc == 0
    assert out.strip() == "421-test-tree"


def test_candidate_id_command(capsys):
    rc = main(["candidates", "id", "--repo", "r", "--worktree", "wt", "--branch", "b"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["candidate_id"].startswith("cand_")


def test_tests_adapters(capsys):
    rc = main(["tests", "adapters"])
    assert rc == 0
    d = json.loads(capsys.readouterr().out)
    assert "pytest" in d["adapters"]


def test_ci_pipelines_offline_safe(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))  # hermetic: no token file
    os.environ.pop("HERDR_ENGINEERING_CI_TOKEN", None)
    rc = main(["ci", "pipelines", "--repo", "r"])
    assert rc == 0
    d = json.loads(capsys.readouterr().out)
    assert d["available"] is False
    assert d["pipelines"] == []


def test_unknown_command_returns_error(capsys):
    import pytest
    with pytest.raises(SystemExit) as exc:
        main(["doesnotexist"])
    assert exc.value.code != 0


def pytest_raises_SystemExit():
    import pytest
    return pytest.raises(SystemExit)


def test_maybe_pg_source_resolves_correct_module():
    """Regression: _maybe_pg_source must import PostgresSource from
    control_plane.bridge (the real module). Before the fix it used
    `from .bridge` -> herdr_engineering.bridge, a ModuleNotFoundError."""
    from herdr_engineering.control_plane.bridge import PostgresSource

    src = cli._maybe_pg_source("postgresql://u:p@127.0.0.1:5432/herdr")
    assert isinstance(src, PostgresSource)
    assert cli._maybe_pg_source(None) is None
    assert cli._maybe_pg_source("") is None
