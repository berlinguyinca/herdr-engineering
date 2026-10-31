"""Worker event log for the assistant hub: asks, reports and observations posted by enrolled workers.

Tenant and user always come from the authenticated ``Worker``, never from the caller. Provenance is a caller-asserted
argument validated against a fixed set; it is never inferred from payload contents (a payload claiming approval or
attestation is just data). Payloads are redacted and size-capped; the log is capped per tenant (oldest dropped).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # assistant.py imports this module, so avoid a runtime cycle
    from ..assistant import AssistantStore
    from .workers import Worker

EVENT_KINDS: tuple[str, ...] = ("ask", "report", "observation")
PROVENANCES: tuple[str, ...] = ("worker_reported", "harness_attested")
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_LIST_LIMIT = 500
_MAX_SESSION_ID = 128


@dataclass
class WorkerEvent:
    event_id: str
    tenant_id: str
    user_id: str
    worker_id: str
    session_id: str
    kind: str
    provenance: str
    payload: dict[str, Any]
    created_at: datetime


def record_event(store: AssistantStore, worker: Worker, *, kind: str, provenance: str, session_id: str,
                 payload: dict[str, Any]) -> WorkerEvent:
    from ..assistant import redact_obj

    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind!r}")
    if provenance not in PROVENANCES:
        raise ValueError(f"unknown provenance: {provenance!r}")
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > _MAX_SESSION_ID:
        raise ValueError(f"session_id must be 1-{_MAX_SESSION_ID} characters")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    safe = redact_obj(payload)
    try:
        size = len(json.dumps(safe, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("payload is not serializable") from exc
    if size > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    event = WorkerEvent(
        event_id="evt_" + secrets.token_hex(8), tenant_id=worker.tenant_id, user_id=worker.user_id,
        worker_id=worker.worker_id, session_id=session_id, kind=kind, provenance=provenance, payload=safe,
        created_at=datetime.now(UTC))
    with store._lock:
        store.worker_events[event.event_id] = event
        own = [e.event_id for e in store.worker_events.values() if e.tenant_id == worker.tenant_id]
        for old in own[:max(0, len(own) - store.max_events_per_tenant)]:
            del store.worker_events[old]
        store._after_mutation()
    return event


def list_events(store: AssistantStore, *, tenant_id: str, user_id: str, worker_id: str | None = None,
                kind: str | None = None, limit: int = 100) -> list[WorkerEvent]:
    limit = max(0, min(int(limit), MAX_LIST_LIMIT))
    with store._lock:
        rows = [e for e in reversed(list(store.worker_events.values()))
                if e.tenant_id == tenant_id and e.user_id == user_id
                and (worker_id is None or e.worker_id == worker_id) and (kind is None or e.kind == kind)]
    return rows[:limit]
