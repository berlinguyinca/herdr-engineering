"""Durable, version-pinned report attestations and reader feedback.

Authorization belongs to the calling API/store boundary. This layer validates
the exact version and descriptor anchors and acknowledges writes only after the
database transaction commits. It keeps no mutable mirror of persisted records.
"""
from __future__ import annotations

import json
import secrets
from datetime import date, datetime
from typing import Any

STAGES = ("rendered", "evidence_checked", "reviewed")
STATUSES = frozenset({"passed", "failed"})
MAX_TEXT = 10_000
MAX_REFERENCES = 1_000


class ReportStateError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_report_state"):
        self.code = code
        super().__init__(message)


def _text(value: Any, name: str, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ReportStateError(f"{name} must be nonempty text of at most {maximum} characters")
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _row(row: Any) -> dict:
    result = _jsonable(dict(row))
    if isinstance(result.get("evidence_refs"), str):
        result["evidence_refs"] = json.loads(result["evidence_refs"])
    return result


def _object(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


class ArtifactReportState:
    def __init__(self, store):
        self.store = store
        self.conn = store._conn

    async def _version(self, artifact_id: str, version_id: str) -> dict:
        _text(artifact_id, "artifact_id", 256)
        _text(version_id, "version_id", 256)
        version = await self.store.get_version(version_id, artifact_id)
        if version is None:
            raise ReportStateError("report version not found", code="not_found")
        # Keep the caller's pair authoritative even if a store adapter is faulty.
        if version.get("artifact_id") != artifact_id or version.get("version_id") != version_id:
            raise ReportStateError("report version not found", code="not_found")
        descriptor = _object(version.get("report_descriptor"))
        if (not descriptor or type(descriptor.get("schema_version")) is not int
                or descriptor["schema_version"] != 1):
            raise ReportStateError("version has no supported report descriptor")
        return {**version, "report_descriptor": descriptor}

    @staticmethod
    def _validate_references(descriptor: dict, references: Any) -> list[str]:
        if not isinstance(references, list) or len(references) > MAX_REFERENCES:
            raise ReportStateError("evidence_refs must be a bounded list of descriptor references")
        allowed = {f"{kind}:{entry['id']}"
                   for kind, collection in (("evidence", "evidence"), ("finding", "findings"),
                                            ("figure", "figures"))
                   for entry in descriptor.get(collection, [])
                   if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
        checked = []
        for reference in references:
            if not isinstance(reference, str) or reference not in allowed:
                raise ReportStateError("evidence_refs contains an unknown descriptor reference")
            if reference not in checked:
                checked.append(reference)
        return checked

    @staticmethod
    def _render_validation(version: dict) -> dict | None:
        validation = _object(version.get("validation"))
        provenance = _object(version.get("provenance"))
        toolchain = _object(provenance.get("toolchain"))
        checks = validation.get("checks")
        if (validation.get("status") not in STATUSES or not isinstance(checks, list)
                or not checks or not (version.get("job_id") or toolchain.get("renderer"))):
            return None
        return {"stage": "rendered", "status": validation["status"],
                "source": "renderer_validation", "actor": toolchain.get("renderer") or "worker",
                "notes": "Persisted worker rendering validation.", "evidence_refs": [],
                "created_at": _jsonable(version.get("committed_at") or version.get("created_at")),
                "event_sequence": 0, "validation": validation}

    async def _inspect_version(self, version: dict) -> dict:
        records = await self.conn.fetch(
            "SELECT DISTINCT ON (stage) * FROM artifact_report_checks "
            "WHERE artifact_id=$1 AND version_id=$2 ORDER BY stage, event_sequence DESC",
            version["artifact_id"], version["version_id"],
        )
        latest = {record["stage"]: _row(record) for record in records}
        rendered = self._render_validation(version)
        if "rendered" not in latest and rendered is not None:
            latest["rendered"] = rendered
        projected = {}
        for index, stage in enumerate(STAGES):
            value = dict(latest.get(stage) or {"stage": stage, "status": "not_checked",
                                              "actor": None, "created_at": None})
            if value["status"] == "passed" and index:
                upstream = [projected[name] for name in STAGES[:index]]
                newer = any(item.get("event_sequence", -1) > value.get("event_sequence", -1)
                            for item in upstream)
                failed = any(item["status"] in {"failed", "stale"} for item in upstream)
                missing_review_prerequisite = stage == "reviewed" and any(
                    item["status"] != "passed" for item in upstream)
                if newer or failed or missing_review_prerequisite:
                    value.update(status="stale", recorded_status="passed",
                                 reason="An upstream check changed or no longer passes; repeat this attestation.")
            projected[stage] = value
        statuses = [projected[stage]["status"] for stage in STAGES]
        if "failed" in statuses:
            state = "needs_attention"
        elif statuses == ["passed"] * 3:
            state = "reviewed"
        elif statuses[:2] == ["passed", "passed"]:
            state = "evidence_checked"
        elif statuses[0] == "passed":
            state = "rendered"
        elif "passed" in statuses:
            state = "partially_checked"
        else:
            state = "not_checked"
        return {"artifact_id": version["artifact_id"], "version_id": version["version_id"],
                "checks": projected, "state": state}

    async def inspect(self, artifact_id: str, version_id: str) -> dict:
        return await self._inspect_version(await self._version(artifact_id, version_id))

    async def record_check(self, artifact_id: str, version_id: str, *, stage: str,
                           status: str, notes: str, evidence_refs: list[str], actor: str) -> dict:
        if not isinstance(stage, str) or not isinstance(status, str) or stage not in STAGES or status not in STATUSES:
            raise ReportStateError("stage and status must name a supported report check")
        _text(artifact_id, "artifact_id", 256)
        _text(version_id, "version_id", 256)
        _text(notes, "notes")
        _text(actor, "actor", 1_000)
        # Locking the immutable version serializes attestations, so a concurrent
        # upstream failure cannot race a review completion based on stale reads.
        async with self.conn.transaction():
            locked = await self.conn.fetchrow(
                "SELECT version_id FROM artifact_versions WHERE version_id=$1 "
                "AND artifact_id=$2 FOR UPDATE", version_id, artifact_id,
            )
            if locked is None:
                raise ReportStateError("report version not found", code="not_found")
            version = await self._version(artifact_id, version_id)
            references = self._validate_references(version["report_descriptor"], evidence_refs)
            if status == "passed" and stage != "rendered" and not references:
                raise ReportStateError("passed evidence/review checks require explicit evidence_refs")
            if stage == "reviewed" and status == "passed":
                current = await self._inspect_version(version)
                if any(current["checks"][name]["status"] != "passed"
                       for name in ("rendered", "evidence_checked")):
                    raise ReportStateError("rendered and evidence_checked must pass before reviewed can pass")
            record = await self.conn.fetchrow(
                "INSERT INTO artifact_report_checks "
                "(check_id,artifact_id,version_id,stage,status,notes,evidence_refs,actor) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8) RETURNING *",
                "check_" + secrets.token_hex(12), artifact_id, version_id, stage, status,
                notes, json.dumps(references), actor,
            )
        return _row(record)

    async def add_feedback(self, artifact_id: str, version_id: str, *, target_kind: str,
                           target_id: str | None, body: str, actor: str) -> dict:
        _text(body, "body")
        _text(actor, "actor", 1_000)
        version = await self._version(artifact_id, version_id)
        if target_kind == "report":
            if target_id is not None:
                raise ReportStateError("report feedback must have a null target_id")
        elif isinstance(target_kind, str) and target_kind in {"finding", "figure"}:
            _text(target_id, "target_id", 256)
            self._validate_references(version["report_descriptor"], [f"{target_kind}:{target_id}"])
        else:
            raise ReportStateError("feedback target_kind must be report, finding or figure")
        async with self.conn.transaction():
            record = await self.conn.fetchrow(
                "INSERT INTO artifact_report_feedback "
                "(feedback_id,artifact_id,version_id,target_kind,target_id,body,actor) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *",
                "feedback_" + secrets.token_hex(12), artifact_id, version_id, target_kind,
                target_id, body, actor,
            )
        return _row(record)

    async def list_feedback(self, artifact_id: str, version_id: str, *,
                            limit: int = 100, offset: int = 0) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ReportStateError("feedback limit must be an integer from 1 to 100")
        if type(offset) is not int or not 0 <= offset <= 2_147_483_647:
            raise ReportStateError("feedback offset must be a nonnegative integer")
        await self._version(artifact_id, version_id)
        records = await self.conn.fetch(
            "SELECT * FROM artifact_report_feedback WHERE artifact_id=$1 AND version_id=$2 "
            "ORDER BY feedback_sequence LIMIT $3 OFFSET $4", artifact_id, version_id, limit, offset,
        )
        return [_row(row) for row in records]

    async def feedback_count(self, artifact_id: str, version_id: str) -> int:
        await self._version(artifact_id, version_id)
        record = await self.conn.fetchrow(
            "SELECT count(*) AS total FROM artifact_report_feedback WHERE artifact_id=$1 AND version_id=$2",
            artifact_id, version_id,
        )
        return int(record["total"])
