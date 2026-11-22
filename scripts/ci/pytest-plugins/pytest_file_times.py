"""Prints, at the end of a pytest run, how long each test FILE took and how the files spread over xdist workers.

`--dist=loadfile` hands whole files to workers, so one slow file can leave the other workers idle while it finishes; the per-test
`--durations` list does not show that, a per-file total and a per-worker total do. Loaded with `-p pytest_file_times` and
PYTHONPATH=scripts/ci/pytest-plugins (scripts/ci/pytest-parallel.sh: herdr_pytest_report_load). Only reads reports: it changes no result.
"""
from __future__ import annotations

import os

TOP_FILES = int(os.environ.get("HERDR_PYTEST_FILE_TIMES_TOP", "25"))


def _worker(report) -> str:
    node = getattr(report, "node", None)
    return (getattr(node, "workerinput", None) or {}).get("workerid", "main")


_FILES: dict[str, float] = {}
_WORKERS: dict[str, float] = {}
_FILE_WORKER: dict[str, str] = {}


def pytest_runtest_logreport(report) -> None:
    path = report.nodeid.split("::", 1)[0]
    worker = _worker(report)
    _FILES[path] = _FILES.get(path, 0.0) + report.duration
    _WORKERS[worker] = _WORKERS.get(worker, 0.0) + report.duration
    _FILE_WORKER[path] = worker


def pytest_terminal_summary(terminalreporter) -> None:
    files = _FILES
    if not files:
        return
    write = terminalreporter.write_line
    total = sum(files.values())
    terminalreporter.write_sep("=", f"per-test-file time (top {min(TOP_FILES, len(files))} of {len(files)} files, {total:.1f}s total)")
    for path, seconds in sorted(files.items(), key=lambda item: -item[1])[:TOP_FILES]:
        write(f"{seconds:9.2f}s  {_FILE_WORKER.get(path, 'main'):>6}  {path}")
    workers = _WORKERS
    if len(workers) > 1:
        busiest = max(workers.values())
        terminalreporter.write_sep("=", f"per-worker busy time (slowest {busiest:.1f}s, mean {total / len(workers):.1f}s over {len(workers)} workers)")
        for worker, seconds in sorted(workers.items(), key=lambda item: -item[1]):
            write(f"{seconds:9.2f}s  {worker}")
