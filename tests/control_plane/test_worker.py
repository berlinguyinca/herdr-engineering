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

    async def runner(job):
        return {"version_id": "version_2", "outputs": [{"blob_sha256": "sha"}],
                "validation": {"status": "passed"}}

    conn = _Conn()
    summary = _run(worker.run_one_pass(conn, worker_id="host-a",
                                       artifact_job_runner=runner))
    assert summary["artifact_jobs_completed"] == 1
    assert any("status='completed'" in sql for sql, _ in conn.executed)
