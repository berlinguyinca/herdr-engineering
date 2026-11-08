"""control_plane.agent_rules: validation, the read-only GET /api/v1/agent-rules/config, and host extra-vars."""
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from herdr_engineering.control_plane.agent_rules_config import agent_rules_vars, parse_agent_rules
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigError, FleetConfigStore

URL = "git@github.com:berlinguyinca/agent-rules.git"
SHA = "8a92a224a0fb3eb661cc5c79e035442a856a8c0e"


def write_fleet(tmp_path, section):
    values = {} if section is None else {"agent_rules": section}
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1, "sections": {"control_plane": {"roles": ["control"], "values": values}},
        "hosts": {"h": {"roles": ["node"], "token_sha256": hashlib.sha256(b"t").hexdigest()}}}))
    os.chmod(path, 0o600)
    return path


def serve(tmp_path, section, **kw):
    svr = ControlPlaneServer(ControlPlaneRepo(), fleet_config=FleetConfigStore(write_fleet(tmp_path, section)), **kw)
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


def test_parse_returns_exact_shape():
    assert parse_agent_rules({"repo_url": URL, "ref": SHA}) == {"repo_url": URL, "ref": SHA}
    assert parse_agent_rules({"repo_url": "https://github.com/a/b.git", "ref": "v1.2.0"})["ref"] == "v1.2.0"


def test_endpoint_unset_is_404(tmp_path):
    svr = serve(tmp_path, None)
    try:
        assert get(svr, "/api/v1/agent-rules/config") == (404, {"error": "not configured"})
    finally:
        svr.shutdown()


def test_endpoint_serves_section_and_requires_key(tmp_path):
    svr = serve(tmp_path, {"repo_url": URL, "ref": SHA}, api_keys={"rk": "viewer"})
    try:
        assert get(svr, "/api/v1/agent-rules/config")[0] == 401
        assert get(svr, "/api/v1/agent-rules/config", "rk") == (200, {"repo_url": URL, "ref": SHA})
        req = urllib.request.Request(f"http://127.0.0.1:{svr.port}/api/v1/agent-rules/config",
                                     data=b"{}", method="POST", headers={"X-API-Key": "rk"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        assert err.value.code in (403, 404, 405, 501)
    finally:
        svr.shutdown()


@pytest.mark.parametrize("section", [
    {},
    {"repo_url": URL},                                   # the pin is mandatory
    {"ref": SHA},
    {"repo_url": URL, "ref": ""},
    {"repo_url": URL, "ref": "-x"},
    {"repo_url": URL, "ref": "a..b"},
    {"repo_url": URL, "ref": "a b"},
    {"repo_url": URL, "ref": "x.lock"},
    {"repo_url": URL, "ref": 5},
    {"repo_url": "file:///etc/passwd", "ref": SHA},
    {"repo_url": "--upload-pack=x", "ref": SHA},
    {"repo_url": "https://user:pw@github.com/a/b.git", "ref": SHA},
    {"repo_url": URL, "ref": SHA, "token": "leak"},
])
def test_invalid_section_is_rejected(tmp_path, section):
    with pytest.raises(ValueError):
        parse_agent_rules(section)
    with pytest.raises(FleetConfigError):
        FleetConfigStore(write_fleet(tmp_path, section))


def test_agent_rules_vars_reads_central_file(tmp_path):
    assert agent_rules_vars(tmp_path / "missing.yaml") == {}
    assert agent_rules_vars(write_fleet(tmp_path, None)) == {}
    path = write_fleet(tmp_path, {"repo_url": URL, "ref": SHA})
    assert agent_rules_vars(path) == {"agent_rules_repo_url": URL, "agent_rules_ref": SHA}
