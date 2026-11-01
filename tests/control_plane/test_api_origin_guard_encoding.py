"""The cross-origin mutation guard decides on the path the router sees (each segment percent-decoded), so an encoded
resource name (``%61rtifacts``) or an encoded ``/`` inside an id cannot get past it. Real HTTP server, no mocks."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

EVIL = {"Origin": "https://evil.example"}

# (guarded route, encoded form the router decodes back to the same route)
ROUTES = [
    ("/api/v1/artifacts/a1/collaborators", "/api/v1/%61rtifacts/a1/collaborators"),
    ("/api/v1/artifacts/a1/collaborators/ag1/revoke", "/api/v1/%61rtifacts/a1/collaborators/ag1/%72evoke"),
    ("/api/v1/artifacts/a1/collaborators/invite-grant", "/api/v1/artifact%73/a1/collaborators/invite%2Dgrant"),
    ("/api/v1/artifacts/a1/collab-restrict", "/api/v1/%61rtifacts/a1/collab-%72estrict"),
    ("/api/v1/artifacts/a1/retract", "/api/v1/%61rtifacts/a1/retr%61ct"),
    ("/api/v1/artifacts/a1/retract", "/api/v1/artifacts/a%2F1/retract"),
    ("/api/v1/artifacts/a1/lineage/backfill", "/api/v1/%61rtifacts/a1/%6Cineage/backfill"),
    ("/api/v1/artifacts/a1/versions/v1/collab-lineage", "/api/v1/%61rtifacts/a1/versions/v1/collab-%6Cineage"),
    ("/api/v1/artifacts/a1/versions/v1/collab-lineage", "/api/v1/artifacts/a1/versions/v%2F1/collab-lineage"),
    ("/api/v1/artifact-jobs/j1/lineage", "/api/v1/%61rtifact-jobs/j1/lineage"),
    ("/api/v1/artifact-reviews/r1/decision", "/api/v1/artifact%2Dreviews/r1/decision"),
    ("/api/v1/publications/p1/revoke", "/api/v1/%70ublications/p1/revoke"),
    ("/api/v1/artifact-reports/r1/approve", "/api/v1/artifact-%72eports/r1/approve"),
    # a decoded %2F stays ONE segment for the router, so the regex guards must still match
    ("/api/v1/graph/nodes/xy/review-quorum/rereview", "/api/v1/graph/nodes/x%2Fy/review-quorum/rereview"),
    ("/api/v1/sessions/ab/terminal", "/api/v1/sessions/a%2Fb/terminal"),
    ("/api/v1/steering/ab/ack", "/api/v1/steering/a%2Fb/ack"),
    ("/api/v1/sessions/ab/summary/fix", "/api/v1/sessions/a%2Fb/summary/fix"),
    ("/api/v1/artifacts/a1/collab-restrict", "/api/v1/artifacts/a%2Fb%2F1/collab-restrict"),
]


@pytest.fixture(scope="module")
def base():
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"k": "operator"})
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()


def post(base, path, headers=None):
    req = urllib.request.Request(base + path, data=b"{}", method="POST",
                                 headers={"X-API-Key": "k", "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else None


@pytest.mark.parametrize("plain,encoded", ROUTES)
def test_a_cross_origin_post_is_refused_on_the_plain_and_the_encoded_path(base, plain, encoded):
    for path in (plain, encoded):
        status, body = post(base, path, EVIL)
        assert status == 403, path
        assert "cross-origin" in json.dumps(body), path


@pytest.mark.parametrize("plain,encoded", ROUTES)
def test_the_same_origin_post_is_not_refused_by_the_guard(base, plain, encoded):
    for path in (plain, encoded):
        status, body = post(base, path, {"Origin": base})
        assert "cross-origin" not in json.dumps(body), path


def test_agent_ids_in_the_room_namespace_are_refused():
    from herdr_engineering.control_plane.artifact_collab_api import AGENT_ID
    assert AGENT_ID.fullmatch("agent_1.x:y") and AGENT_ID.fullmatch("roomy:1")
    assert not AGENT_ID.fullmatch("room:abc") and not AGENT_ID.fullmatch("room:")


def test_user_prefixed_agent_ids_are_refused():
    from herdr_engineering.control_plane.artifact_collab_api import AGENT_ID
    assert not AGENT_ID.fullmatch("user:x") and AGENT_ID.fullmatch("users:x")
