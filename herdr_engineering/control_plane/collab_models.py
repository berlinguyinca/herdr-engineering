"""Model choice for helper agents (the decision step). Stub: Task 6 implements the rules."""
from __future__ import annotations

from typing import Any, NamedTuple


class Pick(NamedTuple):
    model: str | None
    tier: str
    reason: str


def pick(role: str, task: str, family_hint: str | None, model_hint: str | None,
         catalogue: Any, allowed: list[str] | None, author_model: str | None) -> Pick:
    """Choose a model and tier for a helper. Not decided yet: the runner's default model is used."""
    return Pick(None, "standard", "unpicked")
