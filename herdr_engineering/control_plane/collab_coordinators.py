"""Team collaboration, slice 3: the coordinator role, with limits.

Design: ``docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md`` (sections 4.6, 4.9, 4.11, 10, 10.2, 13,
14 slice 3, 15, 16). Operation and rollback: ``docs/operations/team-rooms.md``.

Everything sits behind ``control_plane.collab.coordinators.enabled`` (YAML only, absent means off, read per request, fail
closed) and also needs ``team_rooms`` and ``team_roles`` on, because a coordinator assigns by ROLE. The external-person
variant has its own second flag, ``coordinators.external.enabled``. With either off nothing changes for anybody.
"""
from __future__ import annotations

from typing import Any

COORDINATORS_KEYS = frozenset({"enabled", "kill_switch", "tasks_per_hour", "max_open", "external"})
EXTERNAL_KEYS = frozenset({"enabled", "tasks_per_hour", "max_open", "max_depth", "max_items_per_request"})


def normalize_coordinators(raw: Any) -> dict:
    """Validate ``control_plane.collab.coordinators`` (YAML only). Unknown keys and wrong types raise ValueError. Everything is
    OFF until ``enabled: true`` (and, for people, ``external.enabled: true``); ``kill_switch: true`` turns both off at once.
    The external cap must be strictly LOWER than the operator-owned cap (design section 15)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.collab.coordinators must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - COORDINATORS_KEYS)
    if unknown:
        raise ValueError("control_plane.collab.coordinators: unknown keys: " + ", ".join(unknown))
    cfg: dict[str, Any] = {"enabled": False, "kill_switch": False, "tasks_per_hour": 20, "max_open": 20}
    ranges = {"tasks_per_hour": (2, 200), "max_open": (1, 100)}
    for key, value in raw.items():
        name = f"control_plane.collab.coordinators.{key}"
        if key in ("enabled", "kill_switch"):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        elif key == "external":
            continue
        else:
            lo, hi = ranges[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer {lo}-{hi}")
        cfg[key] = value
    cfg["external"] = _normalize_external(raw.get("external"), cfg)
    return cfg


def _normalize_external(raw: Any, parent: dict) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.collab.coordinators.external must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - EXTERNAL_KEYS)
    if unknown:
        raise ValueError("control_plane.collab.coordinators.external: unknown keys: " + ", ".join(unknown))
    ext: dict[str, Any] = {"enabled": False, "tasks_per_hour": min(5, parent["tasks_per_hour"] - 1),
                           "max_open": min(5, parent["max_open"]), "max_depth": 2, "max_items_per_request": 10}
    ranges = {"tasks_per_hour": (1, 100), "max_open": (1, 50), "max_depth": (1, 3), "max_items_per_request": (1, 50)}
    for key, value in raw.items():
        name = f"control_plane.collab.coordinators.external.{key}"
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        else:
            lo, hi = ranges[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer {lo}-{hi}")
        ext[key] = value
    if ext["tasks_per_hour"] >= parent["tasks_per_hour"]:
        raise ValueError("control_plane.collab.coordinators.external.tasks_per_hour must be LOWER than "
                         "control_plane.collab.coordinators.tasks_per_hour")
    if ext["max_open"] > parent["max_open"]:
        raise ValueError("control_plane.collab.coordinators.external.max_open must not exceed "
                         "control_plane.collab.coordinators.max_open")
    return ext


def _err(status: int, message: str, **kw):
    from .collab import CollabError
    return CollabError(status, message, **kw)


class CoordinatorsMixin:
    """State and operations for slice 3. Mixed into ``CollabService`` after the slice 1 and 2 mixins."""

    def _init_coordinators(self) -> None:
        self.coordinators = normalize_coordinators(None)
        self.coordinators_source = None          # optional callable -> raw YAML mapping, read per request (hot kill switch)

    # ----------------------------------------------------------------- config (hot, fail closed)
    def coordinators_cfg(self) -> dict:
        if self.coordinators_source is None:
            return self.coordinators
        try:
            return normalize_coordinators(self.coordinators_source())
        except Exception:                                      # noqa: BLE001 - any failure is OFF
            return {**normalize_coordinators(None), "enabled": False, "kill_switch": True}

    def coordinators_active(self) -> bool:
        cfg = self.coordinators_cfg()
        return bool(cfg["enabled"] and not cfg["kill_switch"] and self.team_roles_active())

    def external_coordinators_active(self) -> bool:
        return bool(self.coordinators_active() and self.coordinators_cfg()["external"]["enabled"])
