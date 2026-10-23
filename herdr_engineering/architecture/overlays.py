"""Overlays and correlations: attach existing Herdr/Pi records to a snapshot WITHOUT inventing links.

A record relates to a snapshot only through an exact canonical repository identity plus a full commit
SHA (the same rule as ``project_links.annotate_ci``). Fields with no source today are returned as null
and named in ``unknown`` so a consumer can tell "not recorded" from "none".
"""
from __future__ import annotations

import re

from ..control_plane import project_links as pl

CORRELATION_FIELDS = ("snapshot_id", "node_or_edge_id", "mission_id", "run_id", "task_id", "worker_id",
                      "parent_worker_id", "session_id", "worktree_id", "host_id", "commit_sha", "issue_ref",
                      "pr_ref", "plan_ref", "autospec_ref", "room_ref", "evidence_ref")
_ISSUE = re.compile(r"(?<![\w/#])((?:[\w.-]+/[\w.-]+)?)#([1-9]\d{0,6})\b")
CI_LIMIT, RELATED_LIMIT = 50, 100


def _matches(record: dict, meta: dict) -> bool:
    return pl._identity(record) == meta["repository_id"] and pl._commit(record) == meta["commit_sha"]


def _compact_ci(row: dict) -> dict:
    keep = ("provider", "repository", "run_number", "state", "status", "branch", "url", "event", "started_at",
            "finished_at", "created_at")
    return {k: row[k] for k in keep if k in row and isinstance(row[k], (str, int, float, bool, type(None)))}


def ci_runs(repo, meta: dict) -> list[dict]:
    out = []
    for (etype, _eid), stream in list(getattr(repo, "_events", {}).items()):
        if etype != "ci_observation" or not stream:
            continue
        row = stream[-1].get("payload")
        if isinstance(row, dict) and _matches(row, meta):
            out.append(_compact_ci(row))
    out.sort(key=lambda r: (str(r.get("started_at") or r.get("created_at") or ""), str(r.get("run_number"))), reverse=True)
    return out[:CI_LIMIT]


def pull_requests(gate_store, meta: dict) -> list[dict]:
    """PRs whose current head is exactly this commit, from the review gate's own records."""
    if gate_store is None:
        return []
    out = []
    for snap in gate_store.snapshots():
        repo = pl.canonical_repository(snap.key.repo, forge_host="github.com")
        if repo == meta["repository_id"] and snap.head_sha == meta["commit_sha"]:
            host, _, path = repo.partition("/")
            out.append({"pr_ref": f"{repo}#{snap.key.number}", "number": snap.key.number, "state": snap.state.value,
                        "head_sha": snap.head_sha,
                        "url": f"https://{host}/{path}/pull/{snap.key.number}" if host == "github.com" else None})
    return out


def _session_rows(repo, meta: dict) -> list[dict]:
    rows = []
    for s in repo.list_sessions():
        if not _matches(s, meta):
            continue
        rows.append(s)
    rows.sort(key=lambda s: s["session_id"])
    return rows[:RELATED_LIMIT]


def correlation(meta: dict, session: dict | None = None, *, node=None, pr_refs=(), mission=None, evidence=None) -> dict:
    rec = dict.fromkeys(CORRELATION_FIELDS)
    rec.update(snapshot_id=meta["snapshot_id"], host_id=meta["host_id"], commit_sha=meta["commit_sha"],
               node_or_edge_id=node, evidence_ref=evidence)
    if session:
        rec.update(session_id=session["session_id"], parent_worker_id=session.get("parent_session_id"),
                   mission_id=session.get("mission_key") or session.get("mission_id"))
        rec["flow"] = session.get("mission_source") or "unknown"   # who owns the mission: pi / herdr / autospec
        rec["session_host_id"] = session.get("host_id")
    if len(pr_refs) == 1:
        rec["pr_ref"] = pr_refs[0]
    rec["unknown"] = sorted(k for k in CORRELATION_FIELDS if rec[k] is None)
    return rec


def issue_mentions(repo, meta: dict, missions: list[dict]) -> list[dict]:
    """Explicit `#N` / `owner/repo#N` mentions in related mission titles. Marked inferred: text, not a link."""
    out, seen = [], set()
    for m in missions:
        full = repo.get_mission(m.get("mission_key") or m.get("mission_id")) or {}
        for field in ("title", "purpose"):
            for repo_part, num in _ISSUE.findall(str(full.get(field) or "")):
                ident = pl.canonical_repository(repo_part, forge_host="github.com") if repo_part else meta["repository_id"]
                ref = f"{ident}#{num}"
                if ident and ref not in seen:
                    seen.add(ref)
                    out.append({"issue_ref": ref, "inferred": True, "evidence": f"mission {field}"})
    return out


def overlays(repo, gate_store, meta: dict) -> dict:
    related = pl._related_records(repo)
    key = (meta["repository_id"], meta["commit_sha"])
    missions = related["missions"].get(key, [])[:RELATED_LIMIT]
    sessions = _session_rows(repo, meta)
    prs = pull_requests(gate_store, meta)
    return {"snapshot_id": meta["snapshot_id"], "repository_id": meta["repository_id"], "commit_sha": meta["commit_sha"],
            "ci": ci_runs(repo, meta), "pull_requests": prs, "missions": missions,
            "sessions": [{"session_id": s["session_id"], "agent_role": s.get("agent_role"), "status": s.get("status"),
                          "host_id": s.get("host_id"), "parent_session_id": s.get("parent_session_id"),
                          "flow": s.get("mission_source") or "unknown"} for s in sessions],
            "issues": issue_mentions(repo, meta, missions),
            "match_rule": "exact canonical repository identity + full commit SHA; branch names and cwd are not evidence",
            "gate_available": gate_store is not None}
