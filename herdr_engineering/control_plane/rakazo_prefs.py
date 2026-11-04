"""Small per-account, per-space settings for the Rakazo mobile bridge (pinned or archived groups, read markers, trusted link
sites, ...). Memory is authoritative; an optional table (``rakazo_prefs``, migration 0038) makes them survive a restart. A
failed write changes nothing and raises :class:`PrefsError` (503). Database I/O never runs under the readers' lock."""
from __future__ import annotations

import copy
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)
MAX_PER_KIND = 2000
MAX_VALUE_CHARS = 20_000


class PrefsError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class PrefsStore:
    """``persistence`` needs ``load() -> [row]``, ``upsert(row)`` and ``delete(account, space, kind, key)``."""

    def __init__(self, persistence=None):
        self._persistence = persistence
        self._lock = threading.Lock()
        self._write = threading.Lock()
        self._data: dict[tuple[str, str, str], dict[str, Any]] = {}
        if persistence is not None:
            try:
                for row in persistence.load():
                    self._data.setdefault((row["account_id"], row["space_id"], row["kind"]), {})[row["key"]] = row["value"]
            except Exception as exc:  # noqa: BLE001
                log.error("rakazo prefs: could not load the table (%s); settings start empty", type(exc).__name__)

    def get(self, account: str, space: str, kind: str, key: str, default: Any = None) -> Any:
        with self._lock:
            found = self._data.get((account, space, kind), {}).get(key, default)
        return copy.deepcopy(found)

    def items(self, account: str, space: str, kind: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data.get((account, space, kind), {}))

    def put(self, account: str, space: str, kind: str, key: str, value: Any) -> None:
        if not (0 < len(key) <= 200) or len(repr(value)) > MAX_VALUE_CHARS:
            raise PrefsError(400, "That setting is too large.")
        with self._write:
            with self._lock:
                bucket = self._data.get((account, space, kind), {})
                if key not in bucket and len(bucket) >= MAX_PER_KIND:
                    raise PrefsError(400, "Too many saved settings of this kind.")
            if self._persistence is not None:
                try:
                    self._persistence.upsert({"account_id": account, "space_id": space, "kind": kind, "key": key,
                                              "value": value})
                except Exception as exc:  # noqa: BLE001
                    log.error("rakazo prefs: could not write the table (%s)", type(exc).__name__)
                    raise PrefsError(503, "The setting could not be saved. Try again.") from None
            with self._lock:
                self._data.setdefault((account, space, kind), {})[key] = copy.deepcopy(value)

    def delete(self, account: str, space: str, kind: str, key: str) -> None:
        with self._write:
            if self._persistence is not None:
                try:
                    self._persistence.delete(account, space, kind, key)
                except Exception as exc:  # noqa: BLE001
                    log.error("rakazo prefs: could not write the table (%s)", type(exc).__name__)
                    raise PrefsError(503, "The setting could not be saved. Try again.") from None
            with self._lock:
                self._data.get((account, space, kind), {}).pop(key, None)

    def drop_space(self, account: str, space: str) -> None:
        """Forget every setting of a removed space."""
        with self._write:
            with self._lock:
                keys = [(k, key) for k in self._data if k[0] == account and k[1] == space for key in self._data[k]]
            for k, key in keys:
                self.delete(k[0], k[1], k[2], key)
