"""End-to-end CLI tests for the herdr-eng console entry point."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering import cli
from herdr_engineering.cli import _parse_api_keys, _seed_demo, main
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def test_seed_demo_populates_repo():
    repo = ControlPlaneRepo()
    _seed_demo(repo)
    assert len(repo.list_missions()) == 2
    assert len(repo.list_sessions()) == 2
    assert len(repo.list_hosts()) == 2
    assert len(repo.list_services()) == 3
    assert len(repo.recent_events(limit=100)) > 0


def test_local_artifact_upload_uses_restored_control_plane(tmp_path, capsys):
    import urllib.request

    repo = ControlPlaneRepo()
    session_id = repo.create_session()
    server = ControlPlaneServer(repo)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    base = f"http://127.0.0.1:{server.port}"
    path = tmp_path / "release notes.txt"
    path.write_bytes(b"restored artifact upload")
    try:
        assert main(["artifacts", "upload", str(path), "--control-plane", base,
                     "--mime", "text/plain", "--session", session_id]) == 0
        artifact = json.loads(capsys.readouterr().out)
        assert artifact["original_filename"] == path.name
        assert artifact["session_id"] == session_id
        url = f"{base}/api/v1/artifacts/{artifact['artifact_id']}/download"
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.read() == path.read_bytes()
    finally:
        server.shutdown()
        thread.join(timeout=5)


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


@pytest.fixture
def pi_feed_server():
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, api_keys={
        "cli-ingest-secret": "ingest", "cli-viewer-secret": "viewer",
    })
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        yield f"http://127.0.0.1:{server.port}", repo
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def pi_feed_snapshot(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_bytes((Path(__file__).parent / "fixtures/pi-snapshot-v3.json").read_bytes())
    return path


@pytest.mark.parametrize("interval_args", [[], ["--interval", "0"]])
def test_feed_pi_once_posts_snapshot(pi_feed_server, pi_feed_snapshot, monkeypatch,
                                     capsys, caplog, interval_args):
    base, repo = pi_feed_server
    monkeypatch.setenv("HERDR_CP_INGEST_KEY", "cli-ingest-secret")
    before = pi_feed_snapshot.read_bytes()
    real_sleep = time.sleep
    cli_thread = threading.current_thread()

    def no_sleep(seconds):
        # The HTTP server's maintenance threads still need their real sleeps.
        if threading.current_thread() is not cli_thread:
            return real_sleep(seconds)
        pytest.fail("once mode must not sleep")

    monkeypatch.setattr(time, "sleep", no_sleep)
    assert main(["control-plane", "feed-pi", "--snapshot", str(pi_feed_snapshot),
                 "--url", base, *interval_args]) == 0
    missions = repo.list_missions()
    assert {m["mission_key"] for m in missions} == {
        "pi:MSN-golden-nulls", "pi:MSN-golden-full"}
    assert {m["status"] for m in missions} == {"NEW"}
    assert pi_feed_snapshot.read_bytes() == before
    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": True, "posted": 2}
    assert "cli-ingest-secret" not in output.out + output.err + caplog.text


@pytest.mark.parametrize("key", [None, "", "   "])
def test_feed_pi_missing_key_exits_two(pi_feed_server, tmp_path, monkeypatch, capsys,
                                      fake_host_config, key):
    base, repo = pi_feed_server
    fake_host_config({"ingest": {"api_key": "host-key-must-not-be-used"}})
    if key is None:
        monkeypatch.delenv("HERDR_CP_INGEST_KEY", raising=False)
    else:
        monkeypatch.setenv("HERDR_CP_INGEST_KEY", key)
    assert main(["control-plane", "feed-pi", "--snapshot", str(tmp_path / "missing"),
                 "--url", base, "--interval", "0"]) == 2
    output = capsys.readouterr()
    assert "HERDR_CP_INGEST_KEY" in output.err
    assert "host-key-must-not-be-used" not in output.out + output.err
    assert repo.list_missions() == []


def test_feed_pi_uses_host_url(pi_feed_server, pi_feed_snapshot, monkeypatch,
                              fake_host_config):
    base, repo = pi_feed_server
    fake_host_config(control_plane_url=base)
    monkeypatch.setenv("HERDR_CP_INGEST_KEY", "cli-ingest-secret")
    assert main(["control-plane", "feed-pi", "--snapshot", str(pi_feed_snapshot)]) == 0
    assert len(repo.list_missions()) == 2


def test_feed_pi_missing_url_exits_two(pi_feed_snapshot, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_CP_INGEST_KEY", "cli-ingest-secret")
    assert main(["control-plane", "feed-pi", "--snapshot", str(pi_feed_snapshot)]) == 2
    assert "--url" in capsys.readouterr().err


def test_feed_pi_interval_rereads_snapshot(pi_feed_server, pi_feed_snapshot, monkeypatch):
    base, repo = pi_feed_server
    monkeypatch.setenv("HERDR_CP_INGEST_KEY", "cli-ingest-secret")
    waits = []
    real_sleep = time.sleep
    cli_thread = threading.current_thread()

    def next_cycle(seconds):
        if threading.current_thread() is not cli_thread:
            return real_sleep(seconds)
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt
        doc = json.loads(pi_feed_snapshot.read_text())
        doc["missions"][0]["title"] = "Updated between cycles"
        pi_feed_snapshot.write_text(json.dumps(doc))

    monkeypatch.setattr(time, "sleep", next_cycle)
    assert main(["control-plane", "feed-pi", "--snapshot", str(pi_feed_snapshot),
                 "--url", base, "--interval", "0.25"]) == 130
    assert waits == [0.25, 0.25]
    missions = {m["mission_key"]: m for m in repo.list_missions()}
    assert len(missions) == 2
    assert missions["pi:MSN-golden-nulls"]["title"] == "Updated between cycles"


@pytest.mark.parametrize("interval", ["-1", "nan", "inf", "-inf", "1e300"])
def test_feed_pi_rejects_invalid_interval(pi_feed_server, pi_feed_snapshot, monkeypatch,
                                         capsys, interval):
    base, repo = pi_feed_server
    monkeypatch.setenv("HERDR_CP_INGEST_KEY", "cli-ingest-secret")
    assert main(["control-plane", "feed-pi", "--snapshot", str(pi_feed_snapshot),
                 "--url", base, f"--interval={interval}"]) == 2
    assert "--interval" in capsys.readouterr().err
    assert repo.list_missions() == []


@pytest.mark.parametrize("key", ["rejected-secret", "cli-viewer-secret", "bad\nsecret", "unicode-\u2603-secret"])
def test_feed_pi_rejected_key_is_not_exposed(pi_feed_server, pi_feed_snapshot,
                                           monkeypatch, capsys, caplog, key):
    base, repo = pi_feed_server
    monkeypatch.setenv("HERDR_CP_INGEST_KEY", key)
    assert main(["control-plane", "feed-pi", "--snapshot", str(pi_feed_snapshot),
                 "--url", base, "--interval", "1"]) == 1
    output = capsys.readouterr()
    assert "HERDR_CP_INGEST_KEY" in output.err
    assert key not in output.out + output.err + caplog.text
    assert repo.list_missions() == []


@pytest.mark.parametrize("contents", [
    None, "not json", "[]", '{"contractVersion": 9}',
    '{"contractVersion": 3, "missions": null}',
    '{"contractVersion": 3, "missions": [null]}',
    '{"contractVersion": 3, "missions": [{}]}',
])
def test_feed_pi_invalid_snapshot_fails_safely(tmp_path, monkeypatch, capsys, contents):
    key = "path-secret"
    monkeypatch.setenv("HERDR_CP_INGEST_KEY", key)
    path = tmp_path / key
    if contents is not None:
        path.write_text(contents)
    assert main(["control-plane", "feed-pi", "--snapshot", str(path),
                 "--url", "http://127.0.0.1:1"]) == 2
    output = capsys.readouterr()
    assert "snapshot" in output.err.lower()
    assert key not in output.out + output.err


def test_feed_pi_invalid_url_is_sanitized(pi_feed_snapshot, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_CP_INGEST_KEY", "url-secret")
    assert main(["control-plane", "feed-pi", "--snapshot", str(pi_feed_snapshot),
                 "--url", "http://url-secret@localhost"]) == 1
    output = capsys.readouterr()
    assert "--url" in output.err
    assert "url-secret" not in output.out + output.err


def test_fabric_compaction_settings_default_to_off_and_dry_run():
    from herdr_engineering.cli import _fabric_compaction_settings
    off = _fabric_compaction_settings({})
    assert off.enabled is False and off.dry_run is True and off.rebuild_checkpoint is True
    on = _fabric_compaction_settings({"fabric_compaction": {"enabled": True, "dry_run": False, "min_age_days": 5,
                                                            "archive_retention_days": None}})
    assert on.enabled and not on.dry_run and on.min_age_days == 5.0 and on.archive_retention_days is None
    assert _fabric_compaction_settings({"fabric_compaction": {"enabled": "yes"}}).enabled is False
    with pytest.raises(SystemExit):
        _fabric_compaction_settings({"fabric_compaction": {"min_age_days": 0}})
