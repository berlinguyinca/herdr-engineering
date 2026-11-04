"""Worker registry for the assistant hub: one-time enrollment codes and digest-only worker credentials.

Only sha256 hex digests are ever stored or audited; the plaintext credential and enrollment code are returned to the
caller exactly once. Every authentication failure raises the same ``WorkerAuthError`` (uniform 401).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # assistant.py imports this module, so avoid a runtime cycle
    from ..assistant import AssistantStore

SCOPES: tuple[str, ...] = ("ask", "report", "read", "act")
DEFAULT_SCOPES: tuple[str, ...] = ("ask", "report")
AUTH_SCHEME = "Worker"
_AUTH_MESSAGE = "worker authentication failed"
_CRED_PREFIX = "hcw_"
_CODE_PREFIX = "hce_"
_HUB_TENANT = "hub"
_FAIL_WINDOW = timedelta(seconds=60)
_MAX_FAIL_KEYS = 1024
_SEEN_PERSIST_INTERVAL = timedelta(seconds=60)
_MAX_NAME = 64

log = logging.getLogger(__name__)


class WorkerAuthError(Exception):
    """Uniform authentication failure; callers map it to HTTP 401 without distinguishing causes."""

    def __init__(self) -> None:
        super().__init__(_AUTH_MESSAGE)


@dataclass
class Worker:
    worker_id: str
    tenant_id: str
    user_id: str
    name: str
    harness: str
    device_id: str
    secret_digest: str
    status: str
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime | None = None
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("secret_digest", None)
        for k in ("created_at", "expires_at", "last_seen_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


@dataclass
class Enrollment:
    enrollment_id: str
    tenant_id: str
    user_id: str
    name: str
    harness: str
    code_digest: str
    expires_at: datetime
    redeemed_at: datetime | None = None
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))    # older snapshots load with the default


def _normalize_scopes(scopes: Sequence[str]) -> list[str]:
    """Validated, de-duplicated scopes in vocabulary order; unknown names and the empty set are refused."""
    if isinstance(scopes, str) or not isinstance(scopes, Sequence):
        raise ValueError("scopes must be a list of scope names")
    bad = [s for s in scopes if not isinstance(s, str) or s not in SCOPES]
    if bad:
        raise ValueError(f"unknown scope: {bad[0]!r}")
    if not scopes:
        raise ValueError("scopes must not be empty")
    return [s for s in SCOPES if s in scopes]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkerRegistry:
    def __init__(self, store: AssistantStore, *, enrollment_ttl: timedelta, worker_ttl: timedelta,
                 now: Callable[[], datetime] = _utcnow) -> None:
        self._store = store
        self._enrollment_ttl = enrollment_ttl
        self._worker_ttl = worker_ttl
        self._now = now
        self._fail_windows: dict[tuple[str, str], tuple[datetime, int]] = {}

    def create_enrollment(self, *, tenant_id: str, user_id: str, name: str, harness: str,
                          scopes: Sequence[str] | None = None) -> tuple[Enrollment, str]:
        from ..assistant_dispatch import HARNESSES  # lazy: assistant_dispatch imports assistant

        if harness not in HARNESSES:
            raise ValueError(f"unknown harness: {harness!r}")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be non-empty")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be non-empty")
        if not isinstance(name, str) or not name.strip() or len(name) > _MAX_NAME:
            raise ValueError(f"name must be 1-{_MAX_NAME} characters")
        if not name.isprintable():
            raise ValueError("name must not contain control characters")
        granted = _normalize_scopes(DEFAULT_SCOPES if scopes is None else scopes)
        code = _CODE_PREFIX + secrets.token_urlsafe(24)
        enrollment = Enrollment(
            enrollment_id="enr_" + secrets.token_hex(8), tenant_id=tenant_id, user_id=user_id, name=name,
            harness=harness, code_digest=_digest(code), expires_at=self._now() + self._enrollment_ttl, scopes=granted)
        with self._store._lock:
            self._store.enrollments[enrollment.enrollment_id] = enrollment
            self._store._after_mutation()
            self._store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id, action="worker.enrolled",
                                     subject_id=enrollment.enrollment_id, detail=f"harness={harness}")
        return enrollment, code

    def _fail(self, reason: str, *, tenant_id: str | None = None, user_id: str = "-",
              subject_id: str = "-") -> WorkerAuthError:
        """Audit a failure and return the uniform error. Failures matching no known worker/enrollment (tenant_id
        None) are coalesced to one entry per window; an audit-write failure never changes the 401."""
        with self._store._lock:
            # Coalesce per (subject, reason): unknown credentials share one key, known workers/enrollments get their own.
            key = (subject_id, reason) if tenant_id is not None else ("-", "-")
            now = self._now()
            window = self._fail_windows.get(key)
            if window is not None and now - window[0] < _FAIL_WINDOW:
                self._fail_windows[key] = (window[0], window[1] + 1)
                return WorkerAuthError()
            detail = f"{reason}; suppressed={window[1] if window else 0}" if window or tenant_id is None else reason
            if len(self._fail_windows) >= _MAX_FAIL_KEYS:
                self._fail_windows = {k: v for k, v in self._fail_windows.items() if now - v[0] < _FAIL_WINDOW}
            self._fail_windows[key] = (now, 0)
            try:
                self._store.audit_record(tenant_id=tenant_id or _HUB_TENANT, user_id=user_id, actor="worker",
                                         action="worker.auth_failed", subject_id=subject_id, detail=detail)
            except Exception as exc:  # noqa: BLE001 - fail closed on the caller side, never turn a 401 into a 500
                log.warning("worker auth-failure audit write failed (%s)", type(exc).__name__)
        return WorkerAuthError()

    def redeem(self, code: str, *, device_id: str) -> tuple[Worker, str]:
        digest = _digest(code) if isinstance(code, str) else ""
        with self._store._lock:
            found = None
            for e in self._store.enrollments.values():
                if _eq(e.code_digest, digest):
                    found = e
            if found is None or not device_id:
                raise self._fail("redeem: unknown code")
            if found.redeemed_at is not None:
                raise self._fail("redeem: code already used", tenant_id=found.tenant_id, user_id=found.user_id,
                                 subject_id=found.enrollment_id)
            now = self._now()
            if now >= found.expires_at:
                raise self._fail("redeem: code expired", tenant_id=found.tenant_id, user_id=found.user_id,
                                 subject_id=found.enrollment_id)
            cred = _CRED_PREFIX + secrets.token_urlsafe(32)
            worker_id = "wrk_" + secrets.token_hex(8)
            # Audit first: if the durable sink fails, nothing is consumed and the code stays redeemable.
            self._store.audit_record(tenant_id=found.tenant_id, user_id=found.user_id, actor=worker_id,
                                     action="worker.redeemed", subject_id=worker_id,
                                     detail=f"harness={found.harness}")
            found.redeemed_at = now
            worker = Worker(
                worker_id=worker_id, tenant_id=found.tenant_id, user_id=found.user_id,
                name=found.name, harness=found.harness, device_id=device_id, secret_digest=_digest(cred),
                status="active", created_at=now, expires_at=now + self._worker_ttl, scopes=list(found.scopes))
            self._store.workers[worker.worker_id] = worker
            self._store._after_mutation()
        return worker, cred

    def authenticate(self, authorization: str, device: str | None) -> Worker:
        parts = authorization.split(" ") if isinstance(authorization, str) else []
        if len(parts) != 2 or parts[0] != AUTH_SCHEME or not parts[1].startswith(_CRED_PREFIX):
            raise self._fail("authenticate: malformed authorization")
        digest = _digest(parts[1])
        with self._store._lock:
            found = None
            for w in self._store.workers.values():
                if _eq(w.secret_digest, digest):
                    found = w
            if found is None:
                raise self._fail("authenticate: unknown credential")
            ctx = {"tenant_id": found.tenant_id, "user_id": found.user_id, "subject_id": found.worker_id}
            if found.status != "active":
                raise self._fail("authenticate: revoked", **ctx)
            now = self._now()
            if now >= found.expires_at:
                raise self._fail("authenticate: expired", **ctx)
            if not isinstance(device, str) or not _eq(found.device_id, device):
                raise self._fail("authenticate: device mismatch", **ctx)
            prev = found.last_seen_at
            found.last_seen_at = now
            if prev is None or now - prev >= _SEEN_PERSIST_INTERVAL:
                self._store._after_mutation()
            return found

    def _owned(self, worker_id: str, tenant_id: str, user_id: str) -> Worker:
        from ..assistant import AssistantNotFound

        w = self._store.workers.get(worker_id)
        if w is None or w.tenant_id != tenant_id or w.user_id != user_id:
            raise AssistantNotFound("worker not found")
        return w

    def revoke(self, worker_id: str, *, tenant_id: str, user_id: str) -> Worker:
        with self._store._lock:
            w = self._owned(worker_id, tenant_id, user_id)
            w.status = "revoked"
            self._store._after_mutation()
            self._store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id, action="worker.revoked",
                                     subject_id=w.worker_id)
            return w

    def set_scopes(self, tenant_id: str, user_id: str, worker_id: str, scopes: Sequence[str]) -> Worker:
        from ..assistant import AssistantNotFound

        granted = _normalize_scopes(scopes)
        with self._store._lock:
            try:
                w = self._owned(worker_id, tenant_id, user_id)
            except AssistantNotFound as exc:
                raise KeyError(worker_id) from exc
            if w.status != "active":
                raise ValueError("a revoked worker's scopes cannot be changed")
            old = list(w.scopes)
            w.scopes = granted
            self._store._after_mutation()
            self._store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                     action="assistant.worker.scopes_changed", subject_id=w.worker_id,
                                     detail=f"{','.join(old)}->{','.join(granted)}")
            return w

    def list_workers(self, *, tenant_id: str, user_id: str) -> list[Worker]:
        with self._store._lock:
            return [w for w in self._store.workers.values()
                    if w.tenant_id == tenant_id and w.user_id == user_id]
