"""The complete life of one agent session, in ordered phases, plus a compact record a later learning step can read.

Same rule as ``pr_trail``: **a value is null with a reason unless there is positive evidence, and a wrong attribution is worse than
missing data.** Everything is derived from records the server already holds; nothing is scraped and nothing is guessed.

Phases (each lists only items that exist; a phase with no evidence says so and why, it is never invented):

* ``assigned``   the session record (task context) and the handoffs that gave it work;
* ``worked``     tool calls from the inspector buffer grouped into runs (a gap longer than ``RUN_GAP`` starts a new run), and the
                 status transitions in the recorded event stream;
* ``handed_off`` handoffs this agent passed on;
* ``produced``   commits (CI runs) and pull requests ``session_commits`` links to this session, each PR linking to its PR trail;
* ``verified``   the CI result of every linked run and the reviews attached by ``session_id``;
* ``outcome``    the last reported status, and for every PR the trail's own signals (copied, never recomputed);
* ``cost``       the priced usage estimate for this session.

Attribution is by evidence only. A handoff attaches by an explicit ``session_id``, or by an ``agent_id`` that belongs to exactly one
known session (this one); a handoff that names neither on a side leaves that side's counts unknown (null), because it may be this
agent's. A review attaches by ``session_id``; a commit or PR by ``session_commits``' rules (commit, branch+window). Agents that
merely share the mission are ``hints`` and never feed a number in ``learning_record``. A pull request's signals cover every agent on
that PR; they are carried only when the PR trail itself lists this agent.

Derived, and labelled so:

* **retries**: a call whose agent, tool and arguments equal an earlier call (in buffer sequence order) that ended in ``error``.
  Null when any argument is withheld, redacted, truncated or absent, or any call has no usable sequence number.
* **restarts**: a move from a terminal status (finished, failed, completed, done, stopped, exited, cancelled, terminated, error,
  killed) to any non-terminal one, in the stream's own sequence order. Null when an event is undated, a sequence is missing,
  repeated or out of order, timestamps run backwards, or a status is in none of the known classes (it could be terminal).
* **ended_at / duration**: the time of the transition into the terminal status the session still reports; else null.
* **outcome**: terminal statuses classify as they are; a non-terminal one only while the last report is fresher than
  ``control_plane.evolution.stale_after_minutes`` (default 120), otherwise null.

Tool figures are null unless the inspector buffer is provably whole: nothing evicted, nothing cut off, and the agent started inside
the buffer's own retention window. A buffer rebuilt after a control-plane restart holds only what was stored durably, so it can look
whole when it is not; the retention rule is the only guard the server can offer. Caps apply to displayed rows only; every aggregate
uses the full set. Pure: ``build`` reads what it is given and writes nothing. ``TTLCache`` is the short per-session cache.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .pr_trail import (
    SPEND_NOTE,
    USAGE_DAYS,
    TTLCache,
    _dict,
    _iso,
    _review_rows,
    _signal,
    _text,
    _time,
)
from .project_links import canonical_repository
from .session_commits import _ACTIVE, _branch, session_commits

MAX_RUNS = 30
MAX_TRANSITIONS = 50
MAX_HANDOFFS = 30
MAX_REVIEWS = 30
MAX_PRS = 20
MAX_TRAILS = 5                            # pull request trails read per request; more linked PRs make the PR figures null
MAX_COMMITS = 50
MAX_TOOLS_PER_RUN = 10
RUN_GAP = timedelta(minutes=10)           # silence longer than this ends a run of tool calls
TOOL_LIMIT = 1000                         # events read from the inspector buffer in one request
DEFAULT_STALE_MINUTES = 120               # a non-terminal status older than this is not an outcome
_LAST = datetime.max.replace(tzinfo=UTC)
_TERMINAL = frozenset({"finished", "failed", "completed", "done", "stopped", "exited", "cancelled", "terminated", "error", "killed"})
_FINISHED = frozenset({"finished", "completed", "done"})
_FAILED = frozenset({"failed", "error", "killed"})
_BLOCKED = frozenset({"blocked", "needs_input", "needs-input", "stuck"})
_STARTING = frozenset({"starting", "pending", "created", "launching", "queued", "spawning"})
_KNOWN = _TERMINAL | _ACTIVE | _BLOCKED | _STARTING
_PROBLEM = frozenset({"rejected", "failed", "expired", "cancelled"})
_PHASES = (("assigned", "Assigned"), ("worked", "Worked"), ("handed_off", "Handed off"), ("produced", "Produced"),
           ("verified", "Verified"), ("outcome", "Outcome"), ("cost", "Cost"))


def evolution_config(mapping):
    """Validated ``control_plane.evolution`` (None = defaults). Raises ValueError naming the bad key."""
    if mapping is None:
        return {"stale_after_minutes": DEFAULT_STALE_MINUTES}
    if not isinstance(mapping, dict):
        raise ValueError("control_plane.evolution must be a mapping")
    unknown = set(mapping) - {"stale_after_minutes"}
    if unknown:
        raise ValueError(f"control_plane.evolution has an unknown key: {sorted(unknown)[0]}")
    minutes = mapping.get("stale_after_minutes", DEFAULT_STALE_MINUTES)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 10080:
        raise ValueError("control_plane.evolution.stale_after_minutes must be a whole number of minutes from 1 to 10080")
    return {"stale_after_minutes": minutes}


def _status(value):
    return str(value or "").strip().lower()


def _phase(key, state, reason, items=(), omitted=0, **extra):
    title = dict(_PHASES)[key]
    return {"key": key, "title": title, "state": state, "reason": reason, "items": list(items), "omitted": omitted, **extra}


def _handoff_detail(row, rule, self_handoff=False):
    """One handoff from this agent's point of view; the lists are bounded free text."""
    row = _dict(row)
    entries = [h for h in (row.get("history") if isinstance(row.get("history"), list) else []) if isinstance(h, dict)]
    queued = next((_time(h.get("at")) for h in entries if h.get("state") == "queued"), None)
    accepted = next((_time(h.get("at")) for h in entries if h.get("state") == "accepted"), None)
    states = {str(h.get("state")) for h in entries} | {str(row.get("state"))}

    def lines(name):
        value = row.get(name)
        return [_text(x, 240) for x in (value if isinstance(value, list) else [])[:10] if x not in (None, "")]
    source, target = _dict(row.get("source")), _dict(row.get("target"))
    return {"handoff_id": _text(row.get("handoff_id"), 160), "rule": rule, "state": _text(row.get("state"), 40),
            "at": _iso(row.get("created_at")), "goal": _text(row.get("goal")), "self_handoff": self_handoff,
            "from": {k: _text(source.get(k), 120) for k in ("harness", "agent_id", "session_id")},
            "to": {k: _text(target.get(k), 120) for k in ("harness", "agent_id", "session_id")},
            "completed": lines("completed"), "remaining": lines("remaining"), "decisions": lines("decisions"),
            "time_to_accept_s": round((accepted - queued).total_seconds()) if queued and accepted and accepted >= queued else None,
            "rejected": "rejected" in states, "failed": "failed" in states, "problem": bool(states & _PROBLEM)}


def _side(descriptor, session, others):
    """('mine' | 'other' | 'unknown', rule). A session id decides; an agent id decides only when it belongs to exactly one known session."""
    descriptor = _dict(descriptor)
    sid = descriptor.get("session_id")
    if isinstance(sid, str) and sid:
        return ("mine" if sid == session.get("session_id") else "other"), "session_id"
    agent = descriptor.get("agent_id")
    if isinstance(agent, str) and agent:
        owners = [s for s in [session, *others] if agent in (s.get("session_id"), s.get("agent_id"))]
        if len(owners) == 1:
            return ("mine" if owners[0] is session else "other"), "agent_id_unique"
    return "unknown", None


def _handoffs(rows, truncated, session, others):
    """Handoffs from this agent's point of view.

    Returns ``{ins, outs, state, unknown_in, unknown_out}``. A handoff that names this agent on neither a session id nor a unique
    agent id leaves that side unknown: it may be this agent's, so the counts for the side are withheld. A handoff from this
    session to itself is listed once, as an outgoing one, and flagged.
    """
    if rows is None:
        return {"ins": [], "outs": [], "state": "unavailable", "unknown_in": 0, "unknown_out": 0}
    ins, outs, unknown_in, unknown_out = [], [], 0, 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        target, rule_in = _side(row.get("target"), session, others)
        source, rule_out = _side(row.get("source"), session, others)
        if source == "mine" and target == "mine":
            outs.append(_handoff_detail(row, rule_out, self_handoff=True))
            continue
        if target == "mine":
            ins.append(_handoff_detail(row, rule_in))
        elif target == "unknown":
            unknown_in += 1
        if source == "mine":
            outs.append(_handoff_detail(row, rule_out))
        elif source == "unknown":
            unknown_out += 1
    for group in (ins, outs):
        group.sort(key=lambda h: _time(h["at"]) or _LAST)
    return {"ins": ins, "outs": outs, "state": "truncated" if truncated else "ok", "unknown_in": unknown_in, "unknown_out": unknown_out}


def _tool_calls(tools, session, now):
    """(rows, state, reason). A row is ``{at, seq, agent, tool, args, status, duration_ms, unreadable}``; state ok / unavailable / none / partial.

    ``ok`` means the buffer is provably whole: nothing evicted, nothing cut off, and the agent started inside the retention window the
    buffer itself reports.
    """
    if tools is None:
        return [], "unavailable", "Tool records could not be read."
    if tools.get("absent"):
        return [], "none", ("The inspector buffer holds no record of this session: it may never have reported a tool call, or its events may have been evicted "
                            "or not restored after a control-plane restart, so this is not a finding that it made no call.")
    events = [e for e in (tools.get("events") if isinstance(tools.get("events"), list) else []) if isinstance(e, dict) and e.get("kind") == "tool"]
    rows = []
    for e in events:
        data = _dict(e.get("data"))
        duration = data.get("duration_ms")
        args = data.get("args_preview") if isinstance(data.get("args_preview"), str) else None
        seq = e.get("seq") if isinstance(e.get("seq"), int) and not isinstance(e.get("seq"), bool) else None
        rows.append({"at": _time(e.get("ts")), "seq": seq, "agent": _text(e.get("agent_id"), 80),
                     "tool": _text(data.get("tool"), 80) or "unknown", "args": args, "status": _status(data.get("status")),
                     "unreadable": bool(data.get("withheld") or e.get("redacted") or e.get("truncated") or e.get("sensitive")) or args is None,
                     "duration_ms": duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration < 10**10 else None})
    rows.sort(key=lambda r: (r["at"] or _LAST, r["seq"] if r["seq"] is not None else 0))
    dropped = tools.get("dropped") if isinstance(tools.get("dropped"), int) else 0
    if dropped:
        return rows, "partial", "The inspector buffer evicted older events, so the full history of tool calls is gone."
    if tools.get("has_more"):
        return rows, "partial", f"More than {TOOL_LIMIT} tool calls are recorded; only the newest were read."
    hours = _dict(tools.get("prefs")).get("retention_hours")
    start = _time(session.get("created_at"))
    if not isinstance(hours, int) or isinstance(hours, bool) or start is None:
        return rows, "partial", "The buffer's retention window or the agent's start time is unknown, so its completeness cannot be shown."
    if now - start > timedelta(hours=hours):
        return rows, "partial", (f"The agent started more than {hours} hours ago, longer than the inspector keeps tool events, so older calls may be gone "
                                 "(and after a control-plane restart the buffer holds only what was stored durably).")
    if not rows:
        return rows, "none", ("No tool event is held in the inspector buffer for this agent: it may never have reported one, or the buffer may have been "
                              "emptied by eviction or a restart, so this is not a finding that it made no call.")
    return rows, "ok", ""


def _retries(rows):
    """Calls that repeat an earlier errored call (same agent, tool and arguments), in buffer sequence order. Returns (count or None, reason)."""
    if any(r["seq"] is None for r in rows) or len({r["seq"] for r in rows}) != len(rows):
        return None, "A call has no usable sequence number (or two share one), so the order of repeats cannot be shown."
    bad = sum(1 for r in rows if r["unreadable"])
    if bad:
        return None, f"{bad} call(s) have arguments that are withheld, redacted, truncated or absent, so equal-looking calls cannot be told apart."
    failed, count = set(), 0
    for r in sorted(rows, key=lambda r: r["seq"]):
        key = (r["agent"], r["tool"], r["args"])
        if key in failed and r["status"] != "running":
            count += 1
        if r["status"] == "error":
            failed.add(key)
    return count, "Calls whose agent, tool and arguments equal an earlier call that ended in an error (derived)."


def _runs(rows):
    """Tool calls grouped into runs separated by ``RUN_GAP``; undated calls are counted, never placed."""
    dated = [r for r in rows if r["at"] is not None]
    runs = []
    for r in dated:
        if runs and r["at"] - runs[-1]["_end"] <= RUN_GAP:
            run = runs[-1]
        else:
            run = {"started_at": r["at"], "_end": r["at"], "calls": 0, "failed": 0, "running": 0, "tools": {}, "longest_call_ms": None}
            runs.append(run)
        run["_end"] = max(run["_end"], r["at"])
        run["calls"] += 1
        run["failed"] += r["status"] == "error"
        run["running"] += r["status"] == "running"
        run["tools"][r["tool"]] = run["tools"].get(r["tool"], 0) + 1
        if r["duration_ms"] is not None:
            run["longest_call_ms"] = max(run["longest_call_ms"] or 0, r["duration_ms"])
    out, previous_end = [], None
    for run in runs:
        top = sorted(run["tools"].items(), key=lambda kv: (-kv[1], kv[0]))
        out.append({"started_at": run["started_at"].isoformat(), "ended_at": run["_end"].isoformat(), "calls": run["calls"], "failed": run["failed"],
                    "running": run["running"], "tools": [{"tool": k, "calls": v} for k, v in top[:MAX_TOOLS_PER_RUN]],
                    "tools_omitted": max(0, len(top) - MAX_TOOLS_PER_RUN), "longest_call_ms": run["longest_call_ms"],
                    "idle_before_s": round((run["started_at"] - previous_end).total_seconds()) if previous_end else None, "rule": "tool_gap"})
        previous_end = run["_end"]
    return out, len(rows) - len(dated)


def _transitions(stream):
    """Status transitions from the session's own recorded events, in the stream's sequence order.

    Returns a dict: ``items`` (transitions), ``restarts`` and ``restarts_reason``, ``terminal_at`` and ``final`` (the last status and, when
    it is terminal and every event is dated and ordered, when the session moved into it), ``undated`` and ``state``.
    """
    if stream is None:
        return {"items": [], "restarts": None, "restarts_reason": "The recorded event stream could not be read.", "terminal_at": None,
                "final": None, "undated": 0, "state": "unavailable"}
    events, undated = [], 0
    for index, ev in enumerate(stream):
        ev = _dict(ev)
        status = _status(_dict(ev.get("payload")).get("status"))
        if ev.get("event_type") not in ("SessionStarted", "SessionUpdated") or not status:
            continue
        seq = ev.get("sequence") if isinstance(ev.get("sequence"), int) and not isinstance(ev.get("sequence"), bool) else None
        at = _time(ev.get("source_timestamp"))
        undated += at is None
        events.append((index, seq, at, status))
    if not events:
        return {"items": [], "restarts": None, "restarts_reason": "No status change is recorded.", "terminal_at": None, "final": None, "undated": 0, "state": "none"}
    seqs = [e[1] for e in events]
    ordered = all(s is not None for s in seqs) and all(a < b for a, b in zip(seqs, seqs[1:], strict=False)) if len(seqs) > 1 else seqs[0] is not None
    if ordered is False and all(s is not None for s in seqs):
        events.sort(key=lambda e: (e[1], e[0]))                                  # same events, in sequence order; the repeat/regression is still reported
    dated = [e for e in events if e[2] is not None]
    backwards = any(b[2] < a[2] for a, b in zip(dated, dated[1:], strict=False))
    items, previous, restarts, unclassified = [], None, 0, set()
    for _i, _seq, at, status in events:
        if status not in _KNOWN:
            unclassified.add(status)
        if status == previous:
            continue
        restart = previous in _TERMINAL and status not in _TERMINAL
        restarts += restart
        if at is not None:
            items.append({"at": at.isoformat(), "from": previous, "to": _text(status, 40), "restart": restart, "rule": "event_stream"})
        previous = status
    trouble = ("an event carries no usable time" if undated else "a sequence number is missing, repeated or out of order" if not ordered
               else "timestamps run backwards along the sequence" if backwards else None)
    if trouble:
        reason = f"The order of status changes cannot be relied on: {trouble}."
        value = None
    elif unclassified:
        reason = f"A status is in none of the known classes ({', '.join(sorted(_text(s, 40) for s in unclassified)[:3])}), so a restart could be missed."
        value = None
    else:
        reason, value = "Counted from terminal to non-terminal moves in the recorded sequence (derived).", restarts
    final_event = events[-1]
    terminal_at = final_event[2] if not trouble and final_event[3] in _TERMINAL else None
    last_move = next((t["at"] for t in reversed(items) if t["to"] == final_event[3]), None)
    return {"items": items, "restarts": value, "restarts_reason": reason, "terminal_at": _time(last_move) if terminal_at is not None else None,
            "final": final_event[3], "undated": undated, "state": "ok"}


def _outcome(session, now, stale_after):
    """(value or None, status, stale). Terminal statuses classify as they are; a non-terminal one only while the last report is fresh."""
    status = _status(session.get("status"))
    if status in _TERMINAL:
        return ("finished" if status in _FINISHED else "failed" if status in _FAILED else None), status, False
    value = "blocked" if status in _BLOCKED else "running" if status in _ACTIVE else None
    last = _time(session.get("updated_at"))
    stale = value is not None and (last is None or now - last > stale_after)
    return value, status, stale


def _cost(session, spend, prices, now):
    start = _time(session.get("created_at"))
    if not prices:
        return None, "No price table is configured (control_plane.usage.prices), so no cost is estimated."
    if start is None:
        return None, "The agent has no usable start time, so its usage window is unknown."
    if start < now - timedelta(days=USAGE_DAYS):
        return None, f"This agent started before {SPEND_NOTE}, so its cost is not known."
    result = spend(session.get("session_id")) if spend else None
    if result is None:
        return None, "Usage records could not be read."
    if not result.get("configured"):
        return None, "No price table is configured (control_plane.usage.prices), so no cost is estimated."
    unpriced = sorted({_text(u.get("model"), 60) or "?" for u in result.get("unpriced") or []})
    if unpriced:
        return None, f"Models without a price entry: {', '.join(unpriced)[:200]}; a partial figure is not reported."
    if not result.get("by_model"):
        return None, "No usage is recorded for this agent, so its cost is not known."
    return round(result.get("total") or 0, 6), f"Estimated from {SPEND_NOTE}, whole session."


def _pr_refs(session):
    """(host or None, owner, name) from the session's repository identity; the commit link never names the repository itself.
    A bare ``owner/name`` carries no host, which the PR trail accepts."""
    value = str(session.get("repository_id") or "").strip().strip("/")
    canon = canonical_repository(value) or ""
    parts = (canon or value).split("/")
    if canon and len(parts) == 3 and all(parts):
        return parts[0], parts[1], parts[2]
    if not canon and len(parts) == 2 and all(parts):
        return None, parts[0], parts[1]
    return None, None, None


def build(session, *, sessions, view, pull_requests, handoffs, handoffs_truncated=False, reviews, plan_reviews, stream, tools,
          spend=None, prices=True, trail_for=None, now=None, stale_after_minutes=DEFAULT_STALE_MINUTES):
    """The evolution of ``session``. ``handoffs``/``reviews``/``plan_reviews``/``stream``/``tools`` are the records, or ``None`` when that
    source could not be read; ``tools`` is the inspector query result (``events``, ``has_more``, ``dropped``, ``prefs``).
    ``trail_for(host, owner, name, number)`` returns that pull request's trail (``pr_trail.build`` output) or ``None``.
    """
    now = now or datetime.now(UTC)
    stale_after = timedelta(minutes=stale_after_minutes)
    sid = session.get("session_id")
    sessions = [s for s in sessions if isinstance(s, dict)]
    pull_requests = [p for p in pull_requests if isinstance(p, dict)]
    ci_ok = bool(view) and view.get("available") is not False

    others = [s for s in sessions if s.get("session_id") != sid]
    hand = _handoffs(handoffs, handoffs_truncated, session, others)
    ins, outs, handoff_state = hand["ins"], hand["outs"], hand["state"]
    rev_rows = _review_rows(reviews, plan_reviews, {sid}, lambda _v: False, None)
    reviews_known = reviews is not None or plan_reviews is not None
    same_repo = [s for s in sessions if session.get("repository_id") and s.get("repository_id") == session.get("repository_id")]
    found = session_commits(session, same_repo, view if ci_ok else {"available": False}, now, pull_requests)
    calls, tool_state, tool_reason = _tool_calls(tools, session, now)
    runs, undated_calls = _runs(calls)
    tr = _transitions(stream)
    stream_state = tr["state"]
    outcome_value, last_status, outcome_stale = _outcome(session, now, stale_after)
    started = _time(session.get("created_at"))
    sources = {"handoffs": handoff_state, "reviews": "ok" if reviews is not None else "unavailable", "plan_reviews": "ok" if plan_reviews is not None else "unavailable",
               "ci": "ok" if ci_ok else "unavailable", "tools": tool_state, "event_stream": stream_state}

    # ---- assigned
    mission = _text(session.get("mission_id"), 160)
    record = {"kind": "session_record", "at": _iso(session.get("created_at")), "rule": "session_record", "session_id": _text(sid, 160),
              "name": _text(session.get("name") or session.get("title") or session.get("display_name")), "role": _text(session.get("agent_role"), 80),
              "runtime": _text(session.get("runtime"), 40), "model": _text(session.get("model"), 80), "host_id": _text(session.get("host_id"), 80),
              "mission_id": mission, "branch": _text(_branch(session.get("branch")), 200), "repository": _text(session.get("repository_id"), 200),
              "parent_session_id": _text(session.get("parent_session_id"), 160)}
    assigned_items = [record, *({"kind": "handoff_in", **h} for h in ins[:MAX_HANDOFFS])]
    if handoff_state == "unavailable":
        assigned_reason = "Handoff records could not be read, so who gave this agent work is unknown."
    elif handoff_state == "truncated":
        assigned_reason = "The handoff list was cut off, so a handoff to this agent may be missing."
    elif hand["unknown_in"]:
        assigned_reason = f"{hand['unknown_in']} handoff(s) name no session (and no agent unique to one session) as their target, so one of them may be this agent's."
    else:
        assigned_reason = "" if ins else "No handoff to this agent is recorded; the session record is all that is known about how it began."
    phases = [_phase("assigned", "recorded", assigned_reason, assigned_items, max(0, len(ins) - MAX_HANDOFFS))]

    # ---- worked
    worked_items = [{"kind": "run", **r} for r in runs[:MAX_RUNS]]
    for t in tr["items"][:MAX_TRANSITIONS]:
        worked_items.append({"kind": "status", **t, "text": f"{t['from'] or 'start'} → {t['to']}" + (" (restart, derived)" if t["restart"] else "")})
    worked_items.sort(key=lambda i: _time(i.get("started_at") or i.get("at")) or _LAST)
    if worked_items:
        worked_state, worked_reason = "recorded", tool_reason if tool_state in ("partial", "unavailable") else ""
    elif tool_state == "unavailable" or stream_state == "unavailable":
        worked_state, worked_reason = "unknown", "Tool and status records could not be read; this is not a finding that nothing happened."
    else:
        worked_state, worked_reason = "none", "No tool call or status change is recorded for this agent."
    phases.append(_phase("worked", worked_state, worked_reason, worked_items, max(0, len(runs) - MAX_RUNS) + max(0, len(tr["items"]) - MAX_TRANSITIONS),
                         undated={"tool_calls": undated_calls, "status_events": tr["undated"]}))

    # ---- handed off
    if handoff_state == "unavailable":
        phases.append(_phase("handed_off", "unknown", "Handoff records could not be read.", ()))
    else:
        if outs:
            reason = ""
        elif handoff_state == "truncated":
            reason = "The handoff list was cut off, so a handoff may be missing."
        elif hand["unknown_out"]:
            reason = f"{hand['unknown_out']} handoff(s) name no session (and no agent unique to one session) as their source, so one of them may be this agent's."
        else:
            reason = "No handoff from this agent is recorded."
        state = "recorded" if outs else "unknown" if (hand["unknown_out"] or handoff_state == "truncated") else "none"
        phases.append(_phase("handed_off", state, reason, ({"kind": "handoff_out", **h} for h in outs[:MAX_HANDOFFS]), max(0, len(outs) - MAX_HANDOFFS)))

    # ---- produced
    commits, pulls = found["commits"], found["pull_requests"]
    host, owner, name = _pr_refs(session)
    produced_items = [{"kind": "commit", "sha": _text(c.get("sha"), 64) or "", "run_number": c.get("run_number"), "at": _iso(c.get("at")),
                       "branch": _text(_branch(c.get("branch")), 200), "rule": _text(c.get("match"), 20), "ci_url": _text(c.get("ci_url"), 400)}
                      for c in commits[:MAX_COMMITS]]
    pr_items = []
    for p in pulls[:MAX_PRS]:
        number = p.get("number")
        pr_items.append({"kind": "pull_request", "number": number, "at": _iso(p.get("at")), "status": _text(p.get("status"), 40), "url": _text(p.get("url"), 400),
                         "repository": f"{owner}/{name}" if owner else None, "host": host, "rule": "session_commits",
                         "trail": f"#/pr/{owner}/{name}/{number}" + (f"?host={host}" if host else "") if owner and isinstance(number, int) else None})
    produced_items = sorted([*produced_items, *pr_items], key=lambda i: _time(i.get("at")) or _LAST)
    if commits or pulls:
        phases.append(_phase("produced", "recorded", "", produced_items, max(0, len(commits) - MAX_COMMITS) + found.get("omitted", 0) + max(0, len(pulls) - MAX_PRS),
                             link_state=found["state"]))
    else:
        phases.append(_phase("produced", "unknown" if found["state"] == "unknown" else "none", found["reason"] or "No commit or pull request is linked to this agent.", (),
                             link_state=found["state"]))

    # ---- verified
    ci_items = [{"kind": "ci_run", "run_number": c.get("run_number"), "sha": _text(c.get("sha"), 64) or "", "state": _text(c.get("state"), 40), "at": _iso(c.get("at")),
                 "ci_url": _text(c.get("ci_url"), 400), "rule": _text(c.get("match"), 20)} for c in commits[:MAX_COMMITS]]
    review_items = [{**r, "kind": "review", "review_kind": r["kind"]} for r in rev_rows[:MAX_REVIEWS]]
    verified_items = sorted([*ci_items, *review_items], key=lambda i: _time(i.get("at")) or _LAST)
    if verified_items:
        why = "" if reviews_known else "Review records could not be read."
        phases.append(_phase("verified", "recorded", why, verified_items, max(0, len(rev_rows) - MAX_REVIEWS)))
    else:
        unknown = not ci_ok or not reviews_known
        phases.append(_phase("verified", "unknown" if unknown else "none",
                             "CI or review records could not be read; this is not a finding that none exist." if unknown else "No CI run or review is attached to this agent.", ()))

    # ---- pull request signals: the PR trail's own, carried only when that trail lists this agent
    prs, pr_unreadable = [], False
    too_many = len(pulls) > MAX_TRAILS
    scope = "Signals cover every agent on that pull request, not only this one."
    for p in ([] if too_many else pulls):
        number = p.get("number")
        trail = trail_for(host, owner, name, number) if trail_for and owner and isinstance(number, int) else None
        if trail is None:
            pr_unreadable = True
            continue
        listed = {s.get("session_id") for s in trail.get("sessions") or [] if isinstance(s, dict)}
        mine = sid in listed
        prs.append({"ref": f"{owner}/{name}#{number}", "status": _text(_dict(trail.get("pull_request")).get("status"), 40), "trail_lists_this_agent": mine,
                    "signals": (trail.get("signals") or {}) if mine else None,
                    "caveat": scope if mine else "The pull request trail does not list this agent, so its signals are not carried here."})

    # ---- outcome
    outcome_items = [{"kind": "status", "value": outcome_value, "last_reported_status": _text(last_status, 40), "at": _iso(session.get("updated_at")),
                      "stale": outcome_stale, "rule": "last_report"}]
    for pr in prs:
        outcome_items.append({"kind": "pull_request_outcome", **pr, "rule": "pr_trail"})
    phases.append(_phase("outcome", "recorded" if outcome_value else "unknown",
                         "" if outcome_value else f"The last reported status ({last_status or 'none'}) is not one the dashboard can classify.", outcome_items,
                         pr_signals_unreadable=pr_unreadable or too_many))

    # ---- cost
    cost, cost_why = _cost(session, spend, prices, now)
    phases.append(_phase("cost", "recorded" if cost is not None else "unknown", cost_why if cost is None else "",
                         [{"kind": "cost", "usd": cost, "rule": "usage_spend", "note": cost_why}] if cost is not None else ()))

    # ---- learning record: value-or-null with a reason, from evidence-linked records only
    def sig(value, reason):
        return _signal(value, reason)
    terminal = last_status in _TERMINAL
    ended = tr["terminal_at"] if terminal and tr["final"] == last_status else None
    ended_reason = ("The moment the recorded status changes moved into the terminal status the agent still reports." if ended else
                    "The agent is not in a terminal status." if not terminal else
                    "The recorded status changes do not show when it reached its terminal status (stream unreadable, undated, out of order or different).")
    full_tools = tool_state == "ok"
    retried, retried_why = _retries(calls) if full_tools else (None, tool_reason or "No tool call is recorded.")

    def count(known, value, reason, why_not):
        return sig(value if known else None, reason if known else why_not)
    handoffs_ok = handoff_state == "ok"
    why_handoffs = ("Handoff records could not be read." if handoff_state == "unavailable" else "The handoff list was cut off, so some may be missing.")
    in_known, out_known = handoffs_ok and not hand["unknown_in"], handoffs_ok and not hand["unknown_out"]
    why_in = why_handoffs if not handoffs_ok else f"{hand['unknown_in']} handoff(s) name an agent but no session on the target side, so attribution is unknown."
    why_out = why_handoffs if not handoffs_ok else f"{hand['unknown_out']} handoff(s) name an agent but no session on the source side, so attribution is unknown."
    if outcome_value and outcome_stale:
        outcome_sig = sig(None, f"The last report ({_iso(session.get('updated_at')) or 'undated'}) is older than {stale_after_minutes} minutes (control_plane.evolution.stale_after_minutes), "
                                f"so '{last_status}' is not taken as an outcome.")
    elif outcome_value:
        outcome_sig = sig(outcome_value, f"Last reported status: {last_status}." + ("" if terminal else f" Reported within {stale_after_minutes} minutes."))
    else:
        outcome_sig = sig(None, "The last reported status cannot be classified.")
    if found["state"] not in ("linked", "none"):
        prs_sig = sig(None, found["reason"] or "No pull request can be attributed to this agent.")
    elif too_many:
        prs_sig = sig(None, f"{len(pulls)} pull requests are linked, more than the {MAX_TRAILS} whose trails are read per request, so none are reported.")
    elif len(pulls) > MAX_PRS or pr_unreadable:
        prs_sig = sig(None, "A pull request's trail could not be read, or more were linked than can be shown.")
    else:
        prs_sig = sig(prs, "Pull requests linked by evidence. " + scope + " A pull request whose trail does not list this agent carries no signals.")
    record_out = {
        "session_id": sig(_text(sid, 160), "The session id."),
        "harness": sig(_text(session.get("runtime"), 40), "Reported runtime." if session.get("runtime") else "The agent reported no runtime."),
        "model": sig(_text(session.get("model"), 80), "Reported model." if session.get("model") else "The agent reported no model."),
        "role": sig(_text(session.get("agent_role"), 80), "Reported role." if session.get("agent_role") else "The agent reported no role."),
        "started_at": sig(_iso(session.get("created_at")), "Session record." if started else "The session has no usable start time."),
        "ended_at": sig(ended.isoformat() if ended else None, ended_reason),
        "duration_s": sig(round((ended - started).total_seconds()) if ended and started and ended >= started else None,
                          "From the start to the moment it reached its terminal status." if ended and started and ended >= started else ended_reason),
        "tool_calls": {"total": sig(len(calls) if full_tools else None, "Every call held in a whole inspector buffer." if full_tools else tool_reason),
                       "failed": sig(sum(1 for r in calls if r["status"] == "error") if full_tools else None,
                                     "Calls whose status is error; interrupted or unfinished calls are not counted." if full_tools else tool_reason),
                       "retried": sig(retried, retried_why)},
        "handoffs_in": count(in_known, len(ins), "Handoffs whose target is this agent (a self-handoff is counted as outgoing).", why_in),
        "handoffs_out": {"total": count(out_known, len(outs), "Handoffs whose source is this agent.", why_out),
                         "rejected": count(out_known, sum(1 for h in outs if h["rejected"]), "Rejected at any point.", why_out),
                         "failed": count(out_known, sum(1 for h in outs if h["failed"]), "Failed at any point.", why_out)},
        "prs": prs_sig,
        "cost_usd_estimate": sig(cost, cost_why if cost is None else "Priced usage of this session."),
        "outcome": outcome_sig,
    }

    hints = [{"session_id": _text(s.get("session_id"), 160), "name": _text(s.get("name") or s.get("title")), "status": _text(s.get("status"), 40)}
             for s in sessions if mission and s.get("mission_id") == session.get("mission_id") and s.get("session_id") != sid][:10]
    return {"session_id": _text(sid, 160), "sources": sources, "phases": phases, "learning_record": record_out,
            "hints": {"mission_id": mission, "same_mission_sessions": hints, "note": "Agents that share this mission are a hint only; they feed no number."},
            "derived": {"retries": "A call whose agent, tool and arguments equal an earlier call that ended in an error.",
                        "restarts": "A move from a terminal status to a non-terminal one, in the recorded sequence.",
                        "runs": f"Tool calls separated by more than {int(RUN_GAP.total_seconds() // 60)} minutes of silence."},
            "restarts": {"value": tr["restarts"], "reason": tr["restarts_reason"]}}


__all__ = ["build", "TTLCache", "TOOL_LIMIT", "evolution_config", "DEFAULT_STALE_MINUTES"]
