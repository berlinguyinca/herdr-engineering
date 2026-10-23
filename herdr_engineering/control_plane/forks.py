"""T13: alternative implementation forks - comparison read model and promotion readiness (HCN workbench).

A fork is NOT a new object kind and Herdr stores nothing new. It is what the existing lineage contract already
names: an ``approach:<id>`` node (OWNED, written by the Pi emitter that created the worktree) with a ``fork`` edge
``origin -> approach``; siblings are the approaches that share an origin. A decision is a ``supersession`` edge
``winner -> loser`` (new -> old), which the T12 fence already treats as "superseded". Everything here is a pure
READ over the graph projection through the room-narrowed reader that T12 builds, so a fork of something the reader
cannot see is the same 404 as a missing one.

TRUST. The approach node and its attributes (diff stats, touched paths, cost) are written by the fork's emitter,
and the fork's own agent controls the code under test. They are shown, always labelled ``self_reported``, and
NEVER decide anything. What may gate a promotion is only head-bound evidence (a ``validated`` / ``reviewed`` edge
bound to the approach's exact ``head_sha``, not stale, not removed) written by a principal the OPERATOR listed in
``control_plane.collab.steering.forks.evidence_writers``. The list comes from YAML, never from an event.
T06's review quorum is not on this stack; ``quorum`` says so rather than inventing a verdict.

Nothing here writes, deletes, closes or merges anything.
"""
from __future__ import annotations

import re
from typing import Any

from ..candidates import Candidate, candidate_id

FORK_ORIGIN_TYPES = frozenset({"attempt", "agent", "task", "approach"})
SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
TERMINAL = frozenset("done failed cancelled superseded merged closed passed completed success error "
                     "rejected approved aborted".split())
SELECTED_STATES = frozenset({"selected", "promoted"})
FORKS_KEYS = frozenset({"enabled", "max_forks_per_origin", "max_open_forks_per_room", "require_validation",
                        "min_reviews", "evidence_writers", "max_compared"})
MAX_PATHS = 100
MAX_PATH_CHARS = 200
_CTRL = re.compile("[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿]")
BASE_ATTRS = ("head_sha", "commit", "base_sha", "checkpoint_sha")


class ForkBlocked(Exception):
    """A fork or promotion cannot proceed; ``code`` is stable, ``blockers`` lists every reason found."""

    def __init__(self, code: str, message: str, blockers: list | None = None):
        super().__init__(message)
        self.code, self.message, self.blockers = code, message, blockers or []


def normalize_forks(raw: Any) -> dict:
    """Validate ``control_plane.collab.steering.forks`` (YAML only). Off by default; unknown keys stop startup."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.collab.steering.forks must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - FORKS_KEYS)
    if unknown:
        raise ValueError("control_plane.collab.steering.forks: unknown keys: " + ", ".join(unknown))
    cfg = {"enabled": False, "max_forks_per_origin": 5, "max_open_forks_per_room": 20,
           "require_validation": True, "min_reviews": 0, "evidence_writers": [], "max_compared": 10}
    ranges = {"max_forks_per_origin": (1, 20), "max_open_forks_per_room": (1, 200), "min_reviews": (0, 8),
              "max_compared": (2, 20)}
    for key, value in raw.items():
        if key in ("enabled", "require_validation"):
            if not isinstance(value, bool):
                raise ValueError(f"control_plane.collab.steering.forks.{key} must be true or false")
        elif key == "evidence_writers":
            if (not isinstance(value, list) or len(value) > 32
                    or not all(isinstance(w, str) and 0 < len(w) <= 128 for w in value)):
                raise ValueError("control_plane.collab.steering.forks.evidence_writers must be a list of at most "
                                 "32 graph principal ids")
            value = sorted(set(value))
        else:
            lo, hi = ranges[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"control_plane.collab.steering.forks.{key} must be an integer {lo}-{hi}")
        cfg[key] = value
    return cfg


def clean_text(value: Any, limit: int) -> str:
    return _CTRL.sub("", value)[:limit] if isinstance(value, str) else ""


def _get(graph, reader, node_id: str, edge_type: str | None = None):
    try:
        return graph.get_node(reader, node_id, edge_type=edge_type, limit=200)
    except KeyError:
        return None


def _facts(graph, reader, node_id: str):
    try:
        return graph.steer_facts(reader, node_id)
    except KeyError:
        return None


def sibling_ids(graph, reader, origin_id: str) -> tuple[list[str], bool]:
    """Approaches the reader can see that were forked from ``origin_id`` (live ``fork`` edges), and whether the
    read was cut. A cut read must be treated as 'unknown' by anything that gates on it."""
    got = _get(graph, reader, origin_id, "fork")
    if got is None:
        return [], False
    ids = [e["to_id"] for e in got["edges"]
           if e["edge_type"] == "fork" and e["from_id"] == origin_id and not e["removed"]
           and e["to_id"].startswith("approach:")]
    return sorted(set(ids)), bool(got["edges_truncated"] or got["scan_exhausted"])


def origin_of(graph, reader, approach_id: str) -> tuple[str | None, bool]:
    got = _get(graph, reader, approach_id, "fork")
    if got is None:
        return None, False
    origins = sorted({e["from_id"] for e in got["edges"]
                      if e["edge_type"] == "fork" and e["to_id"] == approach_id and not e["removed"]})
    return (origins[0] if len(origins) == 1 else None), bool(got["edges_truncated"] or got["scan_exhausted"])


def base_candidates(graph, reader, origin_id: str, facts: dict | None = None) -> dict[str, str]:
    """Commits a person may choose as the fork base for ``origin_id``: the origin's own recorded head/commit
    attributes and the commits of what it ``produced`` (checkpoints). {sha: where it was found}. A SHA the graph
    does not show for this origin is never accepted, so a base cannot be typed in from nowhere."""
    out: dict[str, str] = {}
    view = _get(graph, reader, origin_id, "produced")
    attrs = (facts or {}).get("attrs") or {}
    if view is not None:
        attrs = {**view["node"]["attrs"], **attrs}
    for k in BASE_ATTRS:
        v = attrs.get(k)
        if isinstance(v, str) and SHA_RE.match(v.lower()):
            out.setdefault(v.lower(), f"origin.{k}")
    if view is not None:
        for e in view["edges"]:
            if e["edge_type"] == "produced" and e["from_id"] == origin_id and not e["removed"]:
                c = (e.get("attrs") or {}).get("commit")
                if isinstance(c, str) and SHA_RE.match(c.lower()):
                    out.setdefault(c.lower(), f"produced {e['to_id']}")
    return out


def _paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for p in value[:MAX_PATHS]:
        p = clean_text(p, MAX_PATH_CHARS)
        if p:
            out.append(p)
    return out


def _num(value: Any, hi: float = 1e12):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= hi else None


def _modules(paths: list[str]) -> list[str]:
    return sorted({p.split("/", 1)[0] if "/" in p else "." for p in paths})


def _evidence(graph, reader, approach_id: str, head: str | None, owner: str | None, cfg: dict) -> dict:
    writers = set(cfg["evidence_writers"])
    out = {"validated": [], "reviewed": [], "counted_validations": 0, "counted_reviews": 0, "blocking_reviews": 0,
           "truncated": False}
    for kind in ("validated", "reviewed"):
        got = _get(graph, reader, approach_id, kind)
        if got is None:
            continue
        out["truncated"] = out["truncated"] or bool(got["edges_truncated"] or got["scan_exhausted"])
        for e in got["edges"]:
            if e["edge_type"] != kind or e["to_id"] != approach_id or e["removed"]:
                continue
            writer = (e.get("provenance") or {}).get("writer")
            commit = (e.get("binding") or {}).get("commit")
            attrs = e.get("attrs") or {}
            reasons = []
            if not head or commit is None or str(commit).lower() != head:
                reasons.append("bound_to_another_head")
            if e.get("stale"):
                reasons.append("stale")
            if writer not in writers:
                reasons.append("writer_not_trusted")
            item = {"from_id": e["from_id"], "writer": writer, "commit": commit, "stale": bool(e["stale"]),
                    "verdict": clean_text(attrs.get("verdict"), 16) or None,
                    "counted": False, "not_counted_because": reasons}
            if kind == "validated":
                item["counted"] = not reasons
                out["counted_validations"] += 1 if item["counted"] else 0
            else:
                verdict = item["verdict"]
                if not reasons and verdict == "lgtm":
                    item["counted"] = True
                    out["counted_reviews"] += 1
                elif not reasons and verdict == "blocked":
                    out["blocking_reviews"] += 1
                elif not reasons:
                    item["not_counted_because"] = ["verdict_not_lgtm"]
            out[kind].append(item)
        out[kind] = out[kind][:20]
    return out


def promotion_check(graph, reader, approach_id: str, cfg: dict, expected_head: str | None = None) -> dict:
    """Every reason this approach cannot be promoted RIGHT NOW (empty list = ready). Pure read; called at proposal,
    at confirmation and again at delivery, because none of this is covered by the target node's own fence."""
    blockers: list[dict] = []

    def block(code: str, message: str) -> None:
        blockers.append({"code": code, "message": message})

    facts = _facts(graph, reader, approach_id)
    if facts is None:
        return {"ready": False, "blockers": [{"code": "not_found", "message": "not found"}]}
    view = (_get(graph, reader, approach_id) or {"node": {"attrs": {}}})["node"]
    attrs = view["attrs"]
    head = str(attrs.get("head_sha") or "").lower()
    if facts["node_type"] != "approach" or facts["commons"] or not facts["owner"]:
        block("not_an_approach", "only an approach node can be promoted")
    if not SHA_RE.match(head):
        block("no_head", "the approach reports no full head_sha")
        head = ""
    elif expected_head is not None and expected_head.lower() != head:
        block("stale_head", "the approach head moved since you looked")
    if facts["superseded"]:
        block("superseded", "a sibling already superseded this approach")
    if facts["state"] in TERMINAL:
        block("terminal", f"the approach is {facts['state']}")
    if facts["state"] in SELECTED_STATES:
        block("already_selected", "this approach is already the selected one")
    if facts["scan_exhausted"]:
        block("unreadable", "too many edges to read the approach safely")
    origin, cut = origin_of(graph, reader, approach_id)
    siblings: list[str] = []
    if origin is None:
        block("no_origin", "the approach has no single visible origin")
    else:
        siblings, cut2 = sibling_ids(graph, reader, origin)
        if cut or cut2:
            block("unreadable", "the fork group is too large to read safely")
        for sid in siblings:
            if sid == approach_id:
                continue
            sf = _facts(graph, reader, sid)
            if sf and sf["state"] in SELECTED_STATES:
                block("sibling_selected", f"{sid} was already selected for this origin")
    ev = _evidence(graph, reader, approach_id, head or None, facts["owner"], cfg)
    if ev["truncated"]:
        block("unreadable", "too much evidence to read safely")
    if cfg["require_validation"]:
        if not cfg["evidence_writers"]:
            block("no_trusted_evidence", "no evidence_writers are configured, so nothing can count as validation")
        elif ev["counted_validations"] < 1:
            block("no_validation", "no trusted validation is bound to the approach's current head")
    if ev["blocking_reviews"]:
        block("review_blocked", "a trusted reviewer blocked the current head")
    if cfg["min_reviews"] and ev["counted_reviews"] < cfg["min_reviews"]:
        block("insufficient_reviews", f"{ev['counted_reviews']} of {cfg['min_reviews']} head-bound approvals")
    return {"ready": not blockers, "blockers": blockers, "origin": origin, "siblings": siblings, "head": head or None,
            "owner": facts["owner"], "evidence": ev}


def _card(graph, reader, approach_id: str, cfg: dict) -> dict | None:
    facts = _facts(graph, reader, approach_id)
    got = _get(graph, reader, approach_id)
    if facts is None or got is None:
        return None
    view = got["node"]
    a = view["attrs"]
    head = str(a.get("head_sha") or "").lower()
    base = str(a.get("base_sha") or "").lower()
    paths = _paths(a.get("touched_paths"))
    check = promotion_check(graph, reader, approach_id, cfg)
    ev = check.get("evidence") or {}
    owner = facts["owner"]
    sr = {"source": "approach_owner", "writer": owner, "verified": False}
    cand = Candidate(
        id=candidate_id(clean_text(a.get("repo"), 200), approach_id, clean_text(a.get("branch"), 200)),
        mission_id=clean_text(a.get("mission_id"), 128) or None, repository=clean_text(a.get("repo"), 200) or None,
        worktree_id=clean_text(a.get("worktree_id"), 128) or None, branch=clean_text(a.get("branch"), 200) or None,
        revision=head or None, status=facts["state"] or "unknown", changed_files=paths,
        diff_summary=f"+{_num(a.get('insertions')) or 0} -{_num(a.get('deletions')) or 0} "
                     f"in {_num(a.get('files_changed')) or len(paths)} files (self-reported)",
        ci_state="passed" if ev.get("counted_validations") else None,
        review_state="approved" if ev.get("counted_reviews") else ("blocked" if ev.get("blocking_reviews") else None))
    return {
        "approach_id": approach_id, "label": clean_text(view["label"], 120) or None, "state": facts["state"],
        "superseded": facts["superseded"], "selected": facts["state"] in SELECTED_STATES,
        "pinned_source": owner, "origin": check.get("origin"),
        "branch": clean_text(a.get("branch"), 200) or None, "repo": clean_text(a.get("repo"), 200) or None,
        "base_sha": base or None, "head_sha": head or None, "provenance": sr,
        "diff": {"files_changed": _num(a.get("files_changed")), "insertions": _num(a.get("insertions")),
                 "deletions": _num(a.get("deletions")), **sr},
        "impact": {"touched_paths": paths, "modules": _modules(paths), **sr},
        "cost": {"elapsed_s": _num(a.get("elapsed_s")), "cost_usd": _num(a.get("cost_usd"), 1e9),
                 "tokens": _num(a.get("tokens")), **sr},
        "evidence": {k: ev.get(k) for k in ("validated", "reviewed", "counted_validations", "counted_reviews",
                                            "blocking_reviews")},
        "readiness": {"ready": check["ready"], "blockers": check["blockers"]},
        "candidate": cand.to_dict(),
    }


def comparison(graph, reader, node_id: str, cfg: dict) -> dict:
    """The comparison for the fork group that ``node_id`` (an origin or one approach) belongs to. KeyError when the
    node is hidden or missing. Bounded: at most ``max_compared`` approaches are expanded."""
    facts = _facts(graph, reader, node_id)
    if facts is None:
        raise KeyError(node_id)
    origin = node_id
    if facts["node_type"] == "approach":
        origin, _ = origin_of(graph, reader, node_id)
        if origin is None:
            return {"node_id": node_id, "origin": None, "forks": [], "truncated": False, "overlap": [],
                    "downstream": {"nodes": [], "truncated": False}, "base_candidates": {}, **_policy(cfg)}
    ids, cut = sibling_ids(graph, reader, origin)
    shown = ids[:cfg["max_compared"]]
    cards = [c for c in (_card(graph, reader, i, cfg) for i in shown) if c is not None]
    overlap = []
    for i, x in enumerate(cards):
        for y in cards[i + 1:]:
            both = sorted(set(x["impact"]["touched_paths"]) & set(y["impact"]["touched_paths"]))
            if both:
                overlap.append({"a": x["approach_id"], "b": y["approach_id"], "paths": both[:MAX_PATHS],
                                **x["provenance"]})
    ofacts = _facts(graph, reader, origin) or facts
    return {"node_id": node_id, "origin": {"id": origin, "node_type": ofacts["node_type"], "state": ofacts["state"]},
            "forks": cards, "truncated": bool(cut or len(ids) > len(shown)), "overlap": overlap,
            "base_candidates": base_candidates(graph, reader, origin, ofacts),
            "downstream": _downstream(graph, reader, origin, cards), **_policy(cfg)}


def _policy(cfg: dict) -> dict:
    return {"quorum": {"status": "unavailable",
                       "reason": "T06 review quorum is not part of this build; reviews are counted per approach head"},
            "policy": {"require_validation": cfg["require_validation"], "min_reviews": cfg["min_reviews"],
                       "trusted_evidence_configured": bool(cfg["evidence_writers"]),
                       "note": "diff, paths and cost are reported by the fork's emitter and never decide a promotion"}}


def _downstream(graph, reader, origin: str, cards: list[dict]) -> dict:
    """What depends on or reviews the origin's work (T05 links), and which head-bound evidence a promotion would
    leave pointing at another commit. Read-only; bounded."""
    try:
        sub = graph.neighborhood(reader, origin, depth=2, max_nodes=60, max_edges=200,
                                 edge_types={"dependency", "issue_link", "produced", "validated", "reviewed"})
    except KeyError:
        return {"nodes": [], "truncated": False}
    keep = [n for n in sub["nodes"] if n["node_type"] in ("issue", "pr", "task", "gate") and n["id"] != origin]
    nodes = [{"id": n["id"], "node_type": n["node_type"], "state": n["state"],
              "label": clean_text(n["label"], 120) or None} for n in keep[:30]]
    pr_ids = {n["id"] for n in keep if n["node_type"] == "pr"}
    bound = []
    for e in sub["edges"]:
        if e["edge_type"] in ("validated", "reviewed") and e["to_id"] in pr_ids and not e["removed"]:
            bound.append({"edge_type": e["edge_type"], "from_id": e["from_id"], "pr": e["to_id"],
                          "commit": (e.get("binding") or {}).get("commit")})
    out = []
    for c in cards:
        stale = [b for b in bound if c["head_sha"] and str(b["commit"] or "").lower() != c["head_sha"]]
        out.append({"approach_id": c["approach_id"], "evidence_left_on_other_head": stale[:20]})
    return {"nodes": nodes, "per_fork": out, "truncated": bool(sub["truncated"] or len(keep) > 30)}
