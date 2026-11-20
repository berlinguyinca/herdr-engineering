"""pytest plugin for the unit gate: the flaky quarantine, and the failed-test list the one-shot retry reads.

Quarantine (never hides a test, only stops a *failing* one from failing the gate)
  A quarantined test STILL RUNS and is reported (``QUARANTINED`` section of the terminal summary). It is marked non-strict xfail, so
  a failure is an XFAIL and a pass is an XPASS; neither fails the gate. Two ways in, both need a reason, an issue and an expiry:

    @pytest.mark.flaky(reason="why it is flaky and what was measured", issue="#873", expires="2026-12-31")

    tests/flaky-quarantine.toml     [[quarantine]] nodeid = "tests/test_x.py::test_y"   (all parametrisations of test_y match too)
                                                   reason = "..."  issue = "#873"  expires = "2026-12-31"

  An entry past its ``expires`` date stops quarantining: the test fails the gate again until someone fixes it or consciously renews the
  date. A marker or entry missing a field never quarantines (the test keeps failing loudly) and tests/test_ci_flaky_quarantine.py fails.

Retry support
  With ``HERDR_PYTEST_FAILED_FILE`` set, the pytest controller writes the failed/errored tests as JSON ``[{"nodeid", "summary"}]``.
  ``python herdr_flaky_plugin.py ids FILE`` prints the node ids NUL-separated and ``... settle FIRST SECOND OUT`` compares the first pass
  with the retry pass: tests failing in both are real failures, tests that passed on retry are logged as ``RETRIED`` and written to OUT
  (JSON lines) so they can be tracked. scripts/ci/pytest-rerun.sh drives both.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

ENV_QUARANTINE = "HERDR_FLAKY_QUARANTINE"
ENV_FAILED_FILE = "HERDR_PYTEST_FAILED_FILE"
ENV_TODAY = "HERDR_FLAKY_TODAY"  # tests only: pretend it is this ISO date
REQUIRED_FIELDS = ("reason", "issue", "expires")
MIN_REASON_CHARS = 15
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
            dt.date.fromisoformat(expires.strip())
        except ValueError:
            problems.append("'expires' must be an ISO date (YYYY-MM-DD)")
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
            "flaky(reason, issue, expires): quarantine a known-flaky test. It still runs and is reported but a failure does not fail the "
            "gate; all three keywords are required (see scripts/ci/herdr_flaky_plugin.py). Expired or incomplete: not quarantined.",
        )
        try:
            self.entries = load_entries(quarantine_path(Path(str(config.rootpath))))
        except (OSError, ValueError) as exc:  # a malformed list must not hide tests: nothing is quarantined, the structural test fails
            self.skipped.append(f"quarantine list unreadable: {exc}")

    def _declaration(self, item: Any) -> dict[str, Any] | None:
        marker = item.get_closest_marker("flaky")
        if marker is not None:
            return {**marker.kwargs, "source": "marker"}
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
        seen: dict[str, str] = {}
        for kind in ("failed", "error"):
            for report in (reporter.stats.get(kind, []) if reporter else []):
                seen.setdefault(report.nodeid, summarise(report))
        Path(target).write_text(json.dumps([{"nodeid": n, "summary": s} for n, s in seen.items()]), encoding="utf-8")


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


# --- shell helpers (scripts/ci/pytest-rerun.sh) ------------------------------------------------------------------------------

def _load(path: str) -> list[dict[str, str]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and isinstance(d.get("nodeid"), str)]


def settle(first: str, second: str, out: str) -> int:
    """Print the retry verdict, write the passed-on-retry tests to ``out``; exit 1 when any test failed in both passes."""
    before, after = _load(first), {d["nodeid"] for d in _load(second)}
    retried = [d for d in before if d["nodeid"] not in after]
    real = [d for d in before if d["nodeid"] in after]
    stamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    context = {k: os.environ[v] for k, v in (("pipeline", "CI_PIPELINE_NUMBER"), ("commit", "CI_COMMIT_SHA")) if os.environ.get(v)}
    lines = [json.dumps({"nodeid": d["nodeid"], "first_failure": d.get("summary", ""), "at": stamp, **context}) for d in retried]
    Path(out).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    for d in retried:
        print(f"RETRIED: {d['nodeid']} failed in the first pass and PASSED on retry (flaky, tracked in {out}); first failure: {d.get('summary', '')}")
    for d in real:
        print(f"FAILED TWICE (real failure): {d['nodeid']}; first failure: {d.get('summary', '')}")
    print(f"== retry verdict: {len(retried)} passed on retry (RETRIED), {len(real)} failed again (real failures) ==")
    return 1 if real else 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "ids":
        sys.stdout.write("".join(d["nodeid"] + "\0" for d in _load(argv[2])))
        return 0
    if len(argv) == 5 and argv[1] == "settle":
        return settle(argv[2], argv[3], argv[4])
    print("usage: herdr_flaky_plugin.py ids FILE | settle FIRST SECOND OUT", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
