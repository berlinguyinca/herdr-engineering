"""Listener roles and the route tables each one exposes."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class Role(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"


# Route tables are disjoint by construction; later tasks extend them. Internal
# routes must never be reachable on the public listener.
_ROUTES: MappingProxyType[Role, frozenset[str]] = MappingProxyType(
    {
        Role.PUBLIC: frozenset({"GET /healthz"}),
        Role.INTERNAL: frozenset({"GET /internal/healthz"}),
    }
)


def routes_for(role: Role) -> frozenset[str]:
    return _ROUTES[role]
