"""Reviewer API role and the claim/heartbeat/result/failure routes (review gate)."""

import json
import logging
import socket
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ci_monitor import CIMonitor
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate import github_app
from herdr_engineering.review_gate.coordinator import ReviewCoordinator
from herdr_engineering.review_gate.github_app import ReviewFile, ReviewMaterial
from herdr_engineering.review_gate.models import PrKey, State

REPO = "github.com/acme/widget"
SHA = "a" * 40
KEY = PrKey(REPO, 7)
PEM_TEXT = "not-a-real-key-PRIVATE"
MAX_OUTPUT = 1000
REV = "rk-reviewer-key-9f1c"
KEYS = {
    "read": "viewer",
    "write": "operator",
    REV: "reviewer",
    "agent-key": "agent",
    "ingest-key": "ingest",
}
TOKEN = "ghs_FAKE_INSTALLATION_TOKEN"
PASS = json.dumps({"verdict": "PASS", "findings": []})
CLAIM_FIELDS = {"job_id", "repo", "number", "sha", "messages", "lease_seconds", "attempt"}


class FakeApp:
    instances: list = []

    def __init__(self, config, **kw):
        self.config = config
        self.token = TOKEN
        FakeApp.instances.append(self)

    def open_prs(self, repo):
        return []

    def review_material(self, repo, number, expected_head_sha, *, max_bytes):
        return ReviewMaterial(
            "Fix it",
            "body",
            "main",
            expected_head_sha,
            (ReviewFile("src/x.py", "modified", "@@ -1 +1 @@\n-a\n+b"),),
            False,
        )


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
    key.write_text(PEM_TEXT)
    key.chmod(0o600)
    sec = {
        "enabled": True,
        "app": {"app_id": 1, "installation_id": 2, "private_key_file": str(key)},
        "repos": [{"repo": REPO}],
        "poll_seconds": 3600,
        "reviewer": {"lease_seconds": 120, "max_output_bytes": MAX_OUTPUT},
    }
    sec.update(over)
    return {"review_gate": sec}


def start(server):
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    return thread


@pytest.fixture(autouse=True)
def no_monitor(monkeypatch):
    monkeypatch.setattr(CIMonitor, "start", lambda self: None)


@pytest.fixture
def server(tmp_path, monkeypatch):
    FakeApp.instances = []
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    srv = ControlPlaneServer(
        ControlPlaneRepo(), api_keys=KEYS, fleet_config=FakeFleet(gate_section(tmp_path))
    )
    srv.gate_service.stop()  # no background tick racing the test
    start(srv)
    yield srv
    srv.shutdown()


@pytest.fixture
def plain_server():
    srv = ControlPlaneServer(ControlPlaneRepo(), api_keys=KEYS)
    start(srv)
    yield srv
    srv.shutdown()


def raw_request(srv, path, data, key=REV, headers=None, ctype="application/json"):
    h = {"X-API-Key": key}
    if ctype:
        h["Content-Type"] = ctype
    h.update(headers or {})
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/v1/{path}", data=data, headers=h, method="POST"
    )
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read()


def post(srv, path, body, key=REV, **kw):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    status, raw = raw_request(srv, path, data, key=key, **kw)
    return status, json.loads(raw)


def get(srv, path, key):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/v1/{path}", headers={"X-API-Key": key}
    )
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status


def ready(srv, key=KEY, sha=SHA):
    srv.gate_store.on_push(key, sha)
    srv.gate_store.on_ci_green(key, sha)
    return srv.gate_queue.enqueue(key, sha, "author-model")


def claim(srv, key=REV, worker="w1", model="rev-model"):
    return post(srv, "review-gate/claim", {"worker_id": worker, "reviewer_model": model}, key=key)


# ------------------------------------------------------------------ happy path
def test_reviewer_key_can_claim_and_submit(server):
    assert post(server, "review-gate/claim", {"worker_id": "w1", "reviewer_model": "m"}) == (
        200,
        {"job": None},
    )
    job = ready(server)
    status, body = claim(server)
    assert status == 200
    got = body["job"]
    assert set(got) == CLAIM_FIELDS
    assert got["job_id"] == job.job_id
    assert (got["repo"], got["number"], got["sha"]) == (REPO, 7, SHA)
    assert got["lease_seconds"] == 120 and got["attempt"] == 1
    assert [m["role"] for m in got["messages"]] == ["system", "user"]
    assert server.gate_store.snapshot(KEY).state == State.REVIEWING

    base = f"review-gate/jobs/{job.job_id}"
    assert post(server, f"{base}/heartbeat", {"worker_id": "w1"}) == (200, {"ok": True})
    assert post(server, f"{base}/heartbeat", {"worker_id": "other"}) == (200, {"ok": False})
    status, body = post(server, f"{base}/result", {"worker_id": "w1", "output": PASS})
    assert (status, body) == (200, {"outcome": "recorded"})
    assert server.gate_store.snapshot(KEY).state != State.REVIEWING
    # the job is done: a second result is stale
    assert post(server, f"{base}/result", {"worker_id": "w1", "output": PASS}) == (
        200,
        {"outcome": "stale"},
    )


def test_failure_route_releases_then_is_stale(server):
    job = ready(server)
    claim(server)
    base = f"review-gate/jobs/{job.job_id}"
    assert post(server, f"{base}/failure", {"worker_id": "w1", "reason": "x" * 500}) == (
        200,
        {"outcome": "released"},
    )
    assert post(server, f"{base}/failure", {"worker_id": "w1", "reason": "again"}) == (
        200,
        {"outcome": "stale"},
    )


def test_invalid_output_is_rejected_outcome(server):
    job = ready(server)
    claim(server)
    status, body = post(
        server, f"review-gate/jobs/{job.job_id}/result", {"worker_id": "w1", "output": "nope"}
    )
    assert (status, body) == (200, {"outcome": "rejected"})


def test_coordinator_shares_the_gate_app_and_queue_lease(server):
    coord = server.gate_coordinator
    assert isinstance(coord, ReviewCoordinator)
    assert len(FakeApp.instances) == 1
    assert coord._app is FakeApp.instances[0] is server.gate_service._app
    assert coord._queue is server.gate_queue
    assert server.gate_queue._lease == 120 == coord._config.lease_seconds


# ------------------------------------------------------------------- the role
def test_reviewer_key_cannot_ingest_resume_or_create_reviews(server):
    ev = {"entity_type": "x", "entity_id": "y", "event_type": "z", "payload": {}}
    assert post(server, "ingest", ev)[0] == 403
    assert post(server, f"review-gate/prs/{REPO}/7/resume", {})[0] == 403
    assert post(server, "reviews", {})[0] == 403
    assert post(server, "llm/keys", {})[0] == 403
    assert post(server, "reviews/abc/result", {})[0] == 403


def test_reviewer_key_may_read_like_a_viewer(server):
    assert get(server, "review-gate/queue", REV) == 200
    assert get(server, "review-gate/prs", REV) == 200
    assert get(server, "health", REV) == 200


@pytest.mark.parametrize("key", ["read", "ingest-key", "agent-key", "write"])
def test_viewer_and_ingest_keys_cannot_claim(server, key):
    job = ready(server)
    assert claim(server, key=key)[0] == 403
    base = f"review-gate/jobs/{job.job_id}"
    assert post(server, f"{base}/heartbeat", {"worker_id": "w1"}, key=key)[0] == 403
    assert post(server, f"{base}/result", {"worker_id": "w1", "output": PASS}, key=key)[0] == 403
    assert post(server, f"{base}/failure", {"worker_id": "w1", "reason": "r"}, key=key)[0] == 403
    assert server.gate_store.snapshot(KEY).state == State.CI_GREEN


def test_operator_cannot_claim_through_an_encoded_or_slashed_path(server):
    ready(server)
    for path in ("review-gate/%63laim", "%72eview-gate/claim", "review-gate/claim/"):
        status, _ = post(server, path, {"worker_id": "w1", "reviewer_model": "m"}, key="write")
        assert status == 403, path
    assert server.gate_store.snapshot(KEY).state == State.CI_GREEN


def test_reviewer_unknown_job_route_is_404(server):
    job = ready(server)
    for path in (
        f"review-gate/jobs/{job.job_id}/approve",
        f"review-gate/jobs/{job.job_id}",
        "review-gate/claim/extra",
        "review-gate/claim/",
    ):
        assert post(server, path, {"worker_id": "w1"})[0] == 404, path
    assert server.gate_store.snapshot(KEY).state == State.CI_GREEN


def test_unknown_role_is_not_reviewer(tmp_path, monkeypatch):
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    keys = {"a": "Reviewer", "b": " reviewer ", "c": ""}
    srv = ControlPlaneServer(
        ControlPlaneRepo(), api_keys=keys, fleet_config=FakeFleet(gate_section(tmp_path))
    )
    srv.gate_service.stop()
    start(srv)
    try:
        # " reviewer " is stripped to the real role by the server (as for every role)
        assert claim(srv, key="a")[0] == 403
        assert claim(srv, key="c")[0] == 403
        assert claim(srv, key="b")[0] == 200
    finally:
        srv.shutdown()


def test_unauthenticated_claim_is_401(server):
    ready(server)
    assert claim(server, key="")[0] == 401
    assert claim(server, key="wrong")[0] == 401
    assert server.gate_store.snapshot(KEY).state == State.CI_GREEN


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize(
    "body",
    [
        {"worker_id": "bad id", "reviewer_model": "m"},
        {"worker_id": "", "reviewer_model": "m"},
        {"worker_id": "w" * 129, "reviewer_model": "m"},
        {"worker_id": "w1\n", "reviewer_model": "m"},
        {"worker_id": 5, "reviewer_model": "m"},
        {"worker_id": "w1", "reviewer_model": "m;rm"},
        {"worker_id": "w1"},
        {"worker_id": "w1", "reviewer_model": "m", "extra": 1},
        [],
        "w1",
    ],
)
def test_bad_claim_body_is_400(server, body):
    ready(server)
    status, err = post(server, "review-gate/claim", body)
    assert status == 400
    assert set(err) == {"error"}
    assert "rm" not in err["error"] and "bad id" not in err["error"]
    assert server.gate_store.snapshot(KEY).state == State.CI_GREEN


def test_bad_worker_id_job_id_and_oversize_body_rejected(server):
    job = ready(server)
    claim(server)
    base = f"review-gate/jobs/{job.job_id}"
    # worker id
    assert post(server, f"{base}/heartbeat", {"worker_id": "w 1"})[0] == 400
    assert post(server, f"{base}/result", {"worker_id": "w1\n", "output": PASS})[0] == 400
    # job id: not 32 lowercase hex
    for bad in ("abc", job.job_id.upper(), job.job_id + "0", "%0A" + job.job_id[1:]):
        assert post(server, f"review-gate/jobs/{bad}/heartbeat", {"worker_id": "w1"})[0] == 400
    # field types
    assert post(server, f"{base}/result", {"worker_id": "w1", "output": {"v": 1}})[0] == 400
    assert post(server, f"{base}/failure", {"worker_id": "w1", "reason": "x" * 501})[0] == 400
    assert post(server, f"{base}/failure", {"worker_id": "w1", "reason": 3})[0] == 400
    # malformed / empty / deeply nested JSON
    assert post(server, f"{base}/heartbeat", b"{nope")[0] == 400
    assert post(server, f"{base}/heartbeat", b"")[0] == 400
    assert post(server, f"{base}/heartbeat", b"[" * 4000 + b"]" * 4000)[0] == 400
    assert post(server, f"{base}/heartbeat", b'{"worker_id": "\xff"}')[0] == 400
    # oversize bodies: 8 KiB for claim/heartbeat/failure, max_output_bytes + 4 KiB for result
    pad = "x" * (8 * 1024)
    assert post(server, "review-gate/claim", {"worker_id": "w1", "reviewer_model": pad})[0] == 413
    assert post(server, f"{base}/heartbeat", {"worker_id": pad})[0] == 413
    assert post(server, f"{base}/failure", {"worker_id": "w1", "reason": pad})[0] == 413
    big = "x" * (MAX_OUTPUT + 4 * 1024)
    assert post(server, f"{base}/result", {"worker_id": "w1", "output": big})[0] == 413
    # a result body under the HTTP cap but over max_output_bytes is the coordinator's "rejected"
    assert post(
        server, f"{base}/result", {"worker_id": "w1", "output": "x" * (MAX_OUTPUT + 1)}
    ) == (
        200,
        {"outcome": "rejected"},
    )


def test_error_bodies_never_echo_request_text(server):
    job = ready(server)
    secret = "SECRET-TEXT-123"
    for path, body in (
        ("review-gate/claim", {"worker_id": secret + " ", "reviewer_model": secret}),
        (f"review-gate/jobs/{job.job_id}/failure", {"worker_id": "w1", "reason": secret * 100}),
        (f"review-gate/jobs/{secret}/heartbeat", {"worker_id": secret}),
        ("review-gate/claim", (secret + "{").encode()),
    ):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        status, raw = raw_request(server, path, data)
        assert status == 400
        assert secret.encode() not in raw


# ------------------------------------------------------------- gate disabled
def test_disabled_gate_returns_503(plain_server, tmp_path, monkeypatch):
    assert plain_server.gate_coordinator is None
    assert claim(plain_server) == (503, {"error": "review gate disabled"})
    status, body = post(plain_server, f"review-gate/jobs/{'a' * 32}/heartbeat", {"worker_id": "w1"})
    assert (status, body) == (503, {"error": "review gate disabled"})
    # disabled in config: same
    monkeypatch.setattr(github_app, "GitHubAppClient", FakeApp)
    srv = ControlPlaneServer(
        ControlPlaneRepo(),
        api_keys=KEYS,
        fleet_config=FakeFleet(gate_section(tmp_path, enabled=False)),
    )
    start(srv)
    try:
        assert srv.gate_coordinator is None
        assert claim(srv) == (503, {"error": "review gate disabled"})
        # still reviewer-only when disabled
        assert claim(srv, key="write")[0] == 403
    finally:
        srv.shutdown()


# -------------------------------------------------------------------- secrets
def test_claim_response_has_no_secrets(server, tmp_path):
    ready(server)
    status, raw = raw_request(
        server,
        "review-gate/claim",
        json.dumps({"worker_id": "w1", "reviewer_model": "m"}).encode(),
    )
    assert status == 200
    assert json.loads(raw)["job"] is not None
    for needle in (str(tmp_path), "app.pem", PEM_TEXT, "PRIVATE", TOKEN, REV, "private_key"):
        assert needle.encode() not in raw, needle


def test_coordinator_exception_is_fixed_500(server, monkeypatch, caplog):
    class Boom(RuntimeError):
        pass

    def explode(*a, **kw):
        raise Boom("PR TEXT that must not leak")

    for name in ("claim", "heartbeat", "submit", "fail"):
        monkeypatch.setattr(server.gate_coordinator, name, explode)
    jid = "b" * 32
    caplog.set_level(logging.DEBUG)
    for path, body in (
        ("review-gate/claim", {"worker_id": "w1", "reviewer_model": "m"}),
        (f"review-gate/jobs/{jid}/heartbeat", {"worker_id": "w1"}),
        (f"review-gate/jobs/{jid}/result", {"worker_id": "w1", "output": "o"}),
        (f"review-gate/jobs/{jid}/failure", {"worker_id": "w1", "reason": "r"}),
    ):
        status, raw = raw_request(server, path, json.dumps(body).encode())
        assert status == 500
        assert json.loads(raw) == {"error": "internal error"}
    assert "PR TEXT" not in caplog.text
    assert "Boom" in caplog.text


# ---------------------------------------------------------- CSRF and framing
def test_csrf_and_content_type_checks_apply(server):
    job = ready(server)
    good = {"worker_id": "w1", "reviewer_model": "m"}
    assert (
        post(server, "review-gate/claim", good, headers={"Origin": "https://evil.example"})[0]
        == 403
    )
    assert post(server, "review-gate/claim", good, ctype="text/plain")[0] == 415
    assert post(server, "review-gate/claim", good, ctype=None)[0] == 415
    hb = f"review-gate/jobs/{job.job_id}/heartbeat"
    assert post(server, hb, {"worker_id": "w1"}, headers={"Origin": "null"})[0] == 403
    assert post(server, hb, {"worker_id": "w1"}, ctype="text/plain")[0] == 415
    assert server.gate_store.snapshot(KEY).state == State.CI_GREEN
    # a same-origin browser request is not refused by the CSRF check
    origin = f"http://127.0.0.1:{server.port}"
    assert post(server, "review-gate/claim", good, headers={"Origin": origin})[0] == 200


SMUGGLED = b"GET /api/v1/review-gate/prs HTTP/1.1\r\nHost: x\r\nX-API-Key: read\r\n\r\n"


def raw_exchange(srv, payload: bytes, wait=0.5):
    sock = socket.create_connection(("127.0.0.1", srv.port), timeout=wait)
    sock.sendall(payload)
    data = b""
    try:
        while chunk := sock.recv(65536):
            data += chunk
    except OSError:
        pass
    sock.close()
    return data


def _head(path, extra):
    return (
        f"POST /api/v1/{path} HTTP/1.1\r\nHost: x\r\n"
        f"X-API-Key: {REV}\r\nContent-Type: application/json\r\n{extra}\r\n"
    ).encode()


def _responses(data):
    return data.count(b"HTTP/1.1 ") + data.count(b"HTTP/1.0 ")


def test_framing_rejections_give_one_response(server):
    ready(server)
    chunk = b"%x\r\n" % len(SMUGGLED) + SMUGGLED + b"\r\n0\r\n\r\n"
    data = raw_exchange(
        server, _head("review-gate/claim", "Transfer-Encoding: chunked\r\n") + chunk
    )
    assert _responses(data) == 1 and b" 400 " in data.split(b"\r\n")[0]
    data = raw_exchange(
        server, _head("review-gate/claim", "Content-Length: 0\r\nContent-Length: 5\r\n") + SMUGGLED
    )
    assert _responses(data) == 1 and b" 400 " in data.split(b"\r\n")[0]
    data = raw_exchange(server, _head("review-gate/claim", "Content-Length: 99999\r\n") + SMUGGLED)
    assert _responses(data) == 1 and b" 413 " in data.split(b"\r\n")[0]
    assert b"Connection: close" in data
    # an unknown route with a body that looks like a request: one response only
    data = raw_exchange(
        server,
        _head("review-gate/jobs/x/approve", f"Content-Length: {len(SMUGGLED)}\r\n") + SMUGGLED,
    )
    assert _responses(data) == 1
    assert server.gate_store.snapshot(KEY).state == State.CI_GREEN
