"""Scheduled CI watch for the Herdr personal assistant.

``AssistantService.watch_ci`` only ran when the assistant model invoked it, so nobody noticed a failure unless they
asked. This module is the periodic caller: a deterministic server-side loop (no model, no routine) that, for each user
the operator names in YAML ``control_plane.personal_assistant.ci_watch.users``, calls ``watch_ci`` as the actor
``system:ci_watch``. ``watch_ci`` keeps all the guarantees: it needs an active ``watch_ci`` grant and the user's
project-intelligence scope, dedupes by fingerprint, and only ever queues PROPOSALS that a person approves.

Failure handling: one user failing never stops another; nothing is raised out of the loop; a tick has a time budget.
Status carries counts and fixed reason strings only (never exception text, which could carry a path or a credential).
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

ACTOR = "system:ci_watch"
WARN_EVERY_S = 3600
MAX_JITTER_S = 30


class CIWatchRunner:
    def __init__(self, assistant, scope_fn: Callable[[str, str], str], config: dict[str, Any], *,
                 clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None):
        self.assistant = assistant
        self.scope_fn = scope_fn
        self.config = config
        self._clock = clock
        self._rng = rng or random.Random()
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._warned: dict[tuple[str, str, str], float] = {}
        self._status: dict[str, Any] = {"enabled": bool(config.get("enabled")), "interval_seconds": config["interval_seconds"],
                                         "auto_grant": config["auto_grant"], "last_at": None, "ticks": 0, "created": 0,
                                         "duplicates": 0, "skipped": {}, "last_error": None, "users": []}

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        """Thread body: a jittered start, then a tick every ``interval_seconds`` until ``stop_event`` is set."""
        if self.stop_event.wait(self._rng.uniform(0, min(MAX_JITTER_S, self.config["interval_seconds"] / 4))):
            return
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(self.config["interval_seconds"])

    def stop(self) -> None:
        self.stop_event.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._status, "skipped": dict(self._status["skipped"]),
                    "users": [dict(u) for u in self._status["users"]]}

    # ------------------------------------------------------------------ one tick
    def tick(self) -> dict[str, Any]:
        """Run every configured user once. Never raises."""
        created = duplicates = 0
        skipped: dict[str, int] = {}
        per_user: list[dict[str, Any]] = []
        last_error: str | None = None
        deadline = self._clock() + self.config["tick_budget_seconds"]
        try:
            for u in self.config["users"]:
                if self.stop_event.is_set():
                    break
                if self._clock() > deadline:
                    skipped["budget_exceeded"] = skipped.get("budget_exceeded", 0) + 1
                    per_user.append({"tenant_id": u["tenant_id"], "user_id": u["user_id"], "result": "budget_exceeded"})
                    continue
                try:
                    result, c, d = self._one(u)
                except Exception as exc:  # noqa: BLE001 - one user failing must not stop another
                    log.warning("ci_watch: user %s/%s failed (%s)", u["tenant_id"], u["user_id"], type(exc).__name__)
                    result, c, d = "error", 0, 0
                    last_error = "user_failed"
                created += c
                duplicates += d
                if result not in ("ok",):
                    skipped[result] = skipped.get(result, 0) + 1
                per_user.append({"tenant_id": u["tenant_id"], "user_id": u["user_id"], "result": result,
                                 "created": c, "duplicates": d})
        except Exception:  # noqa: BLE001 - never raise out of the loop
            last_error = "tick_failed"
            log.exception("ci_watch: tick failed")
        with self._lock:
            st = self._status
            st.update(last_at=datetime.now(UTC).isoformat(timespec="seconds"), ticks=st["ticks"] + 1,
                      created=st["created"] + created, duplicates=duplicates, skipped=skipped,
                      last_error=last_error, users=per_user)
            st["last_created"] = created
        return self.status()

    def _warn_once(self, kind: str, u: dict[str, str], message: str) -> None:
        key = (kind, u["tenant_id"], u["user_id"])
        now = self._clock()
        last = self._warned.get(key)
        if last is not None and now - last < WARN_EVERY_S:
            return
        self._warned[key] = now
        log.warning(message, u["tenant_id"], u["user_id"])

    def _one(self, u: dict[str, str]) -> tuple[str, int, int]:
        """(result, created, duplicates) for one user. A result other than ``ok`` is a fixed skip reason."""
        svc = self.assistant
        tenant, user = u["tenant_id"], u["user_id"]
        if not svc.config.get("enabled"):
            return "assistant_disabled", 0, 0
        scope = self.scope_fn(tenant, user)
        if scope != "ok":
            if scope == "no_scope":
                self._warn_once("scope", u, "ci_watch: no project_intelligence.assistant_viewers entry for %s/%s; skipped")
            return scope, 0, 0
        aid = self._assistant_id(u)
        if aid is None:
            return "no_assistant", 0, 0
        if svc._active_grant(assistant_id=aid, tool_id="watch_ci", scope="*", tenant_id=tenant, user_id=user) is None:
            if not self.config["auto_grant"]:
                self._warn_once("grant", u, "ci_watch: %s/%s has no active watch_ci grant (scope *); grant it, or set "
                                            "ci_watch.auto_grant: true. Skipped")
                return "no_grant", 0, 0
            g = svc.grant_tool(tenant_id=tenant, user_id=user, assistant_id=aid, tool_id="watch_ci", scope="*")
            svc.store.audit_record(tenant_id=tenant, user_id=user, actor=ACTOR, action="assistant.ci_watch.auto_granted",
                                   subject_id=g.grant_id, detail="standing watch_ci grant created by ci_watch.auto_grant")
            log.info("ci_watch: created a standing watch_ci grant for %s/%s (auto_grant)", tenant, user)
        out = svc.watch_ci(tenant_id=tenant, user_id=user, assistant_id=aid, actor=ACTOR,
                           source=self.config["source"], max_age_seconds=self.config["max_age_seconds"])
        return "ok", len(out.get("created") or []), int(out.get("duplicates") or 0)

    def _assistant_id(self, u: dict[str, str]) -> str | None:
        svc = self.assistant
        if u["assistant_id"]:
            try:
                a = svc.store.get_assistant(u["assistant_id"], tenant_id=u["tenant_id"], user_id=u["user_id"])
            except LookupError:
                return None
            return a.assistant_id if a.enabled else None
        for a in svc.store.list_assistants(tenant_id=u["tenant_id"], user_id=u["user_id"]):    # oldest first
            if a.enabled:
                return a.assistant_id
        return None
