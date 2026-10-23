"""Head-bound post-PR review quorum, derived from the lineage graph (T06).

Herdr is a projection, not a reviewer or orchestrator: reviewers (each with their
OWN authenticated ingest key) publish ``reviewed`` edges ``review:* -> pr:*`` bound to
``attrs.commit``; this module only DERIVES whether the CURRENT head of the PR has
enough independent approvals. The policy mirrors ``autospec review-quorum``
(``autospec_core::autonomous::review_quorum``): see docs/contracts/lineage-events.md.

Trust, all from operator configuration (``control_plane.review_quorum`` in the fleet
config), never from the events themselves:

* ``required``            approvals needed at the current head (>= 1).
* ``trusted_head_writers``  principal ids whose ``head_sha`` / ``author`` writes define
                          the PR's current head. Anyone else's value is ignored, so a
                          commons writer cannot roll the head back to a reviewed SHA.
* ``reviewers``           principal ids that may vote, each with an optional GitHub
                          ``login`` (to recognise the PR author). Anyone else's edge is
                          not even looked at. A reviewer IS the server-authenticated
                          owner of the edge, never a name carried in the payload.

``stale`` here means "bound to a commit other than the PR's current head". It is NOT
the graph edge flag of the same name, which means "a later binding was admitted".
Nothing is written: a re-review request is the derived ``rereview_required`` flag.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_FULL_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_MAX_REQUIRED = 16


@dataclass(frozen=True)
class QuorumConfig:
    required: int
    reviewers: dict           # principal id -> {"login": str | None}
    trusted_head_writers: frozenset


def parse_config(raw) -> QuorumConfig | None:
    """Validate the ``review_quorum`` mapping; ``None`` (feature off) when absent."""
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise ValueError("review_quorum must be a mapping")
    unknown = set(raw) - {"required", "reviewers", "trusted_head_writers"}
    if unknown:
        raise ValueError(f"review_quorum: unknown key {sorted(unknown)[0]!r}")
    required = raw.get("required")
    if isinstance(required, bool) or not isinstance(required, int) or not 1 <= required <= _MAX_REQUIRED:
        raise ValueError(f"review_quorum.required must be an integer between 1 and {_MAX_REQUIRED}")
    writers = raw.get("trusted_head_writers")
    if (not isinstance(writers, list) or not writers
            or not all(isinstance(w, str) and w.strip() for w in writers)):
        raise ValueError("review_quorum.trusted_head_writers must be a non-empty list of principal ids")
    reviewers = raw.get("reviewers")
    if isinstance(reviewers, list):
        reviewers = {r: {} for r in reviewers}
    if not isinstance(reviewers, dict) or not reviewers:
        raise ValueError("review_quorum.reviewers must name at least one principal id")
    out = {}
    for rid, spec in reviewers.items():
        if not isinstance(rid, str) or not rid.strip() or not isinstance(spec or {}, dict):
            raise ValueError("review_quorum.reviewers entries must be a principal id (optionally with a login)")
        login = (spec or {}).get("login")
        if login is not None and (not isinstance(login, str) or not login.strip()):
            raise ValueError("review_quorum.reviewers login must be a non-empty string")
        out[rid] = {"login": login.strip().lower() if login else None}
    return QuorumConfig(required, out, frozenset(w.strip() for w in writers))


def _sha(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _FULL_SHA.fullmatch(value) else None


def evaluate(evidence: dict, config: QuorumConfig) -> dict:
    """Decide the quorum from ``GraphProjection.review_evidence`` output (pure)."""
    head = _sha(evidence.get("head"))
    author = (evidence.get("author") or "").strip().lower() or None
    base = {"pr": evidence["pr"], "head": head, "required": config.required, "basis": "all_reviews"}
    if head is None:
        # No trusted, well-formed head: nothing can be current, so nothing can pass.
        return {**base, "status": "unknown_head", "counted": [], "blocking": [], "stale_reviewers": [],
                "rejected": [], "hidden_reviewers": 0, "rereview_required": False, "rereview_needed": 0}
    authors = set(evidence.get("authors") or ())
    by_reviewer: dict[str, list] = {}
    for r in evidence.get("reviews") or ():
        by_reviewer.setdefault(r["reviewer"], []).append(r)

    blocking, stale, rejected, approvals = set(), set(), [], {}
    for rid, items in sorted(by_reviewer.items()):
        current = [i for i in items if _sha(i["commit"]) == head]
        if len(current) < len(items):
            stale.add(rid)
        if any(i["verdict"] == "blocked" for i in current):
            blocking.add(rid)             # a block is never filtered out, whoever filed it
            continue
        lgtm = [i for i in current if i["verdict"] == "lgtm"]
        if not lgtm:
            continue
        login = config.reviewers.get(rid, {}).get("login")
        if rid in authors or (login and author and login == author):
            rejected.append((rid, "author"))
            continue
        digests = {i["receipt_digest"].strip() for i in lgtm
                   if isinstance(i.get("receipt_digest"), str) and i["receipt_digest"].strip()}
        if not digests:
            rejected.append((rid, "missing_receipt_digest"))
            continue
        approvals[rid] = digests
    holders: dict[str, set] = {}
    for rid, digests in approvals.items():
        for d in digests:
            holders.setdefault(d, set()).add(rid)
    replayed = {rid for who in holders.values() if len(who) > 1 for rid in who}
    counted = sorted(rid for rid in approvals if rid not in replayed)
    rejected += [(rid, "replayed_receipt") for rid in sorted(replayed)]
    if blocking:
        status = "blocked"
    elif len(counted) >= config.required:
        status = "satisfied"
    else:
        status = "insufficient"
    needed = config.required - len(counted) if status == "insufficient" else 0
    visible = {rid for rid, items in by_reviewer.items() if all(i["visible"] for i in items)}

    def name(rids):
        return sorted(r for r in rids if r in visible)
    hidden = {r for r in by_reviewer if r not in visible}
    return {**base, "status": status, "counted": name(counted), "counted_total": len(counted),
            "blocking": name(blocking), "blocking_total": len(blocking),
            "stale_reviewers": name(stale),
            "rejected": [{"reviewer": r, "reason": why} for r, why in rejected if r in visible],
            "hidden_reviewers": len(hidden),
            "rereview_required": status == "insufficient", "rereview_needed": needed}


def pr_review_quorum(graph, principal, pr_id: str, config: QuorumConfig) -> dict:
    """The quorum of ``pr_id`` as ``principal`` may see it. KeyError if absent/hidden."""
    evidence = graph.review_evidence(principal, pr_id, config.reviewers, config.trusted_head_writers)
    return evaluate(evidence, config)
