"""Validation and privacy redaction for per-version artifact provenance (P03).

Everything here is self-reported by the harness that produced the artifact. It is recorded as evidence, not attested.
Inputs are never stored verbatim: a value is replaced by its digest, secret-looking keys and token shapes are masked,
and the whole list is size-bounded. The result is safe to show to anyone who may read the version.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REDACTED = "[redacted]"
MAX_INPUTS = 100
MAX_INPUTS_BYTES = 16 * 1024
_MAX_DEPTH = 6

# origin field -> (column, max length)
_TEXT_FIELDS = {
    "room_id": ("origin_room_id", 256),
    "task_id": ("origin_task_id", 256),
    "issue_ref": ("origin_issue_ref", 512),
    "project_id": ("origin_project_id", 256),
    "session_ref": ("origin_session_ref", 256),
    "agent_ref": ("origin_agent_ref", 256),
    "model_id": ("model_id", 256),
    "prompt_revision": ("prompt_revision", 256),
    "skill_revision": ("skill_revision", 256),
}
ORIGIN_KEYS = frozenset(_TEXT_FIELDS) | {"commit_sha", "inputs"}
CONTEXT_KINDS = {"project": "project_id", "task": "task_id", "issue": "issue_ref", "room": "room_id"}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY = re.compile(r"(token|secret|passw|authoriz|cookie|api[-_]?key|credential|private[-_]?key|session[-_]?key)",
                         re.IGNORECASE)
_SECRET_VALUE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.)")


class ProvenanceError(ValueError):
    pass


def _text(name: str, value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceError(f"origin.{name} must be a nonempty string")
    if len(value) > limit or _CONTROL.search(value):
        raise ProvenanceError(f"origin.{name} is too long or contains control characters")
    if _SECRET_VALUE.search(value):
        raise ProvenanceError(f"origin.{name} looks like a credential")
    return value


def validate_context(kind: str, ref: Any) -> tuple[str, str]:
    """Return (binding column, ref) for a lookup/link kind, or raise."""
    column = CONTEXT_KINDS.get(kind)
    if column is None:
        raise ProvenanceError("unknown context kind")
    limit = 512 if kind == "issue" else 256
    return column, _text(kind, ref, limit)


def redact_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {str(key)[:128]: (REDACTED if _SECRET_KEY.search(str(key)) else redact_value(item, depth + 1))
                for key, item in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [redact_value(item, depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return REDACTED if _SECRET_VALUE.search(value) else value[:1024]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return REDACTED


def redact_inputs(inputs: Any) -> list[dict]:
    """Normalize ``origin.inputs`` into digest-only records. Raw ``value``/``content`` never survive."""
    if inputs is None:
        return []
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
        raise ProvenanceError(f"origin.inputs must be a list of at most {MAX_INPUTS} entries")
    out: list[dict] = []
    for item in inputs:
        if not isinstance(item, dict):
            raise ProvenanceError("each origin input must be an object")
        record: dict[str, Any] = {}
        name = item.get("name")
        record["name"] = _text("inputs.name", name, 512) if name is not None else ""
        kind = item.get("kind")
        record["kind"] = _text("inputs.kind", kind, 64) if kind is not None else "other"
        redacted = bool(item.get("redacted", False))
        digest = item.get("sha256")
        for raw_key in ("value", "content", "text", "body", "prompt"):
            if raw_key in item:
                blob = json.dumps(item[raw_key], sort_keys=True, default=str).encode()
                digest = digest or hashlib.sha256(blob).hexdigest()
                record.setdefault("size_bytes", len(blob))
                redacted = True
        if digest is not None:
            if not isinstance(digest, str) or not _SHA256.match(digest):
                raise ProvenanceError("origin input sha256 must be 64 lowercase hex characters")
            record["sha256"] = digest
        size = item.get("size_bytes")
        if size is not None:
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ProvenanceError("origin input size_bytes must be a non-negative integer")
            record["size_bytes"] = size
        extra = {key: value for key, value in item.items()
                 if key not in {"name", "kind", "sha256", "size_bytes", "redacted", "value", "content", "text",
                                "body", "prompt"}}
        if extra:
            masked = redact_value(extra)
            record["meta"] = masked
            redacted = redacted or masked != extra
        record["redacted"] = redacted
        out.append(record)
    if len(json.dumps(out, sort_keys=True).encode()) > MAX_INPUTS_BYTES:
        raise ProvenanceError("origin.inputs is too large; reference large inputs by digest")
    return out


def normalize_origin(origin: Any) -> dict[str, Any]:
    """Validate ``origin`` and return the artifact_versions column values (absent fields are None)."""
    if origin is None:
        origin = {}
    if not isinstance(origin, dict):
        raise ProvenanceError("origin must be an object")
    unknown = set(origin) - ORIGIN_KEYS
    if unknown:
        raise ProvenanceError(f"unknown origin field: {sorted(unknown)[0]}")
    columns: dict[str, Any] = {column: None for column, _ in _TEXT_FIELDS.values()}
    for key, (column, limit) in _TEXT_FIELDS.items():
        if origin.get(key) is not None:
            columns[column] = _text(key, origin[key], limit)
    commit = origin.get("commit_sha")
    columns["commit_sha"] = None
    if commit is not None:
        if not isinstance(commit, str) or not _COMMIT.match(commit):
            raise ProvenanceError("origin.commit_sha must be 7-64 lowercase hex characters")
        columns["commit_sha"] = commit
    columns["redacted_inputs"] = redact_inputs(origin.get("inputs"))
    return columns


def context_links(columns: dict[str, Any]) -> dict[str, str | None]:
    """The binding context implied by a version's origin."""
    return {"room_id": columns.get("origin_room_id"), "task_id": columns.get("origin_task_id"),
            "issue_ref": columns.get("origin_issue_ref"), "project_id": columns.get("origin_project_id")}


def ctx_key(link_kind: str, *, task_id=None, project_id=None, issue_ref=None, room_id=None) -> str:
    material = json.dumps([link_kind, task_id or "", project_id or "", issue_ref or "", room_id or ""])
    return hashlib.sha256(material.encode()).hexdigest()
