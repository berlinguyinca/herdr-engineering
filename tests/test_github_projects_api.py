"""``/api/v1/github/projects`` (Task G9): read-only Projects v2 with ETag and ``since``.

The fake is the incremental-sync ``FakeProjects``; the monitor runs on the in-memory store.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.github_store import catalog_store_factory
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.github import GitHubMonitor

_spec = importlib.util.spec_from_file_location("_github_incremental_fakes_g9",
                                               Path(__file__).with_name("test_github_incremental.py"))
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


def _serve(monitor):
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer"}, github_monitor=monitor)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    return server, thread


@pytest.fixture
def seeded():
    return _fakes.projects_seeded()


@pytest.fixture
def server(seeded):
    fake = seeded[0]
    monitor = GitHubMonitor(provider_factory=lambda: fake, store=catalog_store_factory(None), projects=_fakes.ON)
    monitor.start = lambda: None
    monitor.refresh()
    server, thread = _serve(monitor)
    yield server
    server.shutdown()
    thread.join(timeout=3)


def get(server, path, *, etag=None):
    headers = {"X-API-Key": "read"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/{path}", headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        return response.status, response.headers.get("ETag"), json.loads(raw) if raw else None


def test_projects_list_carries_revision_etag_and_answers_304(server, seeded):
    _, p1, p2, p3 = seeded
    status, etag, body = get(server, "github/projects")
    assert status == 200 and etag.startswith(f'W/"{body["revision"]}-')
    assert {p["project_id"] for p in body["projects"]} == {p1, p2, p3}
    assert body["projects_sync"]["status"] == "ready"
    assert get(server, "github/projects", etag=etag)[0] == 304


def test_one_project_lists_its_items_and_its_delta(server, seeded):
    fake, p1, *_ = seeded
    status, etag, body = get(server, f"github/projects?project={p1}")
    assert status == 200 and body["project"]["project_id"] == p1 and len(body["items"]) == 3
    revision = body["revision"]
    moved = next(iter(fake._project(p1)["items"]))
    fake.move_item(p1, moved, "Done")
    server.github_monitor.refresh()
    status, _, delta = get(server, f"github/projects?project={p1}&since={revision}")
    assert status == 200 and delta["resync"] is False
    assert [(i["item_id"], i["status"]) for i in delta["upserted"]] == [(moved, "Done")]
    assert delta["project"]["project_id"] == p1
    status, _, stale = get(server, f"github/projects?project={p1}&since={10**15}")
    assert stale["resync"] is True and len(stale["items"]) == 3


def test_unknown_or_malformed_project_is_rejected(server):
    assert get(server, "github/projects?project=PVT_nope")[0] == 404
    assert get(server, "github/projects?project=../x")[0] == 400
    assert get(server, "github/projects?tab=all")[0] == 400


def test_a_monitor_without_projects_answers_sync_off():
    class Bare:
        def snapshot(self, start=True):
            return {"available": True, "status": "ready"}

        def close(self):
            pass
    server, thread = _serve(Bare())
    try:
        status, _, body = get(server, "github/projects")
        assert status == 200 and body["projects"] == [] and body["projects_sync"]["status"] == "disabled"
    finally:
        server.shutdown()
        thread.join(timeout=3)
