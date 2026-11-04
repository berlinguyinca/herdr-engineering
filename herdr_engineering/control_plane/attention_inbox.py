"""Fleet attention inbox for the operator dashboard (design 2026-10-24-attention-inbox).

Items are derived at read time from state the control plane already holds. Only the
operator's acknowledge, snooze and clear are stored, as ``attention`` events keyed by
``(key, fingerprint)``, so a changed fingerprint reopens an item and the inbox cannot
grow with problem churn. A source that cannot be read becomes a ``source_unavailable``
item and ``partial: true``, never a clean list.
"""
from __future__ import annotations

import collections
import copy
import datetime as dt
import hashlib
import re
import threading
from urllib.parse import quote

ENTITY = "attention"
STREAM = "operator"
SEVERITIES = ("critical", "warning", "info")
KINDS = ("mission_attention", "mission_failed", "agent_failed", "agent_blocked", "ci_failed", "review_changes",
         "service_down", "host_stale", "control_plane", "fleet_drift", "llm_problems", "ci_fix", "deployment_escalation",
         "source_unavailable")
STATES = ("open", "acknowledged", "snoozed")
MAX_ITEMS = 200
MAX_PER_KIND = 50
WRITES_PER_MINUTE = 30
_KEY = re.compile(r"[\x21-\x7e]{1,256}")
_FINGERPRINT = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_FAILED = frozenset(("failed", "error", "crashed"))
_FINISHED = frozenset(("finished", "complete", "completed", "closed", "stopped", "canceled", "cancelled",
                       "succeeded", "done", "archived", "merged"))
_CURRENT = frozenset(("active", "working", "busy", "running", "executing", "pending", "queued", "planned",
                      "planning", "specification", "implementation", "review", "repair", "validation", "pr",
                      "implementing", "testing", "reviewing", "waiting", "blocked", "stalled", "paused", "open",
                      "idle", "starting", "created", "ready", "in_progress"))
_CI_FAILED = frozenset(("failure", "failed", "error", "killed", "timed_out", "crashed"))
_CI_DONE = frozenset(("success", "passed", "finished", "complete", "completed", "canceled", "cancelled",
                      "skipped", "declined", "approved", "closed", "resolved"))
_REVIEW_CHANGES = frozenset(("changes_requested", "request_changes", "rejected", "annotated", "denied"))
_CI_OUTAGE = frozenset(("unavailable", "auth_error", "partial"))
_SOURCE_PAGES = {"missions": "#/missions", "sessions": "#/herd", "summaries": "#/herd", "llm": "#/llm", "ci": "#/ci", "ci_fix": "#/ci", "reviews": "#/reviews",
                 "services": "#/services", "hosts": "#/hosts", "health": "#/topology", "fleet": "#/hosts",
                 "deployments": "#/deployments"}
_SOURCE_LABELS = {"missions": "Missions", "sessions": "Agents", "summaries": "Agent summaries", "llm": "LLM usage", "ci": "CI", "ci_fix": "CI fixes", "reviews": "Reviews",
                  "services": "Services", "hosts": "Hosts", "health": "Control plane", "fleet": "Fleet versions",
                  "deployments": "Deployments"}
_RANK = {name: index for index, name in enumerate(SEVERITIES)}


def attention_config(raw):
    """Validate ``control_plane.attention`` (YAML only); raises ValueError naming the key."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("control_plane.attention must be a mapping")

    def section(name, defaults):
        value = raw.get(name)
        if value is None:
            return dict(defaults)
        if not isinstance(value, dict):
            raise ValueError(f"control_plane.attention.{name} must be a mapping")
        unknown = sorted(set(value) - set(defaults))
        if unknown:
            raise ValueError(f"control_plane.attention.{name} has unknown keys: {', '.join(unknown)}")
        return {**defaults, **value}

    def number(where, value, low, high=None):
        if isinstance(value, bool) or not isinstance(value, int | float) or value < low or (
                high is not None and value > high):
            raise ValueError(f"control_plane.attention.{where} must be a number >= {low}"
                             + (f" and <= {high}" if high is not None else ""))
        return value

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("control_plane.attention.enabled must be true or false")
    unknown = sorted(set(raw) - {"enabled", "snooze", "thresholds", "notify", "resolved", "expected_offline_hosts"})
    if unknown:
        raise ValueError(f"control_plane.attention has unknown keys: {', '.join(unknown)}")
    snooze = section("snooze", {"min_seconds": 3600, "max_seconds": 604800})
    number("snooze.min_seconds", snooze["min_seconds"], 1)
    number("snooze.max_seconds", snooze["max_seconds"], snooze["min_seconds"])
    thresholds = section("thresholds", {"host_stale_seconds": 600, "llm_error_count": 20, "llm_window": "24h",
                                        "failed_window_hours": 168, "blocked_window_hours": 24})
    number("thresholds.host_stale_seconds", thresholds["host_stale_seconds"], 1)
    number("thresholds.llm_error_count", thresholds["llm_error_count"], 1)
    number("thresholds.failed_window_hours", thresholds["failed_window_hours"], 1)
    number("thresholds.blocked_window_hours", thresholds["blocked_window_hours"], 1)
    if thresholds["llm_window"] not in ("1h", "24h", "7d"):
        raise ValueError("control_plane.attention.thresholds.llm_window must be 1h, 24h or 7d")
    notify = section("notify", {"browser": True, "min_severity": "critical",
                                "quiet_hours": {"start": 22, "end": 7}})
    if not isinstance(notify["browser"], bool):
        raise ValueError("control_plane.attention.notify.browser must be true or false")
    if notify["min_severity"] not in SEVERITIES:
        raise ValueError("control_plane.attention.notify.min_severity must be critical, warning or info")
    quiet = notify["quiet_hours"]
    if quiet is not None:
        if not isinstance(quiet, dict) or set(quiet) - {"start", "end"}:
            raise ValueError("control_plane.attention.notify.quiet_hours must be a mapping of start and end")
        for edge in ("start", "end"):
            hour = quiet.get(edge)
            if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
                raise ValueError(f"control_plane.attention.notify.quiet_hours.{edge} must be an hour 0-23")
        notify["quiet_hours"] = {"start": quiet["start"], "end": quiet["end"]}
    resolved = section("resolved", {"keep_hours": 24, "max_items": 100})
    number("resolved.keep_hours", resolved["keep_hours"], 1)
    number("resolved.max_items", resolved["max_items"], 1, 1000)
    expected = raw.get("expected_offline_hosts")
    if expected is None:
        expected = []
    if (not isinstance(expected, list) or len(expected) > 200
            or any(not isinstance(name, str) or not name.strip() or len(name) > 200 for name in expected)):
        raise ValueError("control_plane.attention.expected_offline_hosts must be a list of host names or ids")
    return {"enabled": enabled, "snooze": snooze, "thresholds": thresholds, "notify": notify,
            "resolved": resolved, "expected_offline_hosts": [name.strip() for name in expected]}


def _norm(value):
    return value.strip().lower().replace("-", "_").replace(" ", "_") if isinstance(value, str) else ""


def status_outcome(status, stage=None):
    """Port of ``statusOutcome`` in packages/record-state.js: failed, finished, current or unknown."""
    state, phase = _norm(status), _norm(stage)

    def terminal(value):
        return "failed" if value in _FAILED else "finished" if value in _FINISHED else None

    outcome = terminal(state)
    if outcome:
        return outcome
    if not state or state in ("active", "unknown"):
        outcome = terminal(phase)
        if outcome:
            return outcome
    return "current" if (state in _CURRENT or state.startswith("paused_")
                         or (not state and phase in _CURRENT)) else "unknown"


def mission_health(activity, mission):
    """Port of ``missionHealth`` in packages/mission-live-view.js."""
    activity = activity if isinstance(activity, dict) else {}
    status = str((mission or {}).get("status") or "").strip().upper()
    if status == "COMPLETE":
        return "complete"
    if status == "FAILED":
        return "failed"
    if status == "CANCELED":
        return "canceled"
    if status == "PAUSED" or status.startswith("PAUSED_"):
        return "paused" if status == "PAUSED" else "paused: " + status[7:].lower().replace("_", " ")
    health = [activity.get("health")]
    for worker in activity.get("workerDetails") or ():
        if isinstance(worker, dict) and not worker.get("endedAt") and worker.get("state") in (
                "running", "waiting", "stalled", "blocked"):
            health.append(worker.get("health") or ("active" if worker["state"] == "running" else worker["state"]))
    for rank in ("failed", "stalled", "blocked", "slow", "waiting", "active", "complete"):
        if rank in health:
            return rank
    return "unknown"


def _epoch(value):
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value) if value < 1e11 else value / 1000.0
    if not isinstance(value, str) or not re.match(r"\d{4}-\d{2}-\d{2}T", value):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, tz=dt.UTC).isoformat().replace("+00:00", "Z")


def _short(value, limit=160):
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _fingerprint(*parts):
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _enc(value):
    return quote(str(value), safe="")


def _item(key, kind, severity, title, detail, href, source, condition, *, seen=None, count=1, action=None):
    return {"key": key, "fingerprint": _fingerprint(kind, condition), "kind": kind, "severity": severity,
            "title": _short(title, 120), "detail": _short(detail), "href": href,
            "actions": [{"id": "open", "label": action, "href": href}] if action else [],
            "source": source, "last_seen": seen, "count": count}


class AttentionInbox:
    """Derive the open attention items and fold the operator's acknowledge/snooze events."""

    def __init__(self, repo, *, config=None, ci_snapshot=None, health=None, now=None, ci_fix=None, handoffs=None):
        self.repo = repo
        self._handoffs = handoffs        # callable returning the handoff registry (it is built after the inbox)
        self._ci_fix = ci_fix
        self.config = attention_config(config)
        self._ci_snapshot = ci_snapshot
        self._health = health or repo.health
        self._now = now or repo._now
        self._lock = threading.Lock()
        self._previous = {}
        self._first_seen = {}
        self._resolved = collections.deque()
        self._writes = collections.deque()
        self.sources = {"missions": self._missions, "sessions": self._sessions, "summaries": self._blocked, "ci": self._ci,
                        "reviews": self._reviews, "services": self._services, "hosts": self._hosts,
                        "health": self._control_plane, "fleet": self._fleet, "llm": self._llm}
        if ci_fix is not None:
            self.sources["ci_fix"] = self._ci_fixes
        if getattr(repo, "deployment_escalations", None) is not None:
            self.sources["deployments"] = self._deployments

    # ------------------------------------------------------------- sources
    def _missions(self, now):
        failed_after = now - float(self.config["thresholds"]["failed_window_hours"]) * 3600
        recorded = {}
        for record in self.repo.list_missions():
            source = record.get("source")
            key = (record.get("mission_key")
                   or (f"{source}:{record.get('mission_id')}" if source and source != "herdr" else record.get("mission_id")))
            if key:
                recorded[str(key)] = record
        projections = {str(p.get("mission_key")): p for p in self.repo.mission_activity.list_summaries()
                       if p.get("mission_key")}
        items = []
        for key in sorted({*recorded, *projections}):
            record, projection = recorded.get(key, {}), projections.get(key)
            mission = (projection or {}).get("mission") or {}
            lifecycle = mission if projection else record
            activity = (projection or {}).get("activity") or {}
            title = mission.get("title") or record.get("title") or mission.get("id") or key
            href = f"#/missions/{_enc(key)}"
            outcome = status_outcome(lifecycle.get("status"), lifecycle.get("stage"))
            if outcome == "failed":
                seen = _epoch(record.get("updated_at")) or _epoch((projection or {}).get("activity_snapshot_at"))
                if seen is not None and seen < failed_after:
                    continue
                items.append(_item(f"mission:{key}", "mission_failed", "critical", title, "Mission failed", href,
                                   "missions", "failed", seen=seen, action="Open mission"))
                continue
            if projection is None or outcome != "current":
                continue
            fresh = (projection.get("freshness") or {}).get("state")
            health = mission_health(activity, mission)
            if fresh == "live" and (health in ("failed", "stalled", "blocked", "slow") or health.startswith("paused")):
                reason = activity.get("waitingReason")
                detail = f"Waiting: {_short(reason, 80)}" if reason else health.capitalize()
                items.append(_item(f"mission:{key}", "mission_attention", "warning", title, detail, href,
                                   "missions", health, seen=_epoch(projection.get("activity_snapshot_at")),
                                   action="Open mission"))
        return items

    def _handoff_context(self, session_id):
        """The handoffs that moved work into or out of this agent, newest first, so an item can say how the agent got its task.

        Facts only: what the registry recorded. An agent with no recorded handoff gets an empty list, never a guess.
        """
        registry = self._handoffs() if callable(self._handoffs) else None
        if registry is None:
            return []
        out = []
        for row in registry.list(limit=200):
            source, target = row.get("source") or {}, row.get("target") or {}
            if session_id == target.get("session_id"):
                direction, other = "in", source
            elif session_id == source.get("session_id"):
                direction, other = "out", target
            else:
                continue
            other_session = other.get("session_id")
            out.append({"handoff_id": row.get("handoff_id"), "direction": direction, "state": row.get("state"),
                        "goal": _short(row.get("goal"), 160), "agent": other.get("agent_id") or other.get("harness"),
                        "href": f"#/herd/session/{_enc(other_session)}" if other_session else None,
                        "at": _iso(row.get("updated_at")) if isinstance(row.get("updated_at"), (int, float)) else row.get("updated_at")})
            if len(out) >= 3:
                break
        return out

    def _sessions(self, now):
        failed_after = now - float(self.config["thresholds"]["failed_window_hours"]) * 3600
        items = []
        for session in self.repo.list_sessions():
            sid = session.get("session_id")
            if not sid or status_outcome(session.get("status"), session.get("stage")) != "failed":
                continue
            seen = _epoch(session.get("updated_at")) or _epoch(session.get("last_activity"))
            if seen is not None and seen < failed_after:
                continue
            name = session.get("display_name") or session.get("title") or session.get("agent_role") or "Failed agent"
            item = _item(f"session:{sid}", "agent_failed", "critical", name, "Agent reported failed",
                         f"#/herd/session/{_enc(sid)}", "sessions", "failed", seen=seen, action="Open agent")
            item["handoffs"] = self._handoff_context(sid)
            items.append(item)
        return items

    def _blocked(self, now):
        """An active agent whose latest stored AI summary names a blocker. Never read from the terminal screen."""
        after = now - float(self.config["thresholds"]["blocked_window_hours"]) * 3600
        sessions = {s.get("session_id"): s for s in self.repo.list_sessions()}
        items = []
        for event in self.repo.agent_summary_events():
            summary = event.get("summary") or {}
            session = sessions.get(event.get("session_id"))
            blockers = [" ".join(b.split()) for b in (summary.get("blockers") or ()) if isinstance(b, str) and b.strip()]
            generated = _epoch(summary.get("generated_at"))
            if (session is None or not blockers or status_outcome(session.get("status"), session.get("stage")) != "current"
                    or generated is None or generated < after):
                continue
            sid = session["session_id"]
            name = session.get("display_name") or session.get("title") or session.get("agent_role") or "Agent"
            extra = f" (+{len(blockers) - 1} more)" if len(blockers) > 1 else ""
            item = _item(f"blocked:{sid}", "agent_blocked", "warning", f"Agent blocked · {name}",
                         blockers[0] + extra, f"#/herd/session/{_enc(sid)}", "summaries",
                         "\n".join(blockers), seen=generated, action="Open agent")
            item["handoffs"] = self._handoff_context(sid)
            items.append(item)
        return items

    def _ci(self, now):
        view = self._ci_snapshot() if self._ci_snapshot else {}
        status = str(view.get("status") or "")
        if view.get("stale") or status in _CI_OUTAGE or (status == "syncing" and not view.get("pipelines")) or (view.get("available") is False and status != "not_configured"):
            raise RuntimeError("ci data is stale or unavailable")
        groups = {}
        for repository, runs in (view.get("pipelines") or {}).items():
            for run in runs or ():
                if isinstance(run, dict):
                    repo_name = str(run.get("repository") or repository)
                    groups.setdefault((repo_name, str(run.get("branch") or "")), []).append(run)
        items = []
        for (repository, branch), runs in sorted(groups.items()):
            runs.sort(key=lambda r: (_epoch(r.get("updated_at") or r.get("created_at")) or 0,
                                     _number(r.get("run_number"))), reverse=True)
            streak = []
            for run in runs:
                state = _norm(run.get("state") or run.get("status") or run.get("decision"))
                if state in _CI_FAILED:
                    streak.append(run)
                elif state in _CI_DONE:
                    break
            if not streak:
                continue
            latest = streak[0]
            href = f"#/ci?tab=failed&repository={_enc(repository)}" + (f"&branch={_enc(branch)}" if branch else "")
            where = f"{repository} {branch}".strip()
            items.append(_item(f"ci:{repository}:{branch}", "ci_failed", "critical", f"CI failed · {where}",
                               f"{len(streak)} failed run{'s' if len(streak) != 1 else ''}, latest #{latest.get('run_number', '?')}",
                               href, "ci", "failed", seen=_epoch(latest.get("updated_at") or latest.get("created_at")),
                               count=len(streak), action="Open runs"))
        return items

    def _reviews(self, now):
        from .reviews import ReviewService
        items = []
        for review in ReviewService(self.repo).list_reviews():
            state = _norm(review.get("state") or review.get("status") or review.get("decision"))
            rid = review.get("review_id") or review.get("id")
            if rid and state in _REVIEW_CHANGES:
                items.append(_item(f"review:{rid}", "review_changes", "warning",
                                   review.get("title") or "Review", state.replace("_", " ").capitalize(),
                                   f"#/reviews?tab=changes&search={_enc(rid)}", "reviews", state,
                                   seen=_epoch(review.get("updated_at") or review.get("created_at")),
                                   action="Open review"))
        return items

    def _services(self, now):
        items = []
        for service in self.repo.list_services(include_archived=False):
            life = service.get("lifecycle") or {}
            scope = (service.get("catalog") or {}).get("scope")
            status = str(service.get("status") or "").lower()
            down = status in ("unavailable", "stopped", "dead", "failed") or life.get("state") == "unavailable"
            sid = service.get("service_id")
            if sid and scope == "herdr" and down and not life.get("archived"):
                items.append(_item(f"service:{sid}", "service_down", "critical", service.get("service_name") or sid,
                                   "Service reported unavailable", f"#/services/{_enc(sid)}", "services", "down",
                                   seen=_epoch(service.get("updated_at")), action="Open service"))
        return items

    def _expected_off(self, host):
        """True when the operator listed this host (by id or name, any case) as expected to be offline."""
        names = {n.lower() for n in self.config["expected_offline_hosts"]}
        return bool(names) and any(isinstance(v, str) and v.strip().lower() in names
                                   for v in (host.get("host_id"), host.get("host_name")))

    def _host_down(self, host, now):
        """"down", "stale" or None for a host that reports fine."""
        limit = float(self.config["thresholds"]["host_stale_seconds"])
        seen = _epoch(host.get("last_seen") or host.get("updated_at"))
        if str(host.get("status") or "").lower() in ("unavailable", "offline", "failed"):
            return "down"
        return "stale" if seen is None or now - seen > limit else None

    def expected_off(self, now=None):
        """Listed hosts that are offline right now: shown quietly, never as attention items. A listed host that
        reports healthy is absent from this list and from the items alike. An unreadable host list gives []:
        the hosts source already reports itself unavailable."""
        now = self._now() if now is None else now
        if not self.config["expected_offline_hosts"]:
            return []
        try:
            hosts = list(self.repo.list_hosts())
        except Exception:
            return []
        out = []
        for host in hosts:
            if host.get("host_id") and self._expected_off(host) and (state := self._host_down(host, now)):
                seen = _epoch(host.get("last_seen") or host.get("updated_at"))
                out.append({"host_id": host["host_id"], "name": _short(host.get("host_name") or host["host_id"], 120),
                            "state": state, "last_seen": _iso(seen) if seen is not None else None})
        return sorted(out, key=lambda h: h["name"].lower())

    def _hosts(self, now):
        items = []
        for host in self.repo.list_hosts():
            hid = host.get("host_id")
            seen = _epoch(host.get("last_seen") or host.get("updated_at"))
            state = self._host_down(host, now)
            down = state == "down"
            if hid and state and not self._expected_off(host):
                items.append(_item(f"host:{hid}", "host_stale", "warning", host.get("host_name") or hid,
                                   "No recent healthy host report", f"#/hosts/{_enc(hid)}", "hosts",
                                   "down" if down else "stale", seen=seen, action="Open host"))
        return items

    def _llm(self, now):
        """One item per error class whose count in the window reaches the threshold (proxy records only)."""
        thresholds = self.config["thresholds"]
        window = thresholds["llm_window"]
        classes = {}
        for problem in self.repo.llm_errors(window).get("problems") or ():
            entry = classes.setdefault(problem["class"], {"count": 0, "models": set(), "last_seen": None})
            entry["count"] += int(problem.get("count") or 0)
            entry["models"].add(problem.get("model") or "unknown")
            seen = _epoch(problem.get("last_seen"))
            if seen is not None and (entry["last_seen"] is None or seen > entry["last_seen"]):
                entry["last_seen"] = seen
        items = []
        for cls, entry in sorted(classes.items()):
            if entry["count"] < thresholds["llm_error_count"]:
                continue
            models = len(entry["models"])
            items.append(_item(f"llm:{cls}", "llm_problems", "warning", f"LLM problems · {cls}",
                               f"{entry['count']} failed requests in {window} across {models} model{'s' if models != 1 else ''}",
                               f"#/llm?window={window}", "llm", cls, seen=entry["last_seen"],
                               count=entry["count"], action="Open LLM"))
        return items

    def _ci_fixes(self, now):
        """Finished CI fix runs that want a look: failed or expired, a draft PR to review, or nothing to change.
        Only the bounded outcome fields are shown; the run's own text never becomes a title."""
        after = now - float(self.config["thresholds"]["failed_window_hours"]) * 3600
        items = []
        for fix in self._ci_fix.list(limit=200)["fixes"]:
            ended = _epoch(fix.get("ended_at"))
            state = fix["state"]
            if state not in ("failed", "expired", "succeeded", "no_change") or ended is None or ended < after:
                continue
            what = f"{fix['repository']} #{fix['number']}"
            result = fix.get("result") or {}
            flags = [f for f in (result.get("flags") or ()) if isinstance(f, str)]
            if state in ("failed", "expired"):
                severity, title, detail = "warning", f"CI fix failed · {what}", fix.get("reason") or state
                condition = state
            elif state == "no_change":
                severity, title, detail = "info", f"CI fix found nothing to change · {what}", fix.get("reason") or "No change"
                condition = state
            else:
                severity = "warning" if flags else "info"
                title = f"CI fix opened a draft PR · {what}"
                detail = f"Flagged: {flags[0]}" + (f" (+{len(flags) - 1} more)" if len(flags) > 1 else "") if flags \
                    else "Open it from the CI page, review and merge it yourself."
                condition = "succeeded:" + "|".join(flags)
            items.append(_item(f"cifix:{fix['id']}", "ci_fix", severity, title, detail, "#/ci",
                               "ci_fix", condition, seen=ended, action="Open CI"))
        return items

    def _deployments(self, now):
        """Open deployment escalations (deploy-4 Task 8): the herdr notification for a failed
        component, read later by the operator and the assistant. Validated fields only."""
        from .deployment_escalation import attention_items
        return attention_items(self.repo.deployment_escalations, _item)

    def _control_plane(self, now):
        status = (self._health() or {}).get("status")
        if status == "ok":
            return []
        return [_item("health", "control_plane", "critical", "Control plane needs attention",
                      str(status or "Status unknown").replace("_", " "), "#/topology", "health", str(status))]

    def _fleet(self, now):
        drifted = int((self.repo.fleet_inventory().get("summary") or {}).get("drifted") or 0)
        if drifted <= 0:
            return []
        return [_item("fleet:drift", "fleet_drift", "warning", "Fleet versions differ",
                      f"{drifted} tool or plugin versions differ across hosts", "#/hosts?drift=1", "fleet",
                      "drift", action="Open hosts")]

    # ---------------------------------------------------------- stored state
    def _fold(self, now):
        """Latest stored decision per key: {key: (fingerprint, 'acknowledged'|'snoozed', until|None)}."""
        decided = {}
        for event in self.repo.attention_events():
            payload, key, fingerprint = event.get("payload") or {}, (event.get("payload") or {}).get("key"), (
                event.get("payload") or {}).get("fingerprint")
            kind = event.get("event_type")
            if kind == "attention_ack":
                decided[key] = (fingerprint, "acknowledged", None)
            elif kind == "attention_snooze":
                decided[key] = (fingerprint, "snoozed", _epoch(payload.get("until")))
            elif kind == "attention_clear" and decided.get(key, (None,))[0] == fingerprint:
                decided.pop(key, None)
        return decided

    @staticmethod
    def _apply(item, decided, now):
        state, until = "open", None
        mark = decided.get(item["key"])
        if mark and mark[0] == item["fingerprint"]:
            if mark[1] == "acknowledged":
                state = "acknowledged"
            elif mark[2] is not None and mark[2] > now:
                state, until = "snoozed", _iso(mark[2])
        return {**item, "state": state, "snoozed_until": until}

    # ------------------------------------------------------------- snapshot
    def derive(self):
        """Every current item with its state, plus the sources that could not be read."""
        now = self._now()
        derived, unavailable = [], []
        for name, source in self.sources.items():
            try:
                derived.extend(source(now))
            except Exception:  # noqa: BLE001 - a broken source is reported, never fatal or silent
                unavailable.append(name)
                derived.append(_item(f"source:{name}", "source_unavailable", "warning",
                                     f"{_SOURCE_LABELS.get(name, name)} could not be read",
                                     "Items from this source are unknown, not clear.",
                                     _SOURCE_PAGES.get(name, "#/overview"), name, "unavailable",
                                     action=f"Open {_SOURCE_LABELS.get(name, name).lower()}"))
        decided = self._fold(now)
        with self._lock:
            current = {}
            for item in derived:
                first = self._first_seen.get(item["key"])
                if first is None or (item["last_seen"] is not None and item["last_seen"] < first):
                    first = item["last_seen"] if item["last_seen"] is not None else now
                self._first_seen[item["key"]] = min(first, now)
                shown = self._apply({**item, "first_seen": _iso(self._first_seen[item["key"]]),
                                     "last_seen": _iso(item["last_seen"] if item["last_seen"] is not None else now)},
                                    decided, now)
                current[item["key"]] = shown
            keep = float(self.config["resolved"]["keep_hours"]) * 3600
            carried = {}
            for key, old in self._previous.items():
                if key in current or old["kind"] == "source_unavailable":
                    continue
                if old["source"] in unavailable:
                    carried[key] = old          # unknown, not resolved: wait for the source to come back
                    continue
                self._resolved.appendleft({**old, "state": "resolved", "resolved_at": _iso(now)})
            self._previous = {**carried, **current}
            for key in [k for k in self._first_seen if k not in self._previous]:
                del self._first_seen[key]
            while len(self._resolved) > int(self.config["resolved"]["max_items"]):
                self._resolved.pop()
            while self._resolved and _epoch(self._resolved[-1]["resolved_at"]) < now - keep:
                self._resolved.pop()
            resolved = list(self._resolved)
        return list(current.values()), resolved, unavailable

    def snapshot(self, *, state="open", kind=None, severity=None):
        if state not in ("open", "all"):
            raise ValueError("state must be open or all")
        if kind is not None and kind not in KINDS:
            raise ValueError("unknown kind")
        if severity is not None and severity not in SEVERITIES:
            raise ValueError("unknown severity")
        now = self._now()
        items, resolved, unavailable = self.derive()
        items.sort(key=lambda i: (_RANK[i["severity"]], -(_epoch(i["last_seen"]) or 0), i["key"]))
        counts = {"open": 0, "acknowledged": 0, "snoozed": 0, "resolved": len(resolved)}
        for item in items:
            counts[item["state"]] += 1
        counts["critical"] = sum(1 for i in items if i["state"] == "open" and i["severity"] == "critical")
        shown = [i for i in items if (state == "all" or i["state"] == "open")
                 and (kind is None or i["kind"] == kind) and (severity is None or i["severity"] == severity)]
        capped, per_kind = [], collections.Counter()
        for item in shown:
            if len(capped) >= MAX_ITEMS or per_kind[item["kind"]] >= MAX_PER_KIND:
                continue
            per_kind[item["kind"]] += 1
            capped.append(item)
        body = {"items": capped, "counts": counts, "partial": bool(unavailable), "unavailable": unavailable,
                "truncated": len(capped) < len(shown), "generated_at": _iso(now),
                "notify": copy.deepcopy(self.config["notify"]), "expected_off": self.expected_off(now)}
        if state == "all":
            body["resolved"] = copy.deepcopy(resolved)
        return body

    # ---------------------------------------------------------------- writes
    def _open_item(self, key, fingerprint):
        items, _, _ = self.derive()
        return next((i for i in items if i["key"] == key and i["fingerprint"] == fingerprint), None)

    def _write(self, event_type, key, fingerprint, extra, actor):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("key is required (1-256 printable characters)")
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("fingerprint is required (1-64 characters)")
        now = self._now()
        with self._lock:
            while self._writes and self._writes[0] < now - 60:
                self._writes.popleft()
            if len(self._writes) >= WRITES_PER_MINUTE:
                raise PermissionError("too many attention updates; try again in a minute")
            self._writes.append(now)
        item = self._open_item(key, fingerprint)
        if item is None:
            return {"stored": False, "reason": "not_current"}
        decided = self._fold(now).get(key)
        if event_type == "attention_clear":
            if not decided or decided[0] != fingerprint:
                return {"stored": False, "reason": "nothing_to_clear"}
        elif event_type == "attention_ack" and decided == (fingerprint, "acknowledged", None):
            return {"stored": False, "reason": "unchanged"}
        self.repo.record_attention(event_type, {"key": key, "fingerprint": fingerprint, "actor": _short(actor, 64),
                                                **extra})
        return {"stored": True, "item": self._open_item(key, fingerprint)}

    def acknowledge(self, key, fingerprint, actor="operator"):
        return self._write("attention_ack", key, fingerprint, {}, actor)

    def snooze(self, key, fingerprint, seconds, actor="operator"):
        low, high = self.config["snooze"]["min_seconds"], self.config["snooze"]["max_seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not low <= seconds <= high:
            raise ValueError(f"seconds must be between {low} and {high}")
        return self._write("attention_snooze", key, fingerprint, {"until": _iso(self._now() + seconds)}, actor)

    def clear(self, key, fingerprint, actor="operator"):
        return self._write("attention_clear", key, fingerprint, {}, actor)


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

