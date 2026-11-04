"""Models for the Rakazo mobile bridge: the gateway's own catalog, read-only, and a per-bot model choice.

The catalog is whatever the inference gateway the assistant already talks to lists at ``<endpoint>/models`` (the assistant's
``inference.endpoint`` with a trailing ``/chat/completions`` replaced). It is fetched with a short timeout, cached for a minute
and never fails a call: an unreachable gateway is an empty list. The app cannot add provider keys or sign in to providers here
(the operator owns the gateway's credentials), so those calls are refused by ``rakazo_rest``. A bot's model is the assistant's
``model_policy`` (``auto`` or ``manual:<id>``), set through ``bots/update`` ``modelId``.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.request import Request, urlopen

TTL = 60.0
TIMEOUT = 3.0
MAX_BODY = 2_000_000
MAX_MODELS = 500
PROVIDER = "gateway"


def models_url(endpoint: str) -> str | None:
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
        return None
    return re.sub(r"/chat/completions/?$", "", endpoint.rstrip("/")) + "/models"


def _fetch(url: str, key: str, timeout: float) -> bytes:
    request = Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - the operator-configured gateway, http(s) only
        return response.read(MAX_BODY + 1)


class Models:
    def __init__(self, bridge, error_type, fetch: Callable[[str, str, float], bytes] = _fetch,
                 clock: Callable[[], float] = time.monotonic):
        self.b = bridge
        self.Error = error_type
        self._fetch, self._clock = fetch, clock
        self._lock = threading.Lock()
        self._cache: tuple[float, list[dict[str, Any]]] | None = None

    def procs(self) -> dict[str, Any]:
        return {"models/list": self.list, "models/forRole": self.for_role, "models/role": self.role,
                "models/credentials": self.credentials}

    def catalog(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._cache and self._clock() - self._cache[0] < TTL:
                return self._cache[1]
        rows: list[dict[str, Any]] = []
        inference = (self.b.svc.config or {}).get("inference") or {}
        url = models_url(inference.get("endpoint", ""))
        if url:
            try:
                raw = self._fetch(url, inference.get("api_key", ""), TIMEOUT)
                data = json.loads(raw[:MAX_BODY]).get("data") if len(raw) <= MAX_BODY else None
                for item in data if isinstance(data, list) else []:
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] and len(rows) < MAX_MODELS:
                        rows.append(self._view(item))
            except Exception:  # noqa: BLE001 - an unreachable or odd gateway is an empty catalog, never an error
                rows = []
        with self._lock:
            self._cache = (self._clock(), rows)
        return rows

    @staticmethod
    def _view(item: dict[str, Any]) -> dict[str, Any]:
        return {"provider": PROVIDER, "providerName": "Herdr gateway", "id": item["id"][:512],
                "label": str(item.get("name") or item["id"])[:200], "billing": "managed", "protocol": "chat",
                "managed": True, "credentialIds": [], "live": True}

    def list(self, ctx, inp):
        return self.catalog()

    def for_role(self, ctx, inp):
        if inp.get("role") not in ("main", "approval"):
            raise self.Error(400, "BAD_REQUEST", "role must be main or approval.")
        return self.catalog()

    def role(self, ctx, inp):
        if inp.get("role") not in ("main", "approval"):
            raise self.Error(400, "BAD_REQUEST", "role must be main or approval.")
        return {"primary": None, "backups": []}

    def credentials(self, ctx, inp):
        return []
