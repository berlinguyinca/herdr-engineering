"""Memory, search and usage for the Rakazo mobile bridge, over the assistant store the dashboard already uses.

Memory is the assistant memory service (the same records the assistant retrieves from; ``herdr-memory``/Hindsight consumes
them downstream as a retrieval sink, not as a second ledger). A bot's memory is its private namespace; the user scope is the
``shared`` namespace. "Remove" hides a fact from the app (a tombstone in prefs) rather than deleting the record. Token counts
are not measured by the assistant runs, so usage reports them as unknown (null) and counts runs honestly.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import rakazo_events as ev

HIDDEN_KIND, PROVIDER_KIND = "memory_hidden", "memory_provider"
PROVIDER = "herdr"
MAX_HITS = 25
MAX_USAGE = 500


class Memory:
    def __init__(self, bridge, error_type):
        self.b = bridge
        self.Error = error_type
        self.prefs = bridge.prefs

    def procs(self) -> dict[str, Any]:
        return {"memory/list": self.list, "memory/update": self.update, "memory/remove": self.remove,
                "memory/exportMarkdown": self.export_markdown, "memory/providerConfig": self.provider_config,
                "memory/connectProvider": self.connect_provider, "memory/setDefaultScope": self.set_default_scope,
                "memory/disconnectProvider": self.disconnect_provider, "search/query": self.search,
                "usage/summary": self.usage_summary, "usage/list": self.usage_list}

    # memory -------------------------------------------------------------------------------------------------
    def _hidden(self, ctx) -> dict[str, Any]:
        return self.prefs.items(ctx.account.account_id, ctx.space, HIDDEN_KIND)

    def _view(self, m) -> dict[str, Any]:
        bot_scope = m.namespace != "shared"
        return {"id": m.memory_id, "scope": "bot" if bot_scope else "user", "botId": m.assistant_id if bot_scope else None,
                "path": f"memories/{m.memory_id}.md", "content": m.content, "revision": m.revision,
                "updatedAt": ev.iso(m.updated_at)}

    def _rows(self, ctx, bot_id=None, scope=None):
        hidden = self._hidden(ctx)
        if bot_id is not None:
            self.b._assistant(ctx, bot_id)
        rows = [m for m in list(self.b.svc.store.memory.values())
                if m.tenant_id == ctx.tenant and m.user_id == ctx.user and m.memory_id not in hidden
                and (bot_id is None or m.assistant_id == bot_id)
                and (scope is None or (m.namespace == "shared") == (scope == "user"))]
        return sorted(rows, key=lambda m: (m.updated_at, m.memory_id), reverse=True)

    def list(self, ctx, inp):
        scope = inp.get("scope")
        if scope not in (None, "bot", "user"):
            raise self.Error(400, "BAD_REQUEST", "scope must be bot or user.")
        return [self._view(m) for m in self._rows(ctx, inp.get("botId"), scope)]

    def _one(self, ctx, doc_id):
        if doc_id in self._hidden(ctx):
            raise self.Error(404, "NOT_FOUND", "Not found.")
        try:
            return self.b.svc.store.get_memory(doc_id, tenant_id=ctx.tenant, user_id=ctx.user)
        except KeyError:
            raise self.Error(404, "NOT_FOUND", "Not found.") from None

    def update(self, ctx, inp):
        m = self._one(ctx, inp.get("documentId"))
        content = inp.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 100_000:
            raise self.Error(400, "BAD_REQUEST", "A memory needs content of up to 100000 characters.")
        expected = inp.get("expectedRevision")
        if expected is not None and expected != m.revision:
            raise self.Error(409, "CONFLICT", "This memory changed since you opened it.")
        return self._view(self.b.svc.correct_memory(m.memory_id, tenant_id=ctx.tenant, user_id=ctx.user,
                                                    new_content=content, source_ref="rakazo-app"))

    def remove(self, ctx, inp):
        m = self._one(ctx, inp.get("documentId"))
        self.b.prefs_put(ctx.account.account_id, ctx.space, HIDDEN_KIND, m.memory_id,
                         datetime.now(UTC).isoformat())
        return {"ok": True}

    def export_markdown(self, ctx, inp):
        rows = list(reversed(self._rows(ctx, inp.get("botId"))))
        return "\n\n".join(f"## {m.memory_id}\n\n{m.content}" for m in rows) + ("\n" if rows else "")

    # provider -----------------------------------------------------------------------------------------------
    def _config(self, ctx):
        return self.prefs.get(ctx.account.account_id, ctx.space, PROVIDER_KIND, "config")

    def _save(self, ctx, scope):
        row = {"provider": PROVIDER, "settings": {}, "defaultMemoryScope": scope,
               "updatedAt": datetime.now(UTC).isoformat()}
        self.b.prefs_put(ctx.account.account_id, ctx.space, PROVIDER_KIND, "config", row)
        return row

    def provider_config(self, ctx, inp):
        return self._config(ctx) or {"provider": PROVIDER, "settings": {}, "defaultMemoryScope": "isolated",
                                     "updatedAt": datetime.fromtimestamp(0, UTC).isoformat()}

    def connect_provider(self, ctx, inp):
        if inp.get("provider") != PROVIDER:
            raise self.Error(400, "BAD_REQUEST", "This server's memory is its own (provider 'herdr'); "
                             "other memory providers are not connected through the app.")
        scope = inp.get("defaultMemoryScope")
        if scope not in ("isolated", "shared"):
            raise self.Error(400, "BAD_REQUEST", "defaultMemoryScope must be isolated or shared.")
        return self._save(ctx, scope)          # credentials are never read or stored

    def set_default_scope(self, ctx, inp):
        scope = inp.get("defaultMemoryScope")
        if scope not in ("isolated", "shared"):
            raise self.Error(400, "BAD_REQUEST", "defaultMemoryScope must be isolated or shared.")
        return self._save(ctx, scope)

    def disconnect_provider(self, ctx, inp):
        self.prefs.delete(ctx.account.account_id, ctx.space, PROVIDER_KIND, "config")
        return {"ok": True}

    # search -------------------------------------------------------------------------------------------------
    def search(self, ctx, inp):
        q = inp.get("q")
        if not isinstance(q, str) or len(q) > 200:
            raise self.Error(400, "BAD_REQUEST", "q must be text of up to 200 characters.")
        terms = [t for t in q.casefold().split() if t]
        hits: list[dict[str, Any]] = []
        if not terms:
            return {"hits": hits}
        store = self.b.svc.store
        for a in self.b.svc.list_assistants(tenant_id=ctx.tenant, user_id=ctx.user):
            if not a.enabled:
                continue
            if all(t in (a.name + " " + a.instructions).casefold() for t in terms):
                hits.append({"kind": "conversation", "botId": a.assistant_id, "botName": a.name, "title": a.name,
                             "snippet": ev.snippet(a.instructions or a.name, 160)})
            for thread in self.b.svc.list_threads(tenant_id=ctx.tenant, user_id=ctx.user, assistant_id=a.assistant_id):
                with store._lock:
                    messages = store.list_messages(thread.thread_id, tenant_id=ctx.tenant, user_id=ctx.user)
                for msg in reversed(messages):
                    if msg.visibility == "visible" and all(t in msg.content.casefold() for t in terms):
                        hits.append({"kind": "message", "botId": a.assistant_id, "botName": a.name, "title": a.name,
                                     "snippet": ev.snippet(msg.content, 160), "messageId": msg.message_id})
                        break
            if len(hits) >= MAX_HITS:
                break
        return {"hits": hits[:MAX_HITS]}

    # usage --------------------------------------------------------------------------------------------------
    def _runs(self, ctx):
        mine = {a.assistant_id for a in self.b.svc.list_assistants(tenant_id=ctx.tenant, user_id=ctx.user)}
        runs = self.b.svc.store.list_runs(tenant_id=ctx.tenant, user_id=ctx.user)
        return sorted((r for r in runs if r.assistant_id in mine), key=lambda r: r.created_at, reverse=True)

    def usage_summary(self, ctx, inp):
        runs = self._runs(ctx)
        return {"inputTokens": None, "outputTokens": None, "totalTokens": None, "costUsd": 0, "modelCalls": len(runs),
                "runs": len(runs), "models": []}

    def usage_list(self, ctx, inp):
        return [{"id": "use_" + r.run_id, "botId": r.assistant_id, "runId": r.run_id, "provider": PROVIDER,
                 "model": r.model_route or PROVIDER, "inputTokens": None, "outputTokens": None,
                 "operationKind": "answer", "createdAt": ev.iso(r.created_at)} for r in self._runs(ctx)[:MAX_USAGE]]
