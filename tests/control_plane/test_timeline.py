import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

MKEY = "pi:MSN-1"


def _seed(repo):
    repo.upsert_mission("MSN-1", title="t", source="pi")
    repo.set_mission_stage("MSN-1", "implementation", source="pi")
    repo.upsert_test_run("tr-1", mission_id=MKEY, status="passed", kind="test")
    repo.upsert_build("bd-1", mission_id=MKEY, status="succeeded")
    repo.upsert_review("rv-1", mission_id=MKEY, reviewer="alice",
                       status="completed", verdict="needs-changes")
    repo.add_review_finding("rv-1", severity="low", summary="nit")
    repo.upsert_pull_request("pr-1", mission_id=MKEY, number=42,
                             url="https://example.com/pr/42", status="merged")


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()


@pytest.fixture()
def server():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    yield Client(svr.port), repo
    svr.shutdown()


def _sort_key(item):
    return (item["ts"] or "", item["kind"], item["entity_id"] or "")


def test_mission_timeline_repo_level():
    repo = ControlPlaneRepo()
    _seed(repo)
    items = repo.mission_timeline(MKEY)
    kinds = {i["kind"] for i in items}
    assert {"stage", "test", "build", "review", "pr", "merge"} <= kinds
    # sorted by (ts, kind, entity_id)
    assert items == sorted(items, key=_sort_key)
    # every item has exactly the compact shape, no payloads
    for i in items:
        assert set(i) == {"ts", "kind", "status", "title", "ref", "entity_id"}
    # stage items
    stages = [i for i in items if i["kind"] == "stage"]
    assert any(i["title"] == "stage \u2192 implementation" for i in stages)
    # pr + merge
    prs = [i for i in items if i["kind"] == "pr"]
    assert len(prs) == 1
    assert prs[0]["title"] == "PR #42"
    assert prs[0]["ref"] == "https://example.com/pr/42"
    merges = [i for i in items if i["kind"] == "merge"]
    assert len(merges) == 1
    assert merges[0]["status"] == "merged"
    assert merges[0]["ref"] == "https://example.com/pr/42"
    assert merges[0]["title"] == "PR #42 merged"


def test_mission_timeline_unknown_key_raises():
    repo = ControlPlaneRepo()
    with pytest.raises(KeyError):
        repo.mission_timeline("nope")


def test_timeline_api(server):
    c, repo = server
    _seed(repo)
    expected = repo.mission_timeline(MKEY)
    status, _, body = c.get("/api/v1/missions/pi%3AMSN-1/timeline")
    assert status == 200
    data = json.loads(body)
    assert isinstance(data["timeline"], list)
    assert len(data["timeline"]) == len(expected)


def test_timeline_api_not_found(server):
    c, _ = server
    status, _, body = c.get("/api/v1/missions/nope/timeline")
    assert status == 404
    assert json.loads(body) == {"error": "not found"}
