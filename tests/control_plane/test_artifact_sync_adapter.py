import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter


class _AsyncStore:
    def __init__(self):
        self.loop = asyncio.get_running_loop()

    async def ping(self, value):
        assert asyncio.get_running_loop() is self.loop
        return {"value": value, "loop": id(self.loop)}


def test_sync_adapter_owns_one_persistent_event_loop_for_store_calls():
    adapter = SyncArtifactAdapter(lambda: _AsyncStore())
    try:
        first = adapter("ping", "one")
        second = adapter("ping", "two")
        assert first["value"] == "one"
        assert second["value"] == "two"
        assert first["loop"] == second["loop"]
    finally:
        adapter.close()


class _SingleConnectionStore:
    """Enforce the exclusive-operation contract of one asyncpg connection."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.thread_id = threading.get_ident()
        self.active = False
        self.events = []

    async def transaction(self, value, *, fail=False):
        assert asyncio.get_running_loop() is self.loop
        assert threading.get_ident() == self.thread_id
        if self.active:
            raise RuntimeError("another operation is in progress")
        self.active = True
        self.events.append((value, "begin"))
        try:
            await asyncio.sleep(0.005)
            self.events.append((value, "statement"))
            await asyncio.sleep(0.005)
            if fail:
                raise ValueError("transaction failed")
            return value
        finally:
            self.events.append((value, "end"))
            self.active = False

    async def snapshot(self):
        return list(self.events)


def test_concurrent_http_threads_serialize_whole_store_transactions():
    adapter = SyncArtifactAdapter(_SingleConnectionStore)
    barrier = threading.Barrier(8)

    def request(value):
        barrier.wait(timeout=5)
        return adapter("transaction", value)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(request, range(8))) == list(range(8))
        events = adapter("snapshot")
        assert len(events) == 24
        for offset in range(0, len(events), 3):
            value = events[offset][0]
            assert events[offset:offset + 3] == [(value, "begin"), (value, "statement"), (value, "end")]
    finally:
        adapter.close()


def test_operation_failure_releases_connection_and_preserves_error_semantics():
    adapter = SyncArtifactAdapter(_SingleConnectionStore)
    try:
        with pytest.raises(ValueError, match="transaction failed"):
            adapter("transaction", "failed", fail=True)
        assert adapter("transaction", "next") == "next"
        with pytest.raises(ValueError, match="invalid artifact store method"):
            adapter("_private")
        with pytest.raises(AttributeError, match="unavailable"):
            adapter("unknown")
    finally:
        adapter.close()
    with pytest.raises(RuntimeError, match="closed"):
        adapter("transaction", "closed")

