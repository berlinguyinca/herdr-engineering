"""Artifact build claims and renderer context against isolated PostgreSQL."""
import asyncio
import json
import uuid

import asyncpg

from herdr_engineering.control_plane.worker import _run_artifact_job_pass
from tests.control_plane.test_artifact_report_postgres import report_postgres as _report_postgres

report_postgres = _report_postgres


def test_claim_returns_the_updated_row_to_the_renderer():
    class Connection:
        def __init__(self):
            self.job = {"job_id": "job-context", "status": "queued",
                        "assigned_host_id": None, "started_at": None}

        async def fetchrow(self, sql, *args):
            if self.job["status"] != "queued":
                return None
            if sql.lstrip().startswith("UPDATE"):
                self.job.update(status="running", assigned_host_id=args[0], started_at="now")
            return dict(self.job)

        async def execute(self, sql, *args):
            if "status='running'" in sql:
                self.job.update(status="running", assigned_host_id=args[0], started_at="now")
            elif "status='completed'" in sql:
                self.job.update(status="completed", result_version_id=args[0])

    async def exercise():
        connection = Connection()
        received = []

        async def render(job):
            received.append(job)
            return {"version_id": "version-context"}

        result = await _run_artifact_job_pass(connection, worker_id="host-worker", runner=render)
        assert result["artifact_jobs_completed"] == 1
        assert received[0]["assigned_host_id"] == "host-worker"
        assert received[0]["status"] == "running"
        assert received[0]["started_at"] is not None

    asyncio.run(exercise())


async def _seed_job(connection, workers=1):
    token = uuid.uuid4().hex
    worker_ids = [f"host-report-worker-{token}-{index}" for index in range(workers)]
    for worker_id in worker_ids:
        await connection.execute("INSERT INTO hosts (host_id) VALUES ($1)", worker_id)
    job_id = f"job-report-{token}"
    artifact_id, version_id = f"artifact-report-{token}", f"version-report-{token}"
    await connection.execute("INSERT INTO artifacts (artifact_id) VALUES ($1)", artifact_id)
    # The worker consumes the renderer's version ID. Register a valid renderer
    # result so the test exercises job claiming rather than violating its FK.
    await connection.execute(
        "INSERT INTO artifact_versions (version_id,artifact_id,version_number,state) "
        "VALUES ($1,$2,1,'ready')", version_id, artifact_id,
    )
    await connection.execute(
        "INSERT INTO artifact_build_jobs (job_id,artifact_id,profile,status,request) "
        "VALUES ($1,$2,'report','queued','{}'::jsonb)", job_id, artifact_id,
    )
    return job_id, worker_ids, version_id


def test_renderer_receives_committed_worker_identity_without_an_open_claim_transaction(report_postgres):
    async def exercise():
        connection = await asyncpg.connect(report_postgres["dsn"])
        observer = await asyncpg.connect(report_postgres["dsn"])
        try:
            job_id, worker_ids, version_id = await _seed_job(connection)
            observed = []

            async def render(job):
                observed.append(job)
                assert job["assigned_host_id"] == worker_ids[0]
                assert job["status"] == "running"
                assert job["started_at"] is not None
                assert not connection.is_in_transaction()
                # Another connection must see and be able to lock the claimed row
                # while the renderer executes: no claim transaction spans rendering.
                row = await observer.fetchrow(
                    "SELECT * FROM artifact_build_jobs WHERE job_id=$1 FOR UPDATE NOWAIT", job_id,
                )
                assert row["status"] == "running"
                assert row["assigned_host_id"] == worker_ids[0]
                return {"version_id": version_id}

            result = await _run_artifact_job_pass(
                connection, worker_id=worker_ids[0], runner=render,
            )
            assert result == {"artifact_jobs_claimed": 1, "artifact_jobs_completed": 1,
                              "artifact_jobs_failed": 0}
            assert len(observed) == 1
            saved = await observer.fetchrow("SELECT * FROM artifact_build_jobs WHERE job_id=$1", job_id)
            assert saved["status"] == "completed"
            assert saved["result_version_id"] == version_id
        finally:
            await observer.close()
            await connection.close()

    asyncio.run(exercise())


def test_concurrent_workers_render_a_queued_job_exactly_once(report_postgres):
    async def exercise():
        connections = await asyncio.gather(*[
            asyncpg.connect(report_postgres["dsn"]) for _ in range(8)
        ])
        try:
            job_id, worker_ids, version_id = await _seed_job(connections[0], workers=len(connections))
            rendered = []

            async def render(job):
                rendered.append(job["job_id"])
                await asyncio.sleep(0.03)
                return {"version_id": version_id}

            results = await asyncio.gather(*[
                _run_artifact_job_pass(connection, worker_id=worker_id, runner=render)
                for connection, worker_id in zip(connections, worker_ids, strict=True)
            ])
            assert rendered == [job_id]
            assert sum(result["artifact_jobs_claimed"] for result in results) == 1
            assert sum(result["artifact_jobs_completed"] for result in results) == 1
            assert sum(result["artifact_jobs_failed"] for result in results) == 0
            saved = await connections[0].fetchrow(
                "SELECT status,assigned_host_id FROM artifact_build_jobs WHERE job_id=$1", job_id,
            )
            assert saved["status"] == "completed"
            assert saved["assigned_host_id"] in worker_ids
        finally:
            await asyncio.gather(*(connection.close() for connection in connections))

    asyncio.run(exercise())


def test_render_failure_is_persisted_and_not_reported_as_completed(report_postgres):
    async def exercise():
        connection = await asyncpg.connect(report_postgres["dsn"])
        try:
            job_id, worker_ids, _version_id = await _seed_job(connection)

            async def render(_job):
                raise RuntimeError("report renderer rejected an invalid descriptor")

            result = await _run_artifact_job_pass(
                connection, worker_id=worker_ids[0], runner=render,
            )
            assert result == {"artifact_jobs_claimed": 1, "artifact_jobs_completed": 0,
                              "artifact_jobs_failed": 1}
            saved = await connection.fetchrow("SELECT * FROM artifact_build_jobs WHERE job_id=$1", job_id)
            assert saved["status"] == "failed"
            assert saved["result_version_id"] is None
            assert saved["completed_at"] is not None
            assert json.loads(saved["error"])["message"] == "report renderer rejected an invalid descriptor"

            async def must_not_run(_job):
                raise AssertionError("a failed job must not be silently claimed again")

            next_pass = await _run_artifact_job_pass(
                connection, worker_id=worker_ids[0], runner=must_not_run,
            )
            assert next_pass["artifact_jobs_claimed"] == 0
        finally:
            await connection.close()

    asyncio.run(exercise())
