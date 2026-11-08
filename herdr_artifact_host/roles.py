"""Listener roles and the route tables each one exposes."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class Role(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    HEALTH = "health"  # loopback-only listener next to the public one


# Route tables are disjoint by construction; later tasks extend them. Internal
# routes must never be reachable on the public listener.
_ROUTES: MappingProxyType[Role, frozenset[str]] = MappingProxyType(
    {
        Role.PUBLIC: frozenset(
            {
                "GET /s/{token}",
                "GET /s/{token}/",
                "GET /s/{token}/f/{path}",
                "GET /s/{token}/signin",
                "POST /s/{token}/session",
                "GET /s/{token}/k/{session}/f/{path}",
                "GET /s/{token}/k/{session}/v/{path}",
                "GET /s/{token}/k/{session}/a/{path}",
                "GET /s/{token}/k/{session}/annotations",
                "POST /s/{token}/k/{session}/annotations",
                "POST /s/{token}/k/{session}/annotations/{id}/edit",
                "POST /s/{token}/k/{session}/annotations/{id}/delete",
                "GET /s/callback",
                "GET /s/static/{name}",
                "GET /",
                "GET /my/",
                "GET /my/callback",
                "GET /my/static/{name}",
                "GET /api/member/{path}",
                "POST /api/member/{path}",
                "PUT /api/member/{path}",
                "DELETE /api/member/{path}",
            }
        ),
        Role.INTERNAL: frozenset({"GET /internal/healthz"}),
        Role.HEALTH: frozenset({"GET /healthz"}),
    }
)


def routes_for(role: Role) -> frozenset[str]:
    return _ROUTES[role]
