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
    assert worker.scopes == ["ask", "report"]
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
    assert len(fails) == 3  # revoked + expired individually, all unknown/malformed coalesced into one
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


class CountingBackend:
    """A real in-memory persistence backend that counts writes (not a mock)."""

    def __init__(self) -> None:
        self.writes = 0

    def persist(self, snap) -> None:
        self.writes += 1

    def load(self):
        return None


def make_counted():
    clock = Clock()
    backend = CountingBackend()
    store = AssistantStore(persist_backend=backend)
    reg = WorkerRegistry(store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30), now=clock)
    return store, reg, clock, backend


def test_unknown_credential_failures_are_coalesced_per_window():
    store, reg, clock, backend = make_counted()
    before = backend.writes
    for i in range(500):
        with pytest.raises(WorkerAuthError):
            reg.authenticate(f"Worker hcw_{i:040d}", "d")
    fails = [a for a in store.audit if a["action"] == "worker.auth_failed"]
    assert len(fails) == 1
    assert backend.writes - before == 1   # persists counted via a real counting backend
    clock.advance(seconds=61)
    with pytest.raises(WorkerAuthError):
        reg.authenticate("Worker hcw_zzz", "d")
    fails = [a for a in store.audit if a["action"] == "worker.auth_failed"]
    assert len(fails) == 2
    assert "suppressed=499" in fails[1]["detail"]
    assert "hcw_" not in json.dumps(store.audit)


def test_known_worker_failures_are_coalesced_per_worker_and_reason():
    store, reg, clock = make()
    w, cred = enroll(reg, tenant="tX", user="uX")
    for _ in range(50):
        with pytest.raises(WorkerAuthError):
            reg.authenticate(f"Worker {cred}", "wrong")
    fails = [a for a in store.audit if a["action"] == "worker.auth_failed"]
    assert len(fails) == 1 and fails[0]["tenant_id"] == "tX"   # the first still audits, under its tenant
    # a different reason for the same worker is its own key
    store.workers[w.worker_id].status = "revoked"
    for _ in range(50):
        with pytest.raises(WorkerAuthError):
            reg.authenticate(f"Worker {cred}", "dev-1")
    fails = [a for a in store.audit if a["action"] == "worker.auth_failed"]
    assert len(fails) == 2 and "revoked" in fails[1]["detail"]
    clock.advance(seconds=61)
    with pytest.raises(WorkerAuthError):
        reg.authenticate(f"Worker {cred}", "dev-1")
    fails = [a for a in store.audit if a["action"] == "worker.auth_failed"]
    assert len(fails) == 3 and "suppressed=49" in fails[2]["detail"]


def failing_sink(entry) -> None:
    raise OSError("sink down")


def test_audit_sink_failure_still_yields_uniform_auth_error():
    store, reg, _ = make()
    w, cred = enroll(reg)
    store.audit_sink = failing_sink
    for auth, dev in (("garbage", "d"), (f"Worker {cred}", "wrong"), (f"Worker hcw_{'a' * 43}", "d")):
        with pytest.raises(WorkerAuthError) as e:
            reg.authenticate(auth, dev)
        assert str(e.value) == "worker authentication failed"
    with pytest.raises(WorkerAuthError):
        reg.redeem("hce_nope", device_id="d")


def test_redeem_sink_failure_does_not_consume_code():
    store, reg, _ = make()
    _, code = reg.create_enrollment(tenant_id="t1", user_id="u1", name="b", harness="pi")
    store.audit_sink = failing_sink
    with pytest.raises(OSError):
        reg.redeem(code, device_id="d")
    assert store.workers == {}
    store.audit_sink = None
    worker, cred = reg.redeem(code, device_id="d")
    assert reg.authenticate(f"Worker {cred}", "d").worker_id == worker.worker_id


@pytest.mark.parametrize("kw", [
    {"name": ""}, {"name": "   "}, {"name": "x" * 65}, {"tenant_id": ""}, {"user_id": " "},
])
def test_enrollment_input_validation(kw):
    _, reg, _ = make()
    args = {"tenant_id": "t1", "user_id": "u1", "name": "ok", "harness": "pi"} | kw
    with pytest.raises(ValueError):
        reg.create_enrollment(**args)


def test_restore_empty_and_old_format_snapshot():
    store, reg, _ = make()
    enroll(reg)
    store.restore({})
    assert store.workers == {} and store.enrollments == {}
    old = {k: v for k, v in AssistantStore().snapshot().items() if k not in ("workers", "enrollments")}
    store.restore(old)
    assert store.workers == {} and store.enrollments == {}


def test_last_seen_persistence_is_throttled():
    store, reg, clock, backend = make_counted()
    w, cred = enroll(reg)
    reg.authenticate(f"Worker {cred}", "dev-1")      # first sighting persists
    base = backend.writes
    for _ in range(5):
        clock.advance(seconds=5)
        got = reg.authenticate(f"Worker {cred}", "dev-1")
    assert backend.writes == base                     # in-memory only inside the 60 s interval
    assert got.last_seen_at == clock.t
    clock.advance(seconds=60)
    reg.authenticate(f"Worker {cred}", "dev-1")
    assert backend.writes == base + 1


def test_default_scopes_exclude_read_and_act():
    _, reg, _ = make()
    worker, _ = enroll(reg)
    assert worker.scopes == ["ask", "report"]


def test_enrollment_scopes_are_validated_deduped_sorted_and_applied():
    store, reg, _ = make()
    enrollment, code = reg.create_enrollment(tenant_id="t1", user_id="u1", name="b", harness="pi",
                                             scopes=["act", "ask", "act", "read"])
    assert enrollment.scopes == ["ask", "read", "act"]
    worker, _ = reg.redeem(code, device_id="d")
    assert worker.scopes == ["ask", "read", "act"]
    for bad in ([], ["root"], ["ask", "nope"], "ask"):
        with pytest.raises(ValueError):
            reg.create_enrollment(tenant_id="t1", user_id="u1", name="b", harness="pi", scopes=bad)


def test_existing_worker_keeps_its_stored_read_scope():
    store, reg, _ = make()
    worker, _ = enroll(reg)
    worker.scopes = ["ask", "report", "read"]                     # a Phase 1 worker
    assert store.workers[worker.worker_id].scopes == ["ask", "report", "read"]


def test_set_scopes_updates_persists_and_audits():
    store, reg, _ = make()
    worker, _ = enroll(reg)
    out = reg.set_scopes("t1", "u1", worker.worker_id, ["act", "ask"])
    assert out.scopes == ["ask", "act"] and store.workers[worker.worker_id].scopes == ["ask", "act"]
    entry = [a for a in store.audit if a["action"] == "assistant.worker.scopes_changed"][-1]
    assert entry["subject_id"] == worker.worker_id and "ask,report->ask,act" in entry["detail"]


def test_set_scopes_rejects_unknown_scope_and_empty():
    _, reg, _ = make()
    worker, _ = enroll(reg)
    for bad in ([], ["admin"], ["ask", "ACT"]):
        with pytest.raises(ValueError):
            reg.set_scopes("t1", "u1", worker.worker_id, bad)
    assert worker.scopes == ["ask", "report"]


def test_set_scopes_is_tenant_and_user_scoped():
    _, reg, _ = make()
    worker, _ = enroll(reg)
    for t, u, w in (("t2", "u1", worker.worker_id), ("t1", "u2", worker.worker_id), ("t1", "u1", "wrk_nope")):
        with pytest.raises(KeyError):
            reg.set_scopes(t, u, w, ["ask"])
    assert worker.scopes == ["ask", "report"]
