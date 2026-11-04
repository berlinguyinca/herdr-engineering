"""Connectors for the Rakazo app's Integrations screen: Google (Gmail and Calendar) through the dashboard's own sign-in.

The app's flow is ``begin`` (open the returned URL in a browser) then ``complete`` (poll until connected). ``begin`` is
``AppsService.connect_start``; Google redirects the browser to the dashboard callback, which stores the connection under the
person's ``(tenant, user)`` key, so ``complete`` only reports what the store holds. The key is the same one the dashboard
uses (``canonical_person``), so a phone operator and the dashboard operator share one Google connection. Other connectors
stay refused. Gmail and Calendar are two cards over one Google sign-in: revoking either removes the sign-in.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .app_credentials import AppsError
from .assistant_apps import CARDS, GOOGLE

CONNECTOR = GOOGLE
_PREFIX = GOOGLE + ":"
_NAMES = dict(CARDS)
_REASONS = {"no_client": "Google sign-in is not set up yet. Ask the operator to save the Google client in the dashboard.",
            "unavailable": "Google connections are unavailable right now.",
            "unknown_provider": "That connector is not available."}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Connections:
    def __init__(self, bridge, error_type):
        self.b = bridge
        self.Error = error_type

    def procs(self) -> dict[str, Any]:
        return {"connections/catalog": self.catalog, "connections/list": self.list, "connections/begin": self.begin,
                "connections/complete": self.complete, "connections/revoke": self.revoke, "connections/tools": self.tools}

    def _apps(self):
        fn = getattr(self.b, "apps_fn", None)
        return fn() if fn else None

    def _cards(self, ctx) -> dict[str, dict[str, Any]]:
        apps = self._apps()
        if apps is None:
            return {}
        tenant, user = self.b.canonical_person(ctx.tenant, ctx.user)
        try:
            return {c["id"]: c for c in apps.providers(tenant, user) if c.get("provider") == GOOGLE}
        except AppsError:
            return {}

    def _row(self, card: dict[str, Any]) -> dict[str, Any]:
        connected = card["connected"] is True
        return {"id": _PREFIX + card["id"], "connectorId": CONNECTOR, "provider": card["id"],
                "displayName": (card.get("account") or "") if connected and card.get("account") else _NAMES[card["id"]],
                "status": "connected" if connected else "pending", "capabilities": ["read", "write"], "createdAt": _now()}

    def _slug(self, connection_id: Any) -> str:
        slug = connection_id[len(_PREFIX):] if isinstance(connection_id, str) and connection_id.startswith(_PREFIX) else ""
        if slug not in _NAMES:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        return slug

    def catalog(self, ctx, inp):
        query = str(inp.get("query") or "").strip().lower()
        want = inp.get("connectorId")
        if want not in (None, "", CONNECTOR):
            return []
        cards = self._cards(ctx)
        return [{"connectorId": CONNECTOR, "slug": slug, "name": name, "logo": None, "connected": cards[slug]["connected"] is True,
                 "noAuth": False} for slug, name in CARDS if slug in cards and query in (slug + " " + name).lower()]

    def list(self, ctx, inp):
        return [self._row(c) for c in self._cards(ctx).values() if c["connected"] is True]

    def begin(self, ctx, inp):
        slug = inp.get("provider")
        if inp.get("connectorId") != CONNECTOR or slug not in _NAMES:
            raise self.Error(400, "BAD_REQUEST", _REASONS["unknown_provider"])
        apps = self._apps()
        if apps is None:
            raise self.Error(400, "BAD_REQUEST", _REASONS["unavailable"])
        tenant, user = self.b.canonical_person(ctx.tenant, ctx.user)
        try:
            url = apps.connect_start(tenant, user, GOOGLE)
        except AppsError as exc:
            raise self.Error(400, "BAD_REQUEST", _REASONS.get(exc.reason, _REASONS["unavailable"])) from None
        return {"connectionId": _PREFIX + slug, "authorizationUrl": url}

    def complete(self, ctx, inp):
        slug = self._slug(inp.get("connectionId"))
        card = self._cards(ctx).get(slug)
        if card is None:
            raise self.Error(400, "BAD_REQUEST", _REASONS["unavailable"])
        return self._row(card)

    def revoke(self, ctx, inp):
        self._slug(inp.get("connectionId"))
        apps = self._apps()
        if apps is None:
            raise self.Error(400, "BAD_REQUEST", _REASONS["unavailable"])
        tenant, user = self.b.canonical_person(ctx.tenant, ctx.user)
        try:
            apps.disconnect(tenant, user, GOOGLE)
        except AppsError:
            raise self.Error(400, "BAD_REQUEST", _REASONS["unavailable"]) from None
        return {"ok": True}

    def tools(self, ctx, inp):
        return []
