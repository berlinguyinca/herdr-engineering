"""Explicit, versioned runtime-family resume contracts."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResumeContract:
    family: str
    version: str
    checkpoint: bool
    restore: bool
    single_writer_fence: bool

    @property
    def certified(self) -> bool:
        return self.checkpoint and self.restore and self.single_writer_fence


class ResumeRegistry:
    def __init__(self):
        self._contracts: dict[tuple[str, str], ResumeContract] = {}

    def register(self, family: str, version: str, *, checkpoint: bool,
                 restore: bool, single_writer_fence: bool) -> ResumeContract:
        contract = ResumeContract(family, version, checkpoint, restore, single_writer_fence)
        self._contracts[(family, version)] = contract
        return contract

    def get(self, family: str, version: str) -> ResumeContract | None:
        return self._contracts.get((family, version))

    def certified(self, family: str, version: str) -> bool:
        contract = self.get(family, version)
        return bool(contract and contract.certified)


def native_resume_registry() -> ResumeRegistry:
    """Return conservative contracts for native families until certified per version."""
    registry = ResumeRegistry()
    for family in ("pi", "claude-code", "codex", "agent"):
        registry.register(family, "unknown", checkpoint=False, restore=False,
                          single_writer_fence=False)
    return registry

