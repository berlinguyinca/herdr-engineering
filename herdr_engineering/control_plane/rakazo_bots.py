"""Bot management for the Rakazo mobile bridge: edit, pin, colour, sections, archive/restore and instruction history.

The assistant store keeps name, instructions and enabled; everything else the app shows about a bot (description, colour,
pinned, section, per-bot options) is a small per-space setting in the prefs store, merged into the bot when it is listed.
Nothing here deletes a bot: "remove" archives it, exactly like the archive button, so it stays restorable.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from . import rakazo_events as ev

META_KIND, INSTR_KIND, SECTION_KIND = "bot_meta", "bot_instructions", "bot_section"
MAX_VERSIONS = 20
MAX_SECTIONS = 50
# bots/update fields that live in prefs, with the type check each one needs
_META: dict[str, Any] = {
    "description": lambda v: isinstance(v, str) and len(v) <= 4000,
    "color": lambda v: isinstance(v, str) and 0 < len(v) <= 200_000,
    "avatarSet": lambda v: v in ("organic", "rakazo"),
    "notifyOnFinish": lambda v: isinstance(v, bool),
    "pinned": lambda v: isinstance(v, bool),
    "memoryScope": lambda v: v in (None, "isolated", "shared"),
    "sectionId": lambda v: v is None or (isinstance(v, str) and bool(v)),
    "voiceId": lambda v: v is None or (isinstance(v, str) and len(v) <= 120),
    "autoSpeak": lambda v: isinstance(v, bool),
    "modelProvider": lambda v: v is None or (isinstance(v, str) and 0 < len(v) <= 80),
    "modelId": lambda v: v is None or (isinstance(v, str) and 0 < len(v) <= 200),
    "thinkingLevel": lambda v: v in (None, "off", "minimal", "low", "medium", "high", "xhigh", "max"),
    "teamChatAmbientEnabled": lambda v: isinstance(v, bool),
    "teamChatRules": lambda v: isinstance(v, str) and len(v) <= 4000,
    "crossSpaceCommunication": lambda v: isinstance(v, bool),
    "fleetManagement": lambda v: isinstance(v, bool),
    "disabledBuiltinTools": lambda v: isinstance(v, list) and len(v) <= 200 and all(isinstance(x, str) for x in v),
    "computerMode": lambda v: v in ("team", "dedicated"),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Bots:
    def __init__(self, bridge, error_type):
        self.b = bridge
        self.Error = error_type
        self.prefs = bridge.prefs

    def procs(self) -> dict[str, Any]:
        return {"bots/update": self.update, "bots/archive": self.archive, "bots/remove": self.archive,
                "bots/restore": self.restore, "bots/listArchived": self.list_archived,
                "bots/restoreInstructions": self.restore_instructions, "bots/setComputer": self.set_computer,
                "bots/duplicate": self.duplicate, "bots/reorder": self.reorder,
                "botSections/list": self.sections_list, "botSections/create": self.sections_create,
                "botSections/update": self.sections_update, "botSections/remove": self.sections_remove,
                "threads/markUnread": self.mark_unread, "threads/endCall": self.ok}

    # helpers ------------------------------------------------------------------------------------------------
    def ok(self, ctx, inp):
        return {"ok": True}

    def meta(self, ctx, bot_id: str) -> dict[str, Any]:
        return self.prefs.get(ctx.account.account_id, ctx.space, META_KIND, bot_id, {}) or {}

    def apply(self, ctx, view: dict[str, Any]) -> dict[str, Any]:
        """The bot with its saved app settings laid over the defaults."""
        view.update(self.meta(ctx, view["id"]))
        return view

    def _sections(self, ctx) -> dict[str, Any]:
        return self.prefs.items(ctx.account.account_id, ctx.space, SECTION_KIND)

    def _assistant(self, ctx, inp):
        return self.b._assistant(ctx, inp.get("botId"))

    def _put_meta(self, ctx, bot_id: str, changes: dict[str, Any]) -> None:
        meta = {**self.meta(ctx, bot_id), **changes}
        self.b.prefs_put(ctx.account.account_id, ctx.space, META_KIND, bot_id, meta)

    # bots ---------------------------------------------------------------------------------------------------
    def update(self, ctx, inp):
        assistant = self._assistant(ctx, inp)
        patch: dict[str, Any] = {}
        name = inp.get("name")
        if name is not None:
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
                raise self.Error(400, "BAD_REQUEST", "A bot needs a name of up to 80 characters.")
            patch["name"] = name.strip()
        meta: dict[str, Any] = {}
        for key, ok in _META.items():
            if key in inp:
                if not ok(inp[key]):
                    raise self.Error(400, "BAD_REQUEST", f"{key} is not a valid value.")
                meta[key] = inp[key]
        text = inp.get("instructions")
        if text is not None:
            if not isinstance(text, str) or len(text) > 20_000:
                raise self.Error(400, "BAD_REQUEST", "The instructions are too long.")
            expected = inp.get("expectedInstructions")
            if expected is not None and expected != assistant.instructions:
                raise self.Error(409, "CONFLICT", "The instructions changed since you opened this bot.")
            if text != assistant.instructions:
                self._remember_instructions(ctx, assistant)
                patch["instructions"] = text
        if "sectionId" in meta and meta["sectionId"] is not None and meta["sectionId"] not in self._sections(ctx):
            raise self.Error(404, "NOT_FOUND", "Not found.")
        if meta:
            self._put_meta(ctx, assistant.assistant_id, meta)
        if patch:
            self.b.svc.update_assistant(assistant.assistant_id, tenant_id=ctx.tenant, user_id=ctx.user, patch=patch)
            assistant = self._assistant(ctx, inp)
        return self.b._bot(ctx, assistant)

    def _remember_instructions(self, ctx, assistant) -> None:
        key = assistant.assistant_id
        history = self.prefs.get(ctx.account.account_id, ctx.space, INSTR_KIND, key, []) or []
        history.append({"versionId": "ver" + secrets.token_hex(6), "instructions": assistant.instructions, "at": _now()})
        self.b.prefs_put(ctx.account.account_id, ctx.space, INSTR_KIND, key, history[-MAX_VERSIONS:])

    def restore_instructions(self, ctx, inp):
        assistant = self._assistant(ctx, inp)
        if inp.get("expectedInstructions") != assistant.instructions:
            raise self.Error(409, "CONFLICT", "The instructions changed since you opened this bot.")
        history = self.prefs.get(ctx.account.account_id, ctx.space, INSTR_KIND, assistant.assistant_id, []) or []
        version = next((v for v in history if v["versionId"] == inp.get("versionId")), None)
        if version is None:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        self._remember_instructions(ctx, assistant)
        self.b.svc.update_assistant(assistant.assistant_id, tenant_id=ctx.tenant, user_id=ctx.user,
                                    patch={"instructions": version["instructions"]})
        return self.b._bot(ctx, self._assistant(ctx, inp))

    def set_computer(self, ctx, inp):
        assistant = self._assistant(ctx, inp)
        if inp.get("mode") not in ("team", "dedicated"):
            raise self.Error(400, "BAD_REQUEST", "mode must be team or dedicated.")
        self._put_meta(ctx, assistant.assistant_id, {"computerMode": inp["mode"]})
        return self.b._bot(ctx, assistant)

    def archive(self, ctx, inp):
        """Archive the bot (also what "remove" does: nothing is deleted, the bot stays restorable)."""
        assistant = self._assistant(ctx, inp)
        if assistant.enabled:
            self.b.svc.archive_assistant(assistant.assistant_id, tenant_id=ctx.tenant, user_id=ctx.user)
        self._put_meta(ctx, assistant.assistant_id, {"archivedAt": _now()})
        return {"ok": True}

    def restore(self, ctx, inp):
        assistant = self._assistant(ctx, inp)
        self.b.svc.update_assistant(assistant.assistant_id, tenant_id=ctx.tenant, user_id=ctx.user,
                                    patch={"enabled": True})
        meta = self.meta(ctx, assistant.assistant_id)
        meta.pop("archivedAt", None)
        self.b.prefs_put(ctx.account.account_id, ctx.space, META_KIND, assistant.assistant_id, meta)
        return {"ok": True}

    def list_archived(self, ctx, inp):
        out = []
        for a in self.b.svc.list_assistants(tenant_id=ctx.tenant, user_id=ctx.user):
            if not a.enabled:
                view = self.b._bot(ctx, a)
                view["archivedAt"] = view["archivedAt"] or ev.iso(a.updated_at)
                out.append(view)
        return out

    def duplicate(self, ctx, inp):
        source = self._assistant(ctx, inp)
        with self.b._create_lock:
            live = [a for a in self.b.svc.list_assistants(tenant_id=ctx.tenant, user_id=ctx.user) if a.enabled]
            if len(live) >= self.b.max_bots:
                raise self.Error(400, "BAD_REQUEST", f"This server allows at most {self.b.max_bots} bots per account.")
            copy = self.b.svc.create_assistant(tenant_id=ctx.tenant, user_id=ctx.user,
                                               name=(source.name + " copy")[:80], instructions=source.instructions,
                                               enabled=True)
        keep = {k: v for k, v in self.meta(ctx, source.assistant_id).items() if k not in ("archivedAt", "pinned")}
        if keep:
            self._put_meta(ctx, copy.assistant_id, keep)
        return self.b._bot(ctx, copy)

    def reorder(self, ctx, inp):
        return {"ok": True}      # the app keeps its own order; the server sorts by recency

    def mark_unread(self, ctx, inp):
        if inp.get("groupId"):
            return {"ok": True}
        self._put_meta(ctx, self._assistant(ctx, inp).assistant_id, {"unread": True})
        return {"ok": True}

    # sections -----------------------------------------------------------------------------------------------
    def _section_view(self, key: str, row: dict[str, Any]) -> dict[str, Any]:
        return {"id": key, "name": row["name"], "position": row["position"], "createdAt": row["createdAt"],
                "updatedAt": row["updatedAt"]}

    def sections_list(self, ctx, inp):
        rows = self._sections(ctx)
        return [self._section_view(k, r) for k, r in sorted(rows.items(), key=lambda kv: (kv[1]["position"], kv[0]))]

    def _section_name(self, value) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 60:
            raise self.Error(400, "BAD_REQUEST", "A section needs a name of 1 to 60 characters.")
        return " ".join(value.split())

    def sections_create(self, ctx, inp):
        name = self._section_name(inp.get("name"))
        rows = self._sections(ctx)
        if len(rows) >= MAX_SECTIONS:
            raise self.Error(400, "BAD_REQUEST", "Too many sections.")
        key, stamp = "sec" + secrets.token_hex(8), _now()
        row = {"name": name, "position": 1 + max((r["position"] for r in rows.values()), default=-1),
               "createdAt": stamp, "updatedAt": stamp}
        self.b.prefs_put(ctx.account.account_id, ctx.space, SECTION_KIND, key, row)
        if inp.get("botId") and not inp.get("groupId"):
            self._put_meta(ctx, self._assistant(ctx, inp).assistant_id, {"sectionId": key})
        return self._section_view(key, row)

    def sections_update(self, ctx, inp):
        key, rows = inp.get("sectionId"), self._sections(ctx)
        if key not in rows:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        row = {**rows[key], "name": self._section_name(inp.get("name")), "updatedAt": _now()}
        self.b.prefs_put(ctx.account.account_id, ctx.space, SECTION_KIND, key, row)
        return self._section_view(key, row)

    def sections_remove(self, ctx, inp):
        """Drop the section; bots in it fall back to unsectioned (the bots themselves are untouched)."""
        key = inp.get("sectionId")
        if key not in self._sections(ctx):
            raise self.Error(404, "NOT_FOUND", "Not found.")
        released = 0
        for bot_id, meta in self.prefs.items(ctx.account.account_id, ctx.space, META_KIND).items():
            if meta.get("sectionId") == key:
                self.b.prefs_put(ctx.account.account_id, ctx.space, META_KIND, bot_id, {**meta, "sectionId": None})
                released += 1
        self.prefs.delete(ctx.account.account_id, ctx.space, SECTION_KIND, key)
        return {"id": key, "releasedBots": released, "releasedGroups": 0}
