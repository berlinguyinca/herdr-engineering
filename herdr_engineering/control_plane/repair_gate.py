"""Production ``repair_gate`` for ``review_gate.GateService`` (herdr-ci-fix/ branches only).

* Inert by default: unless ``control_plane.project_intelligence.repair.enabled`` is true the gate returns ``allow``
  and every merge behaves exactly as before (the hook can only ever withhold a merge ``decide_merge`` allowed).
* Enabled, it is fail-closed: unknown config keys, an unreadable PR file list, truncated or renamed files (the old
  path is not reported, so risk cannot be classified), or any missing fact turns into ``wait``/``reject``.
* Facts the repository cannot prove from GitHub (human approvals, signed provenance, reviewer execution context and
  model families) come from ``facts_fn``. The default supplies none of them, so a bare deployment never auto-merges.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import repair_policy as rp

_KEYS = {"enabled", "kill_switch", "auto_merge_enabled", "allowed_repositories", "allowed_branches", "auto_merge_risks",
         "max_patch_lines", "require_human_approval", "max_attempts", "max_open_repair_prs", "version", "model_families"}
MAX_PATCH_BYTES = 262144


def policy_from_section(raw: Any) -> rp.RepairPolicy | None:
    """``None`` when repair policy is not enabled. Unknown keys / wrong types raise ValueError (fail loudly)."""
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None
    unknown = set(raw) - _KEYS
    if unknown:
        raise ValueError(f"project_intelligence.repair: unknown key(s) {sorted(unknown)}")

    def strs(name, default=()):
        value = raw.get(name, list(default))
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"project_intelligence.repair.{name} must be a list of strings")
        return frozenset(value)

    def flag(name, default):
        value = raw.get(name, default)
        if not isinstance(value, bool):
            raise ValueError(f"project_intelligence.repair.{name} must be a boolean")
        return value

    def num(name, default):
        value = raw.get(name, default)
        if type(value) is not int or value < 0:
            raise ValueError(f"project_intelligence.repair.{name} must be a non-negative integer")
        return value

    return rp.RepairPolicy(
        version=str(raw.get("version", "p0")), auto_merge_enabled=flag("auto_merge_enabled", False),
        kill_switch=flag("kill_switch", False), allowed_repositories=strs("allowed_repositories"),
        allowed_branches=strs("allowed_branches", ("main",)), auto_merge_risks=strs("auto_merge_risks"),
        max_patch_lines=num("max_patch_lines", 200), require_human_approval=flag("require_human_approval", True),
        max_attempts=num("max_attempts", 3), max_open_repair_prs=num("max_open_repair_prs", 2))


def change_from_material(material: Any) -> tuple[rp.Change | None, str | None]:
    """(Change, None) or (None, why-not). Renames are refused because the old path is not reported."""
    if material is None:
        return None, "PR moved while reading its files"
    if material.truncated:
        return None, "change too large to classify"
    additions = deletions = 0
    for f in material.files:
        if f.status in ("renamed", "copied"):
            return None, f"{f.path}: rename/copy hides the old path"
        for line in (f.patch or "").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
    return rp.Change(paths=tuple(f.path for f in material.files), additions=additions, deletions=deletions), None


def no_facts(enrollment, pr) -> dict:
    return {}


def make_repair_gate(*, app: Any, policy_fn: Callable[[], rp.RepairPolicy | None],
                     facts_fn: Callable[[Any, Any], dict] = no_facts, attempt_fn: Callable[[Any, Any], int] = lambda e, p: 1,
                     open_repair_prs_fn: Callable[[Any], int] = lambda e: 0) -> Callable[[Any, Any, Any], tuple[str, tuple[str, ...]]]:
    def gate(enrollment, pr, checks):
        policy = policy_fn()
        if policy is None:
            return "allow", ()
        if policy.kill_switch:
            return "reject", ("repair kill switch is on",)
        material = app.review_material(enrollment.repo, pr.number, pr.head_sha, max_bytes=MAX_PATCH_BYTES)
        change, why = change_from_material(material)
        if change is None:
            return "wait", (why or "change could not be read",)
        facts = facts_fn(enrollment, pr) or {}
        ev = rp.Evidence(
            repository=enrollment.repo, base_branch=pr.base_ref, head_sha=pr.head_sha, change=change,
            review_sha=pr.head_sha, review_passed=True,      # the hook runs only after decide_merge accepted a PASS at this head
            reviewer_context=facts.get("reviewer_context"), author_context=facts.get("author_context"),
            reviewer_model_family=facts.get("reviewer_model_family"), author_model_family=facts.get("author_model_family"),
            check_shas={c.name: c.sha for c in checks}, check_status={c.name: c.status for c in checks},
            required_checks=tuple(c.name for c in checks), changes_requested=bool(facts.get("changes_requested", False)),
            human_approvals=int(facts.get("human_approvals", 0) or 0), provenance_signed=facts.get("provenance_signed") is True,
            attempt=attempt_fn(enrollment, pr), open_repair_prs=open_repair_prs_fn(enrollment),
            conflicting_incident=facts.get("conflicting_incident") is True,
            scientific_validation_pending=facts.get("scientific_validation_pending") is True)
        decision = rp.evaluate(policy, ev)
        verdict = rp.combine(decision, "merge")
        return {"merge": "allow", "wait": "wait", "reject": "reject"}[verdict], decision.reasons
    return gate


def make_facts_fn(*, app: Any, repo: Any, registry: Any, families_fn: Callable[[], dict]) -> Callable[[Any, Any], dict]:
    """Facts from systems of record. Anything that cannot be established is left out, which makes the policy wait.

    * human approvals / changes requested: GitHub reviews on this exact head (people only).
    * provenance: GitHub reported every commit signature in the PR as verified.
    * author context: the pinned/discovered authoring session; reviewer context: the gate's separate review runner.
    * model families: operator YAML ``model_families: {prefix: family}`` applied to the recorded model names;
      an unlisted model has no family, so independence cannot be shown.
    """
    from ..review_gate.models import PrKey

    def family(model):
        if not isinstance(model, str):
            return None
        for prefix, fam in sorted((families_fn() or {}).items(), key=lambda kv: -len(kv[0])):
            if isinstance(prefix, str) and isinstance(fam, str) and model.lower().startswith(prefix.lower()):
                return fam
        return None

    def facts(enrollment, pr) -> dict:
        key = PrKey(enrollment.repo, pr.number)
        out: dict = {}
        approvals, changes = app.pr_review_summary(enrollment.repo, pr.number, pr.head_sha)
        out["human_approvals"], out["changes_requested"] = approvals, changes
        out["provenance_signed"] = bool(app.pr_commits_verified(enrollment.repo, pr.number, pr.head_sha))
        author = registry.author_of(key, pr.head_ref)
        if author.kind != "unknown":
            out["author_context"] = author.session_id
            out["author_model_family"] = family(author.model)
        verdict_model = None
        for ev in repo.stream("pr_gate", str(key)):
            p = ev.get("payload") or {}
            if ev.get("event_type") == "verdict" and p.get("verdict") == "PASS" and p.get("sha") == pr.head_sha:
                verdict_model = p.get("reviewer_model")
        if verdict_model:
            out["reviewer_context"] = "gate-reviewer"
            out["reviewer_model_family"] = family(verdict_model)
        return out
    return facts
