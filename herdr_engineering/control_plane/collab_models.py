"""Model choice for helper agents: a deterministic, rules-first decision step.

The catalogue is normalized by the runner from the gateway's model listing; entries look like
``{"id", "family", "vision", "price_out_per_mtok", "context", "local"}``. Strength is approximated
by output price (higher = stronger; unknown = the median); local models tie-break on context.
No model id is ever hard-coded here, and nothing returned names a default: with no eligible
model the answer is ``Pick(None, tier, "blocked: ...")``. Reasons are short and never hold task text.
"""
from __future__ import annotations

import re
from typing import Any, NamedTuple

TIERS = ("trivial", "standard", "hard", "vision")
_VISION_WORDS = re.compile(r"image|screenshot|picture|diagram|photo", re.I)
_HARD_WORDS = ("race", "deadlock", "security", "concurrency", "memory leak", "corruption", "crypto")
_HARD_LONG = 3000
_TRIVIAL_SHORT = 400
_REVIEW_ROLES = ("reviewer", "debugger")


class Pick(NamedTuple):
    model: str | None
    tier: str
    reason: str


def _tier(role: str, task: str) -> tuple[str, str]:
    low = task.lower()
    hard = any(w in low for w in _HARD_WORDS)
    if role == "vision" or _VISION_WORDS.search(task):
        return "vision", "vision role" if role == "vision" else "vision words"
    if role in _REVIEW_ROLES:
        if hard or len(task) > _HARD_LONG:
            return "hard", f"{role} with hard words" if hard else f"{role} with long task"
        return "standard", f"{role} floor"
    if role == "worker" and len(task) < _TRIVIAL_SHORT and not hard:
        return "trivial", "short worker task"
    return "standard", "default"


def _clean(catalogue: Any) -> list[dict]:
    out, seen = [], set()
    for e in catalogue if isinstance(catalogue, (list, tuple)) else ():
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"] or e["id"] in seen:
            continue
        seen.add(e["id"])
        price = e.get("price_out_per_mtok")
        ctx = e.get("context")
        out.append({"id": e["id"], "family": e.get("family") if isinstance(e.get("family"), str) else None,
                    "vision": e.get("vision") is True,
                    "price": float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else None,
                    "context": ctx if isinstance(ctx, int) and not isinstance(ctx, bool) else 0})
    return out


def _median(values: list[float]) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def pick(role: str, task: str, family_hint: str | None, model_hint: str | None,
         catalogue: Any, allowed: list[str] | None, author_model: str | None) -> Pick:
    """Choose a model and tier for a helper (see the module doc for the rules)."""
    task = task if isinstance(task, str) else ""
    tier, why = _tier(role, task)
    notes: list[str] = []
    entries = _clean(catalogue)
    if not entries:
        return Pick(None, tier, f"blocked: no catalogue (tier {tier}, {why})")
    if allowed is not None:
        keep = set(allowed)
        entries = [e for e in entries if e["id"] in keep]
        if not entries:
            return Pick(None, tier, f"blocked: no allowed model in the catalogue (tier {tier}, {why})")
    # strength: price, with unknown prices at the median of the known ones
    med = _median([e["price"] for e in entries if e["price"] is not None])
    for e in entries:
        e["strength"] = (e["price"] if e["price"] is not None else med, e["context"])
    if tier == "vision":
        entries = [e for e in entries if e["vision"]]
        if not entries:
            return Pick(None, tier, f"blocked: no vision-capable model (tier {tier}, {why})")
    if role in _REVIEW_ROLES and author_model is not None:
        entries = [e for e in entries if e["id"] != author_model]
        if not entries:
            return Pick(None, tier, f"blocked: only the author's model is eligible (tier {tier}, {why})")
        notes.append("author excluded")
    if family_hint:
        fam = [e for e in entries if e["family"] == family_hint]
        if fam:
            entries = fam
            notes.append("family hint honoured")
        else:
            notes.append("family hint ignored: no such family")
    if model_hint:
        hit = [e for e in entries if e["id"] == model_hint]
        if hit:
            return Pick(hit[0]["id"], tier, "; ".join([f"tier {tier} ({why})", "model hint honoured", *notes]))
        notes.append("model hint ignored: not allowed or not capable")
    ordered = sorted(entries, key=lambda e: (e["strength"], e["id"]))
    if tier == "trivial":
        chosen, rule = ordered[0], "cheapest"
    elif tier == "hard":
        top = max(e["strength"] for e in ordered)
        chosen, rule = next(e for e in ordered if e["strength"] == top), "strongest"
    else:                       # standard and vision: the cheapest above the lowest third
        chosen, rule = ordered[len(ordered) // 3], "mid-priced"
    return Pick(chosen["id"], tier, "; ".join([f"tier {tier} ({why})", rule, *notes]))
