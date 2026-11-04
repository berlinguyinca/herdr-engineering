"""Durable history failures never become disconnects, secret leaks or cached reads."""

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.projection_history import (
    DurableHistoryUnavailable,
    HistoryCursorStale,
    PostgresHistory,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

SECRET = "private-db-secret"


class DriverFailure(Exception):
    pass


FAILURES = [ConnectionResetError, TimeoutError, ValueError, DriverFailure]


@pytest.fixture
def history_api(monkeypatch):
    history = PostgresHistory("unused-invalid-private-dsn", asyncio.run)
    calls = []
    failure = [DriverFailure]

    async def fail_pool():
        calls.append(True)
        raise failure[0](SECRET)

    monkeypatch.setattr(history, "_get_pool", fail_pool)
    repo = ControlPlaneRepo()
    repo.record_event("service", "s1", "ServiceRegistered", {"description": "cached-partial"})
    repo._durable_history = history
    server = ControlPlaneServer(repo, api_keys={"reader": "viewer"})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()

    def get(extra=None, kind="service"):
        query = urllib.parse.urlencode({"entity_type": kind, "entity_id": "s1", **(extra or {})})
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/v1/events?{query}",
            headers={"X-API-Key": "reader"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response), response.headers

    try:
        yield history, failure, calls, get
    finally:
        server.shutdown()
        thread.join(timeout=5)
        history.close()


@pytest.mark.parametrize("failure_type", FAILURES)
def test_public_legacy_and_page_fail_closed_with_safe_503(history_api, caplog, failure_type):
    _, failure, calls, get = history_api
    failure[0] = failure_type
    caplog.set_level(logging.WARNING)
    request_ids = []
    for extra in ({}, {"limit": 1}):
        status, body, headers = get(extra)
        assert status == 503
        assert body == {"error": "Integration service is unavailable."}
        assert SECRET not in json.dumps(body)
        assert "cached-partial" not in json.dumps(body)
        request_id = headers["X-Request-ID"]
        assert request_id
        request_ids.append(request_id)
    assert len(calls) == 2
    assert len(set(request_ids)) == 2
    # The API's cause mapping must preserve the original class, not its private
    # message or the wrapper name. Main owns that handler integration.
    # The handler logs in finally, after its response can reach the client.
    # Wait for both specific requests, then retain the exact-count assertions.
    deadline = time.monotonic() + 2
    while True:
        records = [
            record for record in caplog.records if record.getMessage().startswith("request id=")
        ]
        if all(
            any(f"id={request_id} " in record.getMessage() for record in records)
            for request_id in request_ids
        ):
            break
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"Missing completed request logs for {request_ids}"
        time.sleep(min(0.01, remaining))
    assert len(records) == 2
    records_by_id = {
        request_id: [record for record in records if f"id={request_id} " in record.getMessage()]
        for request_id in request_ids
    }
    for request_id in request_ids:
        assert len(records_by_id[request_id]) == 1
        record = records_by_id[request_id][0]
        assert f"id={request_id}" in record.getMessage()
        assert f"cause={failure_type.__name__}" in record.getMessage()
        assert record.exc_info is None
    assert SECRET not in caplog.text
    assert "unused-invalid-private-dsn" not in caplog.text


@pytest.mark.parametrize("extra", [{}, {"limit": 1}, {"before": "invalid"}])
def test_private_history_is_404_without_touching_database(history_api, extra):
    _, _, calls, get = history_api
    for kind in ("inspector", "host_metrics", "agent_identity", "lineage", "collab", "deployment"):
        status, body, headers = get(extra, kind)
        assert (status, body) == (404, {"error": "not found"})
        assert headers["X-Request-ID"]
    assert calls == []


@pytest.mark.parametrize("extra", [{"limit": 0}, {"limit": 201}, {"before": "invalid"}])
def test_invalid_history_input_remains_400_without_database(history_api, extra):
    _, _, calls, get = history_api
    status, body, headers = get(extra)
    assert status == 400
    assert "error" in body
    assert headers["X-Request-ID"]
    assert calls == []


@pytest.mark.parametrize("failure_type", FAILURES)
@pytest.mark.parametrize(
    "method", ["stream", "stream_page", "entity_ids", "recent_events", "export_events"]
)
def test_every_durable_read_wraps_original_class_without_private_message(
    monkeypatch, failure_type, method
):
    history = PostgresHistory("unused", asyncio.run)

    async def fail_pool():
        raise failure_type(SECRET)

    monkeypatch.setattr(history, "_get_pool", fail_pool)
    arguments = {
        "stream": ("service", "s1"),
        "stream_page": ("service", "s1"),
        "entity_ids": ("service",),
        "recent_events": (),
        "export_events": (),
    }
    with pytest.raises(DurableHistoryUnavailable) as caught:
        getattr(history, method)(*arguments[method])
    assert str(caught.value) == "Durable history is unavailable."
    assert caught.value.cause_name == failure_type.__name__
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert SECRET not in repr(caught.value)


def test_missing_cursor_anchor_remains_409_not_unavailable(history_api, monkeypatch):
    history, _, calls, get = history_api

    class Connection:
        @asynccontextmanager
        async def transaction(self, **kwargs):
            yield

        async def fetchrow(self, *args):
            return None

    class Pool:
        @asynccontextmanager
        async def acquire(self, **kwargs):
            yield Connection()

    async def pool():
        return Pool()

    monkeypatch.setattr(history, "_get_pool", pool)
    memory = ControlPlaneRepo()
    for n in range(3):
        memory.record_event("service", "s1", "Observed", {"index": n})
    cursor = memory.stream_page("service", "s1", limit=1)["next_cursor"]
    with pytest.raises(HistoryCursorStale, match="^staleHistoryCursor$"):
        history.stream_page("service", "s1", before=cursor)
    status, body, headers = get({"before": cursor})
    assert (status, body) == (409, {"error": "staleHistoryCursor"})
    assert headers["X-Request-ID"]
    assert calls == []
