"""Acknowledged recovery transitions in the existing control-plane event store."""
import copy
import json
from contextlib import contextmanager


class RecoveryJournal:
    def __init__(self, repo):
        self.repo = repo

    @contextmanager
    def operation(self, operation_id):
        if not self.repo.scheduler_execution_ready():
            raise RuntimeError("durable recovery persistence is required")
        lock = getattr(self.repo._sink, "scheduler_recovery_operation", None)
        if callable(lock):
            with lock(operation_id):
                yield self.latest(operation_id)
            return
        with self.repo._command_lock:
            yield self.latest(operation_id)

    def latest(self, operation_id):
        reader = getattr(self.repo._sink, "scheduler_recovery_latest", None)
        if callable(reader):
            return reader(operation_id)
        events = self.repo.stream("scheduler_recovery", operation_id)
        if not events:
            return None
        payload = events[-1].get("payload") or {}
        return json.loads(payload) if isinstance(payload, str) else dict(payload)

    def record(self, operation_id, record):
        snapshot = copy.deepcopy(record)
        self.repo.record_event("scheduler_recovery", operation_id, "RecoveryTransition",
                               snapshot, require_durable=True, publish=False)
        return copy.deepcopy(snapshot)
