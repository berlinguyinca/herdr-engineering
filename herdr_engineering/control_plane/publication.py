"""Approval-gated publication state and provider-neutral exposure contract."""
from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Protocol


class PublicationError(ValueError):
    pass


class ExposureProvider(Protocol):
    def capabilities(self) -> dict: ...
    def prepare(self, publication: dict) -> None: ...
    def expose(self, publication: dict) -> dict: ...
    def revoke(self, publication: dict) -> None: ...


class FakeExposureProvider:
    """Deterministic provider for conformance/integration tests."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.fail_revoke = False

    def capabilities(self) -> dict:
        return {"https": True, "websocket": False, "custom_domain": False}

    def prepare(self, publication: dict) -> None:
        self.calls.append(("prepare", publication["publication_id"]))

    def expose(self, publication: dict) -> dict:
        self.calls.append(("expose", publication["publication_id"]))
        return {"endpoint": f"https://edge.invalid/s/{publication['publication_id']}"}

    def revoke(self, publication: dict) -> None:
        self.calls.append(("revoke", publication["publication_id"]))
        if self.fail_revoke:
            raise RuntimeError("simulated provider revoke failure")


class PrivateOnlyExposureProvider:
    def capabilities(self) -> dict:
        return {"enabled": False, "https": False, "websocket": False}

    def prepare(self, publication: dict) -> None:
        return

    def expose(self, publication: dict) -> dict:
        raise PublicationError("no public exposure provider is configured")

    def revoke(self, publication: dict) -> None:
        return


class PublicationController:
    """Private-by-default publication state machine.

    The controller owns authorization/state; providers only manage transport.
    Tokens are hashed for lookup and the plaintext is retained only in the
    in-process response needed to show the newly approved link.
    """

    def __init__(self, *, provider: ExposureProvider, now: Callable[[], float] = time.time,
                 on_change=None, default_local_target: str | None = None):
        self.provider = provider
        self.now = now
        self.on_change = on_change
        self.default_local_target = default_local_target
        self._records: dict[str, dict] = {}
        self._token_index: dict[str, str] = {}
        self._tokens: dict[str, str] = {}
        self.paused = False

    def request(self, *, resource_id: str, version_id: str, requested_by: str,
                audience: str, expires_at: float, guest_principals=()) -> dict:
        if audience not in {"unlisted_link", "authenticated_guest", "public_discoverable"}:
            raise PublicationError("unsupported publication audience")
        if not resource_id or not version_id or not requested_by:
            raise PublicationError("resource, version, and requester are required")
        if expires_at <= self.now():
            raise PublicationError("publication expiry must be in the future")
        token = secrets.token_urlsafe(32)
        publication_id = "publication_" + secrets.token_hex(12)
        record = {
            "publication_id": publication_id,
            "resource_id": resource_id,
            "pinned_version_id": version_id,
            "requested_by": requested_by,
            "approved_by": None,
            "audience": audience,
            "expires_at": expires_at,
            "token_hash": self._hash(token),
            "state": "requested",
            "endpoint": None,
            "provider_error": None,
            "guest_principals": sorted(set(guest_principals)),
            "local_target": self.default_local_target,
        }
        self._records[publication_id] = record
        self._token_index[record["token_hash"]] = publication_id
        self._tokens[publication_id] = token
        if self.on_change:
            self.on_change("requested", dict(record))
        return {**record, "token": token}

    def approve(self, publication_id: str, *, approver: str) -> dict:
        record = self._record(publication_id)
        if record["state"] != "requested":
            raise PublicationError("publication is not awaiting approval")
        if not approver:
            raise PublicationError("approver is required")
        if record["expires_at"] <= self.now():
            record["state"] = "expired"
            raise PublicationError("publication has expired")
        record["approved_by"] = approver
        record["state"] = "provisioning"
        try:
            self.provider.prepare(record)
            observed = self.provider.expose(record)
        except Exception as exc:  # noqa: BLE001 - provisioning failure is auditable
            record["state"] = "failed"
            record["provider_error"] = str(exc)
            if self.on_change:
                self.on_change("failed", dict(record))
            raise
        record["endpoint"] = observed.get("endpoint")
        record["state"] = "active"
        if self.on_change:
            self.on_change("approved", dict(record))
        return {**record, "token": self._tokens[publication_id]}

    def revoke(self, publication_id: str, *, actor: str) -> dict:
        record = self._record(publication_id)
        if not actor:
            raise PublicationError("actor is required")
        # Fail closed before touching the provider. Provider cleanup is best effort.
        record["state"] = "revoked"
        try:
            self.provider.revoke(record)
        except Exception as exc:  # noqa: BLE001 - revoke must remain denied
            record["provider_error"] = str(exc)
        if self.on_change:
            self.on_change("revoked", dict(record))
        return {**record, "token": self._tokens[publication_id]}

    def lookup_token(self, token: str) -> dict | None:
        publication_id = self._token_index.get(self._hash(token))
        return self._records.get(publication_id) if publication_id else None

    def get(self, publication_id: str) -> dict | None:
        return self._records.get(publication_id)

    def list(self) -> list[dict]:
        return [dict(record) for record in self._records.values()]

    def is_deliverable(self, publication_id: str, *, guest_id: str | None = None) -> bool:
        record = self._record(publication_id)
        if record["state"] != "active" or self.paused:
            return False
        if record["audience"] == "authenticated_guest" and guest_id not in record["guest_principals"]:
            return False
        if record["expires_at"] <= self.now():
            record["state"] = "expired"
            return False
        return True

    def _record(self, publication_id: str) -> dict:
        record = self._records.get(publication_id)
        if record is None:
            raise PublicationError("publication not found")
        return record

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

