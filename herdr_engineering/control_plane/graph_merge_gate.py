"""Lineage-graph evidence -> ``review_gate.decision.decide_merge`` (spec 10.4, the CI-failure -> repair -> re-review loop).

No new gate: the graph's ``validated`` edges are turned into ``CheckRun`` rows and handed to the SAME pure ``decide_merge``
that decides every merge, and (when configured) the head-bound review quorum supplies its verdict. What counts:

* only edges written by an ``evidence_writers`` principal (YAML, never an event) - an agent cannot vouch for its own CI;
* only edges bound to the EXACT head SHA (the server-validated ``binding.commit``), not stale, not removed. Evidence for any
  other head is reported ("stale evidence ignored") but never passed on as a check, so after a push the required check is
  ``missing`` and the gate WAITS (passing an old-SHA check would make ``decide_merge`` reject and post a failure per push);
* a conclusion is a pass only when it says so; an unknown value is "pending", never a pass;
* a re-run at the same SHA replaces the earlier run of the same check (latest admission wins), so a flaky red that was
  re-run green does not block for ever, and a late red on the same SHA does block.

``make_graph_gate`` is the ``GateService(graph_gate=...)`` hook: inert unless YAML ``control_plane.graph_merge_gate.enabled``,
it runs only after ``decide_merge`` and the repair policy already allowed a merge, so it can only withhold. An unreadable
graph or config fails closed to ``wait``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..review_gate.decision import MergeDecision, decide_merge
from ..review_gate.models import CheckRun, Verdict
from .review_quorum import pr_review_quorum

READER = {"kind": "operator", "id": "key:merge-gate", "authenticated_by": "merge_gate"}
PASS = frozenset({"pass", "passed", "success", "succeeded", "green"})
FAIL = frozenset({"fail", "failed", "failure", "error", "red", "cancelled", "canceled", "timed_out", "timeout"})
# an explicit graph principal id, "<kind>:<id>" (e.g. key:0123456789ab); no wildcard, no bare word
_PRINCIPAL = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9_.@-][A-Za-z0-9_.:@-]{0,127}$")
_KEYS = frozenset({"enabled", "evidence_writers", "required_checks", "require_review_quorum"})


@dataclass(frozen=True)
class GraphGateConfig:
    evidence_writers: frozenset
    required_checks: tuple
    require_review_quorum: bool = False


def config_from_section(raw) -> GraphGateConfig | None:
    """``None`` unless ``enabled: true``. Unknown keys / wrong types raise ValueError (fail loudly, like repair)."""
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    unknown = set(raw) - _KEYS
    if unknown:
        raise ValueError(f"graph_merge_gate: unknown key(s) {sorted(unknown)}")
    writers, required = raw.get("evidence_writers"), raw.get("required_checks", ["ci"])
    if not isinstance(writers, list) or not writers or not all(isinstance(w, str) and w for w in writers) or len(writers) > 32:
        raise ValueError("graph_merge_gate.evidence_writers must be a non-empty list of at most 32 graph principal ids")
    if not isinstance(required, list) or not required or not all(isinstance(c, str) and c for c in required) \
            or len(required) > 32:
        raise ValueError("graph_merge_gate.required_checks must be a non-empty list of check names")
    flag = raw.get("require_review_quorum", False)
    if not isinstance(flag, bool):
        raise ValueError("graph_merge_gate.require_review_quorum must be a boolean")
    bad = [w for w in writers if not _PRINCIPAL.fullmatch(w)]
    if bad:
        raise ValueError("graph_merge_gate.evidence_writers must be explicit principal ids like key:<12 hex> "
                         f"(no wildcards): refused {bad[:3]}")
    return GraphGateConfig(frozenset(writers), tuple(dict.fromkeys(required)), flag)


def validate_section(raw) -> GraphGateConfig | None:
    """Startup check of ``control_plane.graph_merge_gate``. ``None`` when absent or ``enabled: false``. Anything that
    looks like an attempt to enable it but is not valid (``enabled`` not a boolean, empty or wildcard writers, unknown
    keys) raises ValueError, so a typo can never leave the gate silently off or silently open."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("graph_merge_gate must be a mapping")
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ValueError("graph_merge_gate.enabled must be true or false")
    return config_from_section(raw)


def _status(conclusion) -> str:
    text = conclusion.strip().lower() if isinstance(conclusion, str) else ""
    return "pass" if text in PASS else "fail" if text in FAIL else "pending"


def graph_checks(graph, reader, pr_id: str, head_sha: str, writers: frozenset) -> tuple[list[CheckRun], dict]:
    """(checks bound to ``head_sha``, notes). Raises KeyError when the PR node is not visible to ``reader``."""
    head = head_sha.lower()
    edges = graph.neighborhood(reader, pr_id, depth=1)["edges"]
    best: dict[str, tuple[int, CheckRun]] = {}
    notes = {"stale": [], "untrusted": 0}
    for e in edges:
        if e.get("edge_type") != "validated" or e.get("to_id") != pr_id or e.get("removed"):
            continue
        if (e.get("provenance") or {}).get("writer") not in writers:
            notes["untrusted"] += 1
            continue
        commit = ((e.get("binding") or {}).get("commit") or "")
        commit = commit.lower() if isinstance(commit, str) else ""
        if e.get("stale") or commit != head:
            notes["stale"].append(f"{e['from_id']} @ {commit[:8] or 'unbound'}")
            continue
        attrs = e.get("attrs") or {}
        name = attrs.get("check") if isinstance(attrs.get("check"), str) and attrs.get("check") else "ci"
        admission = int(((e.get("fields") or {}).get("conclusion") or {}).get("admission") or 0)
        if name not in best or admission >= best[name][0]:
            best[name] = (admission, CheckRun(name=name, status=_status(attrs.get("conclusion")), sha=head))
    return [c for _, c in sorted(best.values(), key=lambda t: t[1].name)], notes


def decide_from_graph(graph, pr_id: str, head_sha: str, cfg: GraphGateConfig, *, quorum_cfg=None,
                      reader: dict = READER) -> tuple[MergeDecision, dict]:
    """The merge decision with the graph as input. The review half is the head-bound quorum when ``quorum_cfg`` is given
    (and ``cfg.require_review_quorum``); otherwise a PASS bound to ``head_sha`` is assumed because the caller (the review
    gate) has already established its own verdict at that head."""
    checks, notes = graph_checks(graph, reader, pr_id, head_sha, cfg.evidence_writers)
    verdict = Verdict("PASS", head_sha, (), "graph-gate")
    quorum = None
    if cfg.require_review_quorum:
        if quorum_cfg is None:
            return MergeDecision("wait", ("review quorum is required but not configured",)), {**notes, "quorum": None}
        quorum = pr_review_quorum(graph, reader, pr_id, quorum_cfg)
        if quorum["status"] != "satisfied" or quorum.get("blocking"):
            verdict = None
    decision = decide_merge(head_sha=head_sha, checks=checks, required_checks=cfg.required_checks, verdict=verdict)
    if decision.decision != "merge" and notes["stale"]:
        decision = MergeDecision(decision.decision, (*decision.reasons, "stale evidence ignored: " + ", ".join(notes["stale"][:5])))
    return decision, {**notes, "quorum": quorum and {"status": quorum["status"], "counted": quorum.get("counted")}}


def make_graph_gate(*, graph_fn, section_fn, quorum_fn=lambda: None):
    """``gate(enrollment, pr, checks) -> ("allow"|"wait"|"reject", reasons)`` for ``GateService(graph_gate=...)``.
    ``section_fn()`` returns the raw ``control_plane.graph_merge_gate`` mapping on every call (a hot kill switch)."""
    def gate(enrollment, pr, checks):
        cfg = config_from_section(section_fn())
        if cfg is None:
            return "allow", ()
        decision, _ = decide_from_graph(graph_fn(), f"pr:{enrollment.repo}#{pr.number}", pr.head_sha, cfg,
                                        quorum_cfg=quorum_fn())
        return {"merge": "allow", "wait": "wait", "reject": "reject"}[decision.decision], decision.reasons
    return gate
