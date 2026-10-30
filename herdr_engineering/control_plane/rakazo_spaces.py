"""Rakazo spaces for the mobile bridge: a space is a hard data partition of one account, realised as a derived assistant tenant.

The assistant store already isolates every record by ``(tenant_id, user_id)`` (assistants, threads, messages, runs, memory,
routines, connectors, grants, approvals and the audit chain), so a space needs no column and no migration of those records.
The account's own tenant IS its default space: its data and the dashboard's view of it are untouched. Any other space is the
tenant ``<account tenant>~<space id>``, which the assistant store treats as a different tenant, so nothing crosses unless a
later feature explicitly allows it.

This module owns only the registry of spaces (id, name, owner, overrides, tombstone) and, optionally, its table
(``rakazo_spaces``, migration 0037). Memory is authoritative; the table lets spaces survive a restart. Database I/O never runs
under the lock that readers take.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_SPACE_ID = "herdr"
DEFAULT_SPACE_NAME = "Herdr"
MAX_SPACES = 10                       # extra spaces per account, besides the default one
MAX_NAME = 60                         # the app's spaceName limit
SETTINGS = ("models", "voice", "confirmations", "credentials")      # settings-scope.ts SpaceSettingSchema
_SEPARATOR = "~"


class SpaceError(Exception):
    """A space request the registry refuses. ``status`` is the HTTP status the bridge answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Space:
    space_id: str
    account_id: str
    name: str
    created: float
    overrides: frozenset = frozenset()       # settings this space customises; empty means "use the account's"
    deleted: bool = False


def space_tenant(base_tenant: str, space_id: str) -> str:
    """The assistant tenant that holds a space. The default space is the account's own tenant."""
    return base_tenant if space_id == DEFAULT_SPACE_ID else f"{base_tenant}{_SEPARATOR}{space_id}"


def clean_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_NAME:
        raise SpaceError(400, f"A space needs a name of 1 to {MAX_NAME} characters.")
    name = " ".join(value.split())
    if any(ord(c) < 32 for c in name):
        raise SpaceError(400, "A space name may not contain control characters.")
    return name


class SpaceRegistry:
    """Spaces by account. ``persistence`` (optional) needs ``load() -> [row]`` and ``upsert(row)``; a failed write raises
    :class:`SpaceError` 503 and changes nothing, so the table and memory never disagree."""

    def __init__(self, persistence=None, clock: Callable[[], float] = time.time):
        self._persistence = persistence
        self._clock = clock
        self._lock = threading.Lock()              # memory only: readers take this, never a database call
        self._write = threading.Lock()             # serialises writers, which may call the database
        self._spaces: dict[str, Space] = {}
        if persistence is not None:
            try:
                for row in persistence.load():
                    self._spaces[row["space_id"]] = Space(row["space_id"], row["account_id"], row["name"],
                                                          row["created"], frozenset(row.get("overrides") or ()),
                                                          bool(row.get("deleted")))
            except Exception as exc:  # noqa: BLE001 - start without them rather than not at all; the log says why
                log.error("rakazo spaces: could not load the space table (%s); extra spaces are unavailable until it can",
                          type(exc).__name__)

    # reads -------------------------------------------------------------------------------------------------
    def list(self, account_id: str) -> list[Space]:
        with self._lock:
            return sorted((s for s in self._spaces.values() if s.account_id == account_id and not s.deleted),
                          key=lambda s: (s.created, s.space_id))

    def get(self, account_id: str, space_id: str) -> Space | None:
        """The account's own live space, or None. Another account's space and a deleted one are both None."""
        with self._lock:
            found = self._spaces.get(space_id)
        return found if found and found.account_id == account_id and not found.deleted else None

    # writes ------------------------------------------------------------------------------------------------
    def _commit(self, space: Space) -> None:
        if self._persistence is not None:
            try:
                self._persistence.upsert({"space_id": space.space_id, "account_id": space.account_id,
                                          "name": space.name, "created": space.created,
                                          "overrides": sorted(space.overrides), "deleted": space.deleted})
            except Exception as exc:  # noqa: BLE001
                log.error("rakazo spaces: could not write the space table (%s)", type(exc).__name__)
                raise SpaceError(503, "The space could not be saved. Try again.") from None
        with self._lock:
            self._spaces[space.space_id] = space

    def create(self, account_id: str, name: Any) -> Space:
        name = clean_name(name)
        with self._write:
            live = self.list(account_id)
            if len(live) >= MAX_SPACES:
                raise SpaceError(400, f"This server allows at most {MAX_SPACES} extra spaces per account.")
            if any(s.name.casefold() == name.casefold() for s in live) or name.casefold() == DEFAULT_SPACE_NAME.casefold():
                raise SpaceError(400, "You already have a space with that name.")
            space = Space("spc" + secrets.token_hex(10), account_id, name, self._clock())
            self._commit(space)
            return space

    def rename(self, account_id: str, space_id: str, name: Any) -> Space:
        name = clean_name(name)
        with self._write:
            space = self._owned(account_id, space_id)
            if any(s.space_id != space_id and s.name.casefold() == name.casefold() for s in self.list(account_id)):
                raise SpaceError(400, "You already have a space with that name.")
            renamed = replace(space, name=name)
            self._commit(renamed)
            return renamed

    def remove(self, account_id: str, space_id: str) -> Space:
        """Tombstone the space: its id is never reused and nothing it held is deleted here (the bridge archives first)."""
        with self._write:
            gone = replace(self._owned(account_id, space_id), deleted=True)
            self._commit(gone)
            return gone

    def set_override(self, account_id: str, space_id: str, setting: Any, enabled: Any) -> Space:
        if setting not in SETTINGS or not isinstance(enabled, bool):
            raise SpaceError(400, "Unknown space setting.")
        with self._write:
            space = self._owned(account_id, space_id)
            changed = replace(space, overrides=(space.overrides | {setting}) if enabled else (space.overrides - {setting}))
            self._commit(changed)
            return changed

    def _owned(self, account_id: str, space_id: str) -> Space:
        if space_id == DEFAULT_SPACE_ID:
            raise SpaceError(400, "The default space cannot be changed or deleted.")
        space = self.get(account_id, space_id)
        if space is None:
            raise SpaceError(404, "Not found.")
        return space
