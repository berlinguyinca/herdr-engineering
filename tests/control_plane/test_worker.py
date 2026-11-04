# ruff: noqa: F811  (pytest fixtures are imported by name further down)
import asyncio
from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane import worker


class _Svc:
    def __init__(self, service_id):
        self.updated = []
        self.services = [{
            "service_id": service_id, "health": "healthy", "status": "active",
            "last_seen": datetime.now(UTC) - timedelta(seconds=999),
        }]

    async def list(self, host_id=None, mission_id=None):
        return self.services

    async def update_health(self, service_id, health):
        self.updated.append((service_id, health))


def _run(coro):
    return asyncio.run(coro)


def test_marks_stale_service_unavailable_without_deleting():
    svc = _Svc("service_s1")
    summary = _run(worker.run_one_pass(None, service_store=svc))
    assert summary["stale_services"] == ["service_s1"]
    assert svc.updated == [("service_s1", "unavailable")]
    assert len(svc.services) == 1  # retained, never deleted


def test_worker_loop_obeys_stop_and_swallows_errors():
    class _Bad:
        async def list(self, **k):
            raise RuntimeError("boom")

    async def main():
        stop = asyncio.Event()

        async def stopper():
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.get_running_loop().create_task(stopper())
        await worker.run_worker(None, interval=0.01, stop=stop,
                                service_store=_Bad())

    _run(main())  # must terminate and not raise


def test_artifact_job_pass_claims_one_job_and_records_completion():
    class _Conn:
        def __init__(self):
            self.executed = []

        async def fetchrow(self, sql, *args):
            if "FOR UPDATE SKIP LOCKED" in sql:
                return {"job_id": "job_1", "artifact_id": "artifact_1",
                        "base_version_id": "version_1", "profile": "web",
                        "request": {}}
            return None

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "UPDATE 1"

    async def runner(job):
        return {"version_id": "version_2", "outputs": [{"blob_sha256": "sha"}],
                "validation": {"status": "passed"}}

    conn = _Conn()
    summary = _run(worker.run_one_pass(conn, worker_id="host-a",
                                       artifact_job_runner=runner))
    assert summary["artifact_jobs_completed"] == 1
    assert any("status='completed'" in sql for sql, _ in conn.executed)


class _JobConn:
    """SQL boundary stand-in: claim returns the row after its running update."""

    def __init__(self, count=1):
        self.pending = [{"job_id": f"job_{i}", "artifact_id": "artifact_1",
                         "base_version_id": None, "profile": "web",
                         "requested_by": "owner", "request": {"source": "<html>ok</html>"}}
                        for i in range(count)]
        self.executed = []
        self.claims = 0

    async def fetchrow(self, sql, *args):
        assert "FOR UPDATE SKIP LOCKED" in sql
        assert "UPDATE artifact_build_jobs" in sql
        assert "RETURNING" in sql
        self.claims += 1
        if not self.pending:
            return None
        return {**self.pending.pop(0), "assigned_host_id": args[0], "status": "running"}

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def test_artifact_job_failure_is_durable_and_next_job_still_runs():
    import json

    conn = _JobConn(count=3)
    seen = []

    async def runner(job):
        seen.append(job)
        assert job["assigned_host_id"] == "host-a"
        assert job["status"] == "running"
        if job["job_id"] == "job_0":
            raise ValueError("bad source")
        return {"version_id": "version_2"}

    summary = _run(worker.run_one_pass(conn, worker_id="host-a",
                                       artifact_job_runner=runner, max_artifact_jobs=2))
    assert summary["artifact_jobs_claimed"] == 2
    assert summary["artifact_jobs_completed"] == 1
    assert summary["artifact_jobs_failed"] == 1
    assert len(conn.pending) == 1
    failures = [args for sql, args in conn.executed if "status='failed'" in sql]
    assert json.loads(failures[0][0]) == {"message": "bad source"}
    assert failures[0][1] == "job_0"
    assert len(seen) == 2


def test_artifact_job_pass_without_runner_does_not_claim():
    conn = _JobConn()
    summary = _run(worker.run_one_pass(conn, worker_id="host-a"))
    assert conn.claims == 0
    assert summary["artifact_jobs_claimed"] == 0


def test_artifact_job_pass_stops_at_empty_queue():
    conn = _JobConn(count=0)

    async def runner(job):
        raise AssertionError("empty queue must not invoke renderer")

    summary = _run(worker.run_one_pass(conn, worker_id="host-a",
                                       artifact_job_runner=runner, max_artifact_jobs=5))
    assert conn.claims == 1
    assert summary["artifact_jobs_claimed"] == 0


def test_background_worker_runs_real_renderer_and_commits_version():
    from herdr_engineering.control_plane.artifact_render_runner import ArtifactRenderRunner
    from herdr_engineering.control_plane.artifact_renderers import RendererRegistry

    class RustFS:
        def __init__(self):
            self.objects = {}

        def put(self, data):
            import hashlib
            digest = hashlib.sha256(data).hexdigest()
            key = "sha256/" + digest
            self.objects[key] = data
            return key, digest

    class Store:
        def __init__(self, stop):
            self._rf = RustFS()
            self.commits = []
            self.stop = stop

        async def commit_version(self, **kwargs):
            self.commits.append(kwargs)
            self.stop.set()
            return {"version_id": "version_rendered"}

    async def main():
        stop = asyncio.Event()
        conn = _JobConn()
        store = Store(stop)
        await asyncio.wait_for(worker.run_worker(
            conn, interval=0.001, stop=stop, worker_id="host-a",
            artifact_job_runner=ArtifactRenderRunner(store, RendererRegistry())), timeout=2)
        assert len(store.commits) == 1
        commit = store.commits[0]
        assert commit["job_id"] == "job_0"
        assert commit["host_id"] == "host-a"
        assert commit["created_by"] == "owner"
        assert commit["output_manifest"]
        assert any(b"<html>ok</html>" in data for data in store._rf.objects.values())
        assert any("status='completed'" in sql and args == ("version_rendered", "job_0", "host-a")
                   for sql, args in conn.executed)

    _run(main())



def test_cancelled_job_is_not_overwritten_by_worker_completion_or_failure():
    class CancelledConn(_JobConn):
        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            # Simulate cancellation landing while rendering is in progress.
            if "AND status='running'" in sql and "assigned_host_id=$3" in sql:
                return "UPDATE 0"
            return "UPDATE 1"

    async def main():
        for fail in (False, True):
            conn = CancelledConn()

            async def runner(job):
                if fail:
                    raise ValueError("renderer failed after cancellation")
                return {"version_id": "late_version"}

            summary = await worker.run_one_pass(conn, worker_id="host-a",
                                                 artifact_job_runner=runner)
            assert summary["artifact_jobs_claimed"] == 1
            assert summary["artifact_jobs_completed"] == 0
            assert summary["artifact_jobs_failed"] == 0
            assert all(args[-1] == "host-a" for sql, args in conn.executed
                       if "UPDATE artifact_build_jobs" in sql)

    _run(main())



def test_worker_registers_observed_host_before_claim_without_overwriting_existing(monkeypatch):
    monkeypatch.setattr(worker.socket, "gethostname", lambda: "observed-executor")

    class HostConn(_JobConn):
        async def fetchrow(self, sql, *args):
            assert self.executed, "worker must persist its host before claiming"
            host_sql, host_args = self.executed[0]
            assert "INSERT INTO hosts" in host_sql
            assert "ON CONFLICT (host_id) DO NOTHING" in host_sql
            assert host_args == ("host-a", "observed-executor")
            return await super().fetchrow(sql, *args)

    async def runner(job):
        return {"version_id": "version_1"}

    summary = _run(worker.run_one_pass(HostConn(), worker_id="host-a",
                                       artifact_job_runner=runner))
    assert summary["artifact_jobs_completed"] == 1


# ======================================================================================================================
# CollabBridgeLoop (plan 3, Task 9): the 1 s grants / mirror / applier tasks beside the 30 s worker pass.
# Real PostgreSQL, real mirror / applier / bridge objects; only the connection factory and the blocking RustFS calls
# are injected.
# ======================================================================================================================
import json  # noqa: E402
import time  # noqa: E402

import asyncpg  # noqa: E402

from herdr_engineering.control_plane.artifact_collab_store import CollabGrantStore  # noqa: E402
from herdr_engineering.control_plane.artifact_scope import ArtifactGraphGate  # noqa: E402
from herdr_engineering.control_plane.artifacts import ArtifactStore  # noqa: E402
from herdr_engineering.control_plane.collab_artifacts import CollabArtifactBridge  # noqa: E402
from herdr_engineering.control_plane.collab_bridge_files import (  # noqa: E402
    BridgeLimits,
    upload_id_for,
)
from herdr_engineering.control_plane.collab_intents import CollabIntentApplier  # noqa: E402
from herdr_engineering.control_plane.collab_mirror import CollabMirror  # noqa: E402
from tests.control_plane.registry_pg import FakeBlobs, registry_pg  # noqa: E402, F401 - imported fixture
from tests.control_plane.test_collab_tasks import W, svc  # noqa: E402, F401 - imported fixture

BASE = "line 1\nline 2\n"


class SlowPutBlobs(FakeBlobs):
    """``put_stream`` blocks for ``put_delay`` seconds (as a slow RustFS upload would)."""

    def __init__(self):
        super().__init__()
        self.put_delay = 0.0
        self.entered = 0

    def put_stream(self, stream, **kw):
        self.entered += 1
        time.sleep(self.put_delay)
        return super().put_stream(stream, **kw)


class SlowGetBlobs(FakeBlobs):
    """``iter_get`` blocks for ``get_delay`` seconds before the first chunk (a slow mirror refresh)."""

    def __init__(self):
        super().__init__()
        self.get_delay = 0.0

    def iter_get(self, key, expected_sha256, *, chunk_size=1 << 20):
        time.sleep(self.get_delay)
        yield from super().iter_get(key, expected_sha256, chunk_size=chunk_size)


class Connections:
    """The loop's ``dsn_connect``: real asyncpg connections, recorded with the name of the task that asked."""

    def __init__(self, dsn):
        self.dsn, self.made = dsn, []

    async def __call__(self):
        conn = await asyncpg.connect(self.dsn)
        self.made.append((asyncio.current_task().get_name(), conn))
        return conn

    def by_task(self, name):
        return [c for n, c in self.made if n == name]


class LoopRig:
    def __init__(self, svc, tmp_path, dsn, blobs, *, fail_connect=None, **loop_kw):
        self.svc, self.dsn, self.blobs = svc, dsn, blobs
        self.clock = svc.clock
        self.clock.t = time.time()
        self.w = W(svc)
        self.mirror_root, self.outbox = tmp_path / "mirror", tmp_path / "outbox"
        self.outbox.mkdir()
        self.limits = BridgeLimits()
        self.gate = ArtifactGraphGate(None)
        self.mirror = CollabMirror(self.mirror_root, rustfs=blobs, limits=self.limits, clock=self.clock)
        self.applier = CollabIntentApplier(self.outbox, self.mirror, rustfs=blobs, limits=self.limits,
                                           spool_dir=tmp_path / "spool", clock=self.clock, gate=self.gate)
        self.applier.register_commit()
        self.bridge = CollabArtifactBridge(svc, self.mirror_root, self.outbox, clock=self.clock, feed_interval=3600.0)
        svc.artifact_bridge = self.bridge
        self.conns = Connections(dsn)
        kw = {"interval": 0.05, "mirror_every": 0.3, "retractions_every": 0.3, "heartbeat": 0.2,
              "reconnect_delay": 0.05, **loop_kw}
        self.loop = worker.CollabBridgeLoop(self.conns, self.mirror, self.applier, **kw)
        self.stop = asyncio.Event()
        self.task = None
        self.ticker = None

    async def __aenter__(self):
        async def tick():                       # the "fake clock" follows real time, so DB expiry and heartbeat agree
            while True:
                self.clock.t = time.time()
                await asyncio.sleep(0.02)
        self.ticker = asyncio.create_task(tick())
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)
        async with self.pool.acquire() as conn:      # the module-scoped database keeps earlier tests' grants alive
            await conn.execute("DELETE FROM artifact_acl WHERE left(principal_id, 7) = 'collab:'")
        self.bridge.write_agents_feed()
        return self

    def start(self):
        self.task = asyncio.create_task(self.loop.run(self.stop))

    async def __aexit__(self, *exc):
        self.stop.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, 30)
        self.bridge.stop()
        self.ticker.cancel()
        await self.pool.close()

    # -- owner side
    async def artifact(self, text=BASE) -> tuple[str, str]:
        async with self.pool.acquire() as conn:
            store = ArtifactStore(conn, self.blobs)
            aid = (await store.register_logical(original_filename="doc.md", created_by="owner"))["artifact_id"]
            key, digest = self.blobs.put(text.encode())
            v = await store.commit_version(aid, base_version_id=None, source_manifest_blob=None,
                                           output_manifest=[{"name": "doc.md", "media_type": "text/markdown",
                                                             "sha256": digest, "size_bytes": len(text.encode()),
                                                             "storage_key": key}], created_by="owner")
        return aid, v["version_id"]

    def agent(self, name):
        pr, info = self.w.agent("alice", name=name)
        self.bridge.write_agents_feed()
        return pr

    async def grant(self, aid, pr, permission="write"):
        async with self.pool.acquire() as conn:
            await CollabGrantStore(conn).grant(aid, pr.id, permission, expires_at=datetime.now(UTC) + timedelta(days=1),
                                               granted_by="owner")

    async def revoke(self, aid, pr):
        async with self.pool.acquire() as conn:
            assert await CollabGrantStore(conn).revoke(aid, pr.id)

    # -- observers
    def grants(self):
        try:
            return json.loads((self.mirror_root / "grants.json").read_text())
        except (OSError, ValueError):
            return None

    def result(self, ticket):
        try:
            return json.loads((self.mirror_root / "results" / f"{ticket}.json").read_text())
        except (OSError, ValueError):
            return None


async def until(predicate, timeout=20.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(step)
    raise AssertionError(f"condition not reached within {timeout}s")


def loop_run(coro):
    async def go():
        await asyncio.wait_for(coro, 120)
    asyncio.run(go())


def test_the_three_tasks_use_three_connections_none_the_worker_passes(registry_pg, svc, tmp_path):
    async def go():
        worker_conn = await asyncpg.connect(registry_pg["dsn"])      # run_worker's own connection
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs()) as rig:
            aid, _ = await rig.artifact()
            pr = rig.agent("agent-a")
            await rig.grant(aid, pr)
            rig.start()
            await until(lambda: rig.grants() and pr.id in rig.grants()["agents"])
            await until(lambda: (rig.mirror_root / "artifacts" / aid / "head.json").exists())
            await until(lambda: rig.conns.by_task("collab-applier"))
            seen = {name: {id(c) for c in rig.conns.by_task(name)}
                    for name in ("collab-grants", "collab-mirror", "collab-applier")}
            assert all(len(v) >= 1 for v in seen.values()), seen
            ids = [i for v in seen.values() for i in v]
            assert len(ids) == len(set(ids)), "a connection is shared between tasks"
            assert id(worker_conn) not in ids and all(c is not worker_conn for _, c in rig.conns.made)
        await worker_conn.close()
    loop_run(go())


def test_a_failing_mirror_sync_does_not_stop_grants_or_the_applier(registry_pg, svc, tmp_path, caplog):
    async def go():
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs()) as rig:
            aid, v1 = await rig.artifact()
            pr = rig.agent("agent-a")
            await rig.grant(aid, pr)
            rig.start()
            await until(lambda: (rig.mirror_root / "artifacts" / aid / "head.json").exists())

            async def broken(*a, **k):
                raise RuntimeError("sync boom SECRET-DSN")
            rig.mirror.sync = broken
            made = len(rig.conns.by_task("collab-mirror"))
            await until(lambda: len(rig.conns.by_task("collab-mirror")) > made)        # the mirror task reconnects
            assert any("RuntimeError" in r.getMessage() for r in caplog.records)
            assert not any("SECRET-DSN" in r.getMessage() for r in caplog.records)     # the type only
            before = rig.grants()["generated_at"]
            ticket = rig.bridge.commit(pr, {"artifact_id": aid, "base_version_id": v1, "message": "m",
                                            "changes": [{"name": "doc.md", "op": "put", "text": "edit\nline 2\n"}]})
            decided = await until(lambda: rig.result(ticket["ticket"]), timeout=10)     # the applier still decides
            assert decided["result"] in ("created", "merged")
            await until(lambda: rig.grants()["generated_at"] > before)                  # and grants.json heartbeats
    loop_run(go())


def test_a_failing_grants_task_does_not_stop_the_mirror(registry_pg, svc, tmp_path):
    async def go():
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs()) as rig:
            aid, v1 = await rig.artifact()
            pr = rig.agent("agent-a")
            await rig.grant(aid, pr)

            async def broken(*a, **k):
                raise RuntimeError("grants boom")
            rig.mirror.publish_grants = broken
            rig.start()
            await until(lambda: (rig.mirror_root / "artifacts" / aid / "head.json").exists())
            assert rig.grants() is None                                  # the grants task really was broken
    loop_run(go())


def _staged_commit(rig, pr, aid, base, name, data):
    import base64
    import hashlib
    digest = hashlib.sha256(data).hexdigest()
    rig.bridge.stage_chunk(pr, {"artifact_id": aid, "name": name, "size_bytes": len(data), "file_sha256": digest,
                                "total": 1, "index": 0, "data": base64.b64encode(data).decode(),
                                "sha256": digest})
    upl = upload_id_for(pr.id, aid, name, digest, len(data))
    return rig.bridge.commit(pr, {"artifact_id": aid, "base_version_id": base, "message": "bin",
                                  "changes": [{"name": name, "op": "put", "upload_id": upl}]})["ticket"]


def test_a_blocking_put_stream_stops_neither_the_heartbeat_nor_another_agents_intent(registry_pg, svc, tmp_path):
    async def go():
        blobs = SlowPutBlobs()
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], blobs) as rig:
            ax, vx = await rig.artifact()
            ay, vy = await rig.artifact()
            a, b = rig.agent("agent-a"), rig.agent("agent-b")
            await rig.grant(ax, a)
            await rig.grant(ay, b)
            rig.start()
            for aid in (ax, ay):
                await until(lambda aid=aid: (rig.mirror_root / "artifacts" / aid / "head.json").exists())
            await until(lambda: rig.grants() and a.id in rig.grants()["agents"] and b.id in rig.grants()["agents"])
            blobs.put_delay = 5.0
            slow = _staged_commit(rig, a, ax, vx, "big.bin", b"binary payload\x00\x01" * 10)
            await until(lambda: blobs.entered >= 1, timeout=10)         # a lane is inside the 5 s call
            before = rig.grants()["generated_at"]
            fast = rig.bridge.commit(b, {"artifact_id": ay, "base_version_id": vy, "message": "inline",
                                         "changes": [{"name": "doc.md", "op": "put", "text": "b edit\nline 2\n"}]})
            decided = await until(lambda: rig.result(fast["ticket"]), timeout=4)        # well inside the 5 s block
            assert decided["result"] in ("created", "merged")
            assert rig.result(slow) is None                                          # the blocked one is still running
            await until(lambda: rig.grants()["generated_at"] > before + 0.3, timeout=3)   # heartbeat never paused
            blobs.put_delay = 0.0
            await until(lambda: rig.result(slow), timeout=15)
    loop_run(go())


def test_a_five_second_mirror_refresh_does_not_hold_back_a_revoke(registry_pg, svc, tmp_path):
    async def go():
        blobs = SlowGetBlobs()
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], blobs) as rig:
            ax, _ = await rig.artifact()
            ay, _ = await rig.artifact()
            a = rig.agent("agent-a")
            await rig.grant(ax, a)
            rig.start()
            await until(lambda: (rig.mirror_root / "artifacts" / ax / "head.json").exists())
            blobs.get_delay = 5.0                                                     # every new download takes 5 s
            await rig.grant(ay, a)                                                    # a new artifact to mirror slowly
            await until(lambda: rig.grants() and ay in rig.grants()["agents"].get(a.id, {}), timeout=4)
            assert not list((rig.mirror_root / "artifacts" / ay / "blobs").glob("*"))    # its download is still running
            before = rig.grants()["generated_at"]
            await rig.revoke(ax, a)
            t0 = time.monotonic()
            await until(lambda: ax not in rig.grants()["agents"].get(a.id, {}), timeout=3)
            assert time.monotonic() - t0 < 2.5
            await until(lambda: rig.grants()["generated_at"] > before + 0.3, timeout=3)
            blobs.get_delay = 0.0
            await until(lambda: list((rig.mirror_root / "artifacts" / ay / "blobs").glob("*")), timeout=15)
    loop_run(go())


def test_stop_tears_everything_down(registry_pg, svc, tmp_path):
    async def go():
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs()) as rig:
            closed = []
            real_close, real_aclose = rig.mirror.close, rig.applier.aclose
            rig.mirror.close = lambda: (closed.append("mirror"), real_close())[1]

            async def aclose(*a, **k):
                closed.append("applier")
                await real_aclose(*a, **k)
            rig.applier.aclose = aclose
            rig.start()
            await until(lambda: rig.grants())
            rig.stop.set()
            await asyncio.wait_for(rig.task, 20)                      # honours the stop event promptly
            assert closed == ["applier", "mirror"]
            assert all(c.is_closed() for _, c in rig.conns.made)       # every connection the loop opened is closed
    loop_run(go())


def test_the_gate_is_refreshed_from_the_registrys_retractions(registry_pg, svc, tmp_path):
    async def go():
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs()) as rig:
            aid, _ = await rig.artifact()
            pr = rig.agent("agent-a")
            await rig.grant(aid, pr)
            rig.start()
            await until(lambda: rig.grants() and aid in rig.grants()["agents"].get(pr.id, {}))
            async with rig.pool.acquire() as conn:
                await ArtifactStore(conn, rig.blobs).record_retraction(aid, None)
            await until(lambda: rig.gate.artifact_retracted(aid))
            await until(lambda: aid not in rig.grants()["agents"].get(pr.id, {}), timeout=3)   # grants.json drops it
    loop_run(go())


def test_the_loop_has_no_environment_reads():
    import inspect
    src = inspect.getsource(worker)
    assert "os.environ" not in src and "getenv" not in src


def test_staging_sweep_runs_with_the_fresh_feed_survives_any_exception_and_skips_a_stale_feed(registry_pg, svc, tmp_path,
                                                                                              caplog):
    async def go():
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs(), sweep_every=0.1) as rig:
            aid, _ = await rig.artifact()
            pr = rig.agent("agent-a")
            await rig.grant(aid, pr)
            calls, results = [], []
            real_sweep = rig.applier.sweep_staging

            async def sweep(conn, *, agents, grants):
                calls.append((agents, list(grants)))
                if len(calls) == 2:
                    raise RecursionError("maximum recursion depth SECRET")
                return await real_sweep(conn, agents=agents, grants=grants)
            rig.applier.sweep_staging = sweep
            real_results = rig.applier.sweep_results

            async def sweep_results(*a, **k):
                results.append(1)
                return await real_results(*a, **k)
            rig.applier.sweep_results = sweep_results
            rig.start()
            await until(lambda: len(calls) >= 4)                    # a RecursionError (call 2) did not end the task
            agents, grants = calls[0]
            assert pr.id in agents and [g["artifact_id"] for g in grants] == [aid]
            assert len(rig.conns.by_task("collab-mirror")) == 1       # and it did not even reconnect
            assert any("RecursionError" in r.getMessage() for r in caplog.records)
            assert not any("SECRET" in r.getMessage() for r in caplog.records)
            assert results                                           # sweep_results ran (awaited)
            (rig.outbox / "agents.json").unlink()                   # a missing feed: the staging sweep is skipped
            seen = len(calls)
            n_results = len(results)
            await until(lambda: len(results) > n_results + 2)       # but the loop kept running
            assert len(calls) == seen
            stale = {"generated_at": time.time() - 3600, "agents": {}}
            (rig.outbox / "agents.json").write_text(json.dumps(stale))      # a stale feed: skipped as well
            n_results = len(results)
            await until(lambda: len(results) > n_results + 2)
            assert len(calls) == seen
    loop_run(go())


def test_a_failed_post_commit_refresh_and_a_grant_change_force_the_next_sync(registry_pg, svc, tmp_path):
    async def go():
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs(), mirror_every=3600.0) as rig:
            aid, _ = await rig.artifact()
            pr = rig.agent("agent-a")
            forced = []
            real_sync = rig.mirror.sync

            async def sync(store, *, gate, force=frozenset(), grants=None):
                forced.append(set(force))
                return await real_sync(store, gate=gate, force=force, grants=grants)
            rig.mirror.sync = sync
            rig.start()
            await until(lambda: forced)                                  # the first sync (nothing granted yet)
            await rig.grant(aid, pr)                                     # grants changed: synced at once, not in 3600 s
            await until(lambda: any(aid in f for f in forced), timeout=5)
            await until(lambda: (rig.mirror_root / "artifacts" / aid / "head.json").exists())
            n = len(forced)
            rig.applier._mirror_force.add(aid)                           # what the applier leaves after a failed refresh
            await until(lambda: len(forced) > n and aid in forced[-1], timeout=5)
            assert not rig.applier._mirror_force
    loop_run(go())


def test_grants_are_not_published_until_the_first_retraction_load_succeeds(registry_pg, svc, tmp_path, monkeypatch,
                                                                         caplog):
    async def go():
        real = ArtifactStore.load_retractions
        state = {"fail": True, "calls": 0}

        async def flaky(self):
            state["calls"] += 1
            if state["fail"]:
                raise RuntimeError("retractions boom SECRET")
            return await real(self)
        monkeypatch.setattr(ArtifactStore, "load_retractions", flaky)
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs()) as rig:
            aid, _ = await rig.artifact()
            pr = rig.agent("agent-a")
            await rig.grant(aid, pr)
            rig.start()
            await until(lambda: state["calls"] >= 4)                   # retried every pass, not every 30 s
            assert rig.grants() is None                                # fail closed: nothing published
            assert any("RuntimeError" in r.getMessage() for r in caplog.records)
            assert not any("SECRET" in r.getMessage() for r in caplog.records)
            state["fail"] = False
            await until(lambda: rig.grants() and aid in rig.grants()["agents"].get(pr.id, {}))
    loop_run(go())


def test_a_failed_later_retraction_refresh_keeps_the_last_good_gate(registry_pg, svc, tmp_path, monkeypatch):
    async def go():
        real = ArtifactStore.load_retractions
        state = {"fail": False}

        async def flaky(self):
            if state["fail"]:
                raise RuntimeError("boom")
            return await real(self)
        monkeypatch.setattr(ArtifactStore, "load_retractions", flaky)
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs()) as rig:
            aid, _ = await rig.artifact()
            other, _ = await rig.artifact()
            pr = rig.agent("agent-a")
            await rig.grant(aid, pr)
            await rig.grant(other, pr)
            rig.start()
            await until(lambda: rig.grants() and aid in rig.grants()["agents"].get(pr.id, {}))
            async with rig.pool.acquire() as conn:
                await ArtifactStore(conn, rig.blobs).record_retraction(aid, None)
            await until(lambda: rig.gate.artifact_retracted(aid))
            state["fail"] = True
            before = rig.grants()["generated_at"]
            await asyncio.sleep(1.0)                                   # several failing refreshes (every 0.3 s)
            assert rig.gate.artifact_retracted(aid)                    # the last good gate is kept
            assert aid not in rig.grants()["agents"].get(pr.id, {})
            await until(lambda: rig.grants()["generated_at"] > before)  # and publishing continues
    loop_run(go())


def _inline_commit(rig, pr, aid, base, text):
    return rig.bridge.commit(pr, {"artifact_id": aid, "base_version_id": base, "message": "m",
                                  "changes": [{"name": "doc.md", "op": "put", "text": text}]})["ticket"]


def test_a_dead_scan_connection_is_replaced_and_a_fresh_intent_is_decided(registry_pg, svc, tmp_path):
    """F1/A1: the applier's dedicated scan connection is killed by the server (restart, idle kill); intake must
    recover, not stall forever, and the grants heartbeat must be unaffected."""
    async def go():
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs()) as rig:
            aid, v1 = await rig.artifact()
            pr = rig.agent("agent-a")
            await rig.grant(aid, pr)
            rig.start()
            await until(lambda: rig.grants() and pr.id in rig.grants()["agents"])
            await until(lambda: (rig.mirror_root / "artifacts" / aid / "head.json").exists())
            first = _inline_commit(rig, pr, aid, v1, "first\nline 2\n")
            await until(lambda: rig.result(first), timeout=10)
            for conn in rig.conns.by_task("collab-applier"):         # the server kills every applier-task connection
                if not conn.is_closed():
                    await rig.pool.execute("SELECT pg_terminate_backend($1)", conn.get_server_pid())
            await asyncio.sleep(0.3)
            head = rig.result(first)["version_id"]
            second = _inline_commit(rig, pr, aid, head, "second\nline 2\n")
            decided = await until(lambda: rig.result(second), timeout=15)
            assert decided["result"] in ("created", "merged")
            before = rig.grants()["generated_at"]
            await until(lambda: rig.grants()["generated_at"] > before + 0.3, timeout=3)
    loop_run(go())


def test_the_invite_scan_does_not_use_the_default_executor_so_the_heartbeat_survives_its_saturation(
        registry_pg, svc, tmp_path):
    """F1/A6: every default-executor thread is blocked; grants.json must still advance (heartbeat)."""
    import concurrent.futures
    import threading

    async def go():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        gate = threading.Event()
        async with LoopRig(svc, tmp_path, registry_pg["dsn"], FakeBlobs()) as rig:
            aid, _ = await rig.artifact()
            pr = rig.agent("agent-a")
            await rig.grant(aid, pr)
            rig.start()
            await until(lambda: rig.grants() and pr.id in rig.grants()["agents"])
            blockers = [loop.run_in_executor(None, gate.wait, 30) for _ in range(2)]     # saturate the default pool
            try:
                await asyncio.sleep(0.2)
                before = rig.grants()["generated_at"]
                await until(lambda: rig.grants()["generated_at"] > before + 0.3, timeout=5)
            finally:
                gate.set()
                await asyncio.gather(*blockers)
    loop_run(go())
