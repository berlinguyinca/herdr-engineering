"""pytest plugin for the unit gate: the flaky quarantine, and the failed-test list the one-shot retry reads.

Quarantine (never hides a test, only stops a *failing* one from failing the gate)
  A quarantined test STILL RUNS and is reported (``QUARANTINED`` section of the terminal summary). It is marked non-strict xfail, so
  a failure is an XFAIL and a pass is an XPASS; neither fails the gate. Two ways in, both need a reason, an issue and an expiry:

    @pytest.mark.quarantine_flaky(reason="why it is flaky and what was measured", issue="#873", expires="2026-12-31")

    tests/flaky-quarantine.toml     [[quarantine]] nodeid = "tests/test_x.py::test_y"   (all parametrisations of test_y match too)
                                                   reason = "..."  issue = "#873"  expires = "2026-12-31"

  An entry past its ``expires`` date stops quarantining: the test fails the gate again until someone fixes it or consciously renews the
  date, and ``expires`` may be at most MAX_HORIZON_DAYS (90) days away, so a quarantine cannot become permanent by writing 2099. A marker or
  entry missing a field never quarantines (the test keeps failing loudly) and tests/test_ci_flaky_quarantine.py fails. The marker is named
  ``quarantine_flaky`` (not ``flaky``) so it cannot collide with the ``flaky`` / pytest-rerunfailures plugins, and it works on a test
  FUNCTION only: a module-level ``pytestmark`` or a class decorator would hide every test in the file, so it never quarantines and the
  structural test fails.

Retry support
  With ``HERDR_PYTEST_FAILED_FILE`` set, the pytest controller writes JSON ``{"failed": [{"nodeid", "summary"}], "passed": [nodeid]}``.
  ``python herdr_flaky_plugin.py ids FILE`` / ``files FILE`` print the failed node ids / their test files NUL-separated (the retry reruns
  those whole FILES, so same-module pollution still reproduces) and ``... settle FIRST SECOND OUT [HISTORY]`` compares the first pass with
  the retry: a test counts as RETRIED only when the retry recorded a real PASS for it (a skip or a test that did not run does not), tests
  failing again are real failures, and a test RETRIED in HERDR_PYTEST_CHRONIC_RUNS (3) distinct runs inside HERDR_PYTEST_CHRONIC_DAYS (14)
  days (HISTORY, a JSON-lines file under the CI state dir) is a CHRONIC flake: a loud warning (and ``chronic_runs`` in its JSONL row), failing
  the gate only with HERDR_PYTEST_CHRONIC_FAIL=1.
  scripts/ci/pytest-rerun.sh drives all of it.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

ENV_QUARANTINE = "HERDR_FLAKY_QUARANTINE"
ENV_FAILED_FILE = "HERDR_PYTEST_FAILED_FILE"
ENV_TODAY = "HERDR_FLAKY_TODAY"  # tests only: pretend it is this ISO date
REQUIRED_FIELDS = ("reason", "issue", "expires")
MIN_REASON_CHARS = 15
MARKER = "quarantine_flaky"
MAX_HORIZON_DAYS = 90
QUARANTINE_PREFIX = "QUARANTINED flaky"
DEFAULT_LIST = Path("tests") / "flaky-quarantine.toml"


def today() -> dt.date:
    override = os.environ.get(ENV_TODAY)
    return dt.date.fromisoformat(override) if override else dt.date.today()


def field_problems(fields: dict[str, Any]) -> list[str]:
    """What is wrong with one quarantine declaration (a list entry or a marker's keywords); empty when it is valid."""
    problems: list[str] = []
    for name in REQUIRED_FIELDS:
        value = fields.get(name)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"missing required '{name}'")
    reason = fields.get("reason")
    if isinstance(reason, str) and reason.strip() and len(reason.strip()) < MIN_REASON_CHARS:
        problems.append(f"'reason' must say why, in at least {MIN_REASON_CHARS} characters")
    issue = fields.get("issue")
    if isinstance(issue, str) and issue.strip() and not (issue.strip().startswith(("#", "http://", "https://")) or issue.strip()[:1].isdigit()):
        problems.append("'issue' must be an issue number like '#873' or a URL")
    expires = fields.get("expires")
    if isinstance(expires, dt.date):
        expires = expires.isoformat()
    if isinstance(expires, str) and expires.strip():
        try:
            until = dt.date.fromisoformat(expires.strip())
        except ValueError:
            problems.append("'expires' must be an ISO date (YYYY-MM-DD)")
        else:
            if until > today() + dt.timedelta(days=MAX_HORIZON_DAYS):
                problems.append(f"'expires' is more than {MAX_HORIZON_DAYS} days away; quarantine in short steps and renew consciously")
    return problems


def expiry(fields: dict[str, Any]) -> dt.date | None:
    value = fields.get("expires")
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def is_active(fields: dict[str, Any], on: dt.date | None = None) -> bool:
    """A valid declaration whose expiry has not passed."""
    until = expiry(fields)
    return not field_problems(fields) and until is not None and (on or today()) <= until


def load_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("quarantine", [])
    return [e for e in entries if isinstance(e, dict)]


def entry_problems(entries: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    for i, entry in enumerate(entries):
        nodeid = entry.get("nodeid")
        label = nodeid if isinstance(nodeid, str) and nodeid else f"entry #{i + 1}"
        if not isinstance(nodeid, str) or "::" not in nodeid:
            problems.append(f"{label}: 'nodeid' must be 'path/to/test_file.py::test_name'")
        for problem in field_problems(entry):
            problems.append(f"{label}: {problem}")
    return problems


def quarantine_path(rootdir: Path) -> Path:
    override = os.environ.get(ENV_QUARANTINE)
    return Path(override) if override else rootdir / DEFAULT_LIST


def matches(nodeid: str, wanted: str) -> bool:
    return nodeid == wanted or nodeid.startswith(wanted + "[")


class FlakyPlugin:
    """Registered by ``-p herdr_flaky_plugin`` (the gate) and by tests/conftest.py (a plain pytest run); the second registration is a no-op."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.skipped: list[str] = []

    def pytest_configure(self, config: Any) -> None:
        config.addinivalue_line(
            "markers",
            f"{MARKER}(reason, issue, expires): quarantine a known-flaky test FUNCTION. It still runs and is reported but a failure does not "
            "fail the gate; all three keywords are required (see scripts/ci/herdr_flaky_plugin.py). Expired or incomplete: not quarantined.",
        )
        try:
            self.entries = load_entries(quarantine_path(Path(str(config.rootpath))))
        except (OSError, ValueError) as exc:  # a malformed list must not hide tests: nothing is quarantined, the structural test fails
            self.skipped.append(f"quarantine list unreadable: {exc}")

    def _declaration(self, item: Any) -> dict[str, Any] | None:
        marker = item.get_closest_marker(MARKER)
        if marker is not None:
            if any(m.name == MARKER for m in item.own_markers):
                return {**marker.kwargs, "source": "marker"}
            self.skipped.append(f"{item.nodeid}: quarantine not applied (a module/class-level {MARKER} would hide every test in it; mark the test function)")
        for entry in self.entries:
            if isinstance(entry.get("nodeid"), str) and matches(item.nodeid, entry["nodeid"]):
                return {**entry, "source": "list"}
        return None

    def pytest_collection_modifyitems(self, config: Any, items: list[Any]) -> None:
        import pytest

        for item in items:
            decl = self._declaration(item)
            if decl is None:
                continue
            if not is_active(decl):
                self.skipped.append(f"{item.nodeid}: quarantine not applied ({'; '.join(field_problems(decl)) or 'expired ' + str(decl.get('expires'))})")
                continue
            item.add_marker(pytest.mark.xfail(
                reason=f"{QUARANTINE_PREFIX} ({decl['issue']}, until {decl['expires']}): {decl['reason']}", strict=False))

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        stats = terminalreporter.stats
        ran = [(kind, r) for kind in ("xfailed", "xpassed") for r in stats.get(kind, [])
               if str(getattr(r, "wasxfail", "")).startswith(QUARANTINE_PREFIX)]
        if ran:
            terminalreporter.write_sep("=", "QUARANTINED flaky tests (ran; they do not fail the gate)")
            for kind, report in ran:
                terminalreporter.write_line(f"QUARANTINED {'failed' if kind == 'xfailed' else 'passed'}: {report.nodeid} - {report.wasxfail}")
        for note in self.skipped:
            terminalreporter.write_line(f"QUARANTINE NOTE: {note}")

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        target = os.environ.get(ENV_FAILED_FILE)
        if not target or hasattr(session.config, "workerinput"):
            return
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        stats = reporter.stats if reporter else {}
        failed: dict[str, str] = {}
        for kind in ("failed", "error"):
            for report in stats.get(kind, []):
                failed.setdefault(report.nodeid, summarise(report))
        # a real PASS only: a skip, a deselection or a test that never ran is none of these (a setup/teardown error is in ``failed``)
        passed = sorted(({r.nodeid for r in stats.get("passed", []) if getattr(r, "when", "call") == "call"}
                         | {r.nodeid for r in stats.get("xpassed", [])}) - set(failed))
        Path(target).write_text(json.dumps({"failed": [{"nodeid": n, "summary": t} for n, t in failed.items()], "passed": passed}),
                                encoding="utf-8")


def summarise(report: Any) -> str:
    """One line naming why a test failed: the assertion/exception message, never the whole traceback."""
    crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
    text = getattr(crash, "message", None) or str(getattr(report, "longrepr", "") or "")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "no failure text")
    return first[:300]


def pytest_configure(config: Any) -> None:  # loaded as a ``-p herdr_flaky_plugin`` module: register the class once
    register(config)


def register(config: Any) -> None:
    if not config.pluginmanager.has_plugin("herdr-flaky-quarantine"):
        config.pluginmanager.register(FlakyPlugin(), "herdr-flaky-quarantine")


def missing_test(root: Path, nodeid: str) -> str | None:
    """Why ``nodeid`` ('path.py::[Class::]test_name[params]') names no real test, or None when the file defines that function."""
    path, _, rest = nodeid.partition("::")
    file = root / path
    if not file.is_file():
        return f"{nodeid}: no such test file (stale entry; delete it)"
    name = rest.split("::")[-1].split("[")[0]
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    except SyntaxError as exc:
        return f"{nodeid}: {path} does not parse: {exc}"
    if not any(isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name for n in ast.walk(tree)):
        return f"{nodeid}: {path} defines no test named '{name}' (renamed or deleted; update or delete the entry)"
    return None


# --- shell helpers (scripts/ci/pytest-rerun.sh) ------------------------------------------------------------------------------

def _read(path: str) -> tuple[list[dict[str, str]], set[str]]:
    """(failed [{nodeid, summary}], passed node ids) of a pass file; tolerates a missing or unreadable file and the old list format."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], set()
    failed, passed = (data.get("failed", []), data.get("passed", [])) if isinstance(data, dict) else (data, [])
    return ([d for d in failed if isinstance(d, dict) and isinstance(d.get("nodeid"), str)],
            {p for p in passed if isinstance(p, str)})


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


TRUSTED_EVENTS = ("push", "cron")
DEFAULT_STATE_DIR = Path("/home/wohlgemuth/woodpecker/ci/state/herdr")
HISTORY_NAME = "pytest-retried-history.jsonl"


def resolve_history() -> tuple[str, str]:
    """(history file, note): '' when the counter is off. Only push/cron pipelines run reviewed code, so a pull_request, manual or local run
    never reads or writes the shared history. The file is HERDR_PYTEST_RETRY_HISTORY, else HERDR_CI_STATE/<name> (both explicit, parent dirs
    may be created), else the default CI state dir ONLY when it already exists: a dev machine never gets one created."""
    event = os.environ.get("CI_PIPELINE_EVENT") or os.environ.get("CI_EVENT") or ""
    if event not in TRUSTED_EVENTS:
        return "", "off (untrusted event)"
    explicit = os.environ.get("HERDR_PYTEST_RETRY_HISTORY")
    if explicit:
        return explicit, ""
    state = os.environ.get("HERDR_CI_STATE")
    if state:
        return str(Path(state) / HISTORY_NAME), ""
    if DEFAULT_STATE_DIR.is_dir():
        return str(DEFAULT_STATE_DIR / HISTORY_NAME), ""
    return "", "off (no CI state dir on this machine)"


def _locked_update(history: Path, update: Any) -> Any:
    """Run ``update()`` holding an exclusive flock on HISTORY.lock; without flock (no fcntl, unsupported fs) it runs unlocked."""
    try:
        import fcntl
        history.parent.mkdir(parents=True, exist_ok=True)
        handle = (history.parent / (history.name + ".lock")).open("a")
    except (ImportError, OSError):
        return update()
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError:
            return update()
        return update()


def chronic(history: str, retried: list[str], stamp: dt.datetime, run: str) -> tuple[list[tuple[str, int]], str]:
    """Record this run's retried tests in the JSON-lines HISTORY (read-modify-write under flock) and return [(nodeid, distinct runs inside
    the window)] for those at or above the threshold, plus a note ('' when fine). An unusable history never fails the gate."""
    if not history or not retried:
        return [], ""
    days, limit = _env_int("HERDR_PYTEST_CHRONIC_DAYS", 14), _env_int("HERDR_PYTEST_CHRONIC_RUNS", 3)
    cutoff = stamp - dt.timedelta(days=days)
    path = Path(history)

    def update() -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
            try:
                row = json.loads(line)
                if isinstance(row, dict) and dt.datetime.fromisoformat(row["at"]) >= cutoff and row["run"] != run:
                    rows.append(row)
            except (ValueError, KeyError, TypeError):
                continue
        rows += [{"nodeid": n, "at": stamp.isoformat(timespec="seconds"), "run": run} for n in retried]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(path)
        return rows

    try:
        rows = _locked_update(path, update)
    except OSError as exc:
        return [], f"retry history {history} is not usable ({exc}); the chronic-flake counter is OFF for this run"
    counts = {n: len({r["run"] for r in rows if r["nodeid"] == n}) for n in retried}
    return [(n, c) for n, c in counts.items() if c >= limit], ""


def settle(first: str, second: str, out: str, history: str | None = None) -> int:
    """Print the retry verdict, write the passed-on-retry tests to ``out``; exit 1 for anything that is not a clean pass on retry."""
    before, _ = _read(first)
    again, passed = _read(second)
    failed_again = {d["nodeid"]: d for d in again}
    first_ids = {d["nodeid"] for d in before}
    retried = [d for d in before if d["nodeid"] in passed and d["nodeid"] not in failed_again]
    real = [d for d in before if d["nodeid"] in failed_again]
    unresolved = [d for d in before if d["nodeid"] not in passed and d["nodeid"] not in failed_again]
    new_failures = [d for n, d in failed_again.items() if n not in first_ids]
    now = dt.datetime.now(dt.UTC)
    stamp = now.isoformat(timespec="seconds")
    context = {k: os.environ[v] for k, v in (("pipeline", "CI_PIPELINE_NUMBER"), ("commit", "CI_COMMIT_SHA")) if os.environ.get(v)}
    run = context.get("pipeline") or context.get("commit") or f"{os.getpid()}-{time.time_ns()}"
    if history is None:
        history, note = resolve_history()
        note = f"RETRY HISTORY: {note}" if note and retried else ""
    else:
        note = ""
    chronic_ids, note2 = chronic(history, [d["nodeid"] for d in retried], now, run)
    note = note or note2
    runs_by_id = dict(chronic_ids)
    lines = [json.dumps({"nodeid": d["nodeid"], "first_failure": d.get("summary", ""), "at": stamp, **context,
                         **({"chronic_runs": runs_by_id[d["nodeid"]]} if d["nodeid"] in runs_by_id else {})}) for d in retried]
    Path(out).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    for d in retried:
        print(f"RETRIED: {d['nodeid']} failed in the first pass and PASSED on retry (flaky, tracked in {out}); first failure: {d.get('summary', '')}")
    for line in lines:  # the log is the archive until the pipeline publishes the file: every record is in it verbatim
        print(f"RETRIED-JSONL {line}")
    for d in real:
        print(f"FAILED TWICE (real failure): {d['nodeid']}; first failure: {d.get('summary', '')}")
    for d in unresolved:
        print(f"NOT PASSED ON RETRY: {d['nodeid']} did not record a pass on retry (skipped, deselected or never ran); first failure: {d.get('summary', '')}")
    for d in new_failures:
        print(f"FAILED ON RETRY: {d['nodeid']} passed in the fast pass but failed when its file reran; first-pass failures in that file may be pollution")
    if note:
        print(note if note.startswith("RETRY HISTORY") else f"RETRY HISTORY: {note}")
    chronic_fails = os.environ.get("HERDR_PYTEST_CHRONIC_FAIL") == "1"
    for nodeid, runs in chronic_ids:
        print(f"CHRONIC FLAKE: {nodeid} needed a retry in {runs} runs within {_env_int('HERDR_PYTEST_CHRONIC_DAYS', 14)} days; "
              "fix it or quarantine it with an issue (@pytest.mark.quarantine_flaky)"
              + ("; the gate fails until then" if chronic_fails else " (warning only; HERDR_PYTEST_CHRONIC_FAIL=1 makes it fail the gate)"))
    print(f"== retry verdict: {len(retried)} passed on retry (RETRIED), {len(real)} failed again, {len(unresolved)} not passed, "
          f"{len(new_failures)} new failures, {len(chronic_ids)} chronic ==")
    return 1 if real or unresolved or new_failures or (chronic_ids and chronic_fails) else 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "ids":
        sys.stdout.write("".join(d["nodeid"] + "\0" for d in _read(argv[2])[0]))
        return 0
    if len(argv) == 3 and argv[1] == "files":
        # an xdist pseudo id ('gw1', a crashed worker) names no test file: it is dropped, never handed to pytest as an argument
        paths = dict.fromkeys(d["nodeid"].split("::")[0] for d in _read(argv[2])[0] if "::" in d["nodeid"] or Path(d["nodeid"]).is_file())
        sys.stdout.write("".join(f + "\0" for f in paths))
        return 0
    if len(argv) in (5, 6) and argv[1] == "settle":
        return settle(*argv[2:])
    print("usage: herdr_flaky_plugin.py ids FILE | files FILE | settle FIRST SECOND OUT [HISTORY]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
