"""Agent skills for the Rakazo mobile bridge: the owner's own SKILL.md-style notes that the assistant actually reads.

A skill is a name, a one-line description and a body of instructions. They are kept per space in prefs and, when a run starts
for a bot in that space, the model runner adds them to the bot's system prompt (``HttpModelRunnerAdapter.prompt_extras``), so
a skill the person writes in the app changes what the assistant does. They are the owner's own text, so they are treated like
the bot's instructions (not like untrusted data). "Remove" hides a skill; it is not deleted.
"""
from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Any

from . import rakazo_spaces as sp

KIND = "agent_skill"
MAX_SKILLS = 50
MAX_PROMPT_CHARS = 20_000
MAX_EACH = 4_000
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def parse(content: str) -> tuple[str, str, str]:
    """(name, description, body) from SKILL.md text; frontmatter keys name and description."""
    found = _FRONT.match(content)
    if not found:
        return "", "", content
    meta = {}
    for line in found.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta.get("name", ""), meta.get("description", ""), found.group(2)


def render(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Skills:
    def __init__(self, bridge, error_type):
        self.b = bridge
        self.Error = error_type
        self.prefs = bridge.prefs

    def procs(self) -> dict[str, Any]:
        return {"agentSkills/list": self.list, "agentSkills/get": self.get, "agentSkills/create": self.create,
                "agentSkills/update": self.update, "agentSkills/remove": self.remove}

    def _rows(self, account_id: str, space: str) -> dict[str, Any]:
        return {k: r for k, r in self.prefs.items(account_id, space, KIND).items() if not r.get("removed")}

    def _view(self, key: str, row: dict[str, Any], *, full: bool) -> dict[str, Any]:
        out = {"id": key, "name": row["name"], "description": row["description"], "source": "user", "readOnly": False}
        if full:
            out.update(content=render(row["name"], row["description"], row["body"]), createdAt=row["createdAt"],
                       updatedAt=row["updatedAt"])
        return out

    def _fields(self, inp: dict[str, Any], current: dict[str, str] | None = None) -> tuple[str, str, str]:
        name, description, body = (current or {}).get("name", ""), (current or {}).get("description", ""), (current or {}).get("body", "")
        if inp.get("content") is not None:
            if not isinstance(inp["content"], str):
                raise self.Error(400, "BAD_REQUEST", "content must be text.")
            name, description, body = parse(inp["content"])
        for key in ("name", "description", "body"):
            if inp.get(key) is not None:
                if not isinstance(inp[key], str):
                    raise self.Error(400, "BAD_REQUEST", f"{key} must be text.")
                if key == "name":
                    name = inp[key]
                elif key == "description":
                    description = inp[key]
                else:
                    body = inp[key]
        name, description = " ".join(name.split()), " ".join(description.split())
        if not name or len(name) > 80 or not description or len(description) > 2000 or len(body) > 100_000:
            raise self.Error(400, "BAD_REQUEST", "A skill needs a name (up to 80), a description (up to 2000) and a body.")
        return name, description, body

    def list(self, ctx, inp):
        rows = self._rows(ctx.account.account_id, ctx.space)
        return [self._view(k, r, full=False) for k, r in sorted(rows.items(), key=lambda kv: kv[1]["name"].casefold())]

    def _one(self, ctx, inp):
        rows = self._rows(ctx.account.account_id, ctx.space)
        if inp.get("skillId") in rows:
            return inp["skillId"], rows[inp["skillId"]]
        wanted = (inp.get("name") or "").casefold()
        for key, row in rows.items():
            if wanted and row["name"].casefold() == wanted:
                return key, row
        raise self.Error(404, "NOT_FOUND", "Not found.")

    def get(self, ctx, inp):
        key, row = self._one(ctx, inp)
        return self._view(key, row, full=True)

    def create(self, ctx, inp):
        rows = self._rows(ctx.account.account_id, ctx.space)
        name, description, body = self._fields(inp)
        if len(rows) >= MAX_SKILLS:
            raise self.Error(400, "BAD_REQUEST", f"This server allows at most {MAX_SKILLS} skills per space.")
        if any(r["name"].casefold() == name.casefold() for r in rows.values()):
            raise self.Error(400, "BAD_REQUEST", "You already have a skill with that name.")
        stamp, key = _now(), "skl" + secrets.token_hex(8)
        row = {"name": name, "description": description, "body": body, "createdAt": stamp, "updatedAt": stamp}
        self.b.prefs_put(ctx.account.account_id, ctx.space, KIND, key, row)
        return self._view(key, row, full=True)

    def update(self, ctx, inp):
        key, row = self._one(ctx, inp)
        name, description, body = self._fields(inp, row)
        if any(k != key and r["name"].casefold() == name.casefold()
               for k, r in self._rows(ctx.account.account_id, ctx.space).items()):
            raise self.Error(400, "BAD_REQUEST", "You already have a skill with that name.")
        row = {**row, "name": name, "description": description, "body": body, "updatedAt": _now()}
        self.b.prefs_put(ctx.account.account_id, ctx.space, KIND, key, row)
        return self._view(key, row, full=True)

    def remove(self, ctx, inp):
        key, row = self._one(ctx, inp)
        self.b.prefs_put(ctx.account.account_id, ctx.space, KIND, key, {**row, "removed": _now()})
        return {"ok": True}

    # what the assistant reads ------------------------------------------------------------------------------
    def prompt_for(self, run) -> str:
        """The skills of the space this run belongs to, for the bot's system prompt. Empty for any other tenant."""
        account = self.b._by_principal.get(run.user_id)
        if account is None:
            return ""
        base = self.b.svc.tenant_for(account.principal_id)
        if run.tenant_id == base:
            space = sp.DEFAULT_SPACE_ID
        elif run.tenant_id.startswith(base + sp._SEPARATOR):
            space = run.tenant_id[len(base) + 1:]
        else:
            return ""
        rows = self._rows(account.account_id, space)
        if not rows:
            return ""
        lines, used = ["Skills you can use (written by the person you work for):"], 0
        for row in sorted(rows.values(), key=lambda r: r["name"].casefold()):
            entry = f"## {row['name']}: {row['description']}\n{row['body'][:MAX_EACH]}"
            if used + len(entry) > MAX_PROMPT_CHARS:
                break
            lines.append(entry)
            used += len(entry)
        return "\n\n".join(lines)
