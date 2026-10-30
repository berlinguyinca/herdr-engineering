"""How agents got to one pull request: sessions, the handoffs between them, reviews, CI runs, the outcome and the signals for learning.

Everything is derived from records the server already holds; nothing is scraped and nothing is guessed. Every link says why
(``rule``): a session is on the trail when ``session_commits`` links it to this PR (``commit``, ``branch_window`` or
``pull_request_record``), or when it shares the PR record's mission (``mission``). Agents that cannot be told apart on the PR's
branch are listed under ``ambiguous`` with the reason, never in the timeline. A source that could not be read is ``unavailable``
in ``sources`` and the figures that rest on it are ``null`` with a reason, which is not a finding that nothing happened.

``signals`` is what a later learning step reads: each entry is ``{"value": ..., "reason": ...}`` with a ``null`` value when the
evidence is missing. Pure: ``build`` reads the lists it is given and writes nothing.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

from .project_links import canonical_repository
from .redact import redact_text
from .session_commits import (SHARED_BRANCHES, _branch, _entry, _pull_number, _repository_ids, _time, session_commits)

MAX_SESSIONS = 20
MAX_HANDOFFS = 50
MAX_REVIEWS = 50
MAX_RUNS = 50
_TEXT = 240
_PASSED = frozenset(("passed", "success", "succeeded", "ok"))
_FAILED = frozenset(("failed", "failure", "error", "errored", "killed"))
_CHANGES = frozenset(("changes_requested", "request_changes", "rejected", "denied", "annotated"))
_PROBLEM_HANDOFF = frozenset(("rejected", "failed", "expired", "cancelled"))
_EPOCH = datetime.min.replace(tzinfo=UTC)
SPEND_NOTE = "last 30 days of recorded usage"


def _text(value, limit=_TEXT):
    return redact_text(str(value))[:limit] if value not in (None, "") else None


def _iso(value):
    stamp = _time(value)
    return stamp.isoformat() if stamp else None


def _key(value):
    """(host or None, path) for a repository identity; a value without a forge host keeps only its path."""
    if not isinstance(value, str) or not value.strip():
        return None
    canon = canonical_repository(value)
    text = (canon or value).strip().lower().removesuffix(".git").strip("/")
    first, _, rest = text.partition("/")
    if canon and rest:
        return first, rest
    return None, text


def same_repository(a, b):
    """Equal identities; when one side has no forge host, the owner/name path alone decides."""
    ka, kb = _key(a), _key(b)
    if not ka or not kb:
        return False
    return ka[1] == kb[1] and (ka[0] is None or kb[0] is None or ka[0] == kb[0])


def _at(row):
    return _time(row.get("at")) or _EPOCH


def _verdict(row):
    return str(row.get("verdict") or row.get("decision") or row.get("status") or "").lower()


def _find_pull(pull_requests, owner, name, number):
    wanted = f"{owner}/{name}"
    hits = [p for p in pull_requests if isinstance(p, dict) and p.get("number") == number and same_repository(p.get("repository"), wanted)]
    return max(hits, key=lambda p: str(p.get("updated_at") or "")) if hits else None


def _session_row(session, rule, now):
    return {"session_id": session.get("session_id"), "name": _text(session.get("name") or session.get("title")),
            "role": _text(session.get("agent_role"), 80), "runtime": _text(session.get("runtime"), 40), "model": _text(session.get("model"), 80),
            "host_id": _text(session.get("host_id"), 80), "status": _text(session.get("status"), 40), "mission_id": session.get("mission_id"),
            "branch": _branch(session.get("branch")) or None, "commit_sha": _text(session.get("commit_sha"), 64),
            "started_at": _iso(session.get("created_at")), "last_seen_at": _iso(session.get("updated_at")), "rule": rule}


def _link_sessions(sessions, pulls, view, number, pr, now):
    """(linked [(session, rule)], ambiguous [{session_id, reason}])."""
    linked, ambiguous = {}, []
    for session in sessions:
        sid = session.get("session_id")
        if not isinstance(sid, str):
            continue
        found = session_commits(session, sessions, view, now, pulls)
        if found["state"] == "linked" and any(p.get("number") == number for p in found["pull_requests"]):
            hits = [c for c in found["commits"] if c.get("pull_request") == number]
            linked[sid] = (session, "commit" if any(c.get("match") == "commit" for c in hits) else
                           "branch_window" if hits else "pull_request_record")
        elif found["state"] == "ambiguous" and pr and pr.get("branch") and _branch(pr["branch"]) == found.get("branch") \
                and same_repository(session.get("repository_id"), pr.get("repository")):
            ambiguous.append({"session_id": sid, "reason": found["reason"]})
    mission = pr.get("mission_id") if pr else None
    for session in sessions:
        sid = session.get("session_id")
        if mission and isinstance(sid, str) and sid not in linked and session.get("mission_id") == mission:
            linked[sid] = (session, "mission")
    ordered = sorted(linked.values(), key=lambda pair: _time(pair[0].get("created_at")) or _EPOCH)
    return ordered, ambiguous


def _handoff_rows(rows, session_ids, missions):
    out = []
    for row in rows or []:
        source, target = row.get("source") or {}, row.get("target") or {}
        by_session = source.get("session_id") in session_ids or target.get("session_id") in session_ids
        by_mission = row.get("mission_id") in missions
        if not (by_session or by_mission):
            continue
        history = [{"state": h.get("state"), "at": _iso(h.get("at"))} for h in row.get("history") or [] if isinstance(h, dict)]
        queued = next((_time(h.get("at")) for h in row.get("history") or [] if h.get("state") == "queued"), None)
        accepted = next((_time(h.get("at")) for h in row.get("history") or [] if h.get("state") == "accepted"), None)
        states = {h["state"] for h in history}
        out.append({
            "handoff_id": row.get("handoff_id"), "rule": "session" if by_session else "mission",
            "from": {k: _text(source.get(k), 120) for k in ("harness", "agent_id", "session_id")},
            "to": {k: _text(target.get(k), 120) for k in ("harness", "agent_id", "session_id")},
            "to_session_on_trail": target.get("session_id") in session_ids,
            "goal": _text(row.get("goal")), "state": row.get("state"), "created_at": _iso(row.get("created_at")), "history": history,
            "time_to_accept_s": round((accepted - queued).total_seconds()) if queued and accepted and accepted >= queued else None,
            "problem": row.get("state") in _PROBLEM_HANDOFF or bool(states & _PROBLEM_HANDOFF)})
    out.sort(key=lambda h: _time(h.get("created_at")) or _EPOCH)
    return out


def _review_rows(reviews, plan_reviews, session_ids, missions):
    out = []
    for source, rows in (("review", reviews), ("plan_review", plan_reviews)):
        for row in rows or []:
            if row.get("mission_id") not in missions and row.get("session_id") not in session_ids:
                continue
            verdict = _verdict(row)
            at = row.get("started_at") or row.get("created_at")
            out.append({"review_id": row.get("review_id"), "source": source, "kind": row.get("kind") or "code",
                        "reviewer": _text(row.get("reviewer") or row.get("instance_id"), 80), "status": _text(row.get("status"), 40),
                        "verdict": verdict or None, "at": _iso(at), "completed_at": _iso(row.get("completed_at")),
                        "changes_requested": verdict in _CHANGES or str(row.get("status") or "").lower() in _CHANGES,
                        "rule": "session" if row.get("session_id") in session_ids else "mission"})
    out.sort(key=lambda r: _time(r.get("at")) or _EPOCH)
    return out


def _runs(view, pr, number, repository):
    identities = _repository_ids(view)
    rows = []
    for group in (view.get("pipelines") or {}).values():
        for row in group:
            if not isinstance(row, dict) or not same_repository(identities.get(str(row.get("provider_repo_id"))), repository):
                continue
            branch = _branch(row.get("branch"))
            if _pull_number(row) == number:
                rows.append(_entry(row, "pull_request"))
            elif pr and pr.get("branch") and branch == _branch(pr["branch"]) and branch.lower() not in SHARED_BRANCHES \
                    and not str(row.get("event") or "").startswith("pull_request"):
                rows.append(_entry(row, "branch"))
    rows.sort(key=_at)
    return rows


def _state_class(state):
    state = str(state or "").lower()
    return "passed" if state in _PASSED else "failed" if state in _FAILED else None


def _signal(value, reason):
    return {"value": value, "reason": reason}


def build(owner, name, number, *, pull_requests, sessions, view, handoffs, reviews, plan_reviews, spend=None, prices=True, now=None):
    """The trail for ``owner/name#number``, or ``None`` when neither a PR record nor a CI run knows this pull request.

    ``handoffs``/``reviews``/``plan_reviews`` are lists, or ``None`` when that source could not be read. ``spend(session_id)`` is
    the usage estimate for one session (``{"configured": False}`` without a price table) or ``None`` when it cannot be read.
    """
    now = now or datetime.now(UTC)
    repository = f"{owner}/{name}"
    pr = _find_pull(pull_requests, owner, name, number)
    ci_ok = bool(view) and view.get("available") is not False
    runs_all = _runs(view, pr, number, repository) if ci_ok else []
    if pr is None and not any(r.get("match") == "pull_request" for r in runs_all):
        return None
    sources = {"pull_request_record": "ok" if pr else "missing", "ci": "ok" if ci_ok else "unavailable",
               "handoffs": "ok" if handoffs is not None else "unavailable", "reviews": "ok" if reviews is not None else "unavailable",
               "plan_reviews": "ok" if plan_reviews is not None else "unavailable"}
    pr_view = {"number": number, "repository": repository, "url": _text(pr.get("url"), 400) if pr else None, "status": (str(pr.get("status") or "unknown").lower() if pr else "unknown"),
               "branch": (_branch(pr.get("branch")) or None) if pr else None, "base": _text(pr.get("base"), 120) if pr else None,
               "opened_at": _iso(pr.get("created_at")) if pr else None, "updated_at": _iso(pr.get("updated_at")) if pr else None,
               "mission_id": pr.get("mission_id") if pr else None}
    linked, ambiguous = _link_sessions(sessions, pull_requests, view if ci_ok else {"available": False}, number, pr, now)
    omitted = {"sessions": max(0, len(linked) - MAX_SESSIONS)}
    linked = linked[:MAX_SESSIONS]
    session_ids = {s["session_id"] for s, _ in linked}
    missions = {m for m in [pr_view["mission_id"], *(s.get("mission_id") for s, _ in linked)] if m}
    hand = _handoff_rows(handoffs, session_ids, missions)
    revs = _review_rows(reviews, plan_reviews, session_ids, missions)
    omitted.update(handoffs=max(0, len(hand) - MAX_HANDOFFS), reviews=max(0, len(revs) - MAX_REVIEWS), ci_runs=max(0, len(runs_all) - MAX_RUNS))
    hand, revs, runs = hand[:MAX_HANDOFFS], revs[:MAX_REVIEWS], runs_all[:MAX_RUNS]
    session_rows = [_session_row(s, rule, now) for s, rule in linked]

    if session_rows:
        state, reason = "linked", ""
    elif ambiguous:
        state, reason = "ambiguous", "Several agents worked on this pull request's branch at the same time, so none can be named."
    elif not ci_ok and not any(p for p in pull_requests):
        state, reason = "unknown", "Neither pull-request records nor CI data are available, so no agent can be linked; this is not a finding that none exists."
    else:
        state, reason = "none", "No agent session matches this pull request's branch, commits or mission."

    # --- signals
    signals = {}
    ended = pr_view["status"] in ("merged", "closed")
    reviews_known = sources["reviews"] == "ok" or sources["plan_reviews"] == "ok"
    changes = [r for r in revs if r["changes_requested"]]
    first_review = _time(revs[0]["at"]) if revs else None
    fix_after = None
    if first_review and ci_ok:
        revisions = {}
        for r in runs_all:
            revisions.setdefault(r["sha"], _at(r))
        fix_after = any(t > first_review for t in revisions.values())
    if changes:
        rework, why = True, f"{len(changes)} review(s) asked for changes"
    elif not reviews_known:
        rework, why = None, "Review records could not be read."
    elif fix_after:
        rework, why = True, "a new revision was built after the first review"
    elif first_review and not ci_ok:
        rework, why = None, "No review asked for changes, but CI data is unavailable, so later fix commits cannot be ruled out."
    elif not revs:
        rework, why = False, "No review asked for changes and no review is recorded; rework is judged from reviews and later revisions only."
    else:
        rework, why = False, "No review asked for changes and no revision was built after the first review."
    merged = pr_view["status"] == "merged"
    signals["merged_without_rework"] = _signal(None if (not merged or rework is None) else not rework,
                                               "The pull request is not merged." if not merged else why)
    ci_value, ci_why = None, "CI data is unavailable." if not ci_ok else "No CI run was recorded for this pull request."
    if runs_all:
        first_sha = runs_all[0]["sha"]
        first_run = next(r for r in runs_all if r["sha"] == first_sha)
        cls = _state_class(first_run.get("state"))
        ci_value = None if cls is None else cls == "passed"
        ci_why = f"The first run of the first revision {first_sha[:8] or '(unknown)'} {'passed' if ci_value else 'failed' if ci_value is False else 'has no final result yet'}."
    signals["ci_green_first_try"] = _signal(ci_value, ci_why)
    signals["ci_reruns"] = _signal(len(runs_all) - len({r["sha"] for r in runs_all}) if runs_all else None,
                                   "Runs beyond the first of each revision." if runs_all else ci_why)

    cost, cost_why = None, "No agent is linked to this pull request."
    if session_rows:
        results = [spend(sid) if spend and prices else None for sid in session_ids]
        if not prices or any(r is not None and not r.get("configured") for r in results):
            cost_why = "No price table is configured (control_plane.usage.prices), so no cost is estimated."
        elif spend is None or any(r is None for r in results):
            cost_why = "Usage records could not be read."
        else:
            unpriced = sorted({u["model"] for r in results for u in r.get("unpriced") or []})
            total = round(sum(r.get("total") or 0 for r in results), 6)
            if unpriced:
                cost_why = f"Models without a price entry: {', '.join(unpriced)[:200]}; a partial figure of {total} USD is not reported."
            else:
                cost, cost_why = total, f"Estimated from {SPEND_NOTE} of the linked agents; usage older than that is not counted."
    signals["cost_usd_estimate"] = _signal(cost, cost_why)

    starts = [t for t in [*(_time(s.get("started_at")) for s in session_rows), *(_time(h.get("created_at")) for h in hand)] if t]
    end = _time(pr_view["updated_at"]) if ended else None
    if starts and end and end >= min(starts):
        signals["duration_s"] = _signal(round((end - min(starts)).total_seconds()), "From the first linked agent or handoff to the pull request's last update, which is its merge or close time only if nothing touched the record afterwards.")
    else:
        signals["duration_s"] = _signal(None, "The pull request is still open or has no end time." if starts else "No agent or handoff start time is known.")
    signals["review_verdicts"] = _signal([{"review_id": r["review_id"], "verdict": r["verdict"]} for r in revs] if reviews_known else None,
                                         "Verdicts of every linked review." if reviews_known else "Review records could not be read.")
    signals["handoff_problems"] = _signal(sum(1 for h in hand if h["problem"]) if handoffs is not None else None,
                                          "Handoffs that were rejected, failed, cancelled or expired." if handoffs is not None else "Handoff records could not be read.")

    timeline = []
    if pr_view["opened_at"]:
        timeline.append({"at": pr_view["opened_at"], "kind": "pr_opened", "ref": number, "text": "opened"})
    for s in session_rows:
        timeline.append({"at": s["started_at"], "kind": "session", "ref": s["session_id"], "rule": s["rule"], "text": f"{s['name'] or s['role'] or s['session_id']} started"})
    for h in hand:
        timeline.append({"at": h["created_at"], "kind": "handoff", "ref": h["handoff_id"], "rule": h["rule"], "text": f"{h['from'].get('harness') or '?'} → {h['to'].get('harness') or '?'}: {h['goal'] or ''}", "state": h["state"], "problem": h["problem"]})
    for r in revs:
        timeline.append({"at": r["at"], "kind": "review", "ref": r["review_id"], "rule": r["rule"], "text": f"{r['kind'].title()} review {r['verdict'] or r['status'] or ''}".strip(), "problem": r["changes_requested"]})
    for r in runs:
        timeline.append({"at": r["at"], "kind": "ci", "ref": f"{r['provider_repo_id']}:{r['run_number']}", "rule": r["match"], "text": f"run {r['run_number']} {r['state'] or 'unknown'}", "state": r["state"], "url": r.get("ci_url")})
    if ended:
        timeline.append({"at": pr_view["updated_at"], "kind": "pr_closed", "ref": number, "text": pr_view["status"]})
    timeline = sorted((e for e in timeline if e["at"]), key=lambda e: _time(e["at"]) or _EPOCH)

    return {"pull_request": pr_view, "state": state, "reason": reason, "sources": sources, "omitted": omitted,
            "sessions": session_rows, "ambiguous": ambiguous[:MAX_SESSIONS], "handoffs": hand, "reviews": revs, "ci_runs": runs,
            "timeline": timeline, "signals": signals}
