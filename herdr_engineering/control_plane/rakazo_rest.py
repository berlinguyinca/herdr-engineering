"""The rest of the Rakazo procedures: small real ones, "nothing configured here" answers, and plain refusals.

Every upstream procedure gets an answer the app can parse, never a bare 404:

* a few are real (scratchpad, trusted link sites, preferences, bootstrap, billing status, bot export, link favicons);
* ``READS`` are read-only lists and statuses of things this server does not have (no billing, no voice host, no MCP servers,
  ...): they answer with the smallest value the app's schema accepts, from ``rakazo_defaults.json``;
* looking up one object of a kind that cannot exist here is 404; every other change is refused with a message that says the
  server does not offer it, so the app shows a reason instead of failing silently.

Nothing is faked: a refusal never pretends the action happened.
"""
from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import rakazo_events as ev

READS = frozenset({
    "availability/items",
    "capabilities/catalogSearch",
    "capabilities/list",
    "computer/status",
    "computer/updates",
    "connections/catalog",
    "connections/list",
    "deployment/get",
    "integrationSetup/get",
    "marketplace/catalog",
    "marketplace/submissions",
    "mcp/assignments/all",
    "mcp/assignments/list",
    "messaging/channels/list",
    "messaging/connections/list",
    "messaging/installations/list",
    "messaging/status",
    "models/backups",
    "models/credentials",
    "models/forRole",
    "models/list",
    "models/role",
    "paymentWallet/status",
    "security/events",
    "skills/list",
    "updater/status",
    "voice/catalog",
    "voice/credentials",
    "voice/status",
    "voice/voices",
})
PUBLIC = frozenset({"health"})          # answered before sign-in by the bridge itself
NOT_FOUND_TAILS = frozenset({"get", "getById", "comments", "readFile", "downloadFile", "listVersions", "tools", "files",
                             "pluginParts", "pluginPreview", "terminalUrl", "screenUrl", "commands", "voices"})
_REASONS = {


    "billing": "This server has no billing: it is free to use.", "paymentWallet": "Payment wallets are not available.",
    "voice": "Voice is not set up on this server.", "computer": "No computer host is attached to this server.",
    "models": "Models on this server are managed by the operator, not from the app.",
    "mcp": "That MCP action is not available on this server.",
    "connections": "Connectors are managed by the operator, not from the app.",
    "messaging": "Messaging channels are not set up on this server.",
    "marketplace": "There is no marketplace on this server.", "capabilities": "Capabilities are managed by the operator.",
    "skills": "Taught skills are not available on this server.",
    "boards": "Boards cannot be added in this server.", "tickets": "That ticket action is not available.",

    "updater": "Updates are applied by the operator.", "deployment": "The deployment is configured by the operator.",
    "onboarding": "Onboarding is not needed on this server.", "availability": "Availability is managed by the operator.",
    "integrationSetup": "Integrations are managed by the operator.", "externalConversations": "External conversations are not enabled.",
    "bots": "That bot action is not available on this server.", "threads": "That conversation action is not available on this server.",
    "security": "Security events are not recorded here.",
    "export": "That export is not available on this server.",
}
_DATA = json.loads((Path(__file__).with_name("rakazo_defaults.json")).read_text())
DEFAULTS, UPSTREAM = _DATA["defaults"], tuple(_DATA["procedures"])
SCRATCH_KIND, SITES_KIND, PREFS_KIND = "scratchpad", "trusted_site", "app_prefs"
MAX_SITES = 200
STATUSES = ("open", "parked", "done")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Rest:
    def __init__(self, bridge, error_type, known: set[str]):
        self.b = bridge
        self.Error = error_type
        self.prefs = bridge.prefs
        self.known = known
        self.refused: set[str] = set()

    def procs(self) -> dict[str, Any]:
        """Handlers for every upstream procedure that no other module answers."""
        real = {"scratchpad/list": self.scratch_list, "scratchpad/create": self.scratch_create,
                "scratchpad/update": self.scratch_update, "scratchpad/remove": self.scratch_remove,
                "trustedLinkSites/list": self.sites_list, "trustedLinkSites/trust": self.sites_trust,
                "trustedLinkSites/remove": self.sites_remove, "links/favicon": self.favicon,
                "preferences/update": self.preferences_update, "bootstrap": self.bootstrap,
                "billing/status": self.billing_status, "preferences/adoptTimezone": self.adopt_timezone, "export/bot": self.export_bot}
        out = {}
        self.refused = set()
        for name in UPSTREAM:
            if name in self.known or name in PUBLIC:
                continue
            if name not in real and name not in READS:
                self.refused.add(name)
            out[name] = real.get(name) or (self._default(name) if name in READS else self._refuse(name))
        return out

    def _default(self, name):
        def handler(ctx, inp):
            return json.loads(json.dumps(DEFAULTS[name]))
        return handler

    def _refuse(self, name):
        group, _, tail = name.partition("/")
        tail = tail.rsplit("/", 1)[-1]

        def handler(ctx, inp):
            if tail in NOT_FOUND_TAILS:
                raise self.Error(404, "NOT_FOUND", "Not found.")
            raise self.Error(400, "BAD_REQUEST", _REASONS.get(group, "This server does not offer that."))
        return handler

    # scratchpad ---------------------------------------------------------------------------------------------
    def _scratch_all(self, ctx) -> dict[str, Any]:
        return self.prefs.items(ctx.account.account_id, ctx.space, SCRATCH_KIND)

    def _scratch_view(self, key, row):
        return {"id": key, "botId": row["botId"], "title": row["title"], "status": row["status"], "notes": row["notes"],
                "createdAt": row["createdAt"], "updatedAt": row["updatedAt"]}

    def _scratch_fields(self, inp, row):
        for key, limit in (("title", 200), ("notes", 4000)):
            if key in inp:
                value = inp[key]
                if not isinstance(value, str) or len(value) > limit or (key == "title" and not value.strip()):
                    raise self.Error(400, "BAD_REQUEST", f"{key} is not valid.")
                row[key] = value
        if "status" in inp:
            if inp["status"] not in STATUSES:
                raise self.Error(400, "BAD_REQUEST", "status must be open, parked or done.")
            row["status"] = inp["status"]
        return row

    def scratch_list(self, ctx, inp):
        self.b._assistant(ctx, inp.get("botId"))
        rows = [(k, r) for k, r in self._scratch_all(ctx).items() if r["botId"] == inp["botId"] and not r.get("removed")]
        wanted = inp.get("status")
        rows = [(k, r) for k, r in rows if (r["status"] == wanted if wanted else inp.get("includeDone") or r["status"] != "done")]
        return [self._scratch_view(k, r) for k, r in sorted(rows, key=lambda kr: (kr[1]["createdAt"], kr[0]))]

    def scratch_create(self, ctx, inp):
        self.b._assistant(ctx, inp.get("botId"))
        stamp = _now()
        row = self._scratch_fields(inp, {"botId": inp["botId"], "title": "", "status": "open", "notes": "",
                                         "createdAt": stamp, "updatedAt": stamp})
        if not row["title"].strip():
            raise self.Error(400, "BAD_REQUEST", "title is required.")
        key = "scr" + secrets.token_hex(8)
        self.b.prefs_put(ctx.account.account_id, ctx.space, SCRATCH_KIND, key, row)
        return self._scratch_view(key, row)

    def _scratch_one(self, ctx, inp):
        key = inp.get("itemId")
        row = self._scratch_all(ctx).get(key)
        if row is None or row.get("removed"):
            raise self.Error(404, "NOT_FOUND", "Not found.")
        return key, row

    def scratch_update(self, ctx, inp):
        key, row = self._scratch_one(ctx, inp)
        row = self._scratch_fields(inp, dict(row))
        row["updatedAt"] = _now()
        self.b.prefs_put(ctx.account.account_id, ctx.space, SCRATCH_KIND, key, row)
        return self._scratch_view(key, row)

    def scratch_remove(self, ctx, inp):
        key, row = self._scratch_one(ctx, inp)
        self.b.prefs_put(ctx.account.account_id, ctx.space, SCRATCH_KIND, key, {**row, "removed": _now()})
        return {"ok": True}

    # trusted link sites -------------------------------------------------------------------------------------
    def sites_list(self, ctx, inp):
        return sorted(self.prefs.items(ctx.account.account_id, ctx.space, SITES_KIND))

    def sites_trust(self, ctx, inp):
        url = inp.get("url")
        try:
            parts = urlsplit(url if "//" in str(url) else "//" + str(url))
            host = (parts.hostname or "").lower()
        except ValueError:
            host = ""
        if not host or len(host) > 253:
            raise self.Error(400, "BAD_REQUEST", "That is not a web address.")
        if len(self.prefs.items(ctx.account.account_id, ctx.space, SITES_KIND)) >= MAX_SITES:
            raise self.Error(400, "BAD_REQUEST", "Too many trusted sites.")
        self.b.prefs_put(ctx.account.account_id, ctx.space, SITES_KIND, host, True)
        return {"site": host}

    def sites_remove(self, ctx, inp):
        site = inp.get("site")
        if isinstance(site, str):
            self.prefs.delete(ctx.account.account_id, ctx.space, SITES_KIND, site.lower())
        return {"ok": True}

    def favicon(self, ctx, inp):
        return {"icon": None, "retry": False}          # the server never fetches third-party sites for the phone

    # preferences, billing, bootstrap, export ---------------------------------------------------------------
    def me(self, ctx):
        view = ev.me_view(ctx.account, ctx.space)
        view["ticketBoardEnabled"] = True
        view.update(self.prefs.get(ctx.account.account_id, "-", PREFS_KIND, "me", {}) or {})
        return view

    def preferences_update(self, ctx, inp):
        saved = self.prefs.get(ctx.account.account_id, "-", PREFS_KIND, "me", {}) or {}
        if "avatarStyle" in inp:
            if inp["avatarStyle"] not in ("robot", "organic"):
                raise self.Error(400, "BAD_REQUEST", "avatarStyle must be robot or organic.")
            saved["avatarStyle"] = inp["avatarStyle"]
        if "timezone" in inp:
            zone = inp["timezone"]
            if zone is not None:
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(zone)
                except Exception:  # noqa: BLE001 - any bad zone is the same answer
                    raise self.Error(400, "BAD_REQUEST", "That is not a time zone.") from None
            saved["timezone"] = zone
        self.b.prefs_put(ctx.account.account_id, "-", PREFS_KIND, "me", saved)
        return self.me(ctx)

    def adopt_timezone(self, ctx, inp):
        return self.preferences_update(ctx, {"timezone": inp.get("timezone")})

    def billing_status(self, ctx, inp):
        return {"access": True, "canManage": False, "trialAvailable": False, "trialDays": 0, "state": "none", "price": None,
                "seats": 0, "trialEndsAt": None, "currentPeriodEndsAt": None, "cancelAtPeriodEnd": False,
                "hasCustomer": False, "funding": "byok", "balanceUsd": 0, "creditPurchasesEnabled": False}

    def bootstrap(self, ctx, inp):
        call = self.b._procs
        thread = None
        if inp.get("botId"):
            thread = call["threads/get"](ctx, {"botId": inp["botId"]})
        routines = []
        for a in self.b.svc.list_assistants(tenant_id=ctx.tenant, user_id=ctx.user):
            if a.enabled:
                routines += call["routines/list"](ctx, {"botId": a.assistant_id})
        return {"me": self.me(ctx), "bots": call["bots/list"](ctx, {}), "groups": call["groups/list"](ctx, {}),
                "botSections": call["botSections/list"](ctx, {}), "archivedBots": call["bots/listArchived"](ctx, {}),
                "archivedGroups": call["groups/listArchived"](ctx, {}), "thread": thread, "routines": routines,
                "spaces": call["spaces/list"](ctx, {})["spaces"]}

    def export_bot(self, ctx, inp):
        assistant = self.b._assistant(ctx, inp.get("botId"))
        view = self.b._bot(ctx, assistant)
        bot = {"name": assistant.name, "description": view["description"], "instructions": assistant.instructions,
               "color": view["color"], "avatarSet": view["avatarSet"]}
        memory = [{"path": m["path"], "content": m["content"]} for m in self.b._procs["memory/list"](
            ctx, {"botId": assistant.assistant_id, "scope": "bot"})]
        routines = [{"name": r["name"], "prompt": r["prompt"], "crons": r["crons"], "timezone": r["timezone"]}
                    for r in self.b._procs["routines/list"](ctx, {"botId": assistant.assistant_id})]
        return {"version": 1, "exportedAt": _now(), "bot": bot, "memory": memory, "routines": routines, "files": [],
                "history": []}
