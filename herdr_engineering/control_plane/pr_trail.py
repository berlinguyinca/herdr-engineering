"""How agents got to one pull request: sessions, the handoffs between them, reviews, CI runs, the outcome and the signals for learning.

Everything is derived from records the server already holds; nothing is scraped and nothing is guessed. The rule that governs it
all: **a signal is null with a reason unless there is positive evidence, and a wrong attribution is worse than missing data.**

* A session is on the trail (``sessions``) only on evidence: ``session_commits`` links it to this PR by ``commit``,
  ``branch_window`` or ``pull_request_record``. A session that merely shares the PR record's mission is ``inferred`` (listed in
  ``inferred_sessions`` as a hint) and never feeds a signal, cost, duration, handoff or review.
* Handoffs and reviews attach by ``session_id`` of an evidence session, or (reviews) by an explicit reference to this pull request;
  never by mission alone.
* Agents that cannot be told apart on the PR's branch are ``ambiguous`` and named nowhere else. The same owner/name on two forge
  hosts is ``ambiguous`` too, unless the caller names the ``host``.
* A source that could not be read is ``unavailable`` (or ``truncated``) in ``sources``; the figures that rest on it are ``null``.

``signals`` is what a later learning step reads: ``{"value": ..., "reason": ...}`` each. Pure: ``build`` reads the lists it is
given and writes nothing. ``TTLCache`` is the short per-PR cache the endpoint keeps.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from .project_links import canonical_repository
from .redact import redact_text
from .session_commits import (SHARED_BRANCHES, _branch, _entry, _pull_number, _repository_ids, _time, session_commits)

MAX_SESSIONS = 20          # rows shown; signals are computed from every linked row
MAX_HANDOFFS = 50
MAX_REVIEWS = 50
MAX_RUNS = 50
MAX_PRICED_SESSIONS = 100
USAGE_DAYS = 30
_TEXT = 240
_PASSED = frozenset(("passed", "success", "succeeded", "ok"))
_FAILED = frozenset(("failed", "failure", "error", "errored", "killed"))
_CHANGES = frozenset(("changes_requested", "request_changes", "rejected", "denied", "annotated"))
_PROBLEM_HANDOFF = frozenset(("rejected", "failed", "expired", "cancelled"))
_LAST = datetime.max.replace(tzinfo=UTC)
SPEND_NOTE = f"the last {USAGE_DAYS} days of recorded usage"
SIGNALS = ("merged_without_rework", "ci_green_first_try", "ci_reruns", "cost_usd_estimate", "duration_s", "review_verdicts", "handoff_problems")


class TTLCache:
    """A few seconds of memory per key, bounded; the page polls every 30 s and building a trail walks every session."""

    def __init__(self, ttl=5.0, size=64, clock=time.monotonic):
        self.ttl, self.size, self.clock = ttl, size, clock
        self._rows: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._rows.get(key)
            if hit is None:
                return None
            if self.clock() - hit[0] > self.ttl:
                del self._rows[key]
                return None
            return hit[1]

    def put(self, key, value):
        with self._lock:
            self._rows[key] = (self.clock(), value)
            self._rows.move_to_end(key)
            while len(self._rows) > self.size:
                self._rows.popitem(last=False)

    def __len__(self):
        return len(self._rows)


def _text(value, limit=_TEXT):
    return redact_text(str(value))[:limit] if value not in (None, "") else None


def _dict(value):
    return value if isinstance(value, dict) else {}


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


def same_repository(a, b, host=None):
    """Equal identities; when one side has no forge host the owner/name path alone decides, and a named ``host`` must match any host given."""
    ka, kb = _key(a), _key(b)
    if not ka or not kb or ka[1] != kb[1]:
        return False
    if ka[0] and kb[0] and ka[0] != kb[0]:
        return False
    wanted = host.lower() if isinstance(host, str) and host else None
    return not wanted or all(h is None or h == wanted for h in (ka[0], kb[0]))


def _hosts(path, values):
    out = set()
    for value in values:
        k = _key(value)
        if k and k[0] and k[1] == path:
            out.add(k[0])
    return out


def _verdict(row):
    return str(row.get("verdict") or row.get("decision") or row.get("status") or "").lower()


def _find_pull(pull_requests, match, number):
    hits = [p for p in pull_requests if isinstance(p, dict) and p.get("number") == number and match(p.get("repository"))]
    return max(hits, key=lambda p: str(p.get("updated_at") or "")) if hits else None


def _closed_at(pr):
    """A real merge or close time, or None: the record's own ``updated_at`` also moves for unrelated edits, so it is not one."""
    if pr and str(pr.get("status") or "").lower() in ("merged", "closed"):
        return _time(pr.get("merged_at") or pr.get("closed_at"))
    return None


def _session_row(session, rule, inferred):
    return {"session_id": _text(session.get("session_id"), 160), "name": _text(session.get("name") or session.get("title")),
            "role": _text(session.get("agent_role"), 80), "runtime": _text(session.get("runtime"), 40), "model": _text(session.get("model"), 80),
            "host_id": _text(session.get("host_id"), 80), "status": _text(session.get("status"), 40), "mission_id": _text(session.get("mission_id"), 160),
            "branch": _text(_branch(session.get("branch")), 200), "commit_sha": _text(session.get("commit_sha"), 64),
            "started_at": _iso(session.get("created_at")), "last_seen_at": _iso(session.get("updated_at")), "rule": rule, "inferred": inferred}


def _link_sessions(candidates, repo_sessions, pulls, view, number, now):
    """(evidence [(session, rule)], ambiguous [{session_id, reason}]); only ``candidates`` are examined, rivals come from ``repo_sessions``."""
    evidence, ambiguous = [], []
    for session in candidates:
        found = session_commits(session, repo_sessions, view, now, pulls)
        if found["state"] == "linked" and any(p.get("number") == number for p in found["pull_requests"]):
            hits = [c for c in found["commits"] if c.get("pull_request") == number]
            rule = "commit" if any(c.get("match") == "commit" for c in hits) else "branch_window" if hits else "pull_request_record"
            evidence.append((session, rule))
        elif found["state"] == "ambiguous":
            ambiguous.append({"session_id": _text(session.get("session_id"), 160), "reason": _text(found["reason"])})
    evidence.sort(key=lambda pair: _time(pair[0].get("created_at")) or _LAST)       # an undated session sorts last, never first
    return evidence, ambiguous


def _handoff_rows(rows, session_ids):
    out = []
    for row in rows or []:
        row = _dict(row)
        source, target = _dict(row.get("source")), _dict(row.get("target"))
        if source.get("session_id") not in session_ids and target.get("session_id") not in session_ids:
            continue
        entries = [h for h in (row.get("history") if isinstance(row.get("history"), list) else []) if isinstance(h, dict)]
        history = [{"state": _text(h.get("state"), 40), "at": _iso(h.get("at"))} for h in entries]
        queued = next((_time(h.get("at")) for h in entries if h.get("state") == "queued"), None)
        accepted = next((_time(h.get("at")) for h in entries if h.get("state") == "accepted"), None)
        states = {h.get("state") for h in entries}
        out.append({
            "handoff_id": _text(row.get("handoff_id"), 160), "rule": "session",
            "from": {k: _text(source.get(k), 120) for k in ("harness", "agent_id", "session_id")},
            "to": {k: _text(target.get(k), 120) for k in ("harness", "agent_id", "session_id")},
            "to_session_on_trail": target.get("session_id") in session_ids,
            "goal": _text(row.get("goal")), "state": _text(row.get("state"), 40), "created_at": _iso(row.get("created_at")), "history": history,
            "time_to_accept_s": round((accepted - queued).total_seconds()) if queued and accepted and accepted >= queued else None,
            "problem": row.get("state") in _PROBLEM_HANDOFF or bool(states & _PROBLEM_HANDOFF)})
    out.sort(key=lambda h: _time(h.get("created_at")) or _LAST)
    return out


def _names_this_pull(row, match, number):
    explicit = row.get("pull_request_number", row.get("pr_number"))
    return explicit == number and match(row.get("repository"))


def _review_rows(reviews, plan_reviews, session_ids, match, number):
    out = []
    for source, rows in (("review", reviews), ("plan_review", plan_reviews)):
        for row in rows or []:
            row = _dict(row)
            by_session = row.get("session_id") in session_ids
            if not (by_session or _names_this_pull(row, match, number)):
                continue
            verdict = _verdict(row)
            kind = str(row.get("kind") or "code").lower()
            at = row.get("started_at") or row.get("created_at")
            out.append({"review_id": _text(row.get("review_id"), 160), "source": source, "kind": _text(kind, 20),
                        "code": source == "review" or kind == "code",
                        "reviewer": _text(row.get("reviewer") or row.get("instance_id"), 80), "status": _text(row.get("status"), 40),
                        "verdict": _text(verdict, 40), "at": _iso(at), "completed_at": _iso(row.get("completed_at")),
                        "changes_requested": verdict in _CHANGES or str(row.get("status") or "").lower() in _CHANGES,
                        "rule": "session" if by_session else "pull_request"})
    out.sort(key=lambda r: _time(r.get("at")) or _LAST)
    return out


def _runs(view, pr, number, match, closed, now):
    """CI runs of this pull request: those that carry its number, and runs on its own (non-shared) branch inside its lifetime."""
    identities = _repository_ids(view)
    lo = _time(pr.get("created_at")) if pr else None
    hi = closed or now
    rows = []
    for group in (view.get("pipelines") or {}).values():
        for row in group:
            if not isinstance(row, dict) or not match(identities.get(str(row.get("provider_repo_id")))):
                continue
            branch = _branch(row.get("branch"))
            if _pull_number(row) == number:
                rows.append(_entry(row, "pull_request"))
            elif lo and pr.get("branch") and branch == _branch(pr["branch"]) and branch.lower() not in SHARED_BRANCHES \
                    and not str(row.get("event") or "").startswith("pull_request") \
                    and (at := _time(row.get("created_at"))) is not None and lo <= at <= hi:
                rows.append(_entry(row, "branch"))
    clean = []
    for r in rows:
        clean.append({"provider_repo_id": r.get("provider_repo_id"), "sha": _text(r.get("sha"), 64) or "", "run_number": r.get("run_number"),
                      "state": _text(r.get("state"), 40), "branch": _text(r.get("branch"), 200), "event": _text(r.get("event"), 40),
                      "ci_url": _text(r.get("ci_url"), 400), "at": r.get("at"), "match": r["match"]})
    clean.sort(key=lambda r: _time(r.get("at")) or _LAST)
    return clean


def _state_class(state):
    state = str(state or "").lower()
    return "passed" if state in _PASSED else "failed" if state in _FAILED else None


def _signal(value, reason):
    return {"value": value, "reason": reason}


def _all_unknown(reason):
    return {key: _signal(None, reason) for key in SIGNALS}


def _empty(owner, name, number, state, reason, pr=None):
    return {"pull_request": pr or {"number": number, "repository": f"{owner}/{name}", "status": "unknown"}, "state": state, "reason": reason,
            "sources": {}, "omitted": {}, "sessions": [], "inferred_sessions": [], "ambiguous": [], "handoffs": [], "reviews": [], "ci_runs": [],
            "timeline": [], "signals": _all_unknown(reason)}


def build(owner, name, number, *, pull_requests, sessions, view, handoffs, reviews, plan_reviews, spend=None, prices=True, now=None,
          host=None, handoffs_truncated=False):
    """The trail for ``owner/name#number``, or ``None`` when neither a PR record nor a CI run knows this pull request.

    ``handoffs``/``reviews``/``plan_reviews`` are lists, or ``None`` when that source could not be read; ``handoffs_truncated`` says the
    handoff list was cut. ``spend(session_id)`` is the usage estimate for one session (``{"configured": False}`` without a price table) or
    ``None`` when it cannot be read.
    """
    now = now or datetime.now(UTC)
    repository = f"{owner}/{name}"
    path = repository.lower()
    pull_requests = [p for p in pull_requests if isinstance(p, dict)]
    sessions = [s for s in sessions if isinstance(s, dict)]
    ci_ok = bool(view) and view.get("available") is not False
    if not host:
        known = _hosts(path, [*(p.get("repository") for p in pull_requests), *(s.get("repository_id") for s in sessions),
                              *(_repository_ids(view).values() if ci_ok else ())])
        if len(known) > 1:
            return _empty(owner, name, number, "ambiguous",
                          f"{repository} exists on several forge hosts ({', '.join(sorted(known))}); open this page with ?host=<host> to choose one.")

    def match(value):
        return same_repository(value, repository, host)

    pr = _find_pull(pull_requests, match, number)
    closed = _closed_at(pr)
    runs_all = _runs(view, pr, number, match, closed, now) if ci_ok else []
    if pr is None and not any(r.get("match") == "pull_request" for r in runs_all):
        return None
    handoffs_state = "unavailable" if handoffs is None else "truncated" if handoffs_truncated else "ok"
    sources = {"pull_request_record": "ok" if pr else "missing", "ci": "ok" if ci_ok else "unavailable", "handoffs": handoffs_state,
               "reviews": "ok" if reviews is not None else "unavailable", "plan_reviews": "ok" if plan_reviews is not None else "unavailable"}
    pr_view = {"number": number, "repository": repository, "url": _text(pr.get("url"), 400) if pr else None,
               "status": _text(str(pr.get("status") or "unknown").lower(), 40) if pr else "unknown",
               "branch": (_text(_branch(pr.get("branch")), 200)) if pr else None, "base": _text(pr.get("base"), 120) if pr else None,
               "opened_at": _iso(pr.get("created_at")) if pr else None, "updated_at": _iso(pr.get("updated_at")) if pr else None,
               "closed_at": closed.isoformat() if closed else None, "mission_id": _text(pr.get("mission_id"), 160) if pr else None}

    # --- agents: only sessions that could belong are examined
    repo_sessions = [s for s in sessions if match(s.get("repository_id"))]
    pr_branch = _branch(pr.get("branch")) if pr else ""
    candidates = [s for s in repo_sessions if (pr_branch and _branch(s.get("branch")) == pr_branch) or s.get("commit_sha")]
    pulls_here = [p for p in pull_requests if match(p.get("repository"))]
    evidence, ambiguous = _link_sessions(candidates, repo_sessions, pulls_here, view if ci_ok else {"available": False}, number, now)
    evidence_ids = {s["session_id"] for s, _ in evidence if isinstance(s.get("session_id"), str)}
    mission = pr.get("mission_id") if pr else None
    inferred = [s for s in sessions if mission and s.get("mission_id") == mission and s.get("session_id") not in evidence_ids]
    inferred.sort(key=lambda s: _time(s.get("created_at")) or _LAST)

    hand_all = _handoff_rows(handoffs, evidence_ids)
    revs_all = _review_rows(reviews, plan_reviews, evidence_ids, match, number)
    session_rows = [_session_row(s, rule, False) for s, rule in evidence]
    omitted = {"sessions": max(0, len(session_rows) - MAX_SESSIONS), "inferred_sessions": max(0, len(inferred) - MAX_SESSIONS),
               "handoffs": max(0, len(hand_all) - MAX_HANDOFFS), "reviews": max(0, len(revs_all) - MAX_REVIEWS), "ci_runs": max(0, len(runs_all) - MAX_RUNS)}
    hand, revs, runs = hand_all[:MAX_HANDOFFS], revs_all[:MAX_REVIEWS], runs_all[:MAX_RUNS]
    shown = session_rows[:MAX_SESSIONS]
    inferred_rows = [_session_row(s, "mission", True) for s in inferred[:MAX_SESSIONS]]

    if session_rows:
        state, reason = "linked", ""
    elif ambiguous:
        state, reason = "ambiguous", "Several agents worked on this pull request's branch at the same time, so none can be named."
    elif inferred:
        state, reason = "none", "No agent session is linked by evidence; agents that share this pull request's mission are listed only as hints."
    elif not ci_ok and not pulls_here:
        state, reason = "unknown", "Neither pull-request records nor CI data are available, so no agent can be linked; this is not a finding that none exists."
    else:
        state, reason = "none", "No agent session matches this pull request's branch or commits."

    # --- signals, from every linked row (not the displayed ones)
    signals = {}
    status = pr_view["status"]
    code = [r for r in revs_all if r["code"]]
    changed = [r for r in code if r["changes_requested"]]
    dated_runs = [r for r in runs_all if _time(r.get("at")) is not None]
    undated_runs = len(runs_all) - len(dated_runs)
    if status != "merged":
        merged_value, merged_why = None, "The pull request is not merged."
    elif changed:
        merged_value, merged_why = False, f"{len(changed)} code review(s) asked for changes."
    elif sources["reviews"] != "ok":
        merged_value, merged_why = None, "Review records could not be read."
    elif not code:
        merged_value, merged_why = None, "No code review is recorded for this pull request, so rework cannot be judged."
    elif any(_time(r.get("at")) is None for r in code):
        merged_value, merged_why = None, "A code review has no usable time, so what was built after it cannot be told."
    elif not ci_ok:
        merged_value, merged_why = None, "CI data is unavailable, so later fix commits cannot be ruled out."
    elif not runs_all:
        merged_value, merged_why = None, "No CI run is recorded for this pull request, so later fix commits cannot be ruled out."
    elif undated_runs:
        merged_value, merged_why = None, "A CI run has no usable time, so what was built after the first review cannot be told."
    else:
        first_review = min(_time(r["at"]) for r in code)
        first_seen = {}
        for r in dated_runs:
            first_seen.setdefault(r["sha"], _time(r["at"]))
        if any(t > first_review for t in first_seen.values()):
            merged_value, merged_why = False, "A new revision was built after the first code review."
        else:
            merged_value, merged_why = True, "Reviewed, no code review asked for changes, and CI saw no later revision."
    signals["merged_without_rework"] = _signal(merged_value, merged_why)

    ci_value, ci_why, reruns = None, "CI data is unavailable." if not ci_ok else "No CI run was recorded for this pull request.", None
    if runs_all and undated_runs:
        ci_why = "A CI run has no usable time, so the first run cannot be told."
    elif dated_runs:
        first_sha = dated_runs[0]["sha"]
        cls = _state_class(next(r for r in dated_runs if r["sha"] == first_sha).get("state"))
        ci_value = None if cls is None else cls == "passed"
        ci_why = f"The first run of the first revision {first_sha[:8] or '(unknown)'} {'passed' if ci_value else 'failed' if ci_value is False else 'has no final result yet'}."
        reruns = len(dated_runs) - len({r["sha"] for r in dated_runs})
    signals["ci_green_first_try"] = _signal(ci_value, ci_why)
    signals["ci_reruns"] = _signal(reruns, "Runs beyond the first of each revision." if reruns is not None else ci_why)

    starts = [_time(s.get("created_at")) for s, _ in evidence]
    cost, cost_why = None, "No agent is linked to this pull request by evidence."
    if evidence:
        if not prices:
            cost_why = "No price table is configured (control_plane.usage.prices), so no cost is estimated."
        elif len(evidence) > MAX_PRICED_SESSIONS:
            cost_why = f"More than {MAX_PRICED_SESSIONS} agents are linked, too many to price."
        elif any(t is None for t in starts) or min(starts) < now - timedelta(days=USAGE_DAYS):
            cost_why = f"Some linked work is older than {SPEND_NOTE}, so its cost is not known."
        else:
            results = [spend(s["session_id"]) if spend else None for s, _ in evidence]
            if any(r is None for r in results):
                cost_why = "Usage records could not be read."
            elif any(not r.get("configured") for r in results):
                cost_why = "No price table is configured (control_plane.usage.prices), so no cost is estimated."
            elif any(not r.get("by_model") and not r.get("unpriced") for r in results):
                cost_why = "A linked agent has no usage records, so its cost is not known."
            else:
                unpriced = sorted({_text(u.get("model"), 60) or "?" for r in results for u in r.get("unpriced") or []})
                if unpriced:
                    cost_why = f"Models without a price entry: {', '.join(unpriced)[:200]}; a partial figure is not reported."
                else:
                    cost, cost_why = round(sum(r.get("total") or 0 for r in results), 6), f"Estimated from {SPEND_NOTE} of every linked agent, whole sessions."
    signals["cost_usd_estimate"] = _signal(cost, cost_why)

    starts = [t for t in [*starts, *(_time(h.get("created_at")) for h in hand_all)] if t]
    if starts and closed and closed >= min(starts):
        signals["duration_s"] = _signal(round((closed - min(starts)).total_seconds()), "From the first evidence-linked agent or handoff to the recorded merge or close time.")
    else:
        signals["duration_s"] = _signal(None, "No merge or close time is recorded for this pull request." if starts else "No agent or handoff start time is known.")
    reviews_known = sources["reviews"] == "ok" or sources["plan_reviews"] == "ok"
    signals["review_verdicts"] = _signal([{"review_id": r["review_id"], "verdict": r["verdict"]} for r in revs_all] if reviews_known else None,
                                         "Verdicts of every linked review." if reviews_known else "Review records could not be read.")
    problems_known = handoffs_state == "ok"
    signals["handoff_problems"] = _signal(sum(1 for h in hand_all if h["problem"]) if problems_known else None,
                                          "Handoffs that were rejected, failed, cancelled or expired." if problems_known else
                                          "Handoff records could not be read." if handoffs_state == "unavailable" else "The handoff list was cut off, so some may be missing.")

    timeline = []
    if pr_view["opened_at"]:
        timeline.append({"at": pr_view["opened_at"], "kind": "pr_opened", "ref": number, "text": "opened"})
    for s in shown:
        timeline.append({"at": s["started_at"], "kind": "session", "ref": s["session_id"], "rule": s["rule"], "text": f"{s['name'] or s['role'] or s['session_id']} started"})
    for h in hand:
        timeline.append({"at": h["created_at"], "kind": "handoff", "ref": h["handoff_id"], "rule": h["rule"], "text": f"{h['from'].get('harness') or '?'} → {h['to'].get('harness') or '?'}: {h['goal'] or ''}", "state": h["state"], "problem": h["problem"]})
    for r in revs:
        timeline.append({"at": r["at"], "kind": "review", "ref": r["review_id"], "rule": r["rule"], "text": f"{r['kind'].title()} review {r['verdict'] or r['status'] or ''}".strip(), "problem": r["changes_requested"]})
    for r in runs:
        timeline.append({"at": r["at"], "kind": "ci", "ref": f"{r['provider_repo_id']}:{r['run_number']}", "rule": r["match"], "text": f"run {r['run_number']} {r['state'] or 'unknown'}", "state": r["state"], "url": r.get("ci_url")})
    if closed:
        timeline.append({"at": closed.isoformat(), "kind": "pr_closed", "ref": number, "text": status})
    timeline = sorted((e for e in timeline if e["at"]), key=lambda e: _time(e["at"]) or _LAST)

    return {"pull_request": pr_view, "state": state, "reason": reason, "sources": sources, "omitted": omitted,
            "sessions": shown, "inferred_sessions": inferred_rows, "ambiguous": ambiguous[:MAX_SESSIONS], "handoffs": hand, "reviews": revs,
            "ci_runs": runs, "timeline": timeline, "signals": signals}
