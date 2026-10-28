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
