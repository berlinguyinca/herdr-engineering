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


def raw_exchange(server, payload: bytes, wait=0.5):
    import socket
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=wait)
    sock.sendall(payload)
    data = b""
    try:
        while chunk := sock.recv(65536):
            data += chunk
    except OSError:
        pass
    sock.close()
    return data


def test_encoded_resource_cannot_bypass_origin_and_content_type(gate_server):
    escalate(gate_server)
    enc = f"review-gate/prs/{REPO}/7/resume".replace("review-gate", "%72eview-gate")
    status, _ = request(gate_server, enc, {}, key="write", method="POST",
                        headers={"Origin": "https://evil.example"})
    assert status == 403
    assert request(gate_server, enc, {}, key="write", method="POST", ctype="text/plain")[0] == 415
    assert gate_server.gate_store.snapshot(PrKey(REPO, 7)).state.value == "escalated"
    # the same flaw existed for ci
    assert request(gate_server, "%63i/actions", {}, key="write", method="POST",
                   headers={"Origin": "https://evil.example"})[0] == 403
    assert request(gate_server, "%63i/actions", {}, key="write", method="POST", ctype="text/plain")[0] == 415


@pytest.mark.parametrize("pr,escalated", [(7, True), (7, False), (9, False)])
def test_resume_body_is_consumed_so_one_response_only(gate_server, pr, escalated):
    if escalated:
        escalate(gate_server)
    elif pr == 7:
        gate_server.gate_store.on_push(PrKey(REPO, 7), "sha1")
    smuggled = b"GET /api/v1/review-gate/prs HTTP/1.1\r\nHost: x\r\nX-API-Key: read\r\n\r\n"
    req = (f"POST /api/v1/review-gate/prs/{REPO}/{pr}/resume HTTP/1.1\r\nHost: x\r\n"
           f"X-API-Key: write\r\nContent-Type: application/json\r\n"
           f"Content-Length: {len(smuggled)}\r\n\r\n").encode() + smuggled
    data = raw_exchange(gate_server, req)
    assert data.count(b"HTTP/1.") + data.count(b"HTTP/1.0") >= 1
    assert data.count(b"HTTP/1.0 ") + data.count(b"HTTP/1.1 ") == 1


def test_queue_reports_last_tick_and_error(tmp_path, monkeypatch, monitor_starts):
    class Failing(FakeApp):
        def open_prs(self, repo):
            raise github_app.GitHubAppError("authentication")

    monkeypatch.setattr(github_app, "GitHubAppClient", Failing)
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer"},
                                fleet_config=FakeFleet(gate_section(tmp_path)))
    start(server)
    try:
        server.gate_service.tick()
        gate = request(server, "review-gate/queue")[1]["gate"]
        assert gate["counts"]["errors"] > 0
        assert gate["last_error"] == "authentication"
        assert gate["last_tick_at"] is not None
        assert "key" not in json.dumps(gate).lower().replace("last_tick", "")
    finally:
        server.shutdown()


def test_trailing_slash_routes(gate_server):
    assert request(gate_server, "review-gate/prs/")[0] == 200
    assert request(gate_server, "review-gate/queue/")[0] == 200
    assert request(gate_server, "review-gate/queue/x")[0] == 404


SMUGGLED = b"GET /api/v1/review-gate/prs HTTP/1.1\r\nHost: x\r\nX-API-Key: read\r\n\r\n"


def _responses(data):
    return data.count(b"HTTP/1.1 ") + data.count(b"HTTP/1.0 ")


def _post_head(extra):
    return (f"POST /api/v1/review-gate/prs/{REPO}/7/resume HTTP/1.1\r\nHost: x\r\n"
            f"X-API-Key: write\r\nContent-Type: application/json\r\n{extra}\r\n").encode()


def test_chunked_resume_is_rejected_with_one_response(gate_server):
    escalate(gate_server)
    chunk = b"%x\r\n" % len(SMUGGLED) + SMUGGLED + b"\r\n0\r\n\r\n"
    data = raw_exchange(gate_server, _post_head("Transfer-Encoding: chunked\r\n") + chunk)
    assert _responses(data) == 1 and data.startswith(b"HTTP/1.") and b" 400 " in data.split(b"\r\n")[0]
    assert gate_server.gate_store.snapshot(PrKey(REPO, 7)).state.value == "escalated"


def test_duplicate_content_length_is_rejected(gate_server):
    data = raw_exchange(gate_server, _post_head("Content-Length: 0\r\nContent-Length: 5\r\n") + SMUGGLED)
    assert _responses(data) == 1 and b" 400 " in data.split(b"\r\n")[0]


def test_oversize_content_length_closes_connection(gate_server):
    escalate(gate_server)
    data = raw_exchange(gate_server, _post_head("Content-Length: 5000000\r\n") + SMUGGLED)
    assert _responses(data) == 1 and b" 400 " in data.split(b"\r\n")[0]
    assert b"Connection: close" in data


def test_ordinary_resume_still_works_after_framing_rejections(gate_server):
    escalate(gate_server)
    raw_exchange(gate_server, _post_head("Transfer-Encoding: chunked\r\n") + b"0\r\n\r\n")
    assert request(gate_server, RESUME, {}, key="write", method="POST")[0] == 200


@pytest.fixture
def collab_server():
    import hashlib

    from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
    digest = hashlib.sha256(b"tok-alice-0123456789").hexdigest()
    svc = CollabService(StaticTokenAuthenticator({"alice": digest}))
    server = ControlPlaneServer(ControlPlaneRepo(), collab=svc, api_keys={"read": "viewer"})
    start(server)
    yield server
    server.shutdown()


def _collab_head(extra):
    return ("POST /api/v1/collab/projects HTTP/1.1\r\nHost: x\r\n"
            "Authorization: Bearer tok-alice-0123456789\r\nContent-Type: application/json\r\n"
            f"{extra}\r\n").encode()


def test_chunked_collab_post_is_rejected_with_one_response(collab_server):
    chunk = b"%x\r\n" % len(SMUGGLED) + SMUGGLED + b"\r\n0\r\n\r\n"
    data = raw_exchange(collab_server, _collab_head("Transfer-Encoding: chunked\r\n") + chunk)
    assert _responses(data) == 1 and b" 400 " in data.split(b"\r\n")[0]


def test_duplicate_content_length_collab_post_is_rejected(collab_server):
    data = raw_exchange(collab_server, _collab_head("Content-Length: 0\r\nContent-Length: 5\r\n") + SMUGGLED)
    assert _responses(data) == 1 and b" 400 " in data.split(b"\r\n")[0]


def test_ordinary_collab_post_still_works(collab_server):
    body = b'{"name": "proj"}'
    data = raw_exchange(collab_server, _collab_head(f"Content-Length: {len(body)}\r\n") + body)
    assert _responses(data) == 1 and b" 201 " in data.split(b"\r\n")[0]


@pytest.fixture
def ingest_gate_server(tmp_path, monkeypatch, monitor_starts):
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    keys = {"read": "viewer", "write": "operator", "agent-key": "agent", "ingest-key": "ingest"}
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys=keys,
                                fleet_config=FakeFleet(gate_section(tmp_path)))
    start(server)
    yield server
    server.shutdown()


@pytest.mark.parametrize("key", ["agent-key", "ingest-key"])
@pytest.mark.parametrize("entity_type,event_type,payload", [
    ("pr_gate", "verdict", {"verdict": "PASS", "sha": "sha1"}),
    ("pr_gate", "resume", {}),
    ("pr_gate_author", "author_pinned", {"kind": "pi", "model": "forged"}),
    ("review_job", "enqueued", {"job_id": "j1", "key": f"{REPO}#7", "sha": "sha1",
                                "author_model": None, "enqueued_at": "2026-10-22T00:00:00+00:00"}),
    ("pr_gate_x", "verdict", {"verdict": "PASS", "sha": "sha1"}),
])
def test_ingest_cannot_forge_gate_events(ingest_gate_server, key, entity_type, event_type, payload):
    server = ingest_gate_server
    pr = PrKey(REPO, 7)
    server.gate_store.on_push(pr, "sha1")
    server.gate_store.on_ci_green(pr, "sha1")
    before = (request(server, "review-gate/prs"), request(server, "review-gate/queue"))
    status, body = request(server, "ingest", {
        "entity_type": entity_type, "entity_id": str(pr) if entity_type != "review_job" else "index",
        "event_type": event_type, "payload": payload}, key=key, method="POST")
    assert status == 400 and "reserved" in body["error"]
    after = (request(server, "review-gate/prs"), request(server, "review-gate/queue"))
    assert after == before
    assert server.repo.stream("pr_gate_author", str(pr)) == []


def test_resume_durable_write_failure_is_503_and_keeps_escalation(tmp_path, monkeypatch, monitor_starts):
    class Sink:
        fail = False

        def record_required(self, ev):
            if self.fail:
                raise RuntimeError("durable event insert was not acknowledged")

        def record(self, ev):
            pass

    sink = Sink()
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    server = ControlPlaneServer(ControlPlaneRepo(sink=sink), api_keys={"read": "viewer", "write": "operator"},
                                fleet_config=FakeFleet(gate_section(tmp_path)))
    start(server)
    try:
        key = escalate(server)
        sink.fail = True
        status, body = request(server, RESUME, {}, key="write", method="POST")
        assert status == 503 and "persist" in body["error"]
        assert server.gate_store.snapshot(key).state.value == "escalated"
    finally:
        server.shutdown()


def test_sink_without_required_writes_fails_loud(tmp_path, monkeypatch, monitor_starts):
    class LegacySink:
        def record(self, ev):
            pass

    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    with pytest.raises(ValueError, match="durable"):
        ControlPlaneServer(ControlPlaneRepo(sink=LegacySink()), api_keys={"read": "viewer"},
                           fleet_config=FakeFleet(gate_section(tmp_path)))
    assert not any(t.name == "review-gate" for t in threading.enumerate())


def test_resume_accepts_a_mixed_case_repo(gate_server):
    key = escalate(gate_server)
    path = "review-gate/prs/github.com/Acme/Widget/7/resume"
    status, snap = request(gate_server, path, {}, key="write", method="POST")
    assert status == 200 and snap["key"] == str(key)
    assert gate_server.gate_store.snapshot(key).state.value != "escalated"


def _notice(sha, channel, at, ok=True, kind="changes_requested"):
    return {"sha": sha, "kind": kind, "channel": channel, "ok": ok, "detail": "recorded", "at": at}


def test_notices_route_lists_recent_notices_for_viewer(gate_server):
    repo = gate_server.repo
    for i in range(105):
        repo.record_event("pr_gate_notice", f"{REPO}#{i % 3 + 1}", "notified",
                          _notice(f"{i:040x}", "user", float(i)))
    repo.record_event("pr_gate_notice", f"{REPO}#1", "other", {"at": 999.0})
    status, notices = request(gate_server, "review-gate/notices")
    assert status == 200 and len(notices) == 100
    assert [n["at"] for n in notices] == [float(i) for i in range(104, 4, -1)]
    assert notices[0] == {"key": f"{REPO}#3", "sha": f"{104:040x}", "kind": "changes_requested",
                          "channel": "user", "ok": True, "at": 104.0}
    assert all("detail" not in n for n in notices)
    assert request(gate_server, "review-gate/notices/")[0] == 200


def test_notices_route_is_404_when_disabled(plain_server):
    assert request(plain_server, "review-gate/notices")[0] == 404


def test_gate_service_gets_a_notifier_with_the_server_dispatcher(gate_server):
    notifier = gate_server.gate_service._notifier
    assert notifier is not None
    assert notifier._dispatcher == gate_server.dispatcher


@pytest.mark.parametrize("key", ["agent-key", "ingest-key"])
def test_notice_stream_cannot_be_forged_by_ingest(ingest_gate_server, key):
    from herdr_engineering.review_gate.models import State
    from herdr_engineering.review_gate.store import PrSnapshot

    server = ingest_gate_server
    pr = PrKey(REPO, 7)
    status, body = request(server, "ingest", {
        "entity_type": "pr_gate_notice", "entity_id": str(pr), "event_type": "notified",
        "payload": _notice("a" * 40, "user", 1.0)}, key=key, method="POST")
    assert status in (400, 403) and "reserved" in body["error"]
    assert server.repo.stream("pr_gate_notice", str(pr)) == []
    snap = PrSnapshot(pr, "a" * 40, State.ESCALATED, 0, 1, (), None, None, "ci fix cap reached")
    server.gate_service._notifier.notify(pr, snap, "feat/x")
    status, notices = request(server, "review-gate/notices")
    assert [(n["key"], n["kind"], n["channel"]) for n in notices] == [
        (str(pr), "escalated", "user")]


def test_notices_route_survives_a_malformed_notice(gate_server):
    repo = gate_server.repo
    repo.record_event("pr_gate_notice", f"{REPO}#1", "notified", "{not json")
    repo.record_event("pr_gate_notice", f"{REPO}#1", "notified", _notice("a" * 40, "user", 5.0))
    status, notices = request(gate_server, "review-gate/notices")
    assert status == 200 and len(notices) == 2
    assert notices[0]["at"] == 5.0 and notices[1]["at"] is None


def test_main_route_viewer_ok_and_404_when_disabled(tmp_path, monkeypatch, monitor_starts,
                                                    plain_server):
    assert request(plain_server, "review-gate/main")[0] == 404
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    section = gate_section(tmp_path, repos=[{"repo": REPO, "main_pipeline": "push"}])
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer", "write": "operator"},
                                fleet_config=FakeFleet(section))
    start(server)
    try:
        server.gate_service.tick()  # FakeApp cannot read the default branch: state unknown
        status, body = request(server, "review-gate/main/")
        assert status == 200
        assert [(r["repo"], r["state"]) for r in body["repos"]] == [(REPO, "unknown")]
        assert request(server, "review-gate/main", key="write")[0] == 200
    finally:
        server.shutdown()


def test_main_route_requires_a_key(gate_server):
    for key in ("", "wrong"):
        assert request(gate_server, "review-gate/main", key=key)[0] in (401, 403)
    assert request(gate_server, "review-gate/main")[1] == {"repos": []}
