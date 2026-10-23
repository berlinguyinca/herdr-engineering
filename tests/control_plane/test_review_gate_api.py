import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ci_monitor import CIMonitor
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate import github_app
from herdr_engineering.review_gate.models import PrKey

REPO = "github.com/acme/widget"


class FakeApp:
    def __init__(self, config, **kw):
        self.config = config

    def open_prs(self, repo):
        return []


class FakeFleet:
    def __init__(self, section):
        self._section = section

    def current(self):
        return self

    def section(self, name):
        assert name == "control_plane"
        return self._section


def gate_section(tmp_path, **over):
    key = tmp_path / "app.pem"
    key.write_text("not-a-real-key")
    key.chmod(0o600)
    sec = {
        "enabled": True,
        "app": {"app_id": 1, "installation_id": 2, "private_key_file": str(key)},
        "repos": [{"repo": REPO, "ci_fix_cap": 1}],
        "poll_seconds": 3600,
    }
    sec.update(over)
    return {"review_gate": sec}


def start(server):
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    return thread


@pytest.fixture
def monitor_starts(monkeypatch):
    calls = []
    monkeypatch.setattr(CIMonitor, "start", lambda self: calls.append(1))
    return calls


@pytest.fixture
def gate_server(tmp_path, monkeypatch, monitor_starts):
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer", "write": "operator"},
                                fleet_config=FakeFleet(gate_section(tmp_path)))
    start(server)
    yield server
    server.shutdown()


@pytest.fixture
def plain_server():
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer", "write": "operator"})
    start(server)
    yield server
    server.shutdown()


def request(server, path, body=None, key="read", method=None, headers=None, ctype="application/json"):
    h = {"X-API-Key": key}
    if ctype:
        h["Content-Type"] = ctype
    h.update(headers or {})
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=h, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def escalate(server, number=7):
    key = PrKey(REPO, number)
    server.gate_store.on_push(key, "sha1")
    server.gate_store.on_ci_red(key, "sha1", ["lint"])
    assert server.gate_store.snapshot(key).state.value == "escalated"
    return key


RESUME = f"review-gate/prs/{REPO}/7/resume"


def test_disabled_returns_404(plain_server):
    assert plain_server.gate_service is None
    assert not any(t.name == "review-gate" for t in threading.enumerate())
    assert request(plain_server, "review-gate/prs")[0] == 404
    assert request(plain_server, "review-gate/queue")[0] == 404
    assert request(plain_server, RESUME, {}, key="write", method="POST")[0] == 404


def test_viewer_can_list_prs_and_queue(gate_server):
    escalate(gate_server)
    gate_server.gate_queue.enqueue(PrKey(REPO, 8), "abc", "claude")
    status, prs = request(gate_server, "review-gate/prs")
    assert status == 200
    assert prs == [{
        "key": f"{REPO}#7", "head_sha": "sha1", "state": "escalated",
        "review_rounds": 0, "ci_fix_rounds": 1, "reason": "ci fix cap reached",
        "approved_sha": None, "last_findings": [],
    }]
    status, queue = request(gate_server, "review-gate/queue")
    assert status == 200 and queue["depth"] == 1
    assert queue["jobs"][0]["key"] == f"{REPO}#8"
    assert "private_key" not in json.dumps([prs, queue])


def test_viewer_cannot_resume(gate_server):
    escalate(gate_server)
    assert request(gate_server, RESUME, {}, method="POST")[0] == 403
    assert gate_server.gate_store.snapshot(PrKey(REPO, 7)).state.value == "escalated"


def test_operator_resume_clears_escalation(gate_server):
    key = escalate(gate_server)
    status, snap = request(gate_server, RESUME, {}, key="write", method="POST")
    assert status == 200
    assert snap["key"] == str(key) and snap["state"] != "escalated"
    assert gate_server.gate_store.snapshot(key).state.value != "escalated"


def test_resume_unknown_pr_is_404(gate_server):
    assert request(gate_server, RESUME, {}, key="write", method="POST")[0] == 404
    assert request(gate_server, "review-gate/prs/not-a-repo/x/resume", {}, key="write", method="POST")[0] == 404


def test_resume_non_escalated_is_409(gate_server):
    gate_server.gate_store.on_push(PrKey(REPO, 7), "sha1")
    assert request(gate_server, RESUME, {}, key="write", method="POST")[0] == 409


def test_cross_origin_resume_is_refused(gate_server):
    escalate(gate_server)
    status, _ = request(gate_server, RESUME, {}, key="write", method="POST",
                        headers={"Origin": "https://untrusted.example"})
    assert status == 403
    assert gate_server.gate_store.snapshot(PrKey(REPO, 7)).state.value == "escalated"


def test_wrong_content_type_is_415(gate_server):
    escalate(gate_server)
    assert request(gate_server, RESUME, {}, key="write", method="POST", ctype="text/plain")[0] == 415


def test_shutdown_stops_gate_thread(tmp_path, monkeypatch, monitor_starts):
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer"},
                                fleet_config=FakeFleet(gate_section(tmp_path)))
    thread = server.gate_service._thread
    assert thread is not None and thread.is_alive()
    assert monitor_starts, "the gate must start the CI monitor itself"
    start(server)
    server.shutdown()
    thread.join(2)
    assert not thread.is_alive()


def test_gate_not_started_when_disabled(tmp_path, monkeypatch, monitor_starts):
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    sec = gate_section(tmp_path, enabled=False)
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer"}, fleet_config=FakeFleet(sec))
    assert server.gate_service is None and not monitor_starts
    server.shutdown()


def test_invalid_enabled_config_fails_loud(tmp_path):
    sec = gate_section(tmp_path)
    sec["review_gate"]["app"]["private_key_file"] = str(tmp_path / "missing.pem")
    with pytest.raises(ValueError, match="private_key_file"):
        ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer"}, fleet_config=FakeFleet(sec))
    assert not any(t.name == "review-gate" for t in threading.enumerate())


def test_per_repo_caps_reach_the_store(gate_server):
    # the enrollment says ci_fix_cap=1; the default (3) would not have escalated
    escalate(gate_server)
