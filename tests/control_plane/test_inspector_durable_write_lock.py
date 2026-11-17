"""A durable write that stalls on the database must not stall inspector readers (bender stall, 2026-11-17).

``InspectorStore.ingest`` held the store-wide lock across the durable write. When that write waits (here a real row
lock in a real Postgres, in production the sink's single asyncpg connection behind every other ingest), every reader
that needs the same lock waited with it: ``agents/list``, ``health``, ``has_session``, ``newest_seq``.
"""
import threading
import time

import pytest

from herdr_engineering.control_plane.inspector import InspectorStore
from tests.control_plane._private_pg import private_postgres

SID = "w1:p1"
MESSAGE = {"kind": "message", "event_id": "e1", "role": "assistant", "text": "hello"}


@pytest.mark.integration
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
            monitor = psycopg2.connect(dsn)
            monitor.autocommit = True
            with monitor.cursor() as probe:                                 # wait until the write is really blocked
                for _ in range(100):
                    probe.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND pid = %s",
                                  (writer.get_backend_pid(),))
                    if probe.fetchone()[0]:
                        break
                    time.sleep(0.05)
                else:
                    pytest.fail("the durable write never blocked on the row lock")
            monitor.close()

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
            assert ingest.is_alive(), "the durable write should still be blocked in Postgres"
            assert not reader.is_alive(), f"readers stalled behind the blocked durable write: {readers}"
            assert max(readers.values()) < 0.5, readers
        finally:
            blocker.rollback()
            blocker.close()
            ingest.join(5)
            writer.close()
