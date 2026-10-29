"""CI results -> ``validated`` lineage edges: the evidence the graph merge gate reads (``graph_merge_gate``).

Nothing else writes ``validated`` edges, so a gate switched on without this makes every PR wait. This is the writer. It does
not add a store, a scheduler or an endpoint:

* it is called from the review gate's EXISTING tick (``GateService(evidence_hook=...)``) with the open-PR listing's head SHA
  and the Woodpecker rows the gate already holds, and
* it writes through the EXISTING ``IngestService.ingest`` (the same admit / persist / apply path as ``POST /api/v1/ingest``),
  under one dedicated, YAML-named principal, so per-principal idempotency, ownership and visibility are the ordinary ones.

Rules (each is tested through the real ingest and the real gate):

* **Observed head only.** The commit written is the PR head the gate just listed, and the verdict is computed from rows whose
  ``revision`` is that exact SHA (``review_gate.trigger.ci_verdict_for``, the rule the gate itself uses). A pass is never
  written for a head that was not observed, from a stale snapshot, or while a required run is still pending.
* **SHA-bound.** ``attrs.commit`` is the head; the server turns it into the edge binding. A push makes the earlier head's
  edges ``stale`` and the gate ignores them (``graph_merge_gate``).
* **Idempotent.** Every event id, source and sequence is a pure function of (repo, PR, head, check, run signature), so a replay
  (restart, second tick, second writer process) is acknowledged as a duplicate. A re-run has a higher run number, so it is a NEW
  edge from a NEW run node and the gate's "latest admission wins" applies; nothing is ever overwritten in place.
* **Only what the operator lists** (YAML ``control_plane.graph_merge_gate.evidence_source``), validated at startup like the
  gate itself, and **off by default**.
* The principal must start with ``ci-evidence:``. API keys are always ``key:<hash>`` and rooms ``collab:...``, so no caller
  can authenticate as this principal over HTTP; it exists only inside this process. The gate trusts it only if the operator
  ALSO lists it in ``evidence_writers`` (a separate, later step).
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from ..review_gate.trigger import _num, ci_verdict_for
from .artifact_lineage import source_name

log = logging.getLogger(__name__)

PRINCIPAL_PREFIX = "ci-evidence:"
_PRINCIPAL = re.compile(r"^ci-evidence:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CHECK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REPO = re.compile(r"^github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_KEYS = frozenset({"enabled", "principal", "check", "repos"})
_REPO_KEYS = frozenset({"repo", "prs"})
MAX_REPOS = 64
MAX_PRS = 256
_REMEMBER = 4096


@dataclass(frozen=True)
class EvidenceSourceConfig:
    principal: str
    check: str
    repos: tuple          # ((repo, frozenset-of-PR-numbers | None), ...); None = every open non-draft PR of that repo

    def wants(self, repo: str, number: int) -> bool:
        for name, prs in self.repos:
            if name == repo:
                return prs is None or number in prs
        return False


def validate_evidence_source(raw) -> EvidenceSourceConfig | None:
    """Startup check of ``graph_merge_gate.evidence_source``. A mapping is shape-validated whether enabled or not (a typo such
    as ``enabld`` stops startup rather than leaving the writer silently off). Returns the config only when ``enabled: true``."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("graph_merge_gate.evidence_source must be a mapping")
    unknown = set(raw) - _KEYS
    if unknown:
        raise ValueError(f"graph_merge_gate.evidence_source: unknown key(s) {sorted(unknown)}")
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        raise ValueError("graph_merge_gate.evidence_source.enabled must be true or false")
    principal = raw.get("principal")
    if principal is not None and not (isinstance(principal, str) and _PRINCIPAL.fullmatch(principal)):
        raise ValueError("graph_merge_gate.evidence_source.principal must look like ci-evidence:<name> "
                         "(a dedicated principal; API-key ids such as key:<hex> are refused)")
    check = raw.get("check", "ci")
    if not isinstance(check, str) or not _CHECK.fullmatch(check):
        raise ValueError("graph_merge_gate.evidence_source.check must be a short check name")
    repos = raw.get("repos", [])
    if not isinstance(repos, list) or len(repos) > MAX_REPOS:
        raise ValueError(f"graph_merge_gate.evidence_source.repos must be a list of at most {MAX_REPOS} entries")
    parsed, seen = [], set()
    for entry in repos:
        if not isinstance(entry, dict) or set(entry) - _REPO_KEYS:
            raise ValueError("graph_merge_gate.evidence_source.repos entries are {repo: github.com/<owner>/<name>, "
                             "prs: [numbers]?}")
        repo = entry.get("repo")
        if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo in seen:
            raise ValueError("graph_merge_gate.evidence_source.repos[].repo must be a unique github.com/<owner>/<name>")
        seen.add(repo)
        prs = entry.get("prs")
        if prs is not None:
            if not isinstance(prs, list) or not prs or len(prs) > MAX_PRS or not all(
                    isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in prs):
                raise ValueError("graph_merge_gate.evidence_source.repos[].prs must be a non-empty list of PR numbers")
            prs = frozenset(prs)
        parsed.append((repo, prs))
    if raw.get("enabled") is not True:
        return None
    if principal is None:
        raise ValueError("graph_merge_gate.evidence_source.principal is required when enabled")
    if not parsed:
        raise ValueError("graph_merge_gate.evidence_source.repos must list at least one repo when enabled")
    return EvidenceSourceConfig(principal, check, tuple(parsed))


def evidence_config_from_gate_section(section) -> EvidenceSourceConfig | None:
    """From the raw ``graph_merge_gate`` mapping (``None`` when absent or the writer is off)."""
    if not isinstance(section, dict):
        return None
    return validate_evidence_source(section.get("evidence_source"))


def _signature(sha: str, rows, enrollment) -> str:
    """Identity of the CI state at ``sha``: the newest run per required event with its state. A re-run changes it."""
    latest: dict = {}
    for r in rows:
        if r.get("revision") != sha or r.get("event") not in enrollment.required_events:
            continue
        ev = r["event"]
        if ev not in latest or _num(r) > _num(latest[ev]):
            latest[ev] = r
    text = "|".join(f"{ev}:{_num(r)}:{r.get('state')}" for ev, r in sorted(latest.items()))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class GraphEvidenceWriter:
    """``observe(enrollment, pr, rows, stale)`` is the review gate's ``evidence_hook``. Never raises into the gate."""

    def __init__(self, *, ingest, section_fn):
        self._ingest = ingest
        self._section_fn = section_fn          # raw ``control_plane.graph_merge_gate`` mapping, read hot on every call
        self._done: dict = {}
        self.last_error: str | None = None

    def observe(self, enrollment, pr, rows, stale: bool) -> str:
        """Returns what happened: ``off`` | ``skipped`` | ``pending`` | ``written`` | ``known`` | ``error``."""
        try:
            cfg = evidence_config_from_gate_section(self._section_fn())
        except ValueError as exc:
            self.last_error = str(exc)
            return "error"
        if cfg is None:
            return "off"
        repo, number, sha = enrollment.repo, pr.number, str(pr.head_sha or "").lower()
        if not cfg.wants(repo, number) or not _SHA.fullmatch(sha) or stale:
            return "skipped"
        verdict = ci_verdict_for(sha, rows, enrollment, snapshot_stale=False)
        if verdict.result == "pending":
            return "pending"                   # never a pass (or a fail) for a head whose CI has not finished
        conclusion = "success" if verdict.result == "green" else "failure"
        sig = _signature(sha, rows, enrollment)
        memo = (repo, number, sha, cfg.check, sig)
        if memo in self._done:
            return "known"
        try:
            self._write(cfg, repo, number, sha, conclusion, sig)
        except Exception as exc:               # noqa: BLE001 - evidence is best effort; the gate then simply waits
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("graph evidence: %s#%s not written", repo, number)
            return "error"
        self.last_error = None
        if len(self._done) >= _REMEMBER:
            self._done.pop(next(iter(self._done)))
        self._done[memo] = conclusion
        return "written"

    def _write(self, cfg, repo, number, sha, conclusion, sig) -> None:
        principal = {"kind": "agent", "id": cfg.principal, "authenticated_by": "evidence_writer"}
        pr_id = f"pr:{repo}#{number}"
        run_id = f"ci:{repo}#{number}/{cfg.check}/{sig}@{sha}"

        def send(source, seq, event_type, entity_id, payload):
            self._ingest.ingest({
                "schema_version": 2, "event_id": f"{source}-{seq}", "source": source, "source_sequence": seq,
                "entity_type": "lineage", "entity_id": entity_id, "event_type": event_type, "payload": payload,
                "visibility_scope": "org"}, principal)

        # the PR node: an "ensure" under one stable source (a node may be touched by only a few sources per writer)
        send(source_name("cievpr", f"{cfg.principal}|{pr_id}"), 1, "node.upsert", pr_id,
             {"node_type": "pr", "node_id": pr_id, "label": f"{repo}#{number}"})
        # one node and one edge per (head, run signature): never an in-place overwrite, so replay is a pure duplicate and the
        # newest admission is the gate's answer for a re-run
        src = source_name("ciev", f"{cfg.principal}|{run_id}|{conclusion}")
        attrs = {"commit": sha, "check": cfg.check, "conclusion": conclusion, "run": sig}
        send(src, 1, "node.upsert", run_id, {"node_type": "ci", "node_id": run_id,
                                              "label": f"{cfg.check} {conclusion}", "state": conclusion, "attrs": attrs})
        send(src, 2, "edge.add", run_id, {"edge_type": "validated", "from_id": run_id, "to_id": pr_id, "attrs": attrs})
