"""Token-usage retention (LLM page design, slice 1): the control plane enforces
``control_plane.llm_proxy.raw_retention_days`` on token_usage records only."""
import asyncio
import datetime as dt
import json
import threading
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.retention import (
    DEFAULT_RAW_RETENTION_DAYS,
    DEFAULT_ROLLUP_RETENTION_DAYS,
    sweep_token_usage,
    usage_retention_config,
)
from herdr_engineering.control_plane.sink import DurableEventSink

DAY = 86400.0
NOW = dt.datetime(2026, 10, 23, 12, tzinfo=dt.UTC).timestamp()


def _clocked_repo(sink=None):
    clock = [NOW - 20 * DAY]
    repo = ControlPlaneRepo(now=lambda: clock[0], sink=sink)
    return repo, clock


def _seed(repo, clock):
    repo.record_token_usage("p", "m", 1, 1)        # 20 days old: expires
    repo.create_mission("old mission")              # 20 days old: not usage, survives
    clock[0] = NOW - 15 * DAY
    repo.record_token_usage_row({"schema": 2, "project": "q", "model": "m", "status": 200})
    clock[0] = NOW - 1 * DAY
    repo.record_token_usage("p", "m", 2, 2)         # fresh: survives
    clock[0] = NOW


def test_config_defaults_and_validation():
    assert usage_retention_config(None) == {"raw_retention_days": DEFAULT_RAW_RETENTION_DAYS,
                                            "rollup_retention_days": DEFAULT_ROLLUP_RETENTION_DAYS}
    assert (DEFAULT_RAW_RETENTION_DAYS, DEFAULT_ROLLUP_RETENTION_DAYS) == (14, 400)
    assert usage_retention_config({"max_inflight": 4, "raw_retention_days": 3})["raw_retention_days"] == 3
    for bad in ({"raw_retention_days": 0}, {"raw_retention_days": -1}, {"raw_retention_days": "x"},
                {"raw_retention_days": True}, {"rollup_retention_days": 1.5}, ["not", "a", "mapping"]):
        with pytest.raises(ValueError):
            usage_retention_config(bad)


def test_sweep_drops_only_expired_usage_records():
    repo, clock = _clocked_repo()
    _seed(repo, clock)
    assert sweep_token_usage(repo, raw_retention_days=14) == 2
    t = repo.token_analytics()["total"]
    assert (t["requests"], t["input_tokens"]) == (1, 2)
    assert ("token_usage", "q") not in repo._events  # an emptied stream goes away
    assert [m["title"] for m in repo.list_missions()] == ["old mission"]
    assert any(e[0] == "mission" for e in repo._event_order)
    assert sum(1 for e in repo._event_order if e[0] == "token_usage") == 1
    assert sweep_token_usage(repo, raw_retention_days=14) == 0
    repo.record_token_usage("p", "m", 5, 5)  # the stream keeps working after a prune
    assert repo.token_analytics()["total"]["requests"] == 2


class FakeConn:
    def __init__(self):
        self.executed = []

    async def fetchrow(self, sql, *args):
        return [args[0]]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "DELETE 2"


def test_sweep_deletes_the_durable_rows_too():
    conn = FakeConn()
    sink = DurableEventSink(conn=conn, run=lambda coro: asyncio.run(coro))
    repo, clock = _clocked_repo(sink)
    _seed(repo, clock)
    sweep_token_usage(repo, raw_retention_days=14)
    (sql, args) = conn.executed[-1]
    assert sql.startswith("DELETE FROM fabric_events") and "entity_type=$1" in sql
    assert args[0] == "token_usage"
    assert args[1] == dt.datetime.fromtimestamp(NOW - 14 * DAY, tz=dt.UTC)


def test_server_sweeps_usage_with_its_configured_retention():
    repo, clock = _clocked_repo()
    _seed(repo, clock)
    svr = ControlPlaneServer(repo, host="127.0.0.1", port=0, allow_open=True,
                             usage_retention={"raw_retention_days": 16, "rollup_retention_days": 400})
    try:
        svr.sweep_once()
        assert repo.token_analytics()["total"]["requests"] == 2  # only the 20-day-old record went
    finally:
        svr.shutdown()


def test_v2_record_posted_through_the_api_is_stored_whitelisted():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, host="127.0.0.1", port=0, allow_open=True)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        record = {"schema": 2, "project": "p", "key_id": "llmkey_1", "model": "m", "upstream_model": "u",
                  "session_id": "s-1", "agent_id": "a-1", "source": "llm-proxy", "route": "fabric",
                  "fallback": False, "stream": True, "status": 200, "error_class": None,
                  "queue_ms": 1, "ttft_ms": 20, "gen_ms": 300, "duration_ms": 340,
                  "input_tokens": 3, "output_tokens": 9, "reasoning_tokens": None,
                  "cache_read_tokens": None, "usage_known": True, "engine": None, "success": True,
                  "messages": [{"content": "MUST-NOT-BE-STORED"}]}
        req = urllib.request.Request(f"http://127.0.0.1:{svr.port}/api/v1/token-usage",
                                     data=json.dumps(record).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
        (ev,) = repo._events[("token_usage", "p")]
        assert ev["session_id"] == "s-1"
        expected = {k: v for k, v in record.items() if k != "messages"}
        assert {k: ev["payload"].get(k) for k in expected} == expected
        assert "MUST-NOT-BE-STORED" not in json.dumps(repo.export_state())
        with urllib.request.urlopen(f"http://127.0.0.1:{svr.port}/api/v1/analytics/tokens", timeout=5) as r:
            out = json.loads(r.read())
        assert out["total"]["output_tokens"] == 9 and out["by_model"]["m"]["requests"] == 1
    finally:
        svr.shutdown()
