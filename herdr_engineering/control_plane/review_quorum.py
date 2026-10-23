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
import logging
import re
from collections import OrderedDict
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


# ASCII-only normalisation, identical to autospec's: whitespace is the six ASCII blanks and
# case folding touches only A-Z. Python's str.lower()/strip() are Unicode-aware (Kelvin sign
# folds to "k", dotted capital I to "i" plus a mark, NBSP is trimmed), which would let the two
# implementations disagree on who a reviewer or a target is.
_ASCII_BLANKS = " \t\n\r\x0b\x0c"
_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def ascii_norm(value) -> str:
    """Trim ASCII blanks and lower-case A-Z only; non-strings become ''."""
    return value.strip(_ASCII_BLANKS).translate(_ASCII_LOWER) if isinstance(value, str) else ""


def _printable_ascii(value: str) -> bool:
    return all(0x21 <= ord(c) <= 0x7E for c in value)


def canonical_target(value):
    """A review target (``pr:host/owner/repo#n``): non-empty printable ASCII, lower-cased."""
    norm = ascii_norm(value)
    return norm if norm and _printable_ascii(norm) else None


def canonical_identity(value):
    ident = ascii_norm(value)
    if ident.endswith("[bot]"):
        ident = ident[:-5]
    if not ident or len(ident) > 128 or not _printable_ascii(ident):
        return None
    return ident


def receipt_binding(target: str, commit: str, reviewer: str, receipt_digest: str) -> str:
    """What a producer attaches to a receipt digest and an evaluator recomputes."""
    tuple_ = "\0".join(("pr-review-receipt-v1", canonical_target(target) or "", ascii_norm(commit),
                        canonical_identity(reviewer) or "", ascii_norm(receipt_digest)))
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
    value = ascii_norm(value)
    return value if _FULL_SHA.fullmatch(value) else None


def _commit_class(raw, head):
    """'current' | 'stale' | 'invalid' (a block treats a head prefix as current, garbage as invalid)."""
    full = _sha(raw)
    if full is not None:
        return "current" if full == head else "stale"
    low = ascii_norm(raw)
    if low and len(low) >= 7 and re.fullmatch(r"[0-9a-f]+", low) and head.startswith(low):
        return "current"
    return "invalid"


def evaluate(evidence: dict, config: QuorumConfig) -> dict:
    """Decide the quorum from ``GraphProjection.review_evidence`` output (pure)."""
    head = _sha(evidence.get("head"))
    base = {"pr": evidence["pr"], "head": head, "required": config.required, "basis": "all_reviews"}
    empty = {"counted": [], "blocking": [], "stale_reviewers": [], "rejected": [],
             "has_hidden_reviews": False, "blocked_by_hidden": False,
             "rereview_required": False, "rereview_needed": 0, "head_unverified": False, "alert": None,
             "head_resynced": evidence.get("head_state") == "resynced"}
    if head is None:
        return {**base, **empty, "status": "unknown_head"}
    if evidence.get("head_state") == "unverified":
        # A trusted writer's lower-sequence write with a head it had not reported before arrived
        # after a higher-sequence one: a late delivery or an emitter restart. Which one is not
        # knowable, so nothing passes and the response says so (``head_unverified``), until the
        # writer writes past the applied sequence or an operator acknowledges the candidate
        # write (``head_resync``). verify-report's live-head comparison is the second defence.
        return {**base, **empty, "status": "head_unverified", "head_unverified": True,
                "alert": ("the PR head cannot be verified: a trusted emitter wrote a head it had not "
                          "reported before at a lower sequence after a higher one (restart or "
                          "late delivery); re-sync it before relying on this quorum")}
    author = canonical_identity(evidence.get("author"))
    authors = {canonical_identity(a) for a in evidence.get("authors") or ()}
    target = canonical_target(evidence["pr"])
    if target is None:
        return {**base, **empty, "status": "invalid_target"}

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
            verdict = ascii_norm(i.get("verdict"))
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
        digest = ascii_norm(i.get("receipt_digest"))
        if not _DIGEST.fullmatch(digest):
            rejected.append((me, "bad_receipt_digest"))
            continue
        binding = ascii_norm(i.get("receipt_binding"))
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
    visible_counted = shown(counted)
    return {**base, "status": status, "counted": visible_counted, "blocking": shown(blocking),
            "stale_reviewers": shown(stale),
            "rejected": [{"reviewer": r, "reason": why} for r, why in sorted(set(rejected))
                         if r in shown({r})],
            "has_hidden_reviews": hidden_any, "blocked_by_hidden": hidden_block,
            "head_unverified": False, "alert": None,
            "head_resynced": evidence.get("head_state") == "resynced",
            "rereview_required": status == "insufficient",
            # from what THIS viewer can see only: a hidden approval must not shift the number
            "rereview_needed": max(config.required - len(visible_counted), 0) if status == "insufficient" else 0}


_log = logging.getLogger(__name__)


def pr_review_quorum(graph, principal, pr_id: str, config: QuorumConfig) -> dict:
    """The quorum of ``pr_id`` as ``principal`` may see it. KeyError if absent/hidden."""
    try:
        evidence = graph.review_evidence(principal, pr_id, config.reviewers, config.trusted_head_writers)
    except KeyError:
        # PR ids are produced lower-cased by Pi; accept a differently-cased spelling.
        folded = ascii_norm(pr_id)
        if folded == pr_id:
            raise
        evidence = graph.review_evidence(principal, folded, config.reviewers,
                                         config.trusted_head_writers)
    out = evaluate(evidence, config)
    if out["head_unverified"]:
        # once per distinct unverified observation, so a polling consumer cannot flood the log
        warned = getattr(graph, "_quorum_warned", None)
        if warned is None:
            warned = graph._quorum_warned = OrderedDict()
        mark = (evidence["pr"], evidence.get("head_resync_candidate"))
        if mark not in warned:
            warned[mark] = None
            while len(warned) > 256:
                warned.popitem(last=False)
            _log.warning("review quorum: head of %s is unverified (candidate write %s); "
                         "needs a re-sync", evidence["pr"], evidence.get("head_resync_candidate"))
        if principal and principal.get("kind") == "operator" \
                and principal.get("authenticated_by") not in (None, "none"):
            # the exact write an operator acknowledges with a ``head_resync`` attr
            out["resync_event_id"] = evidence.get("head_resync_candidate")
    return out
