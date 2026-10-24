"""Conditional requests and ``since`` deltas on the read-only API (Task G7).

GitHub lists carry the catalog ``revision`` and ``ETag: W/"<revision>-<hash>"``
(the hash covers the query and the sync state, so a status change is never
hidden behind a 304). ``?since=<revision>`` returns only what changed, or
``resync`` with the full list. Hosts, sessions, services and missions get a
content-hash ETag (``etag_json``). A client that sends no ``If-None-Match``
gets exactly the old 200 responses.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer, etag_json
from herdr_engineering.control_plane.github_store import catalog_store_factory
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.github import GitHubMonitor

_spec = importlib.util.spec_from_file_location("_github_incremental_fakes",
                                               Path(__file__).with_name("test_github_incremental.py"))
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


@pytest.fixture
def fake():
    return _fakes.seeded()  # acme/app #1-5, acme/lib #6-8, acme/empty


@pytest.fixture
def server(fake):
    allow = [[]]
    monitor = GitHubMonitor(provider_factory=lambda: fake, store=catalog_store_factory(None),
                            organizations=lambda: allow[0])
    monitor.allow = allow
    monitor.start = lambda: None  # the test drives passes itself
    monitor.refresh()
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer"}, github_monitor=monitor)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
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


def resync_all(server):
    server.github_monitor._retry_at = 0
    server.github_monitor.refresh()


# ---------------------------------------------------------------- GitHub


@pytest.mark.parametrize("path", ["github", "github/issues", "github/issues?repository=acme/app&tab=all"])
def test_unchanged_github_list_answers_304(server, path):
    status, etag, body = get(server, path)
    assert status == 200 and body["revision"] >= 1
    assert etag.startswith(f'W/"{body["revision"]}-')
    status, again, empty = get(server, path, etag=etag)
    assert status == 304 and again == etag and empty is None
    status, _, plain = get(server, path)  # an old client sends no If-None-Match
    assert status == 200 and plain == body


def test_github_etag_depends_on_the_query(server):
    _, first, _ = get(server, "github/issues?page=1")
    _, second, _ = get(server, "github/issues?page=2")
    assert first != second
    assert get(server, "github/issues?page=2", etag=first)[0] == 200


def test_a_changed_issue_changes_the_etag_and_since_returns_only_that_row(server, fake):
    status, etag, body = get(server, "github/issues")
    revision = body["revision"]
    gid = next(g for g, i in fake.repos["acme/app"].items() if i["number"] == 3)
    fake.tick(600)
    fake.edit("acme/app", gid, title="edited three")
    resync_all(server)
    status, new_etag, fresh = get(server, "github/issues", etag=etag)
    assert status == 200 and new_etag != etag and fresh["revision"] > revision
    status, _, delta = get(server, f"github/issues?since={revision}")
    assert status == 200 and delta["resync"] is False
    assert [row["github_id"] for row in delta["upserted"]] == [gid]
    assert delta["upserted"][0]["title"] == "edited three"
    assert delta["removed"] == [] and delta["revision"] == fresh["revision"]
    assert delta["counts"]["all"] == 8 and delta["status"] == "ready"
    status, _, nothing = get(server, f"github/issues?since={delta['revision']}")
    assert nothing["upserted"] == [] and nothing["removed"] == []


def test_since_reports_a_closed_issue_as_removed(server, fake):
    revision = get(server, "github/issues")[2]["revision"]
    gid = next(g for g, i in fake.repos["acme/lib"].items() if i["number"] == 7)
    fake.tick(600)
    fake.edit("acme/lib", gid, state="CLOSED")
    resync_all(server)
    delta = get(server, f"github/issues?since={revision}")[2]
    assert delta["upserted"] == []
    assert delta["removed"] == [{"repository_id": "github.com/acme/lib", "key": gid}]


def test_since_on_repositories_returns_repository_rows(server, fake):
    revision = get(server, "github")[2]["revision"]
    fake.add_repo("acme/new")
    resync_all(server)
    delta = get(server, f"github?since={revision}")[2]
    assert [row["repository_id"] for row in delta["upserted"]] == ["github.com/acme/new"]
    assert "issues" not in delta


def test_a_row_leaving_the_filter_is_removed_from_a_filtered_delta(server, fake):
    revision = get(server, "github/issues?tab=autospec")[2]["revision"]
    gid = next(g for g, i in fake.repos["acme/app"].items() if i["number"] == 1)
    fake.tick(600)
    fake.edit("acme/app", gid, labels={"nodes": [{"name": "auto-implement"}], "pageInfo": {"hasNextPage": False}})
    resync_all(server)
    delta = get(server, f"github/issues?tab=autospec&since={revision}")[2]
    assert [row["github_id"] for row in delta["upserted"]] == [gid]
    revision = delta["revision"]
    fake.tick(600)
    fake.edit("acme/app", gid, labels={"nodes": [], "pageInfo": {"hasNextPage": False}})
    resync_all(server)
    delta = get(server, f"github/issues?tab=autospec&since={revision}")[2]
    assert delta["upserted"] == [] and delta["removed"] == [{"repository_id": "github.com/acme/app", "key": gid}]


@pytest.mark.parametrize("since", ["999999"])
def test_stale_since_answers_resync_with_the_full_list(server, since):
    status, _, body = get(server, f"github/issues?since={since}")
    assert status == 200 and body["resync"] is True
    assert sorted(row["number"] for row in body["issues"]) == list(range(1, 9))  # unpaginated
    assert body["revision"] < int(since)


def test_since_older_than_the_pruned_tombstones_answers_resync(server, fake):
    monitor = server.github_monitor
    with monitor._lock:
        state = monitor._store._state
        state.meta["pruned_through"] = state.meta["revision"]  # every tombstone so far expired
    status, _, body = get(server, "github?since=0")
    assert body["resync"] is True and len(body["repositories"]) == 3


def test_same_revision_but_changed_sync_status_is_not_a_304(server):
    status, etag, body = get(server, "github")
    server.github_monitor.allow[0] = "acme"  # invalid: the next pass reports invalid_config
    resync_all(server)
    status, new_etag, changed = get(server, "github", etag=etag)
    assert status == 200 and changed["status"] == "invalid_config"
    assert changed["revision"] == body["revision"] and new_etag != etag


@pytest.mark.parametrize("query", ["since=-1", "since=abc", "since=1&since=2", "since="])
def test_invalid_since_is_rejected(server, query):
    assert get(server, "github/issues?" + query)[0] == 400


def test_errors_carry_no_etag(server):
    status, etag, _ = get(server, "github/unknown")
    assert status == 404 and etag is None


def test_weak_comparison_and_lists_in_if_none_match(server):
    _, etag, _ = get(server, "github")
    assert get(server, "github", etag=f'"nope", {etag}')[0] == 304
    assert get(server, "github", etag="*")[0] == 304
    assert get(server, "github", etag=etag.removeprefix("W/"))[0] == 304


# ---------------------------------------------------------------- generic


def test_etag_json_is_a_content_hash():
    payload, etag = etag_json({"b": 1, "a": [1, 2]})
    assert json.loads(payload) == {"b": 1, "a": [1, 2]}
    assert etag == etag_json({"b": 1, "a": [1, 2]})[1]
    assert etag != etag_json({"b": 2, "a": [1, 2]})[1]
    assert etag.startswith('"') and etag.endswith('"')


@pytest.mark.parametrize("path", ["hosts", "hosts/h1", "sessions", "services", "missions"])
def test_polled_endpoints_answer_304_for_an_identical_body(server, path):
    server.repo.upsert_host("h1", "h1", "100.64.0.1")
    status, etag, body = get(server, path)
    assert status == 200 and etag
    assert get(server, path, etag=etag)[0] == 304
    assert get(server, path)[2] == body


def test_polled_endpoint_answers_200_after_a_change(server):
    server.repo.upsert_host("h1", "h1", "100.64.0.1")
    _, etag, _ = get(server, "hosts")
    server.repo.upsert_host("h2", "h2", "100.64.0.2")
    status, new_etag, body = get(server, "hosts", etag=etag)
    assert status == 200 and new_etag != etag and len(body["hosts"]) == 2
    assert get(server, "hosts/missing")[1] is None  # a 404 has no ETag
