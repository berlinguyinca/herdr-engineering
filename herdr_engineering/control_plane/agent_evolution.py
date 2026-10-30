"""The complete life of one agent session, in ordered phases, plus a compact record a later learning step can read.

Same rule as ``pr_trail``: **a value is null with a reason unless there is positive evidence, and a wrong attribution is worse than
missing data.** Everything is derived from records the server already holds; nothing is scraped and nothing is guessed.

Phases (each lists only items that exist; a phase with no evidence says so and why, it is never invented):

* ``assigned``   the session record (task context) and the handoffs that gave it work (``target.session_id`` is this session);
* ``worked``     tool calls from the inspector ring grouped into runs (a gap longer than ``RUN_GAP`` starts a new run), and the status
                 transitions in the recorded event stream;
* ``handed_off`` handoffs whose ``source.session_id`` is this session;
* ``produced``   commits (CI runs) and pull requests ``session_commits`` links to this session, each PR linking to its PR trail;
* ``verified``   the CI result of every linked run and the reviews attached by ``session_id``;
* ``outcome``    the last reported status, and for every PR the trail's own signals (copied, never recomputed);
* ``cost``       the priced usage estimate for this session.

Attribution is by evidence only: a handoff or review attaches by an explicit ``session_id``; a commit or PR by ``session_commits``'
rules (commit, branch+window). Agents that merely share the mission are ``hints`` and never feed a number in ``learning_record``.

Derived, and labelled so: **retries** (a tool call whose tool, agent and arguments equal an earlier *errored* call) and **restarts**
(a status that goes from a terminal one back to an active one). Aggregates over the tool ring are null when the ring evicted events,
the list was cut, or an argument was withheld. Caps apply to displayed rows only; every aggregate uses the full set.
Pure: ``build`` reads what it is given and writes nothing. ``TTLCache`` is the short per-session cache the endpoint keeps.
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
MAX_COMMITS = 50
MAX_TOOLS_PER_RUN = 10
RUN_GAP = timedelta(minutes=10)           # silence longer than this ends a run of tool calls
TOOL_LIMIT = 1000                         # events read from the inspector ring in one request
_LAST = datetime.max.replace(tzinfo=UTC)
_TERMINAL = frozenset({"finished", "failed", "completed", "done", "stopped", "exited", "cancelled", "terminated", "error", "killed"})
_FINISHED = frozenset({"finished", "completed", "done"})
_FAILED = frozenset({"failed", "error", "killed"})
_BLOCKED = frozenset({"blocked", "needs_input", "needs-input", "stuck"})
_PROBLEM = frozenset({"rejected", "failed", "expired", "cancelled"})
_PHASES = (("assigned", "Assigned"), ("worked", "Worked"), ("handed_off", "Handed off"), ("produced", "Produced"),
           ("verified", "Verified"), ("outcome", "Outcome"), ("cost", "Cost"))


def _status(value):
    return str(value or "").strip().lower()


def _phase(key, state, reason, items=(), omitted=0, **extra):
    title = dict(_PHASES)[key]
    return {"key": key, "title": title, "state": state, "reason": reason, "items": list(items), "omitted": omitted, **extra}


def _handoff_detail(row):
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
    return {"handoff_id": _text(row.get("handoff_id"), 160), "rule": "session_id", "state": _text(row.get("state"), 40),
            "at": _iso(row.get("created_at")), "goal": _text(row.get("goal")),
            "from": {k: _text(source.get(k), 120) for k in ("harness", "agent_id", "session_id")},
            "to": {k: _text(target.get(k), 120) for k in ("harness", "agent_id", "session_id")},
            "completed": lines("completed"), "remaining": lines("remaining"), "decisions": lines("decisions"),
            "time_to_accept_s": round((accepted - queued).total_seconds()) if queued and accepted and accepted >= queued else None,
            "rejected": "rejected" in states, "failed": "failed" in states, "problem": bool(states & _PROBLEM)}


def _handoffs(rows, truncated, session_id):
    """(in_rows, out_rows, state); state is ok / unavailable / truncated and decides whether counts may be reported."""
    if rows is None:
        return [], [], "unavailable"
    ins, outs = [], []
    for row in rows:
        row = _dict(row)
        if _dict(row.get("target")).get("session_id") == session_id:
            ins.append(_handoff_detail(row))
        if _dict(row.get("source")).get("session_id") == session_id:
            outs.append(_handoff_detail(row))
    for group in (ins, outs):
        group.sort(key=lambda h: _time(h["at"]) or _LAST)
    return ins, outs, "truncated" if truncated else "ok"


def _tool_calls(tools):
    """(rows, state, reason). A row is ``{at, agent, tool, args, status, duration_ms, withheld}``; state ok / unavailable / none / partial."""
    if tools is None:
        return [], "unavailable", "Tool records could not be read."
    events = [e for e in (tools.get("events") if isinstance(tools.get("events"), list) else []) if isinstance(e, dict) and e.get("kind") == "tool"]
    rows = []
    for e in events:
        data = _dict(e.get("data"))
        duration = data.get("duration_ms")
        rows.append({"at": _time(e.get("ts")), "seq": e.get("seq") if isinstance(e.get("seq"), int) else 0, "agent": _text(e.get("agent_id"), 80),
                     "tool": _text(data.get("tool"), 80) or "unknown", "args": data.get("args_preview") if isinstance(data.get("args_preview"), str) else None,
                     "status": _status(data.get("status")), "withheld": bool(data.get("withheld")),
                     "duration_ms": duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration < 10**10 else None})
    rows.sort(key=lambda r: (r["at"] or _LAST, r["seq"]))
    dropped = tools.get("dropped") if isinstance(tools.get("dropped"), int) else 0
    if tools.get("has_more") or dropped:
        return rows, "partial", ("The inspector ring evicted older events, so the full history of tool calls is gone." if dropped
                                 else f"More than {TOOL_LIMIT} tool calls are recorded; only the newest were read.")
    if not rows:
        return rows, "none", "No tool call is recorded for this agent."
    return rows, "ok", ""


def _retries(rows):
    """Calls that repeat an earlier *errored* call (same agent, tool and arguments). Returns (count or None, reason)."""
    if any(r["withheld"] or (r["args"] is None and r["status"] != "running") for r in rows):
        n = sum(1 for r in rows if r["withheld"] or r["args"] is None)
        return None, f"{n} call(s) have no readable arguments, so repeats cannot be told apart."
    failed, count = set(), 0
    for r in rows:
        key = (r["agent"], r["tool"], r["args"])
        if key in failed and r["status"] != "running":
            count += 1
        if r["status"] == "error":
            failed.add(key)
    return count, "Calls whose agent, tool and arguments equal an earlier errored call (derived)."


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
    """(status transitions, restarts, undated, state). Read from the session's own recorded events, in time order."""
    if stream is None:
        return [], None, 0, "unavailable"
    seen, undated = [], 0
    for ev in stream:
        ev = _dict(ev)
        status = _status(_dict(ev.get("payload")).get("status"))
        if ev.get("event_type") not in ("SessionStarted", "SessionUpdated") or not status:
            continue
        at = _time(ev.get("source_timestamp"))
        if at is None:
            undated += 1
            continue
        seen.append((at, ev.get("sequence") if isinstance(ev.get("sequence"), int) else 0, status))
    seen.sort(key=lambda t: (t[0], t[1]))
    out, previous, restarts = [], None, 0
    for at, _seq, status in seen:
        if status == previous:
            continue
        restart = previous in _TERMINAL and status in _ACTIVE
        restarts += restart
        out.append({"at": at.isoformat(), "from": previous, "to": _text(status, 40), "restart": restart, "rule": "event_stream"})
        previous = status
    return out, restarts, undated, "ok" if seen else "none"


def _outcome(session):
    status = _status(session.get("status"))
    value = ("finished" if status in _FINISHED else "failed" if status in _FAILED else "blocked" if status in _BLOCKED
             else "running" if status in _ACTIVE else None)
    return value, status


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
          spend=None, prices=True, trail_for=None, now=None):
    """The evolution of ``session``. ``handoffs``/``reviews``/``plan_reviews``/``stream``/``tools`` are the records, or ``None`` when that
    source could not be read; ``tools`` is the inspector query result (``events``, ``has_more``, ``dropped``).
    ``trail_for(host, owner, name, number)`` returns that pull request's trail (``pr_trail.build`` output) or ``None``.
    """
    now = now or datetime.now(UTC)
    sid = session.get("session_id")
    sessions = [s for s in sessions if isinstance(s, dict)]
    pull_requests = [p for p in pull_requests if isinstance(p, dict)]
    ci_ok = bool(view) and view.get("available") is not False

    ins, outs, handoff_state = _handoffs(handoffs, handoffs_truncated, sid)
    rev_rows = _review_rows(reviews, plan_reviews, {sid}, lambda _v: False, None)
    reviews_known = reviews is not None or plan_reviews is not None
    same_repo = [s for s in sessions if session.get("repository_id") and s.get("repository_id") == session.get("repository_id")]
    found = session_commits(session, same_repo, view if ci_ok else {"available": False}, now, pull_requests)
    calls, tool_state, tool_reason = _tool_calls(tools)
    runs, undated_calls = _runs(calls)
    transitions, restarts, undated_events, stream_state = _transitions(stream)
    outcome_value, last_status = _outcome(session)
    started, last_seen = _time(session.get("created_at")), _time(session.get("updated_at"))
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
    else:
        assigned_reason = "" if ins else "No handoff to this agent is recorded; the session record is all that is known about how it began."
    phases = [_phase("assigned", "recorded", assigned_reason, assigned_items, max(0, len(ins) - MAX_HANDOFFS))]

    # ---- worked
    worked_items = [{"kind": "run", **r} for r in runs[:MAX_RUNS]]
    for t in transitions[:MAX_TRANSITIONS]:
        worked_items.append({"kind": "status", **t, "text": f"{t['from'] or 'start'} → {t['to']}" + (" (restart, derived)" if t["restart"] else "")})
    worked_items.sort(key=lambda i: _time(i.get("started_at") or i.get("at")) or _LAST)
    if worked_items:
        worked_state, worked_reason = "recorded", tool_reason if tool_state in ("partial", "unavailable") else ""
    elif tool_state == "unavailable" or stream_state == "unavailable":
        worked_state, worked_reason = "unknown", "Tool and status records could not be read; this is not a finding that nothing happened."
    else:
        worked_state, worked_reason = "none", "No tool call or status change is recorded for this agent."
    phases.append(_phase("worked", worked_state, worked_reason, worked_items, max(0, len(runs) - MAX_RUNS) + max(0, len(transitions) - MAX_TRANSITIONS),
                         undated={"tool_calls": undated_calls, "status_events": undated_events}))

    # ---- handed off
    if handoff_state == "unavailable":
        phases.append(_phase("handed_off", "unknown", "Handoff records could not be read.", ()))
    else:
        reason = "" if outs else "No handoff from this agent is recorded." if handoff_state == "ok" else "The handoff list was cut off, so a handoff may be missing."
        phases.append(_phase("handed_off", "recorded" if outs else "none", reason, ({"kind": "handoff_out", **h} for h in outs[:MAX_HANDOFFS]),
                             max(0, len(outs) - MAX_HANDOFFS)))

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

    # ---- pull request signals (copied from the trail, which covers every agent on that PR)
    prs, pr_unreadable = [], False
    for p in pulls[:MAX_PRS]:
        number = p.get("number")
        trail = trail_for(host, owner, name, number) if trail_for and owner and isinstance(number, int) else None
        if trail is None:
            pr_unreadable = True
            continue
        listed = {s.get("session_id") for s in trail.get("sessions") or [] if isinstance(s, dict)}
        prs.append({"ref": f"{owner}/{name}#{number}", "status": _text(_dict(trail.get("pull_request")).get("status"), 40),
                    "trail_lists_this_agent": sid in listed, "signals": trail.get("signals") or {}})

    # ---- outcome
    outcome_items = [{"kind": "status", "value": outcome_value, "last_reported_status": _text(last_status, 40), "at": _iso(session.get("updated_at")), "rule": "last_report"}]
    for pr in prs:
        outcome_items.append({"kind": "pull_request_outcome", **pr, "rule": "pr_trail"})
    phases.append(_phase("outcome", "recorded" if outcome_value else "unknown",
                         "" if outcome_value else f"The last reported status ({last_status or 'none'}) is not one the dashboard can classify.", outcome_items,
                         pr_signals_unreadable=pr_unreadable))

    # ---- cost
    cost, cost_why = _cost(session, spend, prices, now)
    phases.append(_phase("cost", "recorded" if cost is not None else "unknown", cost_why if cost is None else "",
                         [{"kind": "cost", "usd": cost, "rule": "usage_spend", "note": cost_why}] if cost is not None else ()))

    # ---- learning record: value-or-null with a reason, from evidence-linked records only
    def sig(value, reason):
        return _signal(value, reason)
    terminal = last_status in _TERMINAL
    ended = last_seen if terminal and last_seen else None
    full_tools = tool_state == "ok"
    retried, retried_why = _retries(calls) if full_tools else (None, tool_reason or "No tool call is recorded.")
    handoffs_known = handoff_state == "ok"
    no_handoff = "Handoff records could not be read." if handoff_state == "unavailable" else "The handoff list was cut off, so some may be missing."
    pr_known = found["state"] in ("linked", "none") and len(pulls) <= MAX_PRS and not pr_unreadable
    record_out = {
        "session_id": sig(_text(sid, 160), "The session id."),
        "harness": sig(_text(session.get("runtime"), 40), "Reported runtime." if session.get("runtime") else "The agent reported no runtime."),
        "model": sig(_text(session.get("model"), 80), "Reported model." if session.get("model") else "The agent reported no model."),
        "role": sig(_text(session.get("agent_role"), 80), "Reported role." if session.get("agent_role") else "The agent reported no role."),
        "started_at": sig(_iso(session.get("created_at")), "Session record." if started else "The session has no usable start time."),
        "ended_at": sig(ended.isoformat() if ended else None, "Time of the last report of a terminal status." if ended else "The agent is not in a terminal status, or has no usable last-report time."),
        "duration_s": sig(round((ended - started).total_seconds()) if ended and started and ended >= started else None,
                          "From the start to the last report of a terminal status." if ended and started and ended >= started else "No terminal status with usable times."),
        "tool_calls": {"total": sig(len(calls) if full_tools else None, "Every recorded tool call." if full_tools else tool_reason),
                       "failed": sig(sum(1 for r in calls if r["status"] == "error") if full_tools else None, "Calls that ended in an error." if full_tools else tool_reason),
                       "retried": sig(retried, retried_why)},
        "handoffs_in": sig(len(ins) if handoffs_known else None, "Handoffs whose target is this session." if handoffs_known else no_handoff),
        "handoffs_out": {"total": sig(len(outs) if handoffs_known else None, "Handoffs whose source is this session." if handoffs_known else no_handoff),
                         "rejected": sig(sum(1 for h in outs if h["rejected"]) if handoffs_known else None, "Rejected at any point." if handoffs_known else no_handoff),
                         "failed": sig(sum(1 for h in outs if h["failed"]) if handoffs_known else None, "Failed at any point." if handoffs_known else no_handoff)},
        "prs": sig(prs if pr_known else None, "Pull requests linked by evidence; signals are the PR trail's and cover every agent on that PR." if pr_known
                   else found["reason"] or "A pull request's trail could not be read, or more were linked than can be shown."),
        "cost_usd_estimate": sig(cost, cost_why if cost is None else "Priced usage of this session."),
        "outcome": sig(outcome_value, f"Last reported status: {last_status}." if outcome_value else "The last reported status cannot be classified."),
    }

    hints = [{"session_id": _text(s.get("session_id"), 160), "name": _text(s.get("name") or s.get("title")), "status": _text(s.get("status"), 40)}
             for s in sessions if mission and s.get("mission_id") == session.get("mission_id") and s.get("session_id") != sid][:10]
    return {"session_id": _text(sid, 160), "sources": sources, "phases": phases, "learning_record": record_out,
            "hints": {"mission_id": mission, "same_mission_sessions": hints, "note": "Agents that share this mission are a hint only; they feed no number."},
            "derived": {"retries": "A call whose agent, tool and arguments equal an earlier errored call.",
                        "restarts": "A status that goes from a terminal one back to an active one.",
                        "runs": f"Tool calls separated by more than {int(RUN_GAP.total_seconds() // 60)} minutes of silence."},
            "restarts": {"value": restarts if stream_state == "ok" else None,
                         "reason": "Derived from the recorded status changes." if stream_state == "ok"
                         else "The recorded event stream could not be read." if stream_state == "unavailable" else "No status change is recorded."}}


__all__ = ["build", "TTLCache", "TOOL_LIMIT"]
