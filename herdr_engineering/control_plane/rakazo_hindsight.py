"""Optional link from the Rakazo app's memory to Hindsight, through the existing ``herdr-memory`` client.

The assistant memory service stays the app's source of truth. When the operator configures
``control_plane.rakazo_bridge.hindsight`` ({url, token_file, project, cache_dir}), two things also happen:

* a fact the person edits or removes in the app is saved to / forgotten in the shared ``fact`` bank (an explicit save, the
  document id is the memory id), so the shared memory follows the person's own corrections;
* ``search/query`` adds semantic recall hits from that bank.

Saves go through a small bounded worker so a slow or down service never delays the app (its calls time out at 8 s); the
herdr-memory client keeps its own durable outbox. Memory text is never logged. Absent configuration, nothing here runs.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any

log = logging.getLogger(__name__)
MAX_QUEUE = 200
RECALL_TIMEOUT = 2


class Mirror:
    def __init__(self, client):
        self.client = client
        self._jobs: queue.Queue = queue.Queue(MAX_QUEUE)
        threading.Thread(target=self._work, name="rakazo-hindsight", daemon=True).start()

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job()
            except Exception as exc:  # noqa: BLE001 - the client's outbox owns retries; never log the text
                log.warning("rakazo bridge: hindsight write failed (%s)", type(exc).__name__)
            finally:
                self._jobs.task_done()

    def wait(self) -> None:
        """Block until queued writes have been attempted (used by tests and shutdown)."""
        self._jobs.join()

    def _submit(self, job) -> None:
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            log.warning("rakazo bridge: hindsight queue is full; a write was skipped")

    def retain(self, memory_id: str, content: str) -> None:
        self._submit(lambda: self.client.retain(content, kind="fact", document_id=memory_id, explicit=True))

    def forget(self, memory_id: str) -> None:
        self._submit(lambda: self.client.forget(memory_id, kind="fact"))

    def recall(self, query: str, limit: int = 5) -> list[str]:
        try:
            data = self.client.recall(query, kind="fact")["data"]
        except Exception as exc:  # noqa: BLE001 - search must not fail because memory is down
            log.info("rakazo bridge: hindsight recall unavailable (%s)", type(exc).__name__)
            return []
        texts = [str(item.get("text") or item.get("content") or "") for item in data.get("results", [])]
        return [t for t in texts if t][:limit]


def build(config: dict[str, Any] | None):
    """A :class:`Mirror` for the configured service, or None. The herdr-memory package must be importable."""
    if not config:
        return None
    from herdr_memory.client import (
        Client,  # noqa: PLC0415 - optional dependency, only when configured
    )
    client = Client(url=config["url"], token_file=config["token_file"], project=config["project"], harness="claude",
                    cache_dir=config["cache_dir"], timeout=RECALL_TIMEOUT)
    return Mirror(client)
