"""Which commits (CI runs) and pull requests an agent session produced, derived from records the server already holds.

Nothing is scraped from terminals and nothing is guessed. A CI run is linked to a session only by one of two rules:

* ``commit``: the run's revision equals the commit SHA the session itself reported (``commit_sha``) in the same repository, and
  the run was created inside the session's window (an agent that made no commit still reports its base commit, which older
  runs and other agents share).
* ``branch_window``: the run is on the session's branch in the session's repository, was created inside the session's window
  (``created_at`` to ``updated_at`` plus a grace period, or until now while the session is active and reported recently), and
  the session owns that branch alone. Pull-request event runs are skipped: the provider reports the *target* branch for them. The branch is *not* owned when it is a shared branch (main, master, ...) or when another session on the
  same repository and branch has an overlapping window; those sessions are reported as ``ambiguous`` with no branch matches.

Pull-request records (repository, branch, creation time) follow the same ownership rule. A session without a repository or a
branch, or with nothing matching, is ``none`` with the reason stated.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .project_links import canonical_repository

GRACE = timedelta(minutes=30)
STALE_ACTIVE = timedelta(hours=2)    # an "active" session that has not reported for this long no longer extends to now
SHARED_BRANCHES = frozenset({"main", "master", "trunk", "develop", "development", "head", "production", "release"})
MAX_COMMITS = 50
_ACTIVE = {"active", "running", "working", "idle", "waiting", "ready", "busy"}
_PULL_REF = re.compile(r"^refs/(?:pull|merge-requests)/([1-9][0-9]{0,9})/(?:head|merge)$")
_EPOCH = datetime.min.replace(tzinfo=UTC)


def _time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _branch(value):
    text = str(value or "")
    return text[len("refs/heads/"):] if text.startswith("refs/heads/") else text


def _window(session, now):
    """(start, last activity, end with grace). A session still marked active extends to now only while it keeps reporting."""
    start = _time(session.get("created_at"))
    if start is None:
        return None
    end = _time(session.get("updated_at")) or start
    if str(session.get("status") or "").lower() in _ACTIVE and now - end <= STALE_ACTIVE:
        end = max(end, now)
    return start, end, end + GRACE


def _pull_number(row):
    explicit = row.get("pull_request_number", row.get("pr_number"))
    if isinstance(explicit, (int, str)) and not isinstance(explicit, bool) and re.fullmatch(r"[1-9][0-9]{0,9}", str(explicit)):
        return int(explicit)
    match = _PULL_REF.match(str(row.get("ref") or ""))
    return int(match.group(1)) if match else None


def _repository_ids(view):
    """CI repository id (as the provider reports it) -> the canonical forge identity the sessions use."""
    out = {}
    for repo in view.get("repos") or []:
        identity = canonical_repository(repo.get("forge_url")) if isinstance(repo, dict) else None
        if identity:
            out[str(repo.get("id"))] = identity
    return out


def _key(row):
    return f"{row.get('provider_repo_id')}:{row.get('run_number')}"


def _entry(row, match):
    pull = _pull_number(row)
    return {"provider_repo_id": row.get("provider_repo_id"), "sha": str(row.get("revision") or ""), "run_number": row.get("run_number"), "state": row.get("state"),
            "branch": row.get("branch"), "event": row.get("event"), "ci_url": row.get("url"),
            "at": row.get("created_at"), "match": match, **({"pull_request": pull} if pull else {})}


def _same_repository(a, b):
    """Equal canonical forge identities; a value without a forge host is compared as written, never fuzzily."""
    if not a or not b:
        return False
    return (canonical_repository(a) or a) == (canonical_repository(b) or b)


def session_commits(session, sessions, view, now=None, pull_requests=()):
    """The link record for one session.

    ``sessions`` is every known session (for the branch-ownership check); ``pull_requests`` are the control plane's
    pull-request records, matched on repository, branch and creation time under the same ownership rule as CI runs.
    """
    now = now or datetime.now(UTC)
    repository, branch = session.get("repository_id"), _branch(session.get("branch")) or None
    base = {"session_id": session.get("session_id"), "repository_id": repository, "branch": branch,
            "commits": [], "omitted": 0, "pull_requests": [], "window": None}
    if not repository:
        return {**base, "state": "none", "reason": "The agent reported no repository, so no commit can be matched."}
    identities = _repository_ids(view)
    sha = str(session.get("commit_sha") or "").lower()
    rows = [row for group in (view.get("pipelines") or {}).values() for row in group
            if isinstance(row, dict) and _same_repository(identities.get(str(row.get("provider_repo_id"))), repository)]
    window = _window(session, now)
    exact = {_key(r): _entry(r, "commit") for r in rows if sha and str(r.get("revision") or "").lower() == sha
             and (window is None or ((at := _time(r.get("created_at"))) is not None and window[0] <= at <= window[2]))}
    state, reason, matched, records = "none", "", [], []
    if not branch or not window:
        reason = "The agent reported no branch or start time, so only an exact commit can be matched."
    elif branch.lower() in SHARED_BRANCHES:
        state, reason = "ambiguous", f"{branch} is a shared branch, so its commits cannot be attributed to one agent."
    else:
        others = [(o, w) for o in sessions if o.get("session_id") != session.get("session_id")
                  and _same_repository(o.get("repository_id"), repository) and _branch(o.get("branch")) == branch
                  and (w := _window(o, now)) is not None]
        rivals = [o for o, w in others if w[0] < window[1] and window[0] < w[1]]     # real activity overlaps; grace alone does not
        if rivals:
            state, reason = "ambiguous", f"{len(rivals) + 1} agents worked on {branch} at the same time, so its commits cannot be attributed to one."
        else:
            end = min([window[2]] + [w[0] for _, w in others if w[0] >= window[1]])     # grace stops where the next agent starts
            matched = [_entry(r, "branch_window") for r in rows
                       if _branch(r.get("branch")) == branch and not str(r.get("event") or "").startswith("pull_request")
                       and (at := _time(r.get("created_at"))) is not None and window[0] <= at <= end]
            records = [{"number": p.get("number"), "url": p.get("url"), "status": p.get("status"), "at": p.get("created_at")}
                       for p in pull_requests if isinstance(p, dict) and _branch(p.get("branch")) == branch
                       and _same_repository(p.get("repository"), repository) and isinstance(p.get("number"), int)
                       and (at := _time(p.get("created_at"))) is not None and window[0] <= at <= end]
    merged = {**{f"{e['provider_repo_id']}:{e['run_number']}": e for e in matched}, **exact}
    ordered = sorted(merged.values(), key=lambda e: _time(e.get("at")) or _EPOCH, reverse=True)
    commits, omitted = ordered[:MAX_COMMITS], max(0, len(ordered) - MAX_COMMITS)
    pulls = {}
    for c in commits:
        if c.get("pull_request"):
            pulls[c["pull_request"]] = {"number": c["pull_request"]}
    for p in records:            # a stored pull-request record knows more (url, status) than a run's ref
        pulls[p["number"]] = {**pulls.get(p["number"], {}), **{k: v for k, v in p.items() if v is not None}}
    if commits or pulls:
        state, reason = "linked", ""
    elif state == "none" and not reason and view.get("available") is False:
        state, reason = "unknown", "CI data is not available from the server right now, so no commit can be matched; this is not a finding that none exists."
    elif state == "none" and not reason:
        reason = f"No CI run or pull request on {branch} appeared while this agent worked, and none matches its reported commit."
    return {**base, "state": state, "reason": reason, "commits": commits, "omitted": omitted,
            "pull_requests": sorted(pulls.values(), key=lambda p: p["number"]),
            "window": {"from": window[0].isoformat(), "to": window[2].isoformat()} if window else None}
