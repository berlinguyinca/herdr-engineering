"""The relay loop: provider messages become room chat, room chat becomes provider messages.

Only what an adapter is configured to accept (an allowlist the operator wrote in YAML) ever reaches the room, and text
from the room is passed to the provider as plain text with its sender named. The relay holds no provider secret beyond
what its adapter was given, and never forwards its own posts back."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .client import BridgeClient, BridgeError

log = logging.getLogger("herdr.external_bridge")
HEARTBEAT_EVERY = 30.0


@dataclass(frozen=True)
class Inbound:
    id: str            # unique per provider message: becomes the idempotency key, so a retry never double-posts
    sender: str        # a display label only; it is not an identity the room trusts
    text: str


class Adapter(Protocol):
    name: str

    def poll(self, timeout: float) -> list[Inbound]: ...
    def send(self, text: str) -> None: ...


def sender_label(event: dict) -> str:
    s = event.get("sender") or {}
    return f"{s.get('type', '?')}:{str(s.get('id', ''))[:24]}"


class Relay:
    def __init__(self, client: BridgeClient, adapter: Adapter, *, clock: Callable[[], float] = time.monotonic):
        self.client, self.adapter, self._clock = client, adapter, clock
        self._beat = -1e18

    def step(self, poll_timeout: float = 20.0) -> int:
        """One round. Returns how many messages moved."""
        moved = 0
        if self._clock() - self._beat >= HEARTBEAT_EVERY:
            self.client.heartbeat("idle", f"bridging {self.adapter.name}")
            self.client.rotate_if_needed()
            if self.client.expires_soon():
                log.warning("external bridge: the invite expires within a day; ask the room owner for a new one")
            self._beat = self._clock()
        for event in self.client.events():
            if event.get("event_type") == "message" and (event.get("sender") or {}).get("id") != self.client.agent_id:
                self.adapter.send(f"{sender_label(event)}: {event.get('text', '')}")      # a failure here is retried next round
                moved += 1
            self.client.advance(event["sequence"])
        for msg in self.adapter.poll(poll_timeout):
            if not self.client.can_post:
                log.warning("external bridge: dropped a %s message, the invite is read-only", self.adapter.name)
                continue
            self.client.post(f"[{self.adapter.name}] {msg.sender}: {msg.text}", key=f"{self.adapter.name}-{msg.id}")
            moved += 1
        return moved

    def run(self, stop: Callable[[], bool] = lambda: False, backoff: float = 5.0) -> None:
        while not stop():
            try:
                self.step()
            except BridgeError as exc:
                if exc.status in (401, 403):
                    raise              # revoked or expired: running on would only hammer the server
                log.warning("external bridge: %s; retrying", exc.message)
                time.sleep(backoff)
