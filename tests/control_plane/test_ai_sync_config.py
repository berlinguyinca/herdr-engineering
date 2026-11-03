"""control_plane.ai_sync: parsing/validation and the read-only GET /api/v1/ai-sync/config."""
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from herdr_engineering.control_plane.ai_sync_config import parse_ai_sync
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigError, FleetConfigStore

URL = "https://github.com/acme/ai-config.git"


def write_fleet(tmp_path, ai_sync):
    values = {} if ai_sync is None else {"ai_sync": ai_sync}
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1, "sections": {"control_plane": {"roles": ["control"], "values": values}},
        "hosts": {"h": {"roles": ["node"], "token_sha256": hashlib.sha256(b"t").hexdigest()}}}))
    os.chmod(path, 0o600)
    return path


def serve(tmp_path, ai_sync, **kw):
    svr = ControlPlaneServer(ControlPlaneRepo(), fleet_config=FleetConfigStore(write_fleet(tmp_path, ai_sync)), **kw)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr


def get(svr, path, key=None):
    req = urllib.request.Request(f"http://127.0.0.1:{svr.port}{path}", headers={"X-API-Key": key} if key else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_unset_is_404_not_configured(tmp_path):
    svr = serve(tmp_path, None)
    try:
        assert get(svr, "/api/v1/ai-sync/config") == (404, {"error": "not configured"})
    finally:
        svr.shutdown()


def test_no_fleet_config_is_404(acknowledged_sink):
    svr = ControlPlaneServer(ControlPlaneRepo())
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        assert get(svr, "/api/v1/ai-sync/config") == (404, {"error": "not configured"})
    finally:
        svr.shutdown()


def test_defaults_and_exact_shape(tmp_path):
    svr = serve(tmp_path, {"repo_url": URL})
    try:
        assert get(svr, "/api/v1/ai-sync/config") == (
            200, {"repo_url": URL, "branch": "main", "envs": ["claude"], "merge_enabled": False})
    finally:
        svr.shutdown()


def test_explicit_values_and_ssh_url(tmp_path):
    cfg = {"repo_url": "git@github.com:acme/cfg.git", "branch": "release/1.x", "envs": ["pi", "codex"],
           "merge_enabled": True, "token": "must-not-leak"}
    with pytest.raises(FleetConfigError):          # unknown keys (possible secrets) are refused, not echoed
        FleetConfigStore(write_fleet(tmp_path, cfg))
    del cfg["token"]
    svr = serve(tmp_path, cfg)
    try:
        assert get(svr, "/api/v1/ai-sync/config")[1] == {
            "repo_url": "git@github.com:acme/cfg.git", "branch": "release/1.x",
            "envs": ["pi", "codex"], "merge_enabled": True}
    finally:
        svr.shutdown()


def test_requires_api_key_when_keys_are_configured(tmp_path):
    svr = serve(tmp_path, {"repo_url": URL}, api_keys={"rk": "viewer"})
    try:
        assert get(svr, "/api/v1/ai-sync/config")[0] == 401
        assert get(svr, "/api/v1/ai-sync/config", "bad")[0] == 401
        assert get(svr, "/api/v1/ai-sync/config", "rk")[0] == 200
    finally:
        svr.shutdown()


def test_no_write_methods(tmp_path):
    svr = serve(tmp_path, {"repo_url": URL})
    try:
        for method in ("POST", "PUT", "DELETE"):
            req = urllib.request.Request(f"http://127.0.0.1:{svr.port}/api/v1/ai-sync/config",
                                         data=b"{}", method=method)
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req)
            assert err.value.code in (404, 405, 501)
        assert get(svr, "/api/v1/ai-sync/config")[1]["repo_url"] == URL
    finally:
        svr.shutdown()


@pytest.mark.parametrize("section", [
    {},                                                              # repo_url is required
    {"repo_url": "file:///etc/passwd"},
    {"repo_url": "/srv/git/cfg.git"},
    {"repo_url": "http://github.com/a/b.git"},
    {"repo_url": "https://github.com/a/b .git"},
    {"repo_url": "https://github.com/a/b.git\n"},
    {"repo_url": "-uhttps://github.com/a/b.git"},
    {"repo_url": "--upload-pack=x"},
    {"repo_url": "ext::sh -c id"},
    {"repo_url": "https://user:pw@github.com/a/b.git"},            # no credentials in the URL
    {"repo_url": 5},
    {"repo_url": URL, "branch": "-main"},
    {"repo_url": URL, "branch": "a..b"},
    {"repo_url": URL, "branch": "a b"},
    {"repo_url": URL, "branch": "x.lock"},
    {"repo_url": URL, "branch": ""},
    {"repo_url": URL, "envs": ["claude", "vim"]},
    {"repo_url": URL, "envs": "claude"},
    {"repo_url": URL, "envs": []},
    {"repo_url": URL, "envs": ["claude", "claude"]},
    {"repo_url": URL, "merge_enabled": "yes"},
    {"repo_url": URL, "write": True},
])
def test_invalid_section_is_rejected(tmp_path, section):
    with pytest.raises(ValueError):
        parse_ai_sync(section)
    with pytest.raises(FleetConfigError):                            # and the central file refuses to load
        FleetConfigStore(write_fleet(tmp_path, section))


def test_allowlisted_envs_accepted():
    cfg = parse_ai_sync({"repo_url": URL, "envs": ["claude", "codex", "opencode", "antigravity", "pi"]})
    assert cfg["envs"] == ["claude", "codex", "opencode", "antigravity", "pi"]
