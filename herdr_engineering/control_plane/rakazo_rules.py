"""Approval rules and auto review for the Rakazo app, enforced by the assistant's own policy gate.

The person's rules are kept in prefs (account scope, or per space) and consulted by ``AssistantService.rule_policy`` for every
tool request of a bot in that space. A rule can only:

* ``deny``: the request is refused as if no grant existed (stricter than the default), or
* ``always_allow``: skip the approval prompt, and only for a tool that already holds an active grant and is not one of the tools
  that can never be preauthorized.

It never creates authority. Deny beats allow; a space rule beats an account rule. Auto review needs a checker model that this
server does not run, so it reports ``checkerAvailable: false`` and refuses ``skipApprovals``.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from . import rakazo_spaces as sp

RULE_KIND, REVIEW_KIND = "approval_rule", "auto_review"
KINDS = ("tool", "connector", "site")
MAX_RULES = 200


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _host(scope: str) -> str:
    try:
        return (urlsplit(scope if "//" in scope else "//" + scope).hostname or "").lower()
    except ValueError:
        return ""


def matches(rule: dict[str, Any], tool_id: str, scope: str) -> bool:
    kind, value = rule["matchKind"], rule["matchValue"]
    if kind == "tool":
        return tool_id == value
    if kind == "connector":
        return tool_id.split(".", 1)[0] == value or scope == value or scope.startswith(value + ":")
    host = _host(scope)
    return bool(host) and (host == value.lower() or host.endswith("." + value.lower()))


def decide(rules: list[dict[str, Any]], tool_id: str, scope: str) -> str | None:
    hits = [r["effect"] for r in rules if matches(r, tool_id, scope)]
    return "deny" if "deny" in hits else ("always_allow" if "always_allow" in hits else None)


class Rules:
    def __init__(self, bridge, error_type):
        self.b = bridge
        self.Error = error_type
        self.prefs = bridge.prefs

    def procs(self) -> dict[str, Any]:
        return {"approvalRules/list": self.list, "approvalRules/set": self.set, "approvalRules/remove": self.remove,
                "approvalRules/preview": self.preview, "autoReview/get": self.review_get, "autoReview/set": self.review_set}

    def _space(self, ctx, scope) -> str:
        if scope not in (None, "account", "space"):
            raise self.Error(400, "BAD_REQUEST", "scope must be account or space.")
        return "-" if scope == "account" else ctx.space

    def _rows(self, account_id: str, space: str) -> dict[str, dict[str, Any]]:
        return {k: r for k, r in self.prefs.items(account_id, space, RULE_KIND).items() if not r.get("removed")}

    @staticmethod
    def _view(key: str, row: dict[str, Any]) -> dict[str, Any]:
        return {"id": key, "effect": row["effect"], "matchKind": row["matchKind"], "matchValue": row["matchValue"],
                "createdAt": row["createdAt"]}

    def list(self, ctx, inp):
        rows = self._rows(ctx.account.account_id, self._space(ctx, inp.get("scope")))
        return [self._view(k, r) for k, r in sorted(rows.items(), key=lambda kv: kv[1]["createdAt"])]

    def set(self, ctx, inp):
        space = self._space(ctx, inp.get("scope"))
        effect, kind, value = inp.get("effect"), inp.get("matchKind"), inp.get("matchValue")
        if not isinstance(effect, str) or not isinstance(kind, str) or effect not in ("always_allow", "deny") or kind not in KINDS:
            raise self.Error(400, "BAD_REQUEST", "Choose always_allow or deny, for a tool, connector or site.")
        if not isinstance(value, str) or not value.strip() or len(value) > 253:
            raise self.Error(400, "BAD_REQUEST", "matchValue is required.")
        if effect == "always_allow" and ctx.account.role != "operator":
            raise self.Error(403, "FORBIDDEN", "Only the operator can skip approvals. A deny rule is always allowed.")
        value = value.strip().lower() if kind == "site" else value.strip()
        rows = self._rows(ctx.account.account_id, space)
        same = next(((k, r) for k, r in rows.items() if (r["matchKind"], r["matchValue"]) == (kind, value)), None)
        if same is None and len(rows) >= MAX_RULES:
            raise self.Error(400, "BAD_REQUEST", "Too many rules.")
        key = same[0] if same else "rul" + secrets.token_hex(8)
        row = {"effect": effect, "matchKind": kind, "matchValue": value, "createdAt": _now()}
        self.b.prefs_put(ctx.account.account_id, space, RULE_KIND, key, row)
        return self._view(key, row)

    def remove(self, ctx, inp):
        space = self._space(ctx, inp.get("scope"))
        if not isinstance(inp.get("id"), str) or inp["id"] not in self._rows(ctx.account.account_id, space):
            raise self.Error(404, "NOT_FOUND", "Not found.")
        self.b.prefs_put(ctx.account.account_id, space, RULE_KIND, inp["id"], {"removed": _now()})
        return {"ok": True}

    def preview(self, ctx, inp):
        space = self._space(ctx, inp.get("scope"))
        effect, tools = inp.get("effect"), (inp.get("sample") or {}).get("tools")
        if (not isinstance(effect, str) or effect not in ("always_allow", "deny", "remove") or not isinstance(tools, list)
                or not tools):
            raise self.Error(400, "BAD_REQUEST", "Provide an effect and a sample tool.")
        rows = self._rows(ctx.account.account_id, space)
        current = list(rows.values())
        if not isinstance(inp.get("id"), str) or inp["id"] not in rows:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        changed = [r for k, r in rows.items() if k != inp["id"]]
        if effect != "remove":
            changed.append({**rows[inp["id"]], "effect": effect})
        name = str(tools[0].get("name", "")) if isinstance(tools[0], dict) else ""
        before, after = decide(current, name, ""), decide(changed, name, "")
        return {"before": before or "default", "after": after or "default"}

    def effective(self, tenant: str, user: str, tool_id: str, scope: str) -> str | None:
        """The rule effect for one request, for ``AssistantService.rule_policy``. Unknown users and tenants get None."""
        account = self.b._by_principal.get(user)
        if account is None:
            return None
        base = self.b.svc.tenant_for(account.principal_id)
        if tenant == base:
            space = sp.DEFAULT_SPACE_ID
        elif tenant.startswith(base + sp._SEPARATOR):
            space = tenant[len(base) + 1:]
        else:
            return None
        space_rows = list(self._rows(account.account_id, space).values())
        account_rows = list(self._rows(account.account_id, "-").values())
        effect = decide(space_rows, tool_id, scope) or decide(account_rows, tool_id, scope)
        return None if effect == "always_allow" and account.role != "operator" else effect

    # auto review --------------------------------------------------------------------------------------------
    def _review(self, ctx, scope) -> dict[str, Any]:
        row = self.prefs.get(ctx.account.account_id, self._space(ctx, scope), REVIEW_KIND, "settings", {}) or {}
        return {"mode": row.get("mode", "never"), "enabled": bool(row.get("enabled", False)),
                "checkerAvailable": False, "skipApprovals": False}

    def review_get(self, ctx, inp):
        return self._review(ctx, inp.get("scope"))

    def review_set(self, ctx, inp):
        space = self._space(ctx, inp.get("scope"))
        if inp.get("skipApprovals"):
            raise self.Error(400, "BAD_REQUEST", "Skipping approvals is not available on this server. "
                             "Use an always_allow rule for a specific tool instead.")
        row = {k: self._review(ctx, inp.get("scope"))[k] for k in ("mode", "enabled")}
        if "mode" in inp:
            if inp["mode"] not in ("never", "auto", "ask"):
                raise self.Error(400, "BAD_REQUEST", "mode must be never, auto or ask.")
            row["mode"] = inp["mode"]
        if "enabled" in inp:
            if not isinstance(inp["enabled"], bool):
                raise self.Error(400, "BAD_REQUEST", "enabled must be true or false.")
            row["enabled"] = inp["enabled"]
        self.b.prefs_put(ctx.account.account_id, space, REVIEW_KIND, "settings", row)
        return self._review(ctx, inp.get("scope"))
