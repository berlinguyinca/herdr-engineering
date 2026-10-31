"""Which model the assistant asks the gateway for: whatever the gateway serves now, in the operator's order of preference.

``inference.model`` may be a model id, ``auto`` or empty. A pinned id that the gateway still serves is used first; one it no
longer serves (or ``auto``) gives way to ``inference.preferred_models`` (ids or ``*`` patterns, first match wins), then to any
served text model. The gateway's catalog (``<endpoint>/models``) is cached for a minute; an unreachable gateway keeps a pinned
model as it is and offers nothing for ``auto``, so a catalog outage never changes what a working configuration asks for.
"""
from __future__ import annotations

import fnmatch
import json
import re
import threading
import time
from collections.abc import Callable
from urllib.request import Request, urlopen

TTL = 60.0
TIMEOUT = 3.0
MAX_BODY = 2_000_000
MAX_MODELS = 500
MAX_CANDIDATES = 4
FAILED_TTL = 5.0
RETRY_CODES = frozenset({404, 429, 500, 502, 503, 504})
NON_TEXT = re.compile(r"modality-(?!text)|embed|rerank|whisper", re.I)


def _fetch(url: str, key: str, timeout: float) -> bytes:
    request = Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - the operator-configured gateway, http(s) only
        return response.read(MAX_BODY + 1)


class ModelResolver:
    def __init__(self, endpoint: str, api_key: str, preferred: list[str],
                 fetch: Callable[[str, str, float], bytes] = _fetch, clock: Callable[[], float] = time.monotonic):
        self.url = re.sub(r"/chat/completions/?$", "", endpoint.rstrip("/")) + "/models"
        self.api_key, self.preferred = api_key, list(preferred)
        self._fetch, self._clock = fetch, clock
        self._lock = threading.Lock()
        self._cache: tuple[float, list[str], float] | None = None

    def served(self) -> list[str]:
        with self._lock:
            if self._cache and self._clock() - self._cache[0] < self._cache[2]:
                return list(self._cache[1])
        ids: list[str] = []
        try:
            raw = self._fetch(self.url, self.api_key, TIMEOUT)
            data = json.loads(raw[:MAX_BODY]).get("data") if len(raw) <= MAX_BODY else None
            ids = [i["id"] for i in (data if isinstance(data, list) else [])[:MAX_MODELS]
                   if isinstance(i, dict) and isinstance(i.get("id"), str) and i["id"]]
        except Exception:  # noqa: BLE001 - an unreachable or odd gateway is an empty catalog, never an error
            ids = []
        with self._lock:
            self._cache = (self._clock(), ids, TTL if ids else FAILED_TTL)
        return ids

    def candidates(self, wanted: str) -> list[str]:
        """Model ids to try, best first. A pinned model is always tried when the catalog is unavailable."""
        served = self.served()
        pinned = wanted not in ("", "auto")
        out: list[str] = [wanted] if pinned and (wanted in served or not served) else []
        for pattern in self.preferred:
            out += [m for m in served if fnmatch.fnmatchcase(m, pattern)]
        out += [m for m in served if not NON_TEXT.search(m)]
        return list(dict.fromkeys(out))[:MAX_CANDIDATES]
