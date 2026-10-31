"""The re-review ledger on a REAL PostgreSQL: a row hydrated after a restart carries its payload as JSON text.

The in-memory tests keep payloads as dicts, so they cannot see a reader that calls ``.get`` on a string. Before the
fix, `RereviewLedger.current` did exactly that: after a restart the re-review POST and the review-quorum GET both
answered 500 for every PR that had a recorded request. Skips when no PostgreSQL binaries are installed.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

import pytest

pytest.importorskip("asyncpg")

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.bridge import PostgresSource  # noqa: E402
from herdr_engineering.control_plane.ingest import IngestService  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402
from herdr_engineering.control_plane.sink import DurableEventSink  # noqa: E402
from tests.control_plane.test_graph import Feed  # noqa: E402
from tests.control_plane.test_t16_herdr_gaps import (  # noqa: E402
    OP_KEY, PI_KEY, PR, VW_KEY, _call, cfg, pid_of, sha)
from tests.control_plane.test_t16_collab_repair_postgres import _bins  # noqa: E402
from tests.e2e.pg import ThrowawayPostgres  # noqa: E402

pytestmark = pytest.mark.skipif(_bins() is None, reason="no PostgreSQL binaries installed")

KEYS = {OP_KEY: "operator", PI_KEY: "ingest", VW_KEY: "viewer"}


@pytest.fixture(scope="module")
def pg():
    root = Path(tempfile.mkdtemp(prefix="t16-rereview-pg-"))
    db = ThrowawayPostgres(_bins(), root).start()
    try:
        yield db
    finally:
        db.stop()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(db.sock, ignore_errors=True)


def _boot(dsn):
    repo = ControlPlaneRepo(sink=DurableEventSink(dsn=dsn))
    srv = ControlPlaneServer(repo, host="127.0.0.1", port=0, api_keys=KEYS, source=PostgresSource(dsn=dsn),
                             review_quorum=cfg([pid_of(PI_KEY)], [pid_of(PI_KEY)]))
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    return srv


def _paths():
    base = "/api/v1/graph/nodes/" + PR.replace(":", "%3A").replace("/", "%2F").replace("#", "%23") + "/review-quorum"
    return base, base + "/rereview"


def test_after_a_restart_the_request_replays_and_the_quorum_read_still_answers(pg):
    quorum, rereview = _paths()
    first = _boot(pg.dsn)
    try:
        Feed(IngestService(first.repo)).node(PR, label="PR 7", principal={"kind": "ingest", "id": pid_of(PI_KEY),
                                             "authenticated_by": "api_key"}, scope="org",
                                             attrs={"head_sha": sha(1), "author": "somebody"})
        s, made = _call(first, "POST", rereview, PI_KEY, {"head": sha(1)})
        assert s == 201, made
        request_id = made["rereview_request"]["request_id"]
    finally:
        first.shutdown()
    second = _boot(pg.dsn)                                     # a new process on the same database: rows come back hydrated
    try:
        s, again = _call(second, "POST", rereview, PI_KEY, {"head": sha(1)})
        assert s == 200 and again["replayed"] is True and again["rereview_request"]["request_id"] == request_id
        s, read = _call(second, "GET", quorum, VW_KEY)
        assert s == 200 and read["rereview_request"]["request_id"] == request_id
    finally:
        second.shutdown()
