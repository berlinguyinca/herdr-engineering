"""Persistence and concurrency proof on an isolated, Unix-socket-only PostgreSQL."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_report_state import (
    ArtifactReportState,
    ReportStateError,
)

ROOT = Path(__file__).resolve().parents[2]


class NativeStore:
    def __init__(self, connection):
        self._conn = connection

    async def get_version(self, version_id, artifact_id):
        row = await self._conn.fetchrow(
            "SELECT * FROM artifact_versions WHERE version_id=$1 AND artifact_id=$2",
            version_id, artifact_id,
        )
        return dict(row) if row else None


@pytest.fixture(scope="module")
def report_postgres():
    if os.getuid() == 0:
        pytest.skip("isolated initdb cannot run as root")
    candidates = []
    config = shutil.which("pg_config")
    if config:
        bindir = Path(subprocess.check_output([config, "--bindir"], text=True).strip())
        candidates.append(bindir / "initdb")
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    initdb = next((candidate for candidate in candidates if candidate.is_file()), None)
    if initdb is None:
        pytest.skip("PostgreSQL server binaries unavailable")
    bindir = initdb.parent
    with tempfile.TemporaryDirectory(prefix="report-pg-") as folder:
        area = Path(folder)
        data = area / "data"
        subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True)

        def start():
            subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l", str(area / "server.log"),
                            "-o", f"-c listen_addresses='' -k {area}", "-w", "start"],
                           check=True, capture_output=True)

        def stop():
            subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                           check=True, capture_output=True)

        def restart():
            stop()
            start()

        start()
        try:
            dsn = f"postgresql:///postgres?host={area}"

            async def initialize():
                connection = await asyncpg.connect(dsn)
                try:
                    for filename in ("0001_control_plane.sql", "0003_artifact_registry.sql",
                                     "0005_artifact_binding_context.sql", "0006_artifact_acl.sql",
                                     "0021_artifact_reports.sql", "0031_artifact_registry_extensions.sql"):
                        await connection.execute((ROOT / "deploy/control-plane/migrations" / filename).read_text())
                finally:
                    await connection.close()

            asyncio.run(initialize())
            yield {"dsn": dsn, "restart": restart}
        finally:
            stop()


async def seed(connection, *, artifact_id=None, number=1, finding="finding-a"):
    artifact_id = artifact_id or "artifact_" + uuid.uuid4().hex
    version_id = "version_" + uuid.uuid4().hex
    descriptor = {"schema_version": 1, "questions": [], "assets": [],
                  "evidence": [{"id": "observation-a"}],
                  "findings": [{"id": finding}], "figures": [{"id": "figure-a"}]}
    await connection.execute(
        "INSERT INTO artifacts (artifact_id,created_by) VALUES ($1,'test-owner') ON CONFLICT DO NOTHING",
        artifact_id,
    )
    await connection.execute(
        "INSERT INTO artifact_versions (version_id,artifact_id,version_number,state,report_descriptor) "
        "VALUES ($1,$2,$3,'ready',$4::jsonb)", version_id, artifact_id, number, json.dumps(descriptor),
    )
    return artifact_id, version_id


async def attest(state, artifact_id, version_id, stage, status="passed", **kwargs):
    return await state.record_check(
        artifact_id, version_id, stage=stage, status=status,
        notes=kwargs.get("notes", "Inspected the identified source records."),
        evidence_refs=[] if stage == "rendered" else ["evidence:observation-a"],
        actor=kwargs.get("actor", "reviewer"),
    )


def test_checks_and_anchored_feedback_survive_database_restart(report_postgres):
    async def write():
        connection = await asyncpg.connect(report_postgres["dsn"])
        try:
            artifact, version = await seed(connection)
            state = ArtifactReportState(NativeStore(connection))
            for stage in ("rendered", "evidence_checked", "reviewed"):
                await attest(state, artifact, version, stage)
            comment = await state.add_feedback(
                artifact, version, target_kind="finding", target_id="finding-a",
                body="Please distinguish the measured denominator from the display subset.", actor="reader",
            )
            return artifact, version, comment
        finally:
            await connection.close()

    artifact, version, comment = asyncio.run(write())
    report_postgres["restart"]()

    async def read():
        connection = await asyncpg.connect(report_postgres["dsn"])
        try:
            state = ArtifactReportState(NativeStore(connection))
            result = await state.inspect(artifact, version)
            assert result["state"] == "reviewed"
            assert await state.list_feedback(artifact, version) == [comment]
            assert await state.feedback_count(artifact, version) == 1
            later_artifact, later_version = await seed(connection, artifact_id=artifact, number=2, finding="later-finding")
            assert (await state.inspect(later_artifact, later_version))["state"] == "not_checked"
            assert await state.list_feedback(later_artifact, later_version) == []
            with pytest.raises(ReportStateError, match="unknown descriptor reference"):
                await state.add_feedback(artifact, later_version, target_kind="finding", target_id="finding-a",
                                         body="This target existed only in the earlier version.", actor="reader")
        finally:
            await connection.close()

    asyncio.run(read())


def test_database_enforces_exact_version_membership_and_append_only_events(report_postgres):
    async def exercise():
        connection = await asyncpg.connect(report_postgres["dsn"])
        try:
            artifact, version = await seed(connection)
            other_artifact, other_version = await seed(connection)
            state = ArtifactReportState(NativeStore(connection))
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute(
                    "INSERT INTO artifact_report_feedback "
                    "(feedback_id,artifact_id,version_id,target_kind,body,actor) "
                    "VALUES ('bad-pair',$1,$2,'report','Wrong artifact','reader')", artifact, other_version,
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute(
                    "INSERT INTO artifact_report_checks "
                    "(check_id,artifact_id,version_id,stage,status,notes,actor) "
                    "VALUES ('bad-check-pair',$1,$2,'rendered','passed','Wrong pair','reader')",
                    other_artifact, version,
                )
            event = await attest(state, artifact, version, "rendered")
            feedback = await state.add_feedback(artifact, version, target_kind="figure", target_id="figure-a",
                                                body="Explain the axis units.", actor="reader")
            for query, identifier in (
                ("UPDATE artifact_report_checks SET status='failed' WHERE check_id=$1", event["check_id"]),
                ("DELETE FROM artifact_report_checks WHERE check_id=$1", event["check_id"]),
                ("UPDATE artifact_report_feedback SET body='rewritten' WHERE feedback_id=$1", feedback["feedback_id"]),
                ("DELETE FROM artifact_report_feedback WHERE feedback_id=$1", feedback["feedback_id"]),
            ):
                with pytest.raises(asyncpg.PostgresError, match="append-only"):
                    await connection.execute(query, identifier)
            assert (await state.inspect(artifact, version))["checks"]["rendered"]["status"] == "passed"
            assert (await state.list_feedback(artifact, version))[0]["body"] == "Explain the axis units."
        finally:
            await connection.close()

    asyncio.run(exercise())


def test_failed_database_insert_leaves_no_claim_and_render_validation_is_separate(report_postgres):
    async def exercise():
        connection = await asyncpg.connect(report_postgres["dsn"])
        try:
            artifact, version = await seed(connection)
            await connection.execute("""
                CREATE FUNCTION reject_report_test_actor() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NEW.actor = 'failure-test-actor' THEN RAISE EXCEPTION 'test database failure'; END IF;
                  RETURN NEW;
                END; $$;
                CREATE TRIGGER report_test_failure BEFORE INSERT ON artifact_report_checks
                FOR EACH ROW EXECUTE FUNCTION reject_report_test_actor();
            """)
            state = ArtifactReportState(NativeStore(connection))
            with pytest.raises(asyncpg.PostgresError, match="test database failure"):
                await attest(state, artifact, version, "rendered", actor="failure-test-actor")
            assert (await state.inspect(artifact, version))["state"] == "not_checked"
            assert await connection.fetchval(
                "SELECT count(*) FROM artifact_report_checks WHERE artifact_id=$1", artifact) == 0
            await connection.execute(
                "UPDATE artifact_versions SET validation=$1::jsonb,job_id='job-rendered' WHERE version_id=$2",
                json.dumps({"status": "passed", "checks": ["descriptor-schema", "html-envelope"]}), version,
            )
            inspected = await state.inspect(artifact, version)
            assert inspected["state"] == "rendered"
            assert inspected["checks"]["evidence_checked"]["status"] == "not_checked"
            assert inspected["checks"]["reviewed"]["status"] == "not_checked"
        finally:
            await connection.close()

    asyncio.run(exercise())


def test_concurrent_upstream_failure_prevents_stale_review_approval(report_postgres):
    async def exercise():
        first = await asyncpg.connect(report_postgres["dsn"])
        second = await asyncpg.connect(report_postgres["dsn"])
        try:
            artifact, version = await seed(first)
            upstream = ArtifactReportState(NativeStore(first))
            reviewer = ArtifactReportState(NativeStore(second))
            await attest(upstream, artifact, version, "rendered")
            await attest(upstream, artifact, version, "evidence_checked")
            async with first.transaction():
                await first.fetchrow("SELECT version_id FROM artifact_versions WHERE version_id=$1 FOR UPDATE", version)
                pending_review = asyncio.create_task(attest(reviewer, artifact, version, "reviewed"))
                await attest(upstream, artifact, version, "rendered", "failed", notes="A rendered output is broken.")
            with pytest.raises(ReportStateError, match="must pass"):
                await pending_review
            inspected = await reviewer.inspect(artifact, version)
            assert inspected["state"] == "needs_attention"
            assert inspected["checks"]["evidence_checked"]["status"] == "stale"
            assert inspected["checks"]["reviewed"]["status"] == "not_checked"
        finally:
            await first.close()
            await second.close()

    asyncio.run(exercise())


def test_database_feedback_pagination_preserves_all_version_comments(report_postgres):
    async def exercise():
        connection = await asyncpg.connect(report_postgres["dsn"])
        try:
            artifact, version = await seed(connection)
            state = ArtifactReportState(NativeStore(connection))
            for index in range(5):
                await state.add_feedback(artifact, version, target_kind="report", target_id=None,
                                         body=f"Comment {index}", actor="reader")
            first = await state.list_feedback(artifact, version, limit=2)
            second = await state.list_feedback(artifact, version, limit=2, offset=2)
            final = await state.list_feedback(artifact, version, limit=2, offset=4)
            assert [row["body"] for row in first + second + final] == [f"Comment {index}" for index in range(5)]
            assert await state.feedback_count(artifact, version) == 5
        finally:
            await connection.close()

    asyncio.run(exercise())
