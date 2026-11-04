"""T15: the attention inbox, evidence-based progress and optimization analytics for one HCN room.

Three READ models and one tiny piece of per-reader state, all on the existing HCN service (`CollabService`):

* **Attention inbox** (``GET /api/v1/collab/rooms/{id}/attention``): items DERIVED at read time from what already exists
  (the reader-filtered room graph, T06 review quorum, T12/T13 steering requests, T11 proposals, T14 consultations, the
  external-agent roster). Nothing is stored per item, so the inbox cannot be spammed into storage; it is capped and
  de-duplicated. The only stored thing is each reader's acknowledgement / snooze of a (key, fingerprint) pair, as
  collab log events, bounded per reader and room.
* **Progress** (``.../progress``) and **analytics** (``.../analytics``): pure functions of one reader-filtered
  snapshot (`collab_insights`), cached per graph state.

Every read answers ONE indistinguishable 404 to anyone who is not a non-guest member of the room, exactly like the T12-T14
routes, and never takes the service lock while it scans the graph (the lock order is collab then graph, and the scan holds
neither for longer than one small chunk). Everything is off until ``control_plane.collab.insights.enabled`` is true.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime
from typing import Any

from . import collab_insights as ins
from .collab_workbench import WorkbenchError
from .lineage_metrics import lineage_metrics
from .review_quorum import pr_review_quorum

INSIGHTS_KEYS = frozenset({
    "enabled", "kill_switch", "inbox", "progress", "analytics", "evidence_writers", "verified_credit",
    "stuck_after_seconds", "blocked_escalate_seconds", "ci_failed_window_seconds", "max_items", "max_items_per_category",
    "max_prs", "max_pages", "min_group_size", "eta_min_samples", "reads_per_minute", "acks_per_10min",
    "max_acks_per_reader", "max_ack_events_per_reader", "log_headroom_percent", "ack_retention_days", "snooze_min_seconds", "snooze_max_seconds", "cache_entries"})
ATTN_EVENTS = frozenset({"attn_ack", "attn_snooze", "attn_clear"})
CATEGORIES = ("review_blocked", "stale_head", "rereview_required", "review_requested", "review_unverifiable",
              "ci_failed", "blocked", "expired_or_stuck", "steering_confirm", "promotion_confirm",
              "proposal_accept", "consult_request", "suggestion_decide", "task_assigned", "task_overdue")
TITLES = {
    "review_blocked": "Review blocked at the current head", "stale_head": "Pull request head cannot be verified",
    "rereview_required": "Re-review needed: the head moved", "review_requested": "Review requested",
    "review_unverifiable": "Review status cannot be established", "ci_failed": "CI failed", "blocked": "Work is blocked",
    "expired_or_stuck": "Agent or task is stuck or expired", "steering_confirm": "Steering request waits for your confirmation",
    "promotion_confirm": "Fork or promotion waits for your confirmation", "proposal_accept": "Proposal waits for your decision",
    "consult_request": "Someone asked you a question",
    "suggestion_decide": "A guest's request waits for your decision",
    "task_assigned": "A task is assigned to you", "task_overdue": "A task assigned to a person needs attention"}
_ACK_KEY = re.compile(r"[a-z_]{1,32}\|[\x21-\x7e]{1,420}\Z")
_FP = re.compile(r"[0-9a-f]{24}\Z")
PRIORITY_RULES = [
    {"severity": "error", "means": "something is failing or blocked by a reviewer right now", "categories": ["review_blocked", "ci_failed"]},
    {"severity": "warning", "means": "a head moved, an agent is stuck, work has been blocked a long time",
     "categories": ["stale_head", "rereview_required", "expired_or_stuck", "blocked (after the escalation time)",
                    "task_overdue"]},
    {"severity": "notice", "means": "waiting for a person or a review",
     "categories": ["review_requested", "blocked", "steering_confirm", "promotion_confirm", "proposal_accept", "consult_request",
                    "suggestion_decide", "task_assigned"]},
    {"severity": "info", "means": "context only", "categories": ["review_unverifiable"]}]
ORDER_RULE = "severity (error first), then items addressed to you, then the oldest first, then key"
MAX_SCAN_ROWS = 200      # rows of any one collab-side list examined per read


def normalize_insights(raw: Any) -> dict:
    """Validate ``control_plane.collab.insights`` (YAML only). Unknown keys and wrong types raise ValueError. Everything
    is OFF until ``enabled: true``; ``kill_switch: true`` turns it all off again at once."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.collab.insights must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - INSIGHTS_KEYS)
    if unknown:
        raise ValueError("control_plane.collab.insights: unknown keys: " + ", ".join(unknown))
    cfg = {"enabled": False, "kill_switch": False, "inbox": True, "progress": True, "analytics": True,
           "evidence_writers": [], "verified_credit": 0.5, "stuck_after_seconds": 1800, "blocked_escalate_seconds": 3600,
           "ci_failed_window_seconds": 7 * 86400, "max_items": 100, "max_items_per_category": 25, "max_prs": 20,
           "max_pages": 3, "min_group_size": 5, "eta_min_samples": 5, "reads_per_minute": 60, "acks_per_10min": 60,
           "max_acks_per_reader": 200, "max_ack_events_per_reader": 500, "log_headroom_percent": 80, "ack_retention_days": 30, "snooze_min_seconds": 300,
           "snooze_max_seconds": 14 * 86400, "cache_entries": 8}
    ranges = {"stuck_after_seconds": (60, 7 * 86400), "blocked_escalate_seconds": (60, 30 * 86400),
              "ci_failed_window_seconds": (3600, 90 * 86400), "max_items": (10, 500), "max_items_per_category": (1, 100),
              "max_prs": (1, 50), "max_pages": (1, 8), "min_group_size": (2, 50), "eta_min_samples": (3, 100),
              "reads_per_minute": (1, 240), "acks_per_10min": (1, 200), "max_acks_per_reader": (10, 500), "max_ack_events_per_reader": (10, 5000), "log_headroom_percent": (10, 100),
              "ack_retention_days": (1, 90), "snooze_min_seconds": (60, 86400), "snooze_max_seconds": (3600, 90 * 86400),
              "cache_entries": (1, 32)}
    for key, value in raw.items():
        if key in ("enabled", "kill_switch", "inbox", "progress", "analytics"):
            if not isinstance(value, bool):
                raise ValueError(f"control_plane.collab.insights.{key} must be true or false")
        elif key == "evidence_writers":
            if (not isinstance(value, list) or len(value) > 32
                    or not all(isinstance(w, str) and 0 < len(w) <= 128 for w in value)):
                raise ValueError("control_plane.collab.insights.evidence_writers must be a list of at most 32 graph principal ids")
            value = sorted(set(value))
        elif key == "verified_credit":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError("control_plane.collab.insights.verified_credit must be a number from 0 to 1")
            value = float(value)
        else:
            lo, hi = ranges[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"control_plane.collab.insights.{key} must be an integer {lo}-{hi}")
        cfg[key] = value
    if cfg["snooze_min_seconds"] > cfg["snooze_max_seconds"]:
        raise ValueError("control_plane.collab.insights.snooze_min_seconds must not exceed snooze_max_seconds")
    return cfg


class AttentionMixin:
    """State and operations for T15. Mixed into ``CollabService`` next to the T12 workbench and the T14 context mixin; it
    relies on the service's lock, log, rooms, `_commit`, `_authorize`, `_member`, `_throttle`, `graph_source`,
    `graph_access` and the T12/T13/T11/T14 indexes, and changes none of them."""

    def _init_attention(self) -> None:
        self.insights = normalize_insights(None)
        self.insights_source = None          # optional callable -> raw YAML mapping, read per request (hot kill switch)
        self.quorum_config = None            # T06 QuorumConfig, handed over by the server; None = review items off
        self._attn: dict[tuple, OrderedDict] = {}
        self._attn_events: dict[tuple, int] = {}   # ack events written per (reader, room): a hard cap on log growth
        self._snap_cache: OrderedDict = OrderedDict()

    # --------------------------------------------------------------- config
    def insights_cfg(self) -> dict:
        """Effective settings NOW. A source that fails or returns something invalid means OFF (fail closed)."""
        if self.insights_source is None:
            return self.insights
        try:
            return normalize_insights(self.insights_source())
        except Exception:
            return {**normalize_insights(None), "enabled": False, "kill_switch": True}

    # ------------------------------------------------------------ the fold
    def _apply_attention(self, ev: dict) -> bool:
        t, p = ev["event_type"], ev["payload"]
        if t not in ATTN_EVENTS:
            return False
        who = (p["actor_user_id"], p["scope_room_id"])
        state = self._attn.setdefault(who, OrderedDict())
        self._attn_events[who] = self._attn_events.get(who, 0) + 1
        horizon = p["epoch"] - p.get("retention", 30 * 86400)
        for old in [k for k, v in state.items() if v["at"] < horizon][:8]:       # lapsed marks are dropped as new ones arrive
            state.pop(old, None)
        if t == "attn_clear":
            state.pop(p["key"], None)
        else:
            state[p["key"]] = {"kind": "ack" if t == "attn_ack" else "snooze", "fp": p["fp"],
                               "until": p.get("until_epoch"), "at": p["epoch"]}
            state.move_to_end(p["key"])
            while len(state) > p["cap"]:             # the cap rode in the event, so a replay under another config is identical
                state.popitem(last=False)
        return True

    # -------------------------------------------------------------- guards
    def _insight_access(self, pr, room_id: str, feature: str, what: str, *, reads: bool = True):
        """(cfg, room, graph, reader, contexts, since) for a signed-in non-guest member whose graph access covers the
        room. Every miss (no such room, not a member, a guest, no graph) is the same 404; the feature switch is only
        revealed to members."""
        from .collab import CollabError
        self._require_user(pr)
        cfg = self.insights_cfg()
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", what)
            graph = self.graph_source() if self.graph_source is not None else None
            access = self.graph_access(pr) if graph is not None else None
            if access is None or room_id not in access["rooms"]:
                self.stats["policy_denied"] += 1
                raise CollabError(404, "not found")
            if not (cfg["enabled"] and not cfg["kill_switch"] and cfg[feature]):
                self.stats["policy_denied"] += 1
                raise WorkbenchError(403, "this feature is switched off on this service", code="insights_disabled")
            if reads:             # marks spend their own bucket (below), never the read budget
                self._throttle("insights:" + pr.key, cfg["reads_per_minute"], 60.0, "insights read")
                self._throttle("insights-room:" + room_id, cfg["reads_per_minute"] * 10, 60.0, "insights read")
            else:
                self._throttle("attn-mark:" + pr.key, cfg["acks_per_10min"], 600.0, "acknowledgement")
            reader = self._room_reader(access, room_id, pr.id,
                                       keep=(*cfg["evidence_writers"], f"collab:room:{room_id}"))
            contexts = frozenset({("room", room_id, None), *(("mission", m, r) for r, m in access["room_missions"]
                                                             if r == room_id)})
            since = None
            if self.replay_history == "since_join":
                joined = ((room.get("members") or {}).get(pr.id) or {}).get("joined_at")
                try:
                    since = datetime.fromisoformat(joined).timestamp()
                except (TypeError, ValueError):
                    since = self._now()          # no recorded join time: fail closed, history starts now
        return cfg, room, graph, reader, contexts, since

    def _snapshot_for(self, cfg, room_id, graph, reader, contexts, since) -> dict:
        """The reader's snapshot of the room, cached per (reader, room, graph state, access): repeated polling by the
        same person costs one scan per change, and a membership or link change is a new key."""
        key = (reader["id"], room_id, id(graph), graph.events_applied, since, frozenset(contexts),
               frozenset(reader["access"]["room_ws"]), cfg["max_pages"], reader.get("keep_writers"))
        with self._lock:
            hit = self._snap_cache.get(key)
            if hit is not None:
                self._snap_cache.move_to_end(key)
                return hit
        mission_ctxs = sorted((c for c in contexts if c[0] == "mission"), key=repr)
        snap = ins.take_snapshot(graph, reader, contexts, since, max_pages=cfg["max_pages"], mission_ctxs=mission_ctxs)
        with self._lock:
            self._snap_cache[key] = snap
            while len(self._snap_cache) > cfg["cache_entries"]:
                self._snap_cache.popitem(last=False)
        return snap

    def _quorum_fn(self, graph, reader, cfg):
        """A memoised, budgeted ``pr_id -> quorum | None`` over the T06 quorum with the room-narrowed reader."""
        qcfg = self.quorum_config
        if qcfg is None:
            return None
        memo: dict[str, Any] = {}
        spent = [0]

        def quorum(pr_id: str):
            if pr_id in memo:
                return memo[pr_id]
            if spent[0] >= cfg["max_prs"]:
                memo[pr_id] = None            # beyond the budget: not evaluated, so never counted
                return None
            spent[0] += 1
            try:
                memo[pr_id] = pr_review_quorum(graph, reader, pr_id, qcfg)
            except KeyError:
                memo[pr_id] = None
            return memo[pr_id]
        return quorum

    # ================================================================ progress / analytics
    def room_progress(self, pr, room_id: str, query: dict) -> dict:
        return self._wb_call(self._room_insight, pr, room_id, "progress")

    def room_analytics(self, pr, room_id: str, query: dict) -> dict:
        return self._wb_call(self._room_insight, pr, room_id, "analytics")

    def _room_insight(self, pr, room_id: str, which: str) -> dict:
        cfg, _room, graph, reader, contexts, since = self._insight_access(pr, room_id, which, f"insights.{which}")
        snap = self._snapshot_for(cfg, room_id, graph, reader, contexts, since)
        quorum = self._quorum_fn(graph, reader, cfg)
        now = self._now()
        if which == "progress":
            out = ins.progress(snap, cfg, room_id, now, quorum)
            out["review_quorum"] = "configured" if self.quorum_config is not None else "not configured: review does not count as evidence"
        else:
            out = ins.analytics_of(snap, cfg, room_id, now, quorum)
            out["delivery"] = lineage_metrics(snap, cfg, now)       # handoffs, gates, findings, lead time, usage
        return {"room_id": room_id, "generated_at": self._iso(now), "trusted_evidence_configured": bool(cfg["evidence_writers"]),
                **out}

    # ================================================================ inbox
    def attention_inbox(self, pr, room_id: str, query: dict) -> dict:
        return self._wb_call(self._attention_inbox, pr, room_id, query)

    def _attention_inbox(self, pr, room_id: str, query: dict) -> dict:
        cfg, room, graph, reader, contexts, since = self._insight_access(pr, room_id, "inbox", "attention.list")
        include = query.get("include")
        if include not in (None, "", "acked"):
            raise WorkbenchError(400, "include must be 'acked' or absent")
        out = self._inbox_core(pr, room_id, cfg, room, graph, reader, contexts, since, include)
        out.pop("_current", None)
        return out

    def _inbox_core(self, pr, room_id, cfg, room, graph, reader, contexts, since, include) -> dict:
        now = self._now()
        with self._lock:                       # phase 1: copy the small collab-side rows, then let go of the lock
            rows = self._personal_rows(pr, room_id, room, cfg)
            saved = dict(self._attn.get((pr.id, room_id), {}))
        snap = self._snapshot_for(cfg, room_id, graph, reader, contexts, since)        # phase 2: graph reads, no service lock
        quorum = self._quorum_fn(graph, reader, cfg)
        items, budget = ins.graph_items(snap, cfg, now, quorum)
        items += self._personal_items(rows, graph, reader, cfg, now)
        by_key: dict[str, dict] = {}
        for it in items:                       # de-duplicate: one item per (category, subject), the most severe wins
            old = by_key.get(it["key"])
            if old is None or ins.SEVERITY_RANK[it["severity"]] > ins.SEVERITY_RANK[old["severity"]]:
                by_key[it["key"]] = it
        shown, quiet, acked, snoozed = [], [], 0, 0
        horizon = now - cfg["ack_retention_days"] * 86400
        for it in ins.order_items(list(by_key.values())):
            st = saved.get(it["key"])
            if st is not None and st["at"] < horizon:
                st = None                       # an old acknowledgement lapses
            it["state"] = "open"
            if st is not None:
                if st["fp"] != it["fp"]:
                    it["resurfaced"] = True      # the condition changed since you dealt with it
                elif st["kind"] == "ack":
                    it["state"] = "acknowledged"
                elif st["until"] is not None and now < st["until"]:
                    it["state"] = "snoozed"
                    it["snoozed_until"] = self._iso(st["until"])
            if it["state"] == "open":
                shown.append(it)
            else:
                acked += it["state"] == "acknowledged"
                snoozed += it["state"] == "snoozed"
                quiet.append(it)
        per: dict[str, int] = {}
        capped: dict[str, int] = {}
        final = []
        for it in shown:
            c = it["category"]
            if per.get(c, 0) >= cfg["max_items_per_category"] or len(final) >= cfg["max_items"]:
                capped[c] = capped.get(c, 0) + 1
                continue
            per[c] = per.get(c, 0) + 1
            final.append(it)
        for it in final:
            it["title"] = TITLES[it["category"]]
        out_quiet = []
        if include == "acked":
            for it in quiet[:cfg["max_items"]]:
                it["title"] = TITLES[it["category"]]
                out_quiet.append(it)
        sev: dict[str, int] = {}
        for it in final:
            sev[it["severity"]] = sev.get(it["severity"], 0) + 1
        return {"_current": {it["key"]: it["fp"] for it in by_key.values()},
                "room_id": room_id, "generated_at": self._iso(now), "items": final, "quiet_items": out_quiet,
                "counts": {"open": len(final) + sum(capped.values()), "shown": len(final), "by_category": per, "by_severity": sev,
                           "acknowledged": acked, "snoozed": snoozed, "not_shown_because_of_caps": capped},
                "truncated": snap["reason"], "timeline_truncated": snap["timeline_reason"],
                "prs_not_evaluated": budget["prs_skipped"] or None,
                "review_quorum": "configured" if self.quorum_config is not None else "not configured: no review items",
                "priority": {"order": ORDER_RULE, "severity": PRIORITY_RULES},
                "limits": {"max_items": cfg["max_items"], "max_items_per_category": cfg["max_items_per_category"],
                           "stuck_after_seconds": cfg["stuck_after_seconds"]},
                "as_of": snap["as_of"]}

    # ---- rows addressed to this reader, copied under the lock (no graph access here)
    def _personal_rows(self, pr, room_id: str, room: dict, cfg: dict) -> dict:
        from .collab import ROLE_ACTIONS
        m = self._member(room, pr.id) or {}
        role = m.get("role")
        rows: dict[str, list] = {"steer": [], "proposal": [], "consult": [], "agent": [], "suggestion": [], "task": []}
        rows["task"] = self._tr2_attention_rows(pr, room_id, role)       # team roles slice 2: ids and dates only
        if role == "owner":                     # team rooms slice 1: the Suggest queue is the owner's to decide
            rows["suggestion"] = self._tr_attention_rows(room_id)
        scfg = self.steer_cfg()
        if scfg["enabled"] and not scfg["kill_switch"] and role in scfg["confirm_roles"]:
            for sid in reversed(self._steers_by_room.get(room_id, ())[-MAX_SCAN_ROWS:]):
                r = self._steers[sid]
                if self._steer_status(r) != "pending" or (r["proposer"] == pr.id and not scfg["self_confirm"]):
                    continue
                rows["steer"].append({"id": sid, "action": r["action"], "node_id": r["node_id"], "node_type": r["node_type"],
                                      "proposer": r["proposer"], "at": r["proposed_at"], "expires_epoch": r["expires_epoch"]})
        if self.delegation_enabled and "invite" in ROLE_ACTIONS.get(role, ()):
            for pid in reversed(self._proposals_by_room.get(room_id, ())[-MAX_SCAN_ROWS:]):
                p = self._proposals[pid]
                if self._proposal_state(p) == "quarantined":
                    rows["proposal"].append({"id": pid, "kind": p["kind"], "size": p["size"], "at": p["created_at"]})
        if self._ctx_on(self.ctx_cfg(), "consultation"):
            for cid in reversed(self._consults_by_room.get(room_id, ())[-MAX_SCAN_ROWS:]):
                r = self._consults[cid]
                if r["target_kind"] == "user" and r["target_id"] == pr.id and self._consult_state(r) == "open":
                    rows["consult"].append({"id": cid, "asker": r["asker"], "node_id": r["node_id"], "at": r["asked_at"],
                                            "expires_epoch": r["expires_epoch"]})
        now = self._now()
        for aid in list(self._externals_by_room.get(room_id, ()))[:MAX_SCAN_ROWS]:
            a = self._agents.get(aid)
            if not a or not a.get("external"):
                continue
            state = self._external_state(a, room, aid)
            ends = min(a["expires_at"], a["external"]["hard_expiry"])
            pres = self._presence.get(aid)
            if state == "expired" and now - ends < 7 * 86400:
                rows["agent"].append({"id": aid, "label": a["external"].get("label"), "why": "expired", "ended": ends})
            elif state == "active" and pres is not None and now - pres["at"] >= cfg["stuck_after_seconds"]:
                rows["agent"].append({"id": aid, "label": a["external"].get("label"), "why": "silent",
                                      "silent": int(now - pres["at"]), "ended": pres["at"]})
        return rows

    def _personal_items(self, rows: dict, graph, reader: dict, cfg: dict, now: float) -> list[dict]:
        out: list[dict] = []

        def item(category, severity, key_id, subject, fp, since, detail=None, expires=None):
            out.append({"key": f"{category}|{key_id}", "category": category, "severity": severity, "subject": subject,
                        "since": since, "for_you": category in ("steering_confirm", "promotion_confirm", "proposal_accept",
                                                                 "consult_request", "suggestion_decide", "task_assigned",
                                                                 "task_overdue"),
                        "fp": ins.fingerprint(category, key_id, *fp), "detail": detail, "expires_at": expires})
        for r in rows["steer"]:
            node = graph.node_context(reader, r["node_id"])
            if node is None:
                continue                      # a target this reader cannot see is not listed, exactly like the steering list
            cat = "promotion_confirm" if r["action"] in ("fork", "promote") else "steering_confirm"
            item(cat, "notice", r["id"], {"node_id": r["node_id"], "node_type": node["node_type"], "label": ins.clip(node["label"])},
                 (r["action"],), r["at"], {"action": r["action"], "proposed_by": r["proposer"], "steering_id": r["id"]},
                 self._iso(r["expires_epoch"]))
        for r in rows["proposal"]:
            item("proposal_accept", "notice", r["id"], {"node_id": None, "node_type": "proposal", "label": None},
                 (r["kind"],), r["at"], {"proposal_id": r["id"], "kind": ins.clip(r["kind"], 24), "size": r["size"]})
        for r in rows["suggestion"]:             # ids and dates only: the request's words stay in the queue, never the inbox
            item("suggestion_decide", "notice", r["id"], {"node_id": None, "node_type": "suggestion", "label": None},
                 (r["digest"],), r["at"], {"suggestion_id": r["id"], "tainted": True}, self._iso(r["expires_epoch"]))
        for r in rows["task"]:                   # ids, role and dates only: the task's words stay in my-tasks
            overdue = r["kind"] == "task_overdue"
            item(r["kind"], "warning" if overdue else "notice", r["id"], {"node_id": None, "node_type": "task", "label": None},
                 (r.get("reason") or "", int(r["expires_epoch"] or 0)), r["at"],
                 {"task_id": r["id"], "role": r.get("role"), **({"reason": r["reason"]} if r.get("reason") else {})},
                 self._iso(r["expires_epoch"]) if r.get("expires_epoch") else None)
        for r in rows["consult"]:
            node = graph.node_context(reader, r["node_id"])
            subject = ({"node_id": r["node_id"], "node_type": node["node_type"], "label": ins.clip(node["label"])}
                       if node is not None else {"hidden": True})
            item("consult_request", "notice", r["id"], subject, (), r["at"],
                 {"consult_id": r["id"], "asked_by": r["asker"]}, self._iso(r["expires_epoch"]))
        for r in rows["agent"]:
            item("expired_or_stuck", "warning", "ext:" + r["id"],
                 {"node_id": "agent:" + r["id"], "node_type": "agent", "label": ins.clip(r["label"])},
                 (r["why"], int(r["ended"])), self._iso(r["ended"]),
                 {"reported_by": "server presence", "why": r["why"], **({"silent_for_seconds": r["silent"]} if "silent" in r else {})})
        return out

    # ================================================================ ack / snooze
    def attention_ack(self, pr, room_id: str, body: dict) -> dict:
        return self._wb_call(self._attention_mark, pr, room_id, body, "ack")

    def attention_snooze(self, pr, room_id: str, body: dict) -> dict:
        return self._wb_call(self._attention_mark, pr, room_id, body, "snooze")

    def attention_restore(self, pr, room_id: str, body: dict) -> dict:
        return self._wb_call(self._attention_mark, pr, room_id, body, "clear")

    def _attention_mark(self, pr, room_id: str, body: dict, kind: str) -> dict:
        """Acknowledge or snooze ONE item for the calling reader only, or take that back. The mark is bound to the item's
        fingerprint, so when the condition changes the item comes back. State is per reader and room, bounded, and
        rate limited. The response never says whether the key exists (nothing to probe)."""
        cfg, room, graph, reader, contexts, since = self._insight_access(pr, room_id, "inbox", f"attention.{kind}", reads=False)
        allowed = {"ack": ("key", "fp"), "snooze": ("key", "fp", "seconds"), "clear": ("key",)}[kind]
        if [k for k in body if k not in allowed]:
            raise WorkbenchError(400, "unknown field")
        key = body.get("key")
        if not isinstance(key, str) or not _ACK_KEY.fullmatch(key) or key.split("|", 1)[0] not in CATEGORIES:
            raise WorkbenchError(400, "invalid key")
        fp = body.get("fp")
        if kind != "clear" and (not isinstance(fp, str) or not _FP.fullmatch(fp)):
            raise WorkbenchError(400, "invalid fp")
        until = None
        if kind == "snooze":
            secs = body.get("seconds")
            if isinstance(secs, bool) or not isinstance(secs, int) or not cfg["snooze_min_seconds"] <= secs <= cfg["snooze_max_seconds"]:
                raise WorkbenchError(400, f"seconds must be an integer {cfg['snooze_min_seconds']}-{cfg['snooze_max_seconds']}")
        if kind != "clear":
            # only a CURRENTLY VISIBLE item of this reader can be marked, with its current fingerprint; anything else
            # (a hidden node, a made-up key, a stale fingerprint) gets one generic answer, so marks are no probe
            current = self._inbox_core(pr, room_id, cfg, room, graph, reader, contexts, since, None)["_current"]
            if current.get(key) != fp:
                raise WorkbenchError(404, "not found")
        with self._lock:
            now = self._now()
            have = self._attn.get((pr.id, room_id), {}).get(key)
            if kind == "ack" and have and have["kind"] == "ack" and have["fp"] == fp:
                return {"ok": True, "state": "acknowledged", "replayed": True}
            if kind == "clear" and have is None:
                return {"ok": True, "state": "open", "replayed": True}
            if (self._attn_events.get((pr.id, room_id), 0) >= cfg["max_ack_events_per_reader"]
                    or len(self._log) * 100 >= self.max_log_events * cfg["log_headroom_percent"]):
                # marks may never use up the log that every other collab write needs: refuse them first
                raise WorkbenchError(429, "too many acknowledgements recorded; marks are paused", code="marks_exhausted")
            if kind == "snooze":
                until = now + body["seconds"]
            self._commit("attn_" + ("snooze" if kind == "snooze" else kind), {
                "actor_user_id": pr.id, "actor_agent_id": None, "actor_kind": "user", "scope_room_id": room_id, "key": key,
                "fp": fp, "until_epoch": until, "epoch": now, "cap": cfg["max_acks_per_reader"],
                "retention": cfg["ack_retention_days"] * 86400})
        return {"ok": True, "state": {"ack": "acknowledged", "snooze": "snoozed", "clear": "open"}[kind],
                **({"snoozed_until": self._iso(until)} if until else {})}
