"""Pi Engineering and AutoSpec semantic integration (spec 0100).

HerdR Engineering consumes typed lifecycle/state and correlates spec -> issue
-> mission -> worker/session -> worktree -> commit/PR/test/CI/review through
stable identifiers. It never reproduces orchestration logic. Workers map to
their native sessions/worktrees; mission/issue status comes from the owner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Worker:
    worker_id: str
    mission_id: str
    status: str = "unknown"
    herdr_session_id: str | None = None
    worktree_id: str | None = None
    repository: str | None = None
    machine_id: str | None = None
    issue_id: str | None = None
    spec_id: str | None = None
    result_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Mission:
    mission_id: str
    title: str = ""
    status: str = "unknown"
    issue_id: str | None = None
    spec_id: str | None = None
    workers: list[Worker] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"mission_id": self.mission_id, "title": self.title,
                "status": self.status, "issue_id": self.issue_id,
                "spec_id": self.spec_id,
                "workers": [w.to_dict() for w in self.workers]}


@dataclass
class AutoSpecIssue:
    issue_id: str
    spec_id: str | None = None
    title: str = ""
    status: str = "open"
    repository: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class PiEngineeringAdapter:
    """Consumes Pi Engineering semantic lifecycle events (typed, idempotent)."""

    def __init__(self) -> None:
        self._missions: dict[str, Mission] = {}
        self._workers: dict[str, Worker] = {}

    def ingest_mission(self, mission_id: str, *, title: str = "", status: str = "running",
                       issue_id: str | None = None, spec_id: str | None = None) -> Mission:
        mission = self._missions.setdefault(mission_id, Mission(mission_id=mission_id))
        mission.title = title
        mission.status = status
        mission.issue_id = issue_id
        mission.spec_id = spec_id
        return mission

    def ingest_worker(self, *, worker_id: str, mission_id: str, status: str = "running",
                      herdr_session_id: str | None = None, worktree_id: str | None = None,
                      repository: str | None = None, machine_id: str | None = None,
                      issue_id: str | None = None, spec_id: str | None = None,
                      result_summary: str | None = None) -> Worker:
        mission = self._missions.setdefault(mission_id, Mission(mission_id=mission_id))
        worker = Worker(worker_id=worker_id, mission_id=mission_id, status=status,
                        herdr_session_id=herdr_session_id, worktree_id=worktree_id,
                        repository=repository, machine_id=machine_id,
                        issue_id=issue_id, spec_id=spec_id, result_summary=result_summary)
        self._workers[worker_id] = worker
        # dedupe workers within mission
        mission.workers = [w for w in mission.workers if w.worker_id != worker_id] + [worker]
        return worker

    def mission(self, mission_id: str) -> Mission | None:
        return self._missions.get(mission_id)

    def missions(self) -> list[Mission]:
        return list(self._missions.values())


class AutoSpecAdapter:
    """Consumes AutoSpec issue/spec transitions."""

    def __init__(self) -> None:
        self._issues: dict[str, AutoSpecIssue] = {}

    def ingest_issue(self, issue_id: str, *, spec_id: str | None = None,
                     title: str = "", status: str = "open",
                     repository: str | None = None) -> AutoSpecIssue:
        issue = AutoSpecIssue(issue_id=issue_id, spec_id=spec_id, title=title,
                              status=status, repository=repository)
        self._issues[issue_id] = issue
        return issue

    def get(self, issue_id: str) -> AutoSpecIssue | None:
        return self._issues.get(issue_id)


class EngineeringCorrelator:
    """Correlates spec -> issue -> mission -> worker/session -> worktree."""

    def __init__(self) -> None:
        self._links: dict[str, dict[str, Any]] = {}

    def link(self, *, worker: Worker, issue: AutoSpecIssue | None = None,
             spec_id: str | None = None) -> None:
        self._links[worker.worker_id] = {
            "worker_id": worker.worker_id,
            "mission_id": worker.mission_id,
            "session_id": worker.herdr_session_id,
            "worktree_id": worker.worktree_id,
            "repository": worker.repository,
            "issue_id": worker.issue_id or (issue.issue_id if issue else None),
            "spec_id": worker.spec_id or spec_id,
            "machine_id": worker.machine_id,
        }

    def by_worktree(self, worktree_id: str) -> dict[str, Any] | None:
        for link in self._links.values():
            if link.get("worktree_id") == worktree_id:
                return link
        return None

    def by_session(self, session_id: str) -> dict[str, Any] | None:
        for link in self._links.values():
            if link.get("session_id") == session_id:
                return link
        return None
