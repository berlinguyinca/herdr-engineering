import json
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.assistant import AssistantNotFound, AssistantStore
from herdr_engineering.control_plane.assistant_hub.workers import (
    WorkerAuthError,
    WorkerRegistry,
)


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


def make(clock: Clock | None = None):
    clock = clock or Clock()
    store = AssistantStore()
    reg = WorkerRegistry(store, enrollment_ttl=timedelta(minutes=10),
                         worker_ttl=timedelta(days=30), now=clock)
    return store, reg, clock


def enroll(reg, device="dev-1", tenant="t1", user="u1", harness="pi"):
    _, code = reg.create_enrollment(tenant_id=tenant, user_id=user, name="box", harness=harness)
    return reg.redeem(code, device_id=device)


def test_enrollment_code_is_single_use():
    _, reg, _ = make()
    _, code = reg.create_enrollment(tenant_id="t1", user_id="u1", name="b", harness="pi")
    assert code.startswith("hce_")
    reg.redeem(code, device_id="d")
    with pytest.raises(WorkerAuthError):
        reg.redeem(code, device_id="d")


def test_enrollment_expires():
    _, reg, clock = make()
    _, code = reg.create_enrollment(tenant_id="t1", user_id="u1", name="b", harness="pi")
    clock.advance(minutes=11)
    with pytest.raises(WorkerAuthError):
        reg.redeem(code, device_id="d")


def test_redeem_returns_credential_once_and_stores_digest_only():
    store, reg, _ = make()
    _, code = reg.create_enrollment(tenant_id="t1", user_id="u1", name="b", harness="claude")
    worker, cred = reg.redeem(code, device_id="d")
    assert cred.startswith("hcw_")
    assert worker.scopes == ["ask", "report", "read"]
    assert "secret_digest" not in worker.as_dict()
    blob = json.dumps(store.snapshot()) + json.dumps(store.audit)
    assert cred not in blob and code not in blob
    assert worker.secret_digest in blob


def test_authenticate_requires_matching_device():
    _, reg, _ = make()
    w, cred = enroll(reg, device="dev-1")
    assert reg.authenticate(f"Worker {cred}", "dev-1").worker_id == w.worker_id
    with pytest.raises(WorkerAuthError):
        reg.authenticate(f"Worker {cred}", "dev-2")
    with pytest.raises(WorkerAuthError):
        reg.authenticate(f"Worker {cred}", None)


def test_authenticate_rejects_revoked_expired_unknown_malformed():
    store, reg, clock = make()
    w1, c1 = enroll(reg, device="d1")
    w2, c2 = enroll(reg, device="d2")
    reg.revoke(w1.worker_id, tenant_id="t1", user_id="u1")
    clock.advance(days=31)
    cases = [
        (f"Worker {c1}", "d1"),                 # revoked
        (f"Worker {c2}", "d2"),                 # expired
        ("Worker hcw_" + "x" * 43, "d1"),       # unknown
        ("Worker", "d1"),                       # malformed
        ("", "d1"),
        (f"Bearer {c1}", "d1"),                 # wrong scheme
        (f"Worker {c1} extra", "d1"),
        (f"Worker nothcw_{c1}", "d1"),
    ]
    msgs = set()
    for auth, dev in cases:
        with pytest.raises(WorkerAuthError) as e:
            reg.authenticate(auth, dev)
        msgs.add(str(e.value))
    assert len(msgs) == 1
    fails = [a for a in store.audit if a["action"] == "worker.auth_failed"]
    assert len(fails) == len(cases)
    blob = json.dumps(store.audit)
    assert c1 not in blob and c2 not in blob


def test_revoke_is_owner_scoped():
    store, reg, _ = make()
    w, cred = enroll(reg)
    for tenant, user in (("t2", "u1"), ("t1", "u2")):
        with pytest.raises(AssistantNotFound):
            reg.revoke(w.worker_id, tenant_id=tenant, user_id=user)
        assert reg.list_workers(tenant_id=tenant, user_id=user) == []
    assert reg.authenticate(f"Worker {cred}", "dev-1")
    assert [x.worker_id for x in reg.list_workers(tenant_id="t1", user_id="u1")] == [w.worker_id]
    assert reg.revoke(w.worker_id, tenant_id="t1", user_id="u1").status == "revoked"
    actions = [a["action"] for a in store.audit]
    assert "worker.enrolled" in actions and "worker.redeemed" in actions and "worker.revoked" in actions


def test_snapshot_restore_roundtrip_keeps_workers():
    store, reg, clock = make()
    w, cred = enroll(reg)
    _, code2 = reg.create_enrollment(tenant_id="t1", user_id="u1", name="b2", harness="codex")
    snap = json.loads(json.dumps(store.snapshot()))
    store2 = AssistantStore()
    store2.restore(snap)
    reg2 = WorkerRegistry(store2, enrollment_ttl=timedelta(minutes=10),
                          worker_ttl=timedelta(days=30), now=clock)
    assert reg2.authenticate(f"Worker {cred}", "dev-1").worker_id == w.worker_id
    assert reg2.redeem(code2, device_id="x")[0].harness == "codex"


def test_unknown_harness_rejected():
    _, reg, _ = make()
    with pytest.raises(ValueError):
        reg.create_enrollment(tenant_id="t1", user_id="u1", name="b", harness="emacs")


def test_last_seen_updates():
    _, reg, clock = make()
    w, cred = enroll(reg)
    assert w.last_seen_at is None
    clock.advance(minutes=5)
    got = reg.authenticate(f"Worker {cred}", "dev-1")
    assert got.last_seen_at == clock.t
