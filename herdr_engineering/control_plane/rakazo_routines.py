"""Routines for the Rakazo mobile bridge, over the assistant routines the dashboard already has.

The assistant store keeps the schedule (``interval`` or ``daily``, timezone, next fire time); the prompt and the app's other
routine fields live in per-space prefs. A cron expression is accepted when it maps exactly onto one of those two schedules
(``*/N * * * *``, ``M */N * * *``, ``M H * * *`` and ``M H * * <days>``); anything else is refused with the reason instead of
being approximated. Webhook, GitHub and watch triggers are not wired to anything here, so they are refused too.

``run_due`` is the execution side: it fires due routines by sending their prompt to the bot, exactly like a message from the
app. It runs only when the operator sets ``routine_runner_seconds``; ``routines/testRun`` does the same on demand.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from . import rakazo_events as ev
from .assistant import AssistantError

log = logging.getLogger(__name__)
META_KIND, RUNS_KIND = "routine_meta", "routine_runs"
MAX_ROUTINES = 50
MAX_RUNS_KEPT = 50
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CRON_DOW = {"0": "sun", "7": "sun", "1": "mon", "2": "tue", "3": "wed", "4": "thu", "5": "fri", "6": "sat"}
_UNSUPPORTED = ("This server's scheduler supports one schedule per routine: every N minutes (*/N * * * *), every N hours "
                "(0 */N * * *), or daily at a time on some weekdays (M H * * *).")
_THINKING = (None, "off", "minimal", "low", "medium", "high", "xhigh", "max")


def cron_to_trigger(cron: str) -> tuple[str, dict[str, Any]] | None:
    parts = cron.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if dom != "*" or month != "*":
        return None
    if hour == "*" and dow == "*" and (m := re.fullmatch(r"\*/(\d{1,4})", minute)):
        return "interval", {"every": "minutes", "amount": int(m.group(1))}
    if re.fullmatch(r"\d{1,2}", minute) and dow == "*" and (m := re.fullmatch(r"\*/(\d{1,3})", hour)) and minute == "0":
        return "interval", {"every": "hours", "amount": int(m.group(1))}
    if re.fullmatch(r"\d{1,2}", minute) and re.fullmatch(r"\d{1,2}", hour) and int(minute) < 60 and int(hour) < 24:
        days: list[str] = []
        if dow == "*":
            days = list(_DAYS)
        else:
            for piece in dow.split(","):
                span = re.fullmatch(r"([0-7])(?:-([0-7]))?", piece)
                if not span:
                    return None
                lo, hi = int(span.group(1)), int(span.group(2) or span.group(1))
                if hi < lo:
                    return None
                days += [_CRON_DOW[str(d)] for d in range(lo, hi + 1)]
        return "daily", {"time": f"{int(hour):02d}:{int(minute):02d}", "days": sorted(set(days), key=_DAYS.index)}
    return None


class Routines:
    def __init__(self, bridge, error_type):
        self.b = bridge
        self.Error = error_type
        self.prefs = bridge.prefs

    def procs(self) -> dict[str, Any]:
        return {"routines/list": self.list, "routines/create": self.create, "routines/update": self.update,
                "routines/remove": self.remove, "routines/history": self.history, "routines/testRun": self.test_run,
                "routines/listForMentions": self.list_for_mentions}

    # helpers ------------------------------------------------------------------------------------------------
    def _meta(self, ctx, rid: str) -> dict[str, Any]:
        return self.prefs.get(ctx.account.account_id, ctx.space, META_KIND, rid, {}) or {}

    def _view(self, ctx, r) -> dict[str, Any]:
        m = self._meta(ctx, r.routine_id)
        return {"id": r.routine_id, "botId": r.assistant_id, "name": r.name, "prompt": m.get("prompt", ""),
                "crons": m.get("crons", []), "timezone": r.timezone, "active": r.enabled,
                "notify": m.get("notify", True), "webhookEnabled": False, "githubEnabled": False,
                "messageProvider": m.get("messageProvider"), "modelProvider": m.get("modelProvider"),
                "modelId": m.get("modelId"), "thinkingLevel": m.get("thinkingLevel"), "watch": None, "watchState": None,
                "lastRunAt": ev.iso(r.last_run_at), "nextRunAt": ev.iso(r.next_at), "createdAt": ev.iso(r.created_at)}

    def _routine(self, ctx, rid):
        try:
            return self.b.svc.get_routine(rid, tenant_id=ctx.tenant, user_id=ctx.user)
        except KeyError:
            raise self.Error(404, "NOT_FOUND", "Not found.") from None

    def _live(self, ctx, bot_id=None):
        return [r for r in self.b.svc.list_routines(tenant_id=ctx.tenant, user_id=ctx.user, assistant_id=bot_id)
                if self._meta(ctx, r.routine_id).get("removed") is None]

    def _schedule(self, crons) -> tuple[str, dict[str, Any]] | None:
        if not isinstance(crons, list) or not all(isinstance(c, str) and c.strip() for c in crons) or len(crons) > 1:
            raise self.Error(400, "BAD_REQUEST", _UNSUPPORTED)
        if not crons:
            return None
        found = cron_to_trigger(crons[0].strip())
        if found is None:
            raise self.Error(400, "BAD_REQUEST", _UNSUPPORTED)
        return found

    def _refuse_unwired(self, inp) -> None:
        if inp.get("webhookEnabled") or inp.get("githubEnabled") or inp.get("watch"):
            raise self.Error(400, "BAD_REQUEST", "Webhook, GitHub and watch triggers are not available on this server; "
                             "a routine runs on its schedule or when you test it.")

    def _extras(self, inp) -> dict[str, Any]:
        out: dict[str, Any] = {}
        checks = {"notify": lambda v: isinstance(v, bool),
                  "messageProvider": lambda v: v is None or (isinstance(v, str) and re.fullmatch(r"[a-z0-9._-]{1,50}", v)),
                  "modelProvider": lambda v: v is None or (isinstance(v, str) and 0 < len(v) <= 80),
                  "modelId": lambda v: v is None or (isinstance(v, str) and 0 < len(v) <= 200),
                  "thinkingLevel": lambda v: v in _THINKING}
        for key, ok in checks.items():
            if key in inp:
                if not ok(inp[key]):
                    raise self.Error(400, "BAD_REQUEST", f"{key} is not a valid value.")
                out[key] = inp[key]
        return out

    def _text(self, inp, key, *, required: bool, limit: int) -> str | None:
        value = inp.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise self.Error(400, "BAD_REQUEST", f"{key} must be text of up to {limit} characters.")
        return value.strip() if key == "name" else value

    def _assistant_error(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except AssistantError as exc:
            raise self.Error(400, "BAD_REQUEST", str(exc)) from None

    def _save_meta(self, ctx, rid, changes):
        self.b.prefs_put(ctx.account.account_id, ctx.space, META_KIND, rid, {**self._meta(ctx, rid), **changes})

    # procedures ---------------------------------------------------------------------------------------------
    def list(self, ctx, inp):
        self.b._assistant(ctx, inp.get("botId"))
        return [self._view(ctx, r) for r in self._live(ctx, inp["botId"])]

    def list_for_mentions(self, ctx, inp):
        ids = inp.get("botIds")
        if not isinstance(ids, list) or not 0 < len(ids) <= 100 or not all(isinstance(i, str) and i for i in ids):
            raise self.Error(400, "BAD_REQUEST", "botIds must list 1 to 100 bot ids.")
        mine = {a.assistant_id for a in self.b.svc.list_assistants(tenant_id=ctx.tenant, user_id=ctx.user)}
        return [{"botId": i, "routines": [self._view(ctx, r) for r in self._live(ctx, i)] if i in mine else []}
                for i in dict.fromkeys(ids)]

    def create(self, ctx, inp):
        assistant = self.b._assistant(ctx, inp.get("botId"))
        name = self._text(inp, "name", required=True, limit=80)
        prompt = self._text(inp, "prompt", required=True, limit=20_000)
        self._refuse_unwired(inp)
        schedule = self._schedule(inp.get("crons", []))
        if len(self._live(ctx)) >= MAX_ROUTINES:
            raise self.Error(400, "BAD_REQUEST", f"This server allows at most {MAX_ROUTINES} routines.")
        trigger_type, spec = schedule or ("interval", {"every": "days", "amount": 1})
        active = bool(inp.get("active", False)) and schedule is not None
        if inp.get("active") and schedule is None:
            raise self.Error(400, "BAD_REQUEST", "A routine needs a schedule before it can be active.")
        r = self._assistant_error(self.b.svc.create_routine, tenant_id=ctx.tenant, user_id=ctx.user,
                                  assistant_id=assistant.assistant_id, name=name, trigger_type=trigger_type,
                                  trigger_spec=spec, timezone=inp.get("timezone") or "UTC", enabled=active)
        self._save_meta(ctx, r.routine_id, {"prompt": prompt, "crons": list(inp.get("crons") or []),
                                            **self._extras(inp)})
        return self._view(ctx, r)

    def update(self, ctx, inp):
        r = self._routine(ctx, inp.get("routineId"))
        if self._meta(ctx, r.routine_id).get("removed") is not None:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        self._refuse_unwired(inp)
        patch: dict[str, Any] = {}
        meta = self._extras(inp)
        if "name" in inp:
            patch["name"] = self._text(inp, "name", required=True, limit=80)
        if "prompt" in inp:
            meta["prompt"] = self._text(inp, "prompt", required=True, limit=20_000)
        if "timezone" in inp:
            patch["timezone"] = inp["timezone"]
        crons = self._meta(ctx, r.routine_id).get("crons", [])
        if "crons" in inp:
            schedule = self._schedule(inp["crons"])
            crons = list(inp["crons"])
            meta["crons"] = crons
            if schedule is not None:
                patch["trigger_type"], patch["trigger_spec"] = schedule
        if "active" in inp:
            if inp["active"] and not crons:
                raise self.Error(400, "BAD_REQUEST", "A routine needs a schedule before it can be active.")
            patch["enabled"] = bool(inp["active"])
        elif "crons" in inp and not crons:
            patch["enabled"] = False
        if patch:
            r = self._assistant_error(self.b.svc.update_routine, r.routine_id, tenant_id=ctx.tenant, user_id=ctx.user,
                                      patch=patch)
        if meta:
            self._save_meta(ctx, r.routine_id, meta)
        return self._view(ctx, r)

    def remove(self, ctx, inp):
        """Switch the routine off and hide it; the record stays (nothing is deleted)."""
        r = self._routine(ctx, inp.get("routineId"))
        self.b.svc.archive_routine(r.routine_id, tenant_id=ctx.tenant, user_id=ctx.user)
        self._save_meta(ctx, r.routine_id, {"removed": datetime.now(UTC).isoformat()})
        return {"ok": True}

    def _fire(self, ctx, r, nonce: str) -> str:
        prompt = self._meta(ctx, r.routine_id).get("prompt")
        if not prompt:
            raise self.Error(400, "BAD_REQUEST", "This routine has no prompt.")
        sent = self.b._threads_send(ctx, {"botId": r.assistant_id, "text": prompt, "clientNonce": nonce})
        runs = self.prefs.get(ctx.account.account_id, ctx.space, RUNS_KIND, r.routine_id, []) or []
        runs.append(sent["runId"])
        self.b.prefs_put(ctx.account.account_id, ctx.space, RUNS_KIND, r.routine_id, runs[-MAX_RUNS_KEPT:])
        return sent["runId"]

    def test_run(self, ctx, inp):
        r = self._routine(ctx, inp.get("routineId"))
        if self._meta(ctx, r.routine_id).get("removed") is not None:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        nonce = inp.get("clientNonce") or "routine-test-" + secrets.token_hex(6)
        return {"runId": self._fire(ctx, r, nonce)}

    def history(self, ctx, inp):
        r = self._routine(ctx, inp.get("routineId"))
        limit = inp.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise self.Error(400, "BAD_REQUEST", "limit must be 1 to 20.")
        ids = list(reversed(self.prefs.get(ctx.account.account_id, ctx.space, RUNS_KIND, r.routine_id, []) or []))
        before = inp.get("before")
        if before:
            ids = ids[ids.index(before["id"]) + 1:] if before.get("id") in ids else ids
        store = self.b.svc.store
        views = []
        for run_id in ids[:limit]:
            run = store.runs.get(run_id)
            if run is None or run.tenant_id != ctx.tenant:
                continue
            status = ev.run_status(run.state)
            done = status in ("completed", "failed", "cancelled")
            views.append({"id": run.run_id, "botId": run.assistant_id, "groupId": None, "status": status,
                          "error": run.error or None, "createdAt": ev.iso(run.created_at),
                          "startedAt": ev.iso(run.created_at), "completedAt": ev.iso(run.updated_at) if done else None,
                          "messageId": None})
        more = len(ids) > limit and views
        return {"runs": views, "nextCursor": {"id": views[-1]["id"], "createdAt": views[-1]["createdAt"]} if more else None}

    # execution ----------------------------------------------------------------------------------------------
    def run_due(self, ctx) -> int:
        """Fire this space's due routines once each; returns how many started. Failures are logged by type only."""
        started = 0
        for r in self.b.svc.due_routines(tenant_id=ctx.tenant, user_id=ctx.user):
            if self._meta(ctx, r.routine_id).get("removed") is not None:
                continue
            bucket = r.next_at
            fired = self.b.svc.trigger(r.routine_id, tenant_id=ctx.tenant, user_id=ctx.user)
            if fired.last_result != "triggered":
                continue
            try:
                self._fire(ctx, r, f"routine-{r.routine_id}-{bucket.isoformat() if bucket else ''}")
                started += 1
            except Exception as exc:  # noqa: BLE001 - one routine must not stop the others
                log.warning("rakazo routines: a routine could not start (%s)", type(exc).__name__)
        return started
