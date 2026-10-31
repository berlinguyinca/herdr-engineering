import asyncio
from concurrent.futures import ThreadPoolExecutor

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


def test_parallel_http_calls_serialize_operations_on_the_single_database_connection():
    class Store:
        def __init__(self):
            self.active = 0
            self.maximum = 0

        async def read(self):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(.01)
            self.active -= 1
            return self.maximum

    adapter = SyncArtifactAdapter(Store)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: adapter('read'), range(8)))
        assert results == [1] * 8
    finally:
        adapter.close()

