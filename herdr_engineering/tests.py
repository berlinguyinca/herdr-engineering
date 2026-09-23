"""Unified test protocol and explorer (spec 0120).

Runner adapters emit TestEvent while native runners remain authoritative.
Adapters: pytest, go, cargo, junit, scala. Re-run translates back into the
native runner command. Cross-worktree isolation via machine/repo/worktree/run
identity. Bounded logs; large logs materialized as artifacts by callers.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .contracts import TestEvent
from .errors import InvalidError

_LO_ISO = "1970-01-01T00:00:00+00:00"


@dataclass
class RunContext:
    repository: str
    worktree_id: str
    machine_id: str
    revision: str
    run_id: str = field(default_factory=lambda: f"testrun_{uuid.uuid4().hex[:16]}")
    workdir: str | None = None


class TestRunnerAdapter(ABC):
    name = "custom"

    @abstractmethod
    def discover(self, ctx: RunContext) -> list[TestEvent]: ...

    @abstractmethod
    def run(self, ctx: RunContext, selector: str | None = None,
            cancel: bool = False) -> list[TestEvent]: ...

    def cancel(self, ctx: RunContext, run_id: str) -> bool:
        return False


def _ev(ctx: RunContext, etype: str, **kw) -> TestEvent:
    base = dict(repository=ctx.repository, worktree_id=ctx.worktree_id,
                machine_id=ctx.machine_id, revision=ctx.revision, run_id=ctx.run_id)
    base.update(kw)
    base["type"] = etype
    if "event_id" not in base:
        base["event_id"] = f"ev_{uuid.uuid4().hex[:16]}"
    return TestEvent(**base)


class PytestAdapter(TestRunnerAdapter):
    """Parses ``pytest -q`` output into TestEvent records."""

    name = "pytest"

    def run(self, ctx, selector=None, cancel=False) -> list[TestEvent]:
        if cancel:
            return []
        cmd = ["python3", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "-rA"]
        if selector:
            cmd.append(selector)
        events: list[TestEvent] = [ _ev(ctx, "run.started")]
        try:
            proc = subprocess.run(cmd, cwd=ctx.workdir, capture_output=True,
                                  text=True, timeout=300)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return events + [_ev(ctx, "run.finished", result_status="errored",
                                  result_message=str(exc))]
        import re
        failed = []
        for line in proc.stdout.splitlines() + proc.stderr.splitlines():
            # short summary lines: "FAILED path::test - message" (message optional)
            m = re.match(r"(PASSED|FAILED|SKIPPED|XFAIL|ERROR)\s+(.+?)(?:\s+-\s+.*)?$", line.strip())
            if m:
                status, name = m.group(1), m.group(2).strip()
                test_id = name.split("[")[0]
                result_status = {"PASSED": "passed", "FAILED": "failed",
                                 "SKIPPED": "skipped", "XFAIL": "xfailed",
                                 "ERROR": "errored"}[status]
                if result_status == "failed":
                    failed.append(name)
                events.append(_ev(ctx, "test.finished", test_id=test_id,
                                  test_display_name=name,
                                  result_status=result_status))
        events.append(_ev(ctx, "run.finished",
                          result_status=("failed" if failed else "passed")))
        return events

    def discover(self, ctx):
        return [_ev(ctx, "run.started")]


class GoAdapter(TestRunnerAdapter):
    name = "go"

    def run(self, ctx, selector=None, cancel=False) -> list[TestEvent]:
        if cancel:
            return []
        cmd = ["go", "test", "-json"]
        if selector:
            cmd.append(selector)
        events: list[TestEvent] = [_ev(ctx, "run.started")]
        try:
            proc = subprocess.run(cmd, cwd=ctx.workdir, capture_output=True,
                                  text=True, timeout=600)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return events + [_ev(ctx, "run.finished", result_status="errored",
                                  result_message=str(exc))]
        for line in proc.stdout.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("Action") == "pass":
                events.append(_ev(ctx, "test.finished",
                                  test_id=rec.get("Test", rec.get("Package", "")),
                                  test_display_name=rec.get("Test"),
                                  result_status="passed",
                                  result_duration_ms=int(rec.get("Elapsed", 0) * 1000)))
            elif rec.get("Action") == "fail":
                events.append(_ev(ctx, "test.finished",
                                  test_id=rec.get("Test", rec.get("Package", "")),
                                  test_display_name=rec.get("Test"),
                                  result_status="failed"))
        events.append(_ev(ctx, "run.finished",
                          result_status=("failed" if proc.returncode else "passed")))
        return events

    def discover(self, ctx):
        return [_ev(ctx, "run.started")]


class CargoAdapter(TestRunnerAdapter):
    name = "cargo"

    def run(self, ctx, selector=None, cancel=False) -> list[TestEvent]:
        if cancel:
            return []
        cmd = ["cargo", "test", "--message-format=json-render-diagnostics"]
        if selector:
            cmd.extend(["--", selector])
        events: list[TestEvent] = [_ev(ctx, "run.started")]
        try:
            proc = subprocess.run(cmd, cwd=ctx.workdir, capture_output=True,
                                  text=True, timeout=900)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return events + [_ev(ctx, "run.finished", result_status="errored",
                                  result_message=str(exc))]
        for line in proc.stdout.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("reason") == "test-result":
                name = rec.get("name", "")
                ok = rec.get("exec_time", 0)
                events.append(_ev(ctx, "test.finished", test_id=name,
                                  test_display_name=name,
                                  result_status=("passed" if rec.get("passed") else "failed"),
                                  result_duration_ms=int(ok * 1000)))
        events.append(_ev(ctx, "run.finished",
                          result_status=("failed" if proc.returncode else "passed")))
        return events

    def discover(self, ctx):
        return [_ev(ctx, "run.started")]


class JunitAdapter(TestRunnerAdapter):
    """Imports JUnit XML reports (Java/Scala-compatible)."""

    name = "junit"

    def __init__(self, report_glob: str = "target/surefire-reports/TEST-*.xml") -> None:
        self.report_glob = report_glob

    def run(self, ctx, selector=None, cancel=False) -> list[TestEvent]:
        if cancel:
            return []
        events: list[TestEvent] = [_ev(ctx, "run.started")]
        try:
            import glob
            import xml.etree.ElementTree as ET
            matches = glob.glob(os.path.join(ctx.workdir or ".", self.report_glob))
            for path in matches:
                tree = ET.parse(path)
                root = tree.getroot()
                for tc in root.iter("testcase"):
                    status = "passed"
                    message = None
                    trace = None
                    for child in tc:
                        if child.tag in ("failure", "error"):
                            status = "failed" if child.tag == "failure" else "errored"
                            message = child.get("message")
                            trace = child.text
                    events.append(_ev(ctx, "test.finished",
                                      test_id=tc.get("name", ""),
                                      test_parent_id=root.get("name"),
                                      test_display_name=tc.get("name"),
                                      test_file=root.get("name"),
                                      result_status=status,
                                      result_message=message,
                                      result_trace=trace,
                                      result_duration_ms=int(float(tc.get("time", 0)) * 1000)))
        except Exception as exc:
            return events + [_ev(ctx, "run.finished", result_status="errored",
                                  result_message=str(exc))]
        events.append(_ev(ctx, "run.finished", result_status="passed"))
        return events

    def discover(self, ctx):
        return [_ev(ctx, "run.started")]


class ScalaAdapter(JunitAdapter):
    """Scala via sbt/ScalaTest/JUnit-compatible reporting."""

    name = "scala"

    def __init__(self) -> None:
        super().__init__(report_glob="target/test-reports/*.xml")


class TestExplorer:
    """Registry of adapters and a normalized test-tree projection.

    ``__test__ = False`` prevents pytest from collecting this application
    class as a test class (its name starts with ``Test``).
    """
    __test__ = False

    def __init__(self, adapters: list[TestRunnerAdapter] | None = None) -> None:
        self._adapters = {a.name: a for a in (adapters or [
            PytestAdapter(), GoAdapter(), CargoAdapter(), JunitAdapter(), ScalaAdapter()])}

    def adapters(self) -> list[str]:
        return sorted(self._adapters)

    def run(self, adapter: str, ctx: RunContext, selector: str | None = None,
            cancel: bool = False) -> list[TestEvent]:
        a = self._adapters.get(adapter)
        if a is None:
            raise InvalidError(f"unknown adapter {adapter}")
        return a.run(ctx, selector=selector, cancel=cancel)

    @staticmethod
    def build_tree(events: list[TestEvent]) -> dict:
        """Build a hierarchy: repo -> suite/parent -> test."""
        nodes: dict[str, dict] = {"id": "root", "name": "root", "kind": "root",
                                  "children": {}, "counts": {}}
        for ev in events:
            if ev.type != "test.finished" or not ev.test_id:
                continue
            suite = ev.test_parent_id or ev.repository or "default"
            sid = f"{ev.repository}:{suite}" if ev.repository else suite
            if sid not in nodes["children"]:
                nodes["children"][sid] = {"id": sid, "name": suite, "kind": "suite",
                                          "children": {}, "counts": {}}
            suite_node = nodes["children"][sid]
            test_id = f"{sid}:{ev.test_id}"
            suite_node["children"][test_id] = {
                "id": test_id, "name": ev.test_display_name or ev.test_id,
                "kind": "test", "status": ev.result_status,
                "file": ev.test_file, "line": ev.test_line,
                "message": ev.result_message, "trace": ev.result_trace,
                "duration_ms": ev.result_duration_ms}
            _bump(suite_node["counts"], ev.result_status)
        return nodes


def _bump(counts: dict, status: str | None) -> None:
    key = status or "unknown"
    counts[key] = counts.get(key, 0) + 1
