"""Structured errors shared across the integration layer.

These map to the structured error taxonomy required by spec 0030:
unreachable, permission, unsupported, timeout, conflict, invalid.
"""
from __future__ import annotations


class HerdrEngineeringError(Exception):
    """Base error with a stable machine-readable ``code``."""

    code = "internal"
    http_status = 500

    def __init__(
        self,
        message: str,
        *,
        machine_id: str | None = None,
        target: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.machine_id = machine_id
        self.target = target
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.machine_id:
            payload["machine_id"] = self.machine_id
        if self.target:
            payload["target"] = self.target
        if self.details:
            payload["details"] = self.details
        return payload


class UnreachableError(HerdrEngineeringError):
    """A machine / host / service cannot be reached."""

    code = "unreachable"
    http_status = 503


class PermissionError_Engineering(HerdrEngineeringError):
    """The caller is not authorized for the action."""

    code = "permission"
    http_status = 403


class UnsupportedError(HerdrEngineeringError):
    """The current Herdr version / plugin does not support the capability."""

    code = "unsupported"
    http_status = 501


class TimeoutError_Engineering(HerdrEngineeringError):
    """An operation exceeded its deadline."""

    code = "timeout"
    http_status = 504


class ConflictError(HerdrEngineeringError):
    """The requested change conflicts with current state (e.g. port taken)."""

    code = "conflict"
    http_status = 409


class InvalidError(HerdrEngineeringError):
    """Input failed validation."""

    code = "invalid"
    http_status = 400
