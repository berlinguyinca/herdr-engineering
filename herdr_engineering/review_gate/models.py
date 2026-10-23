"""Shared value types for the review gate."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class State(StrEnum):
    CI_PENDING = "ci_pending"
    CI_GREEN = "ci_green"
    REVIEW_QUEUED = "review_queued"
    REVIEWING = "reviewing"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    MERGED = "merged"
    CI_RED = "ci_red"
    SUPERSEDED = "superseded"
    ESCALATED = "escalated"


@dataclass(frozen=True)
class PrKey:
    repo: str
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"

    @classmethod
    def parse(cls, text: str) -> PrKey:
        repo, sep, num = text.strip().rpartition("#")
        if not sep or not repo or not num.isdigit():
            raise ValueError(f"invalid PR key: {text!r}")
        return cls(repo=repo, number=int(num))


@dataclass(frozen=True)
class Finding:
    path: str
    category: str
    message: str
    severity: str
    required_change: str
    line: int | None = None


@dataclass(frozen=True)
class Verdict:
    verdict: Literal["PASS", "FAIL"]
    sha: str
    findings: tuple[Finding, ...]
    reviewer_model: str


@dataclass(frozen=True)
class Author:
    kind: Literal["collab", "pi", "human", "unknown"]
    model: str | None = None
    host_id: str | None = None
    pane_id: str | None = None
    session_id: str | None = None
    mission_id: str | None = None


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: Literal["pass", "fail", "pending", "skipped"]
    sha: str
