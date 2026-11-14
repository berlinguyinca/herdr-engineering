"""Control-plane callers that used to send no key: `fleet drift`, `artifacts upload` and the herdr-artifact
wrapper. Each runs against a REAL keyed control plane: no key -> 401, a key file -> accepted. The key never
appears in output, and a key file that is not mode 0600 is refused."""
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from herdr_engineering.cli import main
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "skills/herdr-artifact/scripts/herdr-artifact"
KEYS = {"viewer-secret-k": "viewer", "ingest-secret-k": "ingest", "operator-secret-k": "operator"}


@pytest.fixture
def cp():
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys=dict(KEYS))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    yield f"http://127.0.0.1:{server.port}"
    server.shutdown()


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERDR_API_KEY", raising=False)


def keyfile(tmp_path, key, mode=0o600):
    p = tmp_path / "key"
    p.write_text(key + "\n")
    p.chmod(mode)
    return str(p)


def test_fleet_drift_without_a_key_is_refused_by_a_keyed_server(cp, capsys):
    assert main(["fleet", "drift", "--control-plane", cp, "--json"]) == 1
    assert "401" in capsys.readouterr().err


def test_fleet_drift_sends_the_key_file(cp, tmp_path, capsys):
    kf = keyfile(tmp_path, "viewer-secret-k")
    assert main(["fleet", "drift", "--control-plane", cp, "--api-key-file", kf, "--json"]) == 0
    out = capsys.readouterr()
    assert "viewer-secret-k" not in out.out + out.err


def test_fleet_drift_refuses_a_world_readable_key_file(cp, tmp_path, capsys):
    kf = keyfile(tmp_path, "viewer-secret-k", 0o644)
    assert main(["fleet", "drift", "--control-plane", cp, "--api-key-file", kf]) == 2
    out = capsys.readouterr()
    assert "0600" in out.err and "viewer-secret-k" not in out.out + out.err


def test_art_upload_role_matrix_documents_that_operator_is_required(cp, tmp_path, capsys):
    """A bare POST /api/v1/artifacts is not in the agent POST allow-list: viewer and ingest keys get 403, only
    an operator key gets past the role gate (what the server answers after that is not this test's concern)."""
    data = tmp_path / "a.txt"
    data.write_text("hello")
    for role, key in (("viewer", "viewer-secret-k"), ("ingest", "ingest-secret-k")):
        rc = main(["artifacts", "upload", str(data), "--control-plane", cp,
                   "--api-key-file", keyfile(tmp_path, key), "--mime", "text/plain"])
        assert rc == 1 and "403" in capsys.readouterr().err, role
    main(["artifacts", "upload", str(data), "--control-plane", cp,
          "--api-key-file", keyfile(tmp_path, "operator-secret-k"), "--mime", "text/plain"])
    err = capsys.readouterr().err
    assert "401" not in err and "403" not in err
    assert main(["artifacts", "upload", str(data), "--control-plane", cp, "--mime", "text/plain"]) == 1
    assert "401" in capsys.readouterr().err


def run_wrapper(*args):
    return subprocess.run(["sh", str(WRAPPER), *args], capture_output=True, text=True,
                          env=dict(os.environ), timeout=30)


def test_wrapper_history_sends_the_key_file_and_never_prints_it(cp, tmp_path):
    bad = run_wrapper("history", "art_x", "--control-plane", cp)
    assert bad.returncode != 0 and "401" in bad.stderr
    ok = run_wrapper("history", "art_x", "--control-plane", cp,
                     "--api-key-file", keyfile(tmp_path, "viewer-secret-k"))
    assert "401" not in ok.stderr and "viewer-secret-k" not in ok.stdout + ok.stderr


def test_wrapper_share_request_needs_the_operator_key(cp, tmp_path):
    args = ("share-request", "art_x", "ver_x", "--expires-at", "9999999999", "--control-plane", cp)
    res = run_wrapper(*args, "--api-key-file", keyfile(tmp_path, "operator-secret-k"))
    assert "401" not in res.stderr and "403" not in res.stderr
    assert "operator-secret-k" not in res.stdout + res.stderr
    viewer = run_wrapper(*args, "--api-key-file", keyfile(tmp_path, "viewer-secret-k"))
    assert "403" in viewer.stderr


def test_wrapper_refuses_a_loose_key_file(cp, tmp_path):
    res = run_wrapper("history", "art_x", "--control-plane", cp,
                      "--api-key-file", keyfile(tmp_path, "viewer-secret-k", 0o644))
    assert res.returncode == 2 and "refused" in res.stderr and "viewer-secret-k" not in res.stdout + res.stderr


def test_wrapper_reads_the_host_cache_key_for_the_hosts_own_control_plane(cp, tmp_path):
    home = tmp_path / "home"
    (home / ".config/herdr-engineering").mkdir(parents=True)
    (home / ".cache/herdr-engineering").mkdir(parents=True)
    (home / ".config/herdr-engineering/host.json").write_text(
        json.dumps({"control_plane_url": cp, "host_id": "h", "token": "t"}))
    cache = home / ".cache/herdr-engineering/fleet-config.json"
    cache.write_text(json.dumps({"host_id": "h", "revision": "r", "values": {"ingest": {"api_key": "viewer-secret-k"}}}))
    cache.chmod(0o600)
    res = run_wrapper("history", "art_x", "--control-plane", cp)
    assert "401" not in res.stderr
