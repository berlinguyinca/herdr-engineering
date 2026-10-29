"""Synchronous HTTP boundary for an async durable ArtifactStore.

The control-plane HTTP server is stdlib/threaded. This adapter owns the async
store and its event loop on one dedicated thread, so asyncpg connections and
RustFS clients are never moved between request loops.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable


class SyncArtifactAdapter:
    def __init__(self, store_factory: Callable[[], object]):
        self._ready = threading.Event()
        self._closed = False
        self._error: BaseException | None = None
        self._store = None
        self._operation_lock = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._bootstrap,
                                        args=(store_factory,), daemon=True,
                                        name="herdr-artifact-store")
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            raise RuntimeError("artifact store adapter failed to start") from self._error

    def _bootstrap(self, store_factory):
        asyncio.set_event_loop(self._loop)
        try:
            async def create():
                self._operation_lock = asyncio.Lock()
                result = store_factory()
                return await result if hasattr(result, "__await__") else result
            self._store = self._loop.run_until_complete(create())
        except BaseException as exc:  # surfaced by constructor
            self._error = exc
        finally:
            self._ready.set()
        if self._error is None:
            self._loop.run_forever()

    def __call__(self, method: str, *args, **kwargs):
        if self._closed:
            raise RuntimeError("artifact store adapter is closed")
        if not method or method.startswith("_"):
            raise ValueError("invalid artifact store method")
        function = getattr(self._store, method, None)
        if function is None or not callable(function):
            raise AttributeError(f"artifact store method is unavailable: {method}")
        async def dispatch():
            # One store owns one asyncpg connection. Keep the entire method under
            # the lock so separate HTTP requests cannot overlap statements or
            # enter each other's multi-statement transactions on that connection.
            async with self._operation_lock:
                return await function(*args, **kwargs)

        future = asyncio.run_coroutine_threadsafe(dispatch(), self._loop)
        return future.result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()

