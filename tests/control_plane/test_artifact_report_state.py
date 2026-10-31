"""Report checks are independent attestations pinned to immutable versions."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.artifact_report_state import (
    ArtifactReportState,
    ReportStateError,
)


def descriptor():
    return {"schema_version": 1, "questions": [], "assets": [],
            "evidence": [{"id": "observation-a"}],
            "findings": [{"id": "finding-a"}], "figures": [{"id": "figure-a"}]}


class MemoryConnection:
    def __init__(self):
        self.checks = []
        self.feedback = []
        self.fail_insert = False
        self.locked = False

    @asynccontextmanager
    async def transaction(self):
        before = copy.deepcopy((self.checks, self.feedback))
        try:
            yield
        except Exception:
            self.checks, self.feedback = before
            raise
        finally:
            self.locked = False

    async def fetchrow(self, sql, *args):
        if "SELECT count(*) AS total" in sql:
            return {"total": sum((r["artifact_id"], r["version_id"]) == args for r in self.feedback)}
        if "FOR UPDATE" in sql:
            self.locked = True
            return {"version_id": args[0]}
        if self.fail_insert:
            raise RuntimeError("database write failed")
        if "INSERT INTO artifact_report_checks" in sql:
            assert self.locked
            event, artifact, version, stage, status, notes, refs, actor = args
            row = {"check_id": event, "artifact_id": artifact, "version_id": version,
                   "stage": stage, "status": status, "notes": notes,
                   "evidence_refs": json.loads(refs), "actor": actor,
                   "event_sequence": len(self.checks) + 1,
                   "created_at": datetime.now(UTC)}
            self.checks.append(row)
            return copy.deepcopy(row)
        if "INSERT INTO artifact_report_feedback" in sql:
            event, artifact, version, kind, target, body, actor = args
            row = {"feedback_id": event, "artifact_id": artifact, "version_id": version,
                   "target_kind": kind, "target_id": target, "body": body, "actor": actor,
                   "feedback_sequence": len(self.feedback) + 1,
                   "created_at": datetime.now(UTC)}
            self.feedback.append(row)
            return copy.deepcopy(row)
        raise AssertionError(sql)

    async def fetch(self, sql, artifact, version, *paging):
        if "artifact_report_checks" in sql:
            rows = [r for r in self.checks if (r["artifact_id"], r["version_id"]) == (artifact, version)]
            latest = {row["stage"]: row for row in rows}
            return copy.deepcopy(list(latest.values()))
        limit, offset = paging
        return copy.deepcopy([r for r in self.feedback
                              if (r["artifact_id"], r["version_id"]) == (artifact, version)][offset:offset + limit])


class MemoryStore:
    def __init__(self):
        self._conn = MemoryConnection()
        self.versions = {"v1": {"artifact_id": "a1", "version_id": "v1", "state": "ready",
                                "report_descriptor": descriptor(), "validation": {}}}

    async def get_version(self, version_id, artifact_id):
        row = self.versions.get(version_id)
        return copy.deepcopy(row) if row and row["artifact_id"] == artifact_id else None


def run(coro):
    return asyncio.run(coro)


def check(state, selected_stage, status="passed", **changes):
    kwargs = {"stage": selected_stage, "status": status, "notes": "Checked the recorded evidence.",
              "evidence_refs": ["evidence:observation-a"] if selected_stage != "rendered" else [],
              "actor": "reviewer-one"}
    kwargs.update(changes)
    return run(state.record_check("a1", "v1", **kwargs))


def test_ready_and_unattributed_validation_do_not_claim_checks():
    store = MemoryStore()
    state = ArtifactReportState(store)
    store.versions["v1"]["validation"] = {"status": "passed", "checks": ["html-envelope"]}
    result = run(state.inspect("a1", "v1"))
    assert result["state"] == "not_checked"
    assert {row["status"] for row in result["checks"].values()} == {"not_checked"}


def test_worker_validation_seeds_only_rendered_stage():
    store = MemoryStore()
    store.versions["v1"].update(validation={"status": "passed", "checks": ["html-envelope"]},
                                job_id="job-render")
    result = run(ArtifactReportState(store).inspect("a1", "v1"))
    assert result["state"] == "rendered"
    assert result["checks"]["rendered"]["source"] == "renderer_validation"
    assert result["checks"]["evidence_checked"]["status"] == "not_checked"
    assert result["checks"]["reviewed"]["status"] == "not_checked"


def test_review_needs_separate_render_and_evidence_attestations():
    state = ArtifactReportState(MemoryStore())
    with pytest.raises(ReportStateError, match="rendered.*evidence_checked"):
        check(state, "reviewed")
    check(state, "rendered")
    assert run(state.inspect("a1", "v1"))["state"] == "rendered"
    check(state, "evidence_checked")
    assert run(state.inspect("a1", "v1"))["state"] == "evidence_checked"
    check(state, "reviewed")
    assert run(state.inspect("a1", "v1"))["state"] == "reviewed"


def test_upstream_failure_and_new_checks_stale_existing_signoff():
    state = ArtifactReportState(MemoryStore())
    for stage in ("rendered", "evidence_checked", "reviewed"):
        check(state, stage)
    check(state, "rendered", "failed", notes="Broken image reference.")
    result = run(state.inspect("a1", "v1"))
    assert result["state"] == "needs_attention"
    assert result["checks"]["evidence_checked"]["status"] == "stale"
    assert result["checks"]["reviewed"]["status"] == "stale"
    check(state, "rendered")
    with pytest.raises(ReportStateError):
        check(state, "reviewed")
    check(state, "evidence_checked")
    check(state, "reviewed")
    assert run(state.inspect("a1", "v1"))["state"] == "reviewed"


def test_checks_need_notes_real_descriptor_refs_and_valid_stage_status_actor():
    state = ArtifactReportState(MemoryStore())
    for kwargs in ({"notes": " "}, {"evidence_refs": []},
                   {"evidence_refs": ["evidence:missing"]},
                   {"evidence_refs": ["asset:observation-a"]}, {"actor": ""},
                   {"status": "approved"}, {"stage": "scientifically_proven"}):
        with pytest.raises(ReportStateError):
            check(state, "evidence_checked", **kwargs)


def test_failed_database_write_does_not_create_a_successful_check():
    store = MemoryStore()
    state = ArtifactReportState(store)
    store._conn.fail_insert = True
    with pytest.raises(RuntimeError, match="database write failed"):
        check(state, "rendered")
    assert run(state.inspect("a1", "v1"))["state"] == "not_checked"
    assert store._conn.checks == []


def test_feedback_preserves_exact_version_target_actor_and_literal_body():
    store = MemoryStore()
    state = ArtifactReportState(store)
    body = '<script>not executable here</script> Explain the denominator.'
    written = run(state.add_feedback("a1", "v1", target_kind="finding", target_id="finding-a",
                                     body=body, actor="reader-two"))
    listed = run(ArtifactReportState(store).list_feedback("a1", "v1"))
    assert listed == [written]
    assert written["body"] == body
    assert written["actor"] == "reader-two"
    assert written["version_id"] == "v1"
    assert written["target_id"] == "finding-a"
    json.dumps(written)


def test_feedback_rejects_unknown_anchor_and_cross_artifact_version():
    state = ArtifactReportState(MemoryStore())
    for kind, target in (("finding", "missing"), ("figure", "finding-a"),
                         ("report", "finding-a"), ("figure", None), ("evidence", "observation-a")):
        with pytest.raises(ReportStateError):
            run(state.add_feedback("a1", "v1", target_kind=kind, target_id=target,
                                   body="Clarify", actor="reader"))
    with pytest.raises(ReportStateError, match="not found"):
        run(state.add_feedback("a2", "v1", target_kind="report", target_id=None,
                               body="Wrong owner", actor="reader"))


def test_new_versions_do_not_inherit_checks_or_feedback():
    store = MemoryStore()
    state = ArtifactReportState(store)
    check(state, "rendered")
    run(state.add_feedback("a1", "v1", target_kind="report", target_id=None,
                           body="First version note", actor="reader"))
    store.versions["v2"] = {**store.versions["v1"], "version_id": "v2"}
    assert run(state.inspect("a1", "v2"))["state"] == "not_checked"
    assert run(state.list_feedback("a1", "v2")) == []
    assert len(run(state.list_feedback("a1", "v1"))) == 1


def test_nonreport_version_and_oversized_feedback_are_rejected():
    store = MemoryStore()
    state = ArtifactReportState(store)
    with pytest.raises(ReportStateError):
        run(state.add_feedback("a1", "v1", target_kind="report", target_id=None,
                               body="x" * 10001, actor="reader"))
    store.versions["v1"]["report_descriptor"] = None
    with pytest.raises(ReportStateError, match="report descriptor"):
        run(state.inspect("a1", "v1"))


def test_feedback_pages_have_explicit_bounds_and_total():
    state = ArtifactReportState(MemoryStore())
    for text in ("first", "second", "third"):
        run(state.add_feedback("a1", "v1", target_kind="report", target_id=None,
                               body=text, actor="reader"))
    assert [row["body"] for row in run(state.list_feedback("a1", "v1", limit=1, offset=1))] == ["second"]
    assert run(state.feedback_count("a1", "v1")) == 3
    for limit in (0, 101, True):
        with pytest.raises(ReportStateError):
            run(state.list_feedback("a1", "v1", limit=limit))


def test_malformed_check_values_raise_validation_errors_before_querying():
    state = ArtifactReportState(MemoryStore())
    with pytest.raises(ReportStateError):
        check(state, "rendered", status=[])
    with pytest.raises(ReportStateError):
        run(state.add_feedback("a1", "v1", target_kind=[], target_id=None,
                               body="Invalid target type", actor="reader"))
