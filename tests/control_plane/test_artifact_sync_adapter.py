import asyncio

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

