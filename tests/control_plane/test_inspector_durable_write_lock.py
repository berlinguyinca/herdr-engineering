"""A durable write that stalls on the database must not stall inspector readers (bender stall, 2026-11-17).

``InspectorStore.ingest`` held the store-wide lock across the durable write. When that write waits (here a real row
lock in a real Postgres, in production the sink's single asyncpg connection behind every other ingest), every reader
that needs the same lock waited with it: ``agents/list``, ``health``, ``has_session``, ``newest_seq``.
"""
import contextlib
import threading
import time

import pytest

from herdr_engineering.control_plane.inspector import InspectorStore
from tests.control_plane._private_pg import private_postgres

SID = "w1:p1"


@contextlib.contextmanager
def monitor_connection(dsn):
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _blocked(monitor, conn) -> bool:
    with monitor.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND pid = %s",
                    (conn.get_backend_pid(),))
        return bool(cur.fetchone()[0])
MESSAGE = {"kind": "message", "event_id": "e1", "role": "assistant", "text": "hello"}


def test_readers_do_not_wait_for_a_durable_write_blocked_in_postgres(tmp_path):
    psycopg2 = pytest.importorskip("psycopg2")
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE journal (id int PRIMARY KEY, n int); INSERT INTO journal VALUES (1, 0);")
    with private_postgres(schema) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")
        blocker = psycopg2.connect(dsn)
        writer = psycopg2.connect(dsn)
        try:
            with blocker.cursor() as cur:
                cur.execute("UPDATE journal SET n = 1 WHERE id = 1")      # holds the row lock until blocker.rollback()

            def persist(_event):
                with writer.cursor() as cur:
                    cur.execute("UPDATE journal SET n = 2 WHERE id = 1")   # a real write that now waits on that lock
                writer.commit()

            store = InspectorStore(persist=persist)
            store.ingest(SID, {"events": [{"kind": "lifecycle", "phase": "started"}]}, persist=False)
            ingest = threading.Thread(target=lambda: store.ingest(SID, {"events": [MESSAGE]}), daemon=True)
            ingest.start()
            with monitor_connection(dsn) as probe:                          # wait until the write is really blocked
                for _ in range(100):
                    if _blocked(probe, writer):
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("the durable write never blocked on the row lock")

            readers: dict[str, float] = {}

            def read():
                for name, call in (("has_session", lambda: store.has_session(SID)), ("health", store.health),
                                   ("newest_seq", lambda: store.newest_seq(SID)),
                                   ("phone_digest", lambda: store.phone_digest([SID]))):
                    t0 = time.monotonic()
                    call()
                    readers[name] = time.monotonic() - t0

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            reader.join(3)
            assert ingest.is_alive(), "ingest finished although its write is blocked on the row lock"
            with monitor_connection(dsn) as probe:       # still blocked now, after the readers ran
                assert _blocked(probe, writer), "the durable write is no longer blocked in Postgres"
            assert not reader.is_alive(), f"readers stalled behind the blocked durable write: {readers}"
            assert max(readers.values()) < 0.5, readers
        finally:
            blocker.rollback()
            blocker.close()
            ingest.join(5)
            writer.close()


# ---------------------------------------------------------------- ordering and safety of the unlocked window
class Journal:
    """A real Postgres journal. ``hook`` is the store's persist callable; ``gate`` can park a chosen write."""

    def __init__(self, dsn):
        import psycopg2
        self.conn = psycopg2.connect(dsn)
        self.park = None                 # predicate(event) -> bool: park this write until ``release`` is set
        self.parked = threading.Event()
        self.release = threading.Event()
        self.fail = None                 # predicate(event) -> bool: make this write fail in Postgres
        self._lock = threading.Lock()

    def hook(self, ev):
        with self._lock:                 # one connection; a real driver connection is not thread safe
            pass
        if self.park and self.park(ev):
            self.parked.set()
            assert self.release.wait(10)
        with self._lock, self.conn.cursor() as cur:
            try:
                if self.fail and self.fail(ev):
                    cur.execute("INSERT INTO no_such_table VALUES (1)")
                cur.execute("INSERT INTO journal2 (tag) VALUES (%s)", (_tag(ev),))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def tags(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT tag FROM journal2 ORDER BY id")
            rows = [r[0] for r in cur.fetchall()]
        self.conn.rollback()
        return rows


def _tag(ev):
    p = ev.get("payload") or {}
    if ev.get("event_type") == "inspector.preferences":
        return f"prefs:{p['prefs']['retention_hours']}"
    return f"{p.get('kind')}:{p.get('event_id') or p.get('call_id')}:{p.get('phase', '')}"


@pytest.fixture
def journal(tmp_path):
    pytest.importorskip("psycopg2")
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE journal2 (id serial PRIMARY KEY, tag text);")
    with private_postgres(schema) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")
        j = Journal(dsn)
        try:
            yield j
        finally:
            j.release.set()
            j.conn.close()


def _msg(i):
    return {"kind": "message", "event_id": f"m{i}", "role": "assistant", "text": f"t{i}"}


def test_sweep_does_not_let_an_ingest_interleave_with_an_eviction(journal):
    clock = [1000.0]
    store = InspectorStore(persist=journal.hook, limits={"retention_hours": 1}, clock=lambda: clock[0])
    call = {"kind": "tool", "call_id": "c1", "tool": "bash", "args_preview": "ls"}
    store.ingest(SID, {"events": [{**call, "phase": "start"}, {**call, "phase": "update", "output": "a"}]})
    clock[0] += 7200                                         # the call's entry is now past retention
    journal.park = lambda ev: (ev.get("payload") or {}).get("phase") == "update"   # the held update is written on eviction
    sweeper = threading.Thread(target=store.sweep, daemon=True)
    sweeper.start()
    assert journal.parked.wait(5), "sweep never wrote the held update"
    ingest = threading.Thread(target=lambda: store.ingest(SID, {"events": [{**call, "phase": "update", "output": "b"}]}),
                              daemon=True)
    ingest.start()
    ingest.join(0.7)
    assert ingest.is_alive(), "an ingest ran in the middle of sweep's eviction"
    journal.release.set()
    sweeper.join(5)
    ingest.join(5)
    assert not sweeper.is_alive() and not ingest.is_alive()
    sess = store._sessions[SID]
    assert all(e["seq"] in sess.by_seq for e in sess.tools.values())
    assert [e["data"]["call_id"] for e in sess.events if e["kind"] == "tool"] == ["c1"]    # one row, not a duplicate


def test_concurrent_set_prefs_journal_in_the_order_memory_applied_them(journal):
    store = InspectorStore(persist=journal.hook)
    journal.park = lambda ev: ev.get("event_type") == "inspector.preferences" and ev["payload"]["prefs"]["retention_hours"] == 5
    a = threading.Thread(target=lambda: store.set_prefs({"retention_hours": 5}), daemon=True)
    a.start()
    assert journal.parked.wait(5)
    b = threading.Thread(target=lambda: store.set_prefs({"retention_hours": 6}), daemon=True)
    b.start()
    b.join(0.7)
    journal.release.set()
    a.join(5)
    b.join(5)
    assert journal.tags() == ["prefs:5", "prefs:6"]
    assert store.get_prefs()["retention_hours"] == 6


def test_concurrent_ingest_journals_in_sequence_order(journal):
    store = InspectorStore(persist=journal.hook)
    threads = [threading.Thread(target=lambda i=i: store.ingest(SID, {"events": [_msg(i)]}), daemon=True) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    in_memory = [f"message:{e['event_id']}:" for e in store._sessions[SID].events]
    assert len(in_memory) == 12 and journal.tags() == in_memory


def test_a_failed_durable_write_leaves_the_lock_held_by_nobody_and_state_unchanged(journal):
    store = InspectorStore(persist=journal.hook)
    store.ingest(SID, {"events": [_msg(0)]})
    before = (store.newest_seq(SID), journal.tags())
    journal.fail = lambda ev: True
    with pytest.raises(RuntimeError):
        store.ingest(SID, {"events": [_msg(1)]})
    assert not store._lock._is_owned()                       # type: ignore[attr-defined]  # released and restored cleanly
    assert not store._ingest_lock._is_owned()                # type: ignore[attr-defined]
    seen = []
    t = threading.Thread(target=lambda: seen.append(store.newest_seq(SID)), daemon=True)
    t.start()
    t.join(2)
    assert seen == [before[0]] and journal.tags() == before[1]
    journal.fail = None
    store.ingest(SID, {"events": [_msg(1)]})                 # the failed id was left unseen, so the retry is accepted
    assert store.newest_seq(SID) == before[0] + 1


def test_a_reentrant_store_lock_is_fully_released_and_restored_to_the_same_depth(journal):
    store = InspectorStore(persist=journal.hook)
    free = []

    def probe_from_another_thread(ev):
        def take_and_give_back():
            if store._lock.acquire(timeout=2):             # released by the thread that took it
                store._lock.release()
                free.append(True)
        t = threading.Thread(target=take_and_give_back, daemon=True)
        t.start()
        t.join(3)

    journal.park = lambda ev: (probe_from_another_thread(ev), False)[1]
    with store._lock:
        with store._lock:
            with store._lock:
                store._persist_unlocked({"entity_type": "inspector", "payload": {"kind": "x", "event_id": "r"}})
            assert store._lock._is_owned()                 # type: ignore[attr-defined]  # depth 3 restored, now at 2
        assert store._lock._is_owned()                     # type: ignore[attr-defined]  # and 1
    assert free == [True]                                  # other threads could take it during the write
    assert not store._lock._is_owned()                     # type: ignore[attr-defined]  # depth came back exactly
