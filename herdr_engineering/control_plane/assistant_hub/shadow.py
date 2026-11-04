"""Shadow judge records and the async recorder.

In shadow mode the judge never blocks or changes a decision; its verdict is recorded next to the owner's real
decision so the false-negative rate can be measured before the judge is ever trusted.  A ``JudgeRecord`` holds
ids, the verb/risk class and the verdict numbers only: never command text, display text or the canonical target.
The recorder runs the (slow) network call on one daemon worker thread, strictly outside any store lock, and never
raises into its caller.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from .jev import JevClient, JudgeUnavailable
from .judge import consult
from .verbs import ActionKey

if TYPE_CHECKING:
    from ..assistant import AssistantStore

__all__ = ["JudgeRecord", "ShadowJob", "ShadowRecorder", "label_decision", "mark_regret"]

_log = logging.getLogger(__name__)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
CONSULT_TIMEOUT_S = 30.0
OwnerDecision = Literal["", "approved", "denied"]
RecordStatus = Literal["ok", "unavailable"]


@dataclass
class JudgeRecord:
    record_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    worker_id: str = ""
    session_id: str = ""
    task_id: str = ""
    request_id: str = ""
    key_id: str = ""
    verb: str = ""
    risk_class: str = ""
    judge_version: str = ""
    model: str = ""
    status: RecordStatus = "unavailable"
    reason_code: str = ""
    destructive: float = 0.0
    irreversible: float = 0.0
    severity: float = 0.0
    covers: float = 0.0
    escalate: bool = False
    created_at: datetime = _EPOCH
    owner_decision: OwnerDecision = ""
    decided_at: datetime | None = None
    regret: bool = False
    label_note: str = ""


@dataclass(frozen=True)
class ShadowJob:
    tenant_id: str
    user_id: str
    worker_id: str
    session_id: str
    task_id: str
    request_id: str
    key: ActionKey


class ShadowRecorder:
    def __init__(self, store: AssistantStore, cfg_getter: Callable[[], Mapping[str, object]],
                 client_factory: Callable[[], JevClient | None], now: Callable[[], datetime],
                 queue_size: int = 100) -> None:
        self._store = store
        self._cfg_getter = cfg_getter
        self._client_factory = client_factory
        self._now = now
        self._size = max(1, int(queue_size))
        self._queue: deque[ShadowJob] = deque()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopped = False
        self.dropped = 0

    def _enabled(self) -> bool:
        try:
            return self._cfg_getter().get("enabled") is True
        except Exception:
            return False

    def start(self) -> None:
        self._ensure_thread(self._enabled())

    def _ensure_thread(self, enabled: bool) -> None:
        if not enabled:
            return
        with self._cond:
            if self._stopped or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._run, name="assistant-shadow-judge", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stopped = True
            self._queue.clear()
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def submit(self, job: ShadowJob) -> bool:
        enabled = self._enabled()          # the config is read once, outside our condition lock
        if not enabled:
            return False
        self._ensure_thread(enabled)
        with self._cond:
            if self._stopped:
                return False
            if len(self._queue) >= self._size:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(job)
            self._cond.notify()
            return True

    # -- worker ------------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                job = self._queue.popleft()
            try:
                self._process(job)                    # no lock of ours (or the store's) is held here
            except Exception:
                _log.warning("shadow judge job failed", exc_info=False)

    def _process(self, job: ShadowJob) -> None:
        cfg = self._cfg_getter()
        if cfg.get("enabled") is not True:
            return
        if self._store.get_judge_record_by_request(job.tenant_id, job.user_id, job.request_id) is not None:
            return
        base = JudgeRecord(
            record_id=f"jr-{job.tenant_id}-{job.user_id}-{job.request_id}", tenant_id=job.tenant_id, user_id=job.user_id,
            worker_id=job.worker_id, session_id=job.session_id, task_id=job.task_id, request_id=job.request_id,
            key_id=job.key.key_id(), verb=job.key.verb.value, risk_class=job.key.risk_class.value,
            model=str(cfg.get("model", "")), created_at=self._now())
        record = self._judge(job, cfg, base)
        if self._stopped:
            return                      # stop() was called while the judge call was in flight
        if self._store.add_judge_record(record):
            self._copy_early_decision(job, record)

    def _copy_early_decision(self, job: ShadowJob, record: JudgeRecord) -> None:
        """The owner may decide before this record exists; then the label would be lost, so copy it now.

        ``label_decision`` keeps the first label, so a decision that raced in between is never overwritten."""
        row = self._store.get_action_request(job.request_id, tenant_id=job.tenant_id, user_id=job.user_id)
        if row is not None and row.status in ("approved", "denied"):
            label_decision(self._store, self._cfg_getter, job.tenant_id, job.user_id, job.request_id,
                           row.status == "approved", row.decided_at or self._now())

    def _judge(self, job: ShadowJob, cfg: Mapping[str, object], base: JudgeRecord) -> JudgeRecord:
        try:
            client = self._client_factory()
            if client is None:
                raise JudgeUnavailable("no_client")
            v = consult(client, job.key, cfg, timeout=CONSULT_TIMEOUT_S)    # type: ignore[arg-type]
        except JudgeUnavailable as exc:
            return _with(base, status="unavailable", reason_code=exc.code)
        except Exception:
            return _with(base, status="unavailable", reason_code="internal")
        return _with(base, status="ok", judge_version=v.judge_version, model=v.model, destructive=v.destructive,
                     irreversible=v.irreversible, severity=v.severity, covers=v.covers, escalate=v.escalate)


def _with(base: JudgeRecord, **changes: object) -> JudgeRecord:
    return replace(base, **changes)    # type: ignore[arg-type]


def label_decision(store: AssistantStore, cfg_getter: Callable[[], Mapping[str, object]], tenant_id: str,
                   user_id: str, request_id: str, approved: bool, now: datetime) -> bool:
    """Record the owner's real decision next to the shadow verdict.

    The FIRST label wins: ignored (False) when no record exists or it already carries a decision; a later change of
    mind is expressed with ``mark_regret``.  ``cfg_getter`` is part of the plan's interface but labelling needs no
    judge access, so it is unused."""
    del cfg_getter
    return store.update_judge_label(tenant_id, user_id, request_id,
                                    owner_decision="approved" if approved else "denied", decided_at=now)


def mark_regret(store: AssistantStore, tenant_id: str, user_id: str, key_id: str, worker_id: str) -> int:
    """The owner revoked a grant: every record for that key and worker is flagged as a regretted approval."""
    return store.mark_judge_regret(tenant_id, key_id, worker_id, user_id=user_id)
