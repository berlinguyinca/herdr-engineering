"""POST /api/v1/llm/models: a registered custom model must not be a way to capture another project's model name or
to point the LLM proxy at the cloud metadata service. Real HTTP server, no mocks."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


@pytest.fixture
def server():
    svr = ControlPlaneServer(ControlPlaneRepo())
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield svr
    svr.shutdown()


def post(server, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def models(server):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/v1/llm/models", timeout=10) as r:
        return {m["name"]: m for m in json.loads(r.read())["models"]}


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data", "http://[fe80::1]:80", "http://metadata.google.internal/",
    "http://user:pw@127.0.0.1:9000", "file:///etc/passwd", "http://2852039166/", "http://0.0.0.0:80",
])
def test_a_dangerous_base_url_is_refused_at_registration(server, url):
    status, body = post(server, "/api/v1/llm/models", {"name": "m", "base_url": url})
    assert status == 400, body
    assert "m" not in models(server)


def test_a_private_service_is_still_registered(server):
    for url in ("http://127.0.0.1:9000", "http://llm-gateway:8080/v1", "https://llm.example.test"):
        assert post(server, "/api/v1/llm/models", {"name": "m", "base_url": url})[0] == 200
    assert models(server)["m"]["base_url"] == "https://llm.example.test"


def test_a_name_owned_by_another_project_cannot_be_taken_over(server):
    assert post(server, "/api/v1/llm/models",
                {"name": "m", "base_url": "http://127.0.0.1:9000", "project": "a"})[0] == 200
    for project in ("b", None):
        status, body = post(server, "/api/v1/llm/models",
                            {"name": "m", "base_url": "http://127.0.0.1:9999", "project": project})
        assert status == 400 and "owned" in json.dumps(body), (project, body)
    assert models(server)["m"]["base_url"] == "http://127.0.0.1:9000"
    # the owner can update it, and delete frees the name
    assert post(server, "/api/v1/llm/models",
                {"name": "m", "base_url": "http://127.0.0.1:9001", "project": "a"})[0] == 200
    assert post(server, "/api/v1/llm/models/m/delete", {})[0] == 200
    assert post(server, "/api/v1/llm/models",
                {"name": "m", "base_url": "http://127.0.0.1:9002", "project": "b"})[0] == 200


def test_a_shared_model_cannot_be_claimed_by_a_project(server):
    assert post(server, "/api/v1/llm/models", {"name": "s", "base_url": "http://127.0.0.1:9000"})[0] == 200
    status, body = post(server, "/api/v1/llm/models", {"name": "s", "base_url": "http://127.0.0.1:9999", "project": "a"})
    assert status == 400 and "ownership" in json.dumps(body), body
    assert models(server)["s"]["base_url"] == "http://127.0.0.1:9000"
    assert post(server, "/api/v1/llm/models", {"name": "s", "base_url": "http://127.0.0.1:9001"})[0] == 200
