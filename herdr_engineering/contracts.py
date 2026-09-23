"""Typed dataclasses for the five required cross-cutting contracts.

Implements docs/contracts/{activity-event,artifact-ref,dev-service-lease,
test-event,ci-event}.md as immutable-ish dataclasses with validation,
idempotency keys and JSON round-tripping.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_ID_RE = re.compile(r"^[A-Za-z0-9_\-.:/]{1,256}$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _validate_id(name: str, value: str | None, pattern=_ID_RE) -> None:
    if value is not None and not pattern.match(value):
        raise ValueError(f"{name} is not a valid identifier: {value!r}")


def as_dict(obj: Any) -> dict[str, Any]:
    d = dataclasses.asdict(obj)
    return {k: v for k, v in d.items() if v is not None}


# --------------------------------------------------------------------------
# ArtifactRef
# --------------------------------------------------------------------------
@dataclass
class ArtifactRef:
    id: str
    kind: str
    title: str
    uri: str
    created_at: str = field(default_factory=_now)
    machine_id: str | None = None
    repository: str | None = None
    worktree_id: str | None = None
    revision: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    producer_system: str = "herdr-engineering"
    producer_session_id: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        _validate_id("id", self.id)
        if not self.title:
            raise ValueError("title required")
        if not self.uri:
            raise ValueError("uri required")
        if self.uri.startswith("https://") and ("@" in self.uri.split("://")[1].split("/")[0]):
            raise ValueError("uri must not embed credentials")

    def to_dict(self) -> dict[str, Any]:
        d = as_dict(self)
        d["producer"] = {
            "system": self.producer_system,
            "session_id": self.producer_session_id,
        }
        d.pop("producer_system", None)
        d.pop("producer_session_id", None)
        if self.sha256:
            d["integrity"] = {"sha256": self.sha256}
        return d


# --------------------------------------------------------------------------
# ActivityEvent
# --------------------------------------------------------------------------
_SOURCES = {"herdr", "pi-engineering", "autospec", "git", "browser", "test",
            "ci", "plannotator", "user", "system"}
_TYPES = {"session.started", "status.changed", "task.started", "task.completed",
          "command.started", "command.completed", "git.commit", "git.diff",
          "test.run", "ci.run", "review.requested", "review.completed",
          "artifact.created", "dev.registered", "dev.expired",
          "attention.required", "note"}
_SEVERITIES = {"debug", "info", "notice", "warning", "error"}
_STATUSES = {"working", "blocked", "waiting", "idle", "done", "failed", "unknown"}


@dataclass
class ActivityEvent:
    id: str
    occurred_at: str
    source: str
    type: str
    summary: str
    severity: str = "info"
    observed_at: str = field(default_factory=_now)
    status: str | None = None
    machine_id: str | None = None
    herdr_session_id: str | None = None
    pi_session_id: str | None = None
    mission_id: str | None = None
    autospec_issue_id: str | None = None
    repository: str | None = None
    worktree_id: str | None = None
    branch: str | None = None
    revision: str | None = None
    test_run_id: str | None = None
    ci_run_id: str | None = None
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_id("id", self.id)
        if self.source not in _SOURCES:
            raise ValueError(f"unknown source {self.source}")
        if self.type not in _TYPES:
            raise ValueError(f"unknown type {self.type}")
        if self.severity not in _SEVERITIES:
            raise ValueError(f"unknown severity {self.severity}")
        if self.status is not None and self.status not in _STATUSES:
            raise ValueError(f"unknown status {self.status}")
        if not self.summary:
            raise ValueError("summary required")

    @property
    def correlation(self) -> dict[str, str | None]:
        return {
            "machine_id": self.machine_id,
            "herdr_session_id": self.herdr_session_id,
            "pi_session_id": self.pi_session_id,
            "mission_id": self.mission_id,
            "autospec_issue_id": self.autospec_issue_id,
            "repository": self.repository,
            "worktree_id": self.worktree_id,
            "branch": self.branch,
            "revision": self.revision,
            "test_run_id": self.test_run_id,
            "ci_run_id": self.ci_run_id,
        }

    def to_dict(self) -> dict[str, Any]:
        d = as_dict(self)
        d["correlation"] = self.correlation
        return d


# --------------------------------------------------------------------------
# DevServiceLease
# --------------------------------------------------------------------------
_LEASE_STATES = {"pending", "active", "draining", "expired", "failed"}
_PROTOCOLS = {"http", "https", "tcp"}


@dataclass
class DevServiceLease:
    id: str
    external_port: int
    protocol: str
    target_machine_id: str
    target_host: str
    target_port: int
    owner_user_id: str | None = None
    owner_herdr_session_id: str | None = None
    owner_repository: str | None = None
    owner_worktree_id: str | None = None
    owner_process_id: str | None = None
    label: str | None = None
    state: str = "pending"
    created_at: str = field(default_factory=_now)
    renewed_at: str = field(default_factory=_now)
    expires_at: str = field(default_factory=_now)
    health_last_ok_at: str | None = None
    health_last_error: str | None = None

    def __post_init__(self) -> None:
        _validate_id("id", self.id)
        if self.state not in _LEASE_STATES:
            raise ValueError(f"unknown state {self.state}")
        if self.protocol not in _PROTOCOLS:
            raise ValueError(f"unknown protocol {self.protocol}")
        if not (0 < self.external_port < 65536):
            raise ValueError("external_port out of range")
        if not (0 < self.target_port < 65536):
            raise ValueError("target_port out of range")

    def to_dict(self) -> dict[str, Any]:
        d = as_dict(self)
        d["target"] = {"machine_id": self.target_machine_id,
                       "host": self.target_host, "port": self.target_port}
        d["owner"] = {k: v for k, v in {
            "user_id": self.owner_user_id,
            "herdr_session_id": self.owner_herdr_session_id,
            "repository": self.owner_repository,
            "worktree_id": self.owner_worktree_id,
            "process_id": self.owner_process_id,
        }.items() if v is not None}
        for k in ("target_machine_id", "target_host", "target_port",
                  "owner_user_id", "owner_herdr_session_id", "owner_repository",
                  "owner_worktree_id", "owner_process_id"):
            d.pop(k, None)
        d["health"] = {"last_ok_at": self.health_last_ok_at,
                       "last_error": self.health_last_error}
        d.pop("health_last_ok_at", None)
        d.pop("health_last_error", None)
        return d


# --------------------------------------------------------------------------
# TestEvent
# --------------------------------------------------------------------------
_TEST_TYPES = {"run.started", "suite.started", "test.started", "test.output",
               "test.finished", "suite.finished", "run.finished"}
_TEST_STATUSES = {"running", "passed", "failed", "skipped", "xfailed", "xpassed",
                  "errored", "cancelled", "unknown"}


@dataclass
class TestEvent:
    __test__ = False  # application class, not a pytest class
    schema_version: int = 1
    run_id: str = ""
    event_id: str = ""
    timestamp: str = field(default_factory=_now)
    adapter: str = "custom"
    repository: str | None = None
    worktree_id: str | None = None
    machine_id: str | None = None
    revision: str | None = None
    type: str = ""
    test_id: str | None = None
    test_parent_id: str | None = None
    test_display_name: str | None = None
    test_file: str | None = None
    test_line: int | None = None
    result_status: str | None = None
    result_duration_ms: int | None = None
    result_message: str | None = None
    result_trace: str | None = None
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.type not in _TEST_TYPES:
            raise ValueError(f"unknown type {self.type}")
        if self.result_status is not None and self.result_status not in _TEST_STATUSES:
            raise ValueError(f"unknown status {self.result_status}")

    def to_dict(self) -> dict[str, Any]:
        d = as_dict(self)
        d["test"] = {k: v for k, v in {
            "id": self.test_id, "parent_id": self.test_parent_id,
            "display_name": self.test_display_name,
            "source": ({"file": self.test_file, "line": self.test_line}
                       if self.test_file or self.test_line else None),
        }.items() if v is not None}
        d["result"] = {k: v for k, v in {
            "status": self.result_status, "duration_ms": self.result_duration_ms,
            "message": self.result_message, "trace": self.result_trace,
        }.items() if v is not None}
        for k in ("test_id", "test_parent_id", "test_display_name", "test_file",
                  "test_line", "result_status", "result_duration_ms",
                  "result_message", "result_trace"):
            d.pop(k, None)
        return d


# --------------------------------------------------------------------------
# CIEvent
# --------------------------------------------------------------------------
_CI_TYPES = {"pipeline", "step", "queue", "runner"}
_CI_STATES = {"queued", "pending", "running", "success", "failure", "killed",
              "blocked", "unknown"}


@dataclass
class CIEvent:
    schema_version: int = 1
    provider: str = "woodpecker"
    event_id: str = ""
    timestamp: str = field(default_factory=_now)
    repository: str | None = None
    pipeline_id: str | None = None
    run_number: int | None = None
    revision: str | None = None
    branch: str | None = None
    event_type: str = "pipeline"
    state: str = "unknown"
    step_id: str | None = None
    step_name: str | None = None
    runner_id: str | None = None
    runner_labels: list[str] = field(default_factory=list)
    queue_position: int | None = None
    queued_at: str | None = None
    provider_url: str | None = None
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    capabilities_retry: bool = False
    capabilities_cancel: bool = False

    def __post_init__(self) -> None:
        if self.event_type not in _CI_TYPES:
            raise ValueError(f"unknown event_type {self.event_type}")
        if self.state not in _CI_STATES:
            raise ValueError(f"unknown state {self.state}")

    def to_dict(self) -> dict[str, Any]:
        d = as_dict(self)
        d["step"] = {"id": self.step_id, "name": self.step_name} if (self.step_id or self.step_name) else None
        d["runner"] = {"id": self.runner_id, "labels": self.runner_labels} if self.runner_id else None
        d["queue"] = {"position": self.queue_position, "queued_at": self.queued_at}
        d["links"] = {"provider_url": self.provider_url}
        d["capabilities"] = {"retry": self.capabilities_retry,
                             "cancel": self.capabilities_cancel}
        for k in ("step_id", "step_name", "runner_id", "runner_labels",
                  "queue_position", "queued_at", "provider_url",
                  "capabilities_retry", "capabilities_cancel"):
            d.pop(k, None)
        return {k: v for k, v in d.items() if v is not None}


def to_json(obj: Any) -> str:
    return json.dumps(obj.to_dict(), indent=2, sort_keys=True)
