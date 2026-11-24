"""Assistant store durability and concurrency against a real, private Postgres.

* a snapshot that cannot be read at boot must not be mistaken for "no snapshot yet" (it would be overwritten),
* concurrent mutations must not let an older snapshot be the last one written,
* readers must not iterate the store dicts while a writer inserts, and a decision is decided once.
"""
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.assistant import (
    AssistantAction,
    AssistantApproval,
    AssistantError,
    AssistantGrant,
    AssistantService,
    AssistantStore,
    PostgresStoreBackend,
    StoreLoadError,
    assistant_config,
)
from tests.control_plane._private_pg import private_postgres

SCHEMA = """CREATE TABLE pa_store_snapshot (key text PRIMARY KEY, data jsonb NOT NULL,
                                           updated_at timestamptz NOT NULL DEFAULT now());"""


@pytest.fixture
def dsn(tmp_path):
    pytest.importorskip("psycopg2")
    schema = tmp_path / "schema.sql"
    schema.write_text(SCHEMA)
    with private_postgres(schema) as url:
        if url is None:
            pytest.skip("no postgres binaries")
        yield url


def _grant(i: int) -> AssistantGrant:
    return AssistantGrant(grant_id=f"g{i}", tenant_id="t", user_id="u", assistant_id="a", tool_id="x", scope="s")


def _stored_grants(dsn: str) -> set[str]:
    snap = PostgresStoreBackend(dsn).load() or {}
    return {g["grant_id"] for g in snap.get("grants", [])}


# -- finding 1: a failed load is not "no snapshot" ----------------------------------------------------------------

def test_load_distinguishes_no_row_from_unreadable(dsn):
    assert PostgresStoreBackend(dsn).load() is None                       # no row yet: fine
    unreachable = PostgresStoreBackend("postgresql://t@/postgres?host=/nonexistent-pg-socket")
    with pytest.raises(StoreLoadError):
        unreachable.load()


def test_unreadable_snapshot_at_boot_fails_instead_of_running_empty(dsn):
    seeded = AssistantStore(persist_backend=PostgresStoreBackend(dsn))
    seeded.put_grant(_grant(1))
    broken = AssistantStore(persist_backend=PostgresStoreBackend("postgresql://t@/postgres?host=/nonexistent-pg-socket"))
    with pytest.raises(StoreLoadError):
        broken.load_persisted()
    with pytest.raises(StoreLoadError):
        AssistantService(config=assistant_config({"personal_assistant": {
            "enabled": True, "persistence": {"dsn": "postgresql://t@/postgres?host=/nonexistent-pg-socket"}}}))
    assert _stored_grants(dsn) == {"g1"}                                  # the stored snapshot is untouched


# -- finding 32: an older snapshot never lands after a newer one ---------------------------------------------------

class GatedBackend(PostgresStoreBackend):
    """A real Postgres backend whose first write waits, as a slow connection would."""

    def __init__(self, dsn: str):
        super().__init__(dsn)
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self._n = 0
        self._n_lock = threading.Lock()

    def persist_required(self, snapshot):
        with self._n_lock:
            self._n += 1
            first = self._n == 1
        if first:
            self.first_started.set()
            assert self.release_first.wait(10)
        super().persist_required(snapshot)


def test_a_slow_older_write_cannot_overwrite_a_newer_snapshot(dsn):
    backend = GatedBackend(dsn)
    store = AssistantStore(persist_backend=backend)
    a = threading.Thread(target=store.put_grant, args=(_grant(1),), daemon=True)
    a.start()
    assert backend.first_started.wait(5)                                  # A holds snapshot S1, its write is in flight
    b = threading.Thread(target=store.put_grant, args=(_grant(2),), daemon=True)
    b.start()
    b.join(0.5)                                                           # B must queue behind A (or be superseded)
    backend.release_first.set()
    a.join(10)
    b.join(10)
    assert not a.is_alive() and not b.is_alive()
    assert _stored_grants(dsn) == {"g1", "g2"}


# -- finding 33: readers and writers on the shared dicts -----------------------------------------------------------

def _service() -> AssistantService:
    return AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "actions": {"allow_self_approval": True}}}))


def test_listing_while_another_thread_inserts_never_raises():
    svc = _service()
    store = svc.store
    errors: list[BaseException] = []
    stop = threading.Event()
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    def writer():
        i = 0
        while not stop.is_set() and i < 4000:
            store.put_grant(_grant(i))
            store.put_approval(AssistantApproval(approval_id=f"p{i}", action_id=f"a{i}", tenant_id="t", user_id="u",
                                                 assistant_id="a", actor_id="u"))
            i += 1

    def reader():
        try:
            while not stop.is_set():
                store.list_grants(tenant_id="t", user_id="u")
                store.list_approvals(tenant_id="t", user_id="u")
                svc.list_approvals(tenant_id="t", user_id="u")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    w = threading.Thread(target=writer, daemon=True)
    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    try:
        for t in readers:
            t.start()
        w.start()
        w.join(60)
    finally:
        stop.set()
        sys.setswitchinterval(old)
        for t in readers:
            t.join(10)
    assert errors == []


def test_two_concurrent_decisions_on_one_approval_decide_once():
    entered, release = threading.Event(), threading.Event()

    def clock():
        # the first decider parks on its first clock read, i.e. after it has seen the approval pending
        if threading.current_thread().name == "decider-1" and not entered.is_set():
            entered.set()
            release.wait(10)
        return datetime.now(UTC)

    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "actions": {"allow_self_approval": True}}}), now=clock)
    now = datetime.now(UTC)
    store = svc.store
    store.put_action(AssistantAction(action_id="act", run_id="r", tool_id="t.x", scope="s", request_hash="h",
                                     idempotency_key="k", tenant_id="t", user_id="u", assistant_id="a",
                                     expires_at=now + timedelta(hours=1)))
    store.put_approval(AssistantApproval(approval_id="ap", action_id="act", tenant_id="t", user_id="u",
                                         assistant_id="a", actor_id="u", expires_at=now + timedelta(hours=1)))
    results: list[str] = []

    def decide():
        try:
            svc.decide_approval("ap", tenant_id="t", user_id="u", decision="approved", actor="u")
            results.append("ok")
        except AssistantError as exc:
            results.append(str(exc))

    t1 = threading.Thread(target=decide, daemon=True, name="decider-1")
    t1.start()
    assert entered.wait(5)
    t2 = threading.Thread(target=decide, daemon=True, name="decider-2")
    t2.start()
    t2.join(0.5)
    release.set()
    t1.join(10)
    t2.join(10)
    assert sorted(results) == ["approval already approved", "ok"]
