"""Head-bound post-PR review quorum, derived from the lineage graph (T06).

Herdr is a projection, not a reviewer or orchestrator: reviewers (each with their
OWN authenticated ingest key) publish ``reviewed`` edges ``review:* -> pr:*`` bound to
``attrs.commit``; this module only DERIVES whether the CURRENT head of the PR has
enough independent approvals.

ONE policy, implemented identically here (workbench read model) and in
``autospec_core::autonomous::review_quorum`` (merge gating). Both suites load
``tests/fixtures/review-quorum-vectors.json`` (byte-identical copies); change a rule
in the vectors first. See docs/contracts/lineage-events.md for the rules.

Trust, all from operator configuration (``control_plane.review_quorum``), never from
events: ``required``; ``trusted_head_writers`` (who defines the PR head and author);
``reviewers`` (the only principals read at all, each with an optional GitHub
``login``); ``reviewer_groups`` (ids of one person/organisation count once).

``stale`` here means "bound to a commit other than the PR's current head". It is NOT
the graph edge flag of the same name. Nothing is written; a re-review request is the
derived ``rereview_required`` flag, consumed by ``autospec review-quorum request``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_FULL_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_REQUIRED = 16


@dataclass(frozen=True)
class QuorumConfig:
    required: int
    reviewers: dict           # principal id -> {"login": str | None}
    trusted_head_writers: frozenset
    reviewer_groups: tuple = ()   # tuple of tuples of principal ids


def canonical_identity(value):
    if not isinstance(value, str):
        return None
    ident = value.strip().lower()
    if ident.endswith("[bot]"):
        ident = ident[:-5]
    if not ident or len(ident) > 128 or any(c.isspace() or not c.isprintable() for c in ident):
        return None
    return ident


def receipt_binding(target: str, commit: str, reviewer: str, receipt_digest: str) -> str:
    """What a producer attaches to a receipt digest and an evaluator recomputes."""
    tuple_ = "\0".join(("pr-review-receipt-v1", target.strip().lower(), commit.strip().lower(),
                        canonical_identity(reviewer) or "", receipt_digest.strip().lower()))
    return hashlib.sha256(tuple_.encode()).hexdigest()


def parse_config(raw) -> QuorumConfig | None:
    """Validate the ``review_quorum`` mapping; ``None`` (feature off) when absent."""
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise ValueError("review_quorum must be a mapping")
    unknown = set(raw) - {"required", "reviewers", "trusted_head_writers", "reviewer_groups"}
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
        if login is not None and canonical_identity(login) is None:
            raise ValueError("review_quorum.reviewers login must be a non-empty string")
        out[rid] = {"login": canonical_identity(login) if login else None}
    seen: set = set()
    groups = []
    for grp in raw.get("reviewer_groups") or []:
        if (not isinstance(grp, list) or not grp
                or not all(isinstance(g, str) and g in out for g in grp)):
            raise ValueError("review_quorum.reviewer_groups must be lists of configured reviewer ids")
        if seen & set(grp) or len(set(grp)) != len(grp):
            raise ValueError("review_quorum.reviewer_groups must not overlap")
        seen |= set(grp)
        groups.append(tuple(grp))
    return QuorumConfig(required, out, frozenset(w.strip() for w in writers), tuple(groups))


def _sha(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _FULL_SHA.fullmatch(value) else None


def _commit_class(raw, head):
    """'current' | 'stale' | 'invalid' (a block treats a head prefix as current, garbage as invalid)."""
    full = _sha(raw)
    if full is not None:
        return "current" if full == head else "stale"
    low = raw.strip().lower() if isinstance(raw, str) else ""
    if low and len(low) >= 7 and re.fullmatch(r"[0-9a-f]+", low) and head.startswith(low):
        return "current"
    return "invalid"


def evaluate(evidence: dict, config: QuorumConfig) -> dict:
    """Decide the quorum from ``GraphProjection.review_evidence`` output (pure)."""
    head = _sha(evidence.get("head"))
    base = {"pr": evidence["pr"], "head": head, "required": config.required, "basis": "all_reviews"}
    empty = {"counted": [], "blocking": [], "stale_reviewers": [], "rejected": [],
             "has_hidden_reviews": False, "blocked_by_hidden": False,
             "rereview_required": False, "rereview_needed": 0}
    if head is None:
        return {**base, **empty, "status": "unknown_head"}
    if evidence.get("head_regressed"):
        # A trusted writer's newest head write lost to an older one by sequence: the head we
        # hold is frozen. Not current, so nothing may pass until a write at a higher sequence.
        return {**base, **empty, "status": "head_unverified"}
    author = canonical_identity(evidence.get("author"))
    authors = {canonical_identity(a) for a in evidence.get("authors") or ()}
    target = evidence["pr"].strip().lower()

    ident = {rid: canonical_identity(rid) for rid in config.reviewers}
    group_of = {}
    for grp in config.reviewer_groups:
        key = min(ident[g] for g in grp if ident[g])
        for g in grp:
            group_of[ident[g]] = key

    def group(i):
        return group_of.get(i, i)

    by_reviewer: dict[str, list] = {}
    for r in evidence.get("reviews") or ():
        by_reviewer.setdefault(r["reviewer"], []).append(r)

    blocking, stale, rejected, pending = set(), set(), [], []
    hidden_block = False
    visible_ids = set()
    for rid, items in sorted(by_reviewer.items()):
        me = ident.get(rid)
        if me is None:
            continue
        for i in items:
            verdict = i["verdict"].strip().lower() if isinstance(i.get("verdict"), str) else ""
            cls = _commit_class(i["commit"], head)
            if i["visible"]:
                visible_ids.add(group(me))
            if verdict == "blocked":
                if cls == "stale":
                    stale.add(group(me))
                    continue
                blocking.add(group(me))
                hidden_block = hidden_block or not i["visible"]
                if cls == "invalid":
                    rejected.append((me, "invalid_commit"))
            elif verdict == "lgtm":
                if cls == "stale":
                    stale.add(group(me))
                    rejected.append((me, "stale_head"))
                else:
                    pending.append((rid, me, i, cls))
            else:
                rejected.append((me, "invalid_verdict"))
    approvals: dict[str, set] = {}
    for rid, me, i, cls in pending:
        if cls != "current" or _sha(i["commit"]) is None:
            rejected.append((me, "invalid_commit"))
            continue
        login = config.reviewers[rid].get("login")
        if me in authors or (author and (me == author or login == author)):
            rejected.append((me, "author"))
            continue
        digest = i.get("receipt_digest")
        digest = digest.strip().lower() if isinstance(digest, str) else ""
        if not _DIGEST.fullmatch(digest):
            rejected.append((me, "bad_receipt_digest"))
            continue
        binding = i.get("receipt_binding")
        binding = binding.strip().lower() if isinstance(binding, str) else ""
        if binding != receipt_binding(target, head, me, digest):
            rejected.append((me, "bad_receipt_binding"))
            continue
        approvals.setdefault(group(me), set()).add(digest)
    holders: dict[str, set] = {}
    for who, digests in approvals.items():
        for d in digests:
            holders.setdefault(d, set()).add(who)
    replayed = {w for who in holders.values() if len(who) > 1 for w in who}
    counted = sorted(w for w in approvals if w not in replayed and w not in blocking)
    rejected += [(w, "replayed_receipt") for w in sorted(replayed)]
    if blocking:
        status = "blocked"
    elif author is None:
        status = "unknown_author"       # fail closed: nobody can be excluded as the author
    elif len(counted) >= config.required:
        status = "satisfied"
    else:
        status = "insufficient"
    if status == "unknown_author":
        counted = []                    # approvals cannot be vetted without an author
    hidden_any = any(not i["visible"] for items in by_reviewer.values() for i in items)

    def shown(groups):
        # a group is named only if every edge of every member is visible to the caller
        hide = {group(ident[r]) for r, items in by_reviewer.items() if ident.get(r)
                and any(not i["visible"] for i in items)}
        return sorted(g for g in groups if g not in hide)
    return {**base, "status": status, "counted": shown(counted), "blocking": shown(blocking),
            "stale_reviewers": shown(stale),
            "rejected": [{"reviewer": r, "reason": why} for r, why in sorted(set(rejected))
                         if r in shown({r})],
            "has_hidden_reviews": hidden_any, "blocked_by_hidden": hidden_block,
            "rereview_required": status == "insufficient",
            "rereview_needed": config.required - len(counted) if status == "insufficient" else 0}


def pr_review_quorum(graph, principal, pr_id: str, config: QuorumConfig) -> dict:
    """The quorum of ``pr_id`` as ``principal`` may see it. KeyError if absent/hidden."""
    try:
        evidence = graph.review_evidence(principal, pr_id, config.reviewers, config.trusted_head_writers)
    except KeyError:
        # PR ids are produced lower-cased by Pi; accept a differently-cased spelling.
        if pr_id.lower() == pr_id:
            raise
        evidence = graph.review_evidence(principal, pr_id.lower(), config.reviewers,
                                         config.trusted_head_writers)
    return evaluate(evidence, config)
