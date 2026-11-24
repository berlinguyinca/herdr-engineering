"""The unit gate's retry of failed tests and its flaky quarantine (scripts/ci/pytest-rerun.sh, scripts/ci/herdr_flaky_plugin.py).

After a failing fast pass the failed tests, and only those, run once more. A test that passes then is logged as RETRIED with its first
failure and listed in a machine-readable file; a test that fails twice is a real failure and the pass still fails. Quarantined tests
still run and report but cannot fail the gate, and the structural test at the bottom fails for any entry without a reason.
Everything runs real pytest (xdist included) in real temporary projects; nothing is mocked.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci"
PLUGIN = CI / "herdr_flaky_plugin.py"
LIST = ROOT / "tests" / "flaky-quarantine.toml"
sys.path.insert(0, str(CI))
import herdr_flaky_plugin as plugin  # noqa: E402  (the module under test, imported by path)

SOON = (dt.date.today() + dt.timedelta(days=30)).isoformat()      # inside the 90-day quarantine horizon


def sh(script: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    env = {"HERDR_CI_STATE": str(cwd / "ci-state"), "CI_PIPELINE_EVENT": "push", **env}          # never touch the real CI state dir from a test
    return subprocess.run(
        ["bash", "-c", f'. "{CI / "pytest-parallel.sh"}"; . "{CI / "pytest-rerun.sh"}"; {script}'],
        capture_output=True, text=True, cwd=cwd, timeout=300,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", **env},
    )


def make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    (proj / "pytest.ini").write_text("[pytest]\n")
    for name, body in files.items():
        (proj / name).write_text(body)
    return proj


def run_fast(proj: Path, workers: str = "2", **env: str) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    parallel = f"PYTEST_PARALLEL=(-n {workers} --dist=loadfile)" if workers else "PYTEST_PARALLEL=()"
    done = sh(f'plain() {{ "$@"; }}\n{parallel}\nherdr_pytest_run_fast plain "not e2e"', proj, **env)
    retried_file = proj / "pytest-retried.jsonl"
    rows = [json.loads(x) for x in retried_file.read_text().splitlines()] if retried_file.exists() else []
    return done, rows


# a test that fails the first time it runs and passes the second: a real, persisted first-run marker
FLAKY_ONCE = '''
import os
MARK = os.path.join(os.path.dirname(__file__), "failed-once")
def test_flaky():
    if not os.path.exists(MARK):
        open(MARK, "w").close()
        assert False, "timing blip: took 1.09s, wanted < 1.0"
'''
COUNTED = '''
def test_untouched_neighbour():
    with open(__file__ + ".runs", "a") as f:
        f.write("x")
'''


def test_a_flaky_test_is_retried_alone_reported_loudly_and_listed_for_tracking(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE, "test_ok.py": COUNTED})
    done, rows = run_fast(proj, CI_PIPELINE_NUMBER="1776")
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "RETRIED: test_flaky.py::test_flaky" in out and "timing blip: took 1.09s" in out, out   # original summary is in the log
    assert "retrying their 1 test file(s) once" in out, out
    assert (proj / "test_ok.py.runs").read_text() == "x", "the passing neighbour must not be rerun"
    assert [r["nodeid"] for r in rows] == ["test_flaky.py::test_flaky"]
    assert "timing blip" in rows[0]["first_failure"] and rows[0]["pipeline"] == "1776" and rows[0]["at"]


def test_a_deterministic_failure_is_not_masked_by_the_retry(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_bad.py": "def test_always():\n    assert 1 == 2\n", "test_ok.py": COUNTED})
    done, rows = run_fast(proj)
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert "FAILED TWICE (real failure): test_bad.py::test_always" in out, out
    assert rows == []                                              # nothing passed on retry, nothing to track
    assert (proj / "test_ok.py.runs").read_text() == "x"


def test_a_real_failure_next_to_a_flake_still_fails_but_the_flake_is_still_tracked(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE, "test_bad.py": "def test_always():\n    assert 1 == 2\n"})
    done, rows = run_fast(proj)
    assert done.returncode == 1, done.stdout
    assert [r["nodeid"] for r in rows] == ["test_flaky.py::test_flaky"]
    assert "FAILED TWICE (real failure): test_bad.py::test_always" in done.stdout


def test_a_failure_in_a_parametrised_test_with_odd_characters_is_retried_by_exact_id(tmp_path: Path) -> None:
    body = FLAKY_ONCE.replace("def test_flaky():", '@__import__("pytest").mark.parametrize("v", ["a b", "x]y", "é"])\ndef test_flaky(v):')
    body = body.replace('MARK = os.path.join(os.path.dirname(__file__), "failed-once")', 'MARK = os.path.join(os.path.dirname(__file__), "failed-once")\nimport sys')
    done, rows = run_fast(make_project(tmp_path, {"test_p.py": body}), workers="")
    assert done.returncode == 0 and len(rows) == 1, done.stdout + done.stderr
    assert rows[0]["nodeid"].startswith("test_p.py::test_flaky[")


def test_a_passing_pass_writes_an_empty_tracking_file_and_retries_nothing(tmp_path: Path) -> None:
    done, rows = run_fast(make_project(tmp_path, {"test_ok.py": COUNTED}))
    assert done.returncode == 0 and rows == [] and (tmp_path / "proj" / "pytest-retried.jsonl").exists()
    assert "retrying" not in done.stdout


def test_more_failures_than_the_cap_are_a_broken_change_and_are_not_retried(tmp_path: Path) -> None:
    body = "".join(f"def test_{i}():\n    assert False\n" for i in range(4))
    done, rows = run_fast(make_project(tmp_path, {"test_many.py": body}), HERDR_PYTEST_RETRY_MAX="3")
    assert done.returncode == 1 and "not flakes" in done.stdout and rows == []


def test_the_retry_can_be_switched_off(tmp_path: Path) -> None:
    done, rows = run_fast(make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE}), HERDR_PYTEST_RETRY="0")
    assert done.returncode == 1 and rows == [] and "RETRIED" not in done.stdout


def test_a_failure_in_the_retry_that_is_a_hang_or_error_never_comes_out_as_a_pass(tmp_path: Path) -> None:
    # fails once normally, then the retry errors at collection time (the file is gone): the status must stay non-zero
    body = ("import os\nMARK = __file__ + '.m'\n"
            "def test_flaky():\n    if not os.path.exists(MARK):\n        open(MARK, 'w').close()\n        os.remove(__file__)\n        assert False\n")
    done, _ = run_fast(make_project(tmp_path, {"test_gone.py": body}), workers="")
    assert done.returncode != 0, done.stdout + done.stderr


def test_the_retry_reruns_the_whole_file_so_same_module_pollution_still_fails(tmp_path: Path) -> None:
    # test_a leaves module state behind; test_b asserts it is absent. Run alone, test_b passes: a node-id-only retry would call it a flake.
    body = ("STATE = []\n"
            "def test_a():\n    STATE.append('leak')\n"
            "def test_b():\n    assert not STATE, 'state leaked from test_a'\n")
    proj = make_project(tmp_path, {"test_pollution.py": body})
    alone = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_pollution.py::test_b"], cwd=proj, capture_output=True, text=True)
    assert alone.returncode == 0, alone.stdout                  # the premise: in isolation it passes
    done, rows = run_fast(proj, workers="")
    assert done.returncode != 0 and "FAILED TWICE (real failure): test_pollution.py::test_b" in done.stdout, done.stdout
    assert rows == []


def test_a_retried_test_that_is_skipped_on_retry_is_not_called_passed(tmp_path: Path) -> None:
    body = ("import os, pytest\nMARK = __file__ + '.m'\n"
            "def test_flaky():\n    if not os.path.exists(MARK):\n        open(MARK, 'w').close()\n        assert False, 'blip'\n"
            "    pytest.skip('now skipped')\n")
    done, rows = run_fast(make_project(tmp_path, {"test_skip.py": body}), workers="")
    out = done.stdout + done.stderr
    assert done.returncode != 0 and "NOT PASSED ON RETRY: test_skip.py::test_flaky" in out and "PASSED on retry" not in out, out
    assert rows == []


def test_a_test_that_passed_first_and_fails_when_its_file_reruns_fails_the_gate(tmp_path: Path) -> None:
    body = ("import os\nMARK = __file__ + '.m'\n"
            "def test_late():\n    assert not os.path.exists(MARK), 'only fails on the rerun'\n"
            "def test_flaky():\n    if not os.path.exists(MARK):\n        open(MARK, 'w').close()\n        assert False\n")
    done, _ = run_fast(make_project(tmp_path, {"test_new.py": body}), workers="")
    assert done.returncode != 0 and "FAILED ON RETRY: test_new.py::test_late" in done.stdout + done.stderr, done.stdout


def test_the_archive_is_in_the_log_as_jsonl_lines(tmp_path: Path) -> None:
    done, rows = run_fast(make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE}), workers="")
    lines = [x for x in done.stdout.splitlines() if x.startswith("RETRIED-JSONL ")]
    assert len(lines) == 1 and json.loads(lines[0].split(" ", 1)[1]) == rows[0]


def _three_runs(proj: Path, **env: str) -> list[tuple[int, bool]]:
    outcomes = []
    for pipeline in ("101", "102", "103"):
        (proj / "failed-once").unlink(missing_ok=True)
        done, rows = run_fast(proj, workers="", CI_PIPELINE_NUMBER=pipeline, **env)
        outcomes.append((done.returncode, "CHRONIC FLAKE: test_flaky.py::test_flaky" in done.stdout))
        if pipeline == "103":
            assert ("chronic_runs" in rows[0]) is outcomes[-1][1] and (rows[0].get("chronic_runs", 3) == 3)
    return outcomes


def test_the_same_test_retried_in_three_runs_is_a_chronic_flake_warning_that_does_not_fail_the_gate(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE})
    assert _three_runs(proj) == [(0, False), (0, False), (0, True)]
    history = [json.loads(x) for x in (proj / "ci-state" / "pytest-retried-history.jsonl").read_text().splitlines()]
    assert [h["run"] for h in history] == ["101", "102", "103"]


def test_the_chronic_flake_fails_the_gate_only_when_asked(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE})
    assert _three_runs(proj, HERDR_PYTEST_CHRONIC_FAIL="1") == [(0, False), (0, False), (1, True)]


def test_a_rerun_of_the_same_pipeline_counts_once_and_old_history_ages_out(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE})
    state = proj / "ci-state"
    state.mkdir()
    old = {"nodeid": "test_flaky.py::test_flaky", "at": "2020-01-01T00:00:00+00:00", "run": "1"}
    (state / "pytest-retried-history.jsonl").write_text(json.dumps(old) + "\n" + json.dumps({**old, "run": "2"}) + "\n")
    done, _ = run_fast(proj, workers="", CI_PIPELINE_NUMBER="9")
    assert done.returncode == 0, done.stdout           # two ancient runs do not count, so this is the first recent one
    assert len((state / "pytest-retried-history.jsonl").read_text().splitlines()) == 1


def test_an_untrusted_event_neither_reads_nor_writes_the_history(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE})
    for event in ("pull_request", "manual", ""):
        done, rows = run_fast(proj, workers="", CI_PIPELINE_EVENT=event)
        assert done.returncode == 0 and len(rows) == 1 and "RETRY HISTORY: off (untrusted event)" in done.stdout, (event, done.stdout)
        assert not (proj / "ci-state").exists(), event
        (proj / "failed-once").unlink()


def test_a_trusted_event_writes_the_history(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE})
    for event in ("push", "cron"):
        (proj / "failed-once").unlink(missing_ok=True)
        run_fast(proj, workers="", CI_PIPELINE_EVENT=event, CI_PIPELINE_NUMBER=event)
    assert len((proj / "ci-state" / "pytest-retried-history.jsonl").read_text().splitlines()) == 2


def test_concurrent_writers_lose_no_rows(tmp_path: Path) -> None:
    history = tmp_path / "h.jsonl"
    code = ("import sys, datetime as dt; sys.path.insert(0, sys.argv[1]); import herdr_flaky_plugin as p\n"
            "for i in range(40): p.chronic(sys.argv[2], [f'a::t{sys.argv[3]}_{i}'], dt.datetime.now(dt.UTC), f'{sys.argv[3]}-{i}')\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(CI), str(history), str(n)]) for n in range(4)]
    assert [p.wait(120) for p in procs] == [0, 0, 0, 0]
    assert len(history.read_text().splitlines()) == 160


def test_the_default_state_dir_is_used_only_when_it_exists_and_never_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default = tmp_path / "woodpecker" / "state"
    monkeypatch.setattr(plugin, "DEFAULT_STATE_DIR", default)
    for var in ("HERDR_CI_STATE", "HERDR_PYTEST_RETRY_HISTORY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CI_PIPELINE_EVENT", "push")
    assert plugin.resolve_history() == ("", "off (no CI state dir on this machine)")
    assert not default.exists() and not default.parent.exists()
    default.mkdir(parents=True)
    assert plugin.resolve_history()[0] == str(default / plugin.HISTORY_NAME)


def test_an_xdist_pseudo_id_is_never_a_retry_argument(tmp_path: Path) -> None:
    (tmp_path / "test_real.py").write_text("")
    first = tmp_path / "first.json"
    first.write_text(json.dumps({"failed": [{"nodeid": "gw1", "summary": "crashed"}, {"nodeid": "test_real.py::test_x", "summary": "s"},
                                            {"nodeid": "test_real.py", "summary": "collect"}], "passed": []}))
    done = subprocess.run([sys.executable, str(PLUGIN), "files", str(first)], capture_output=True, cwd=tmp_path, timeout=60)
    assert done.stdout == b"test_real.py\0"
    first.write_text(json.dumps({"failed": [{"nodeid": "gw1", "summary": "crashed"}], "passed": []}))
    done = subprocess.run([sys.executable, str(PLUGIN), "files", str(first)], capture_output=True, cwd=tmp_path, timeout=60)
    assert done.stdout == b""                                                 # nothing left: the shell then fails safe


def test_runs_without_ci_variables_get_a_unique_dedupe_key(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE})
    for _ in range(2):
        (proj / "failed-once").unlink(missing_ok=True)
        run_fast(proj, workers="", CI_PIPELINE_NUMBER="", CI_COMMIT_SHA="")
    runs = [json.loads(x)["run"] for x in (proj / "ci-state" / "pytest-retried-history.jsonl").read_text().splitlines()]
    assert len(set(runs)) == 2, runs


def test_an_unwritable_history_location_turns_the_counter_off_with_a_note_but_does_not_fail(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE})
    blocker = proj / "not-a-dir"
    blocker.write_text("")
    done, rows = run_fast(proj, workers="", HERDR_PYTEST_RETRY_HISTORY=str(blocker / "h.jsonl"))
    assert done.returncode == 0 and len(rows) == 1 and "chronic-flake counter is OFF" in done.stdout, done.stdout


def test_a_session_level_failure_is_not_retried(tmp_path: Path) -> None:
    # a failing test AND a session-level failure (the coverage floor): rerunning the file would come out green and hide the floor
    proj = make_project(tmp_path, {"test_flaky.py": FLAKY_ONCE})
    (proj / "pytest.ini").write_text("[pytest]\n")
    shim = proj / "conftest.py"
    shim.write_text("def pytest_terminal_summary(terminalreporter):\n    terminalreporter.write_line('FAIL Required test coverage of 80% not reached. Total coverage: 12.00%')\n")
    done, rows = run_fast(proj, workers="")
    assert done.returncode == 1 and rows == [] and "failed at session level" in done.stdout, done.stdout


def test_a_non_numeric_retry_cap_is_rejected_like_the_rerun_workers(tmp_path: Path) -> None:
    done, _ = run_fast(make_project(tmp_path, {"test_ok.py": COUNTED}), workers="", HERDR_PYTEST_RETRY_MAX="lots")
    assert done.returncode == 1 and "HERDR_PYTEST_RETRY_MAX must be a number" in done.stderr


# --- the quarantine -----------------------------------------------------------------------------------------------------------

def pytest_run(proj: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "herdr_flaky_plugin", *args], capture_output=True, text=True,
                          cwd=proj, timeout=300,
                          env={**os.environ, "PYTHONPATH": f"{CI}{os.pathsep}{os.environ.get('PYTHONPATH', '')}", **env})


REASON = "waits on the wall clock, fails about one run in twenty on a loaded host"


def marked(reason: str = REASON, issue: str = "#873", expires: str = SOON) -> str:
    kw = ", ".join(f"{k}={v!r}" for k, v in (("reason", reason), ("issue", issue), ("expires", expires)) if v is not None)
    return f"import pytest\n@pytest.mark.quarantine_flaky({kw})\ndef test_wobbly():\n    assert False, 'wobbled'\n"


def test_a_quarantined_test_still_runs_and_is_reported_but_does_not_fail_the_run(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_w.py": marked(), "test_ok.py": "def test_ok():\n    pass\n"})
    done = pytest_run(proj)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "QUARANTINED failed: test_w.py::test_wobbly" in done.stdout and "#873" in done.stdout and REASON in done.stdout, done.stdout


def test_a_quarantined_test_that_passes_is_reported_as_passed(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_w.py": marked().replace("assert False, 'wobbled'", "pass")})
    done = pytest_run(proj)
    assert done.returncode == 0 and "QUARANTINED passed: test_w.py::test_wobbly" in done.stdout, done.stdout


@pytest.mark.parametrize("missing", ["reason", "issue", "expires"])
def test_a_marker_without_reason_issue_or_expiry_does_not_quarantine(tmp_path: Path, missing: str) -> None:
    proj = make_project(tmp_path, {"test_w.py": marked(**{missing: None})})
    done = pytest_run(proj)
    assert done.returncode == 1, done.stdout                      # the test keeps failing the run
    assert "quarantine not applied" in done.stdout and f"missing required '{missing}'" in done.stdout


def test_an_expired_quarantine_fails_the_run_again(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_w.py": marked(expires="2026-01-01")})
    done = pytest_run(proj, HERDR_FLAKY_TODAY="2026-01-02")
    assert done.returncode == 1 and "expired 2026-01-01" in done.stdout, done.stdout
    assert pytest_run(proj, HERDR_FLAKY_TODAY="2026-01-01").returncode == 0     # the expiry day itself is still covered


def write_list(proj: Path, body: str) -> Path:
    path = proj / "quarantine.toml"
    path.write_text(body)
    return path


ENTRY = 'nodeid = "test_w.py::test_wobbly"\nreason = "{r}"\nissue = "#873"\nexpires = "'+SOON+'"\n'


def test_the_list_quarantines_by_node_id_including_every_parametrisation(tmp_path: Path) -> None:
    body = "import pytest\n@pytest.mark.parametrize('v', [1, 2])\ndef test_wobbly(v):\n    assert False\n"
    proj = make_project(tmp_path, {"test_w.py": body, "test_other.py": "def test_other():\n    assert False\n"})
    lst = write_list(proj, "[[quarantine]]\n" + ENTRY.format(r=REASON))
    done = pytest_run(proj, HERDR_FLAKY_QUARANTINE=str(lst))
    assert "QUARANTINED failed: test_w.py::test_wobbly[1]" in done.stdout and "QUARANTINED failed: test_w.py::test_wobbly[2]" in done.stdout
    assert "1 failed" in done.stdout and done.returncode == 1, done.stdout        # test_other is not covered: it still fails the run


def test_a_list_entry_without_a_reason_does_not_quarantine(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_w.py": "def test_wobbly():\n    assert False\n"})
    lst = write_list(proj, "[[quarantine]]\n" + ENTRY.format(r=" "))
    done = pytest_run(proj, HERDR_FLAKY_QUARANTINE=str(lst))
    assert done.returncode == 1 and "missing required 'reason'" in done.stdout, done.stdout


def test_a_quarantined_failure_is_not_retried_and_not_counted_as_a_failure_by_the_gate(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_w.py": marked()})
    done, rows = run_fast(proj)
    assert done.returncode == 0 and rows == [] and "QUARANTINED failed" in done.stdout, done.stdout


# --- the structural gate: no quarantine without a reason ---------------------------------------------------------------------

def test_every_entry_of_the_real_quarantine_list_has_a_reason_an_issue_an_expiry_and_a_real_test() -> None:
    entries = plugin.load_entries(LIST)
    problems = plugin.entry_problems(entries)
    problems += [p for e in entries if isinstance(e.get("nodeid"), str) and (p := plugin.missing_test(ROOT, e["nodeid"]))]
    assert not problems, "flaky quarantine entries are incomplete:\n" + "\n".join(problems)


def test_an_entry_naming_a_test_that_does_not_exist_is_caught(tmp_path: Path) -> None:
    (tmp_path / "test_real.py").write_text("class TestK:\n    def test_method(self):\n        pass\n\ndef test_here():\n    pass\n")
    assert plugin.missing_test(tmp_path, "test_real.py::test_here") is None
    assert plugin.missing_test(tmp_path, "test_real.py::test_here[a b]") is None            # a parametrised id still names the function
    assert plugin.missing_test(tmp_path, "test_real.py::TestK::test_method") is None
    assert "no test named 'test_renamed'" in (plugin.missing_test(tmp_path, "test_real.py::test_renamed") or "")
    assert "no such test file" in (plugin.missing_test(tmp_path, "test_gone.py::test_here") or "")


def _flaky_marker_keywords() -> list[tuple[str, dict[str, object]]]:
    found = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        if path == Path(__file__):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            target = node.func if isinstance(node, ast.Call) else node
            if isinstance(target, ast.Attribute) and target.attr == "quarantine_flaky" and ast.unparse(target).endswith("mark.quarantine_flaky"):
                kws = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg and isinstance(k.value, ast.Constant)} \
                    if isinstance(node, ast.Call) else {}
                found.append((f"{path.relative_to(ROOT)}:{node.lineno}", kws))
    return found


def _hiding_declarations(paths: list[Path] | None = None) -> list[str]:
    """A quarantine at module or class level: ``pytestmark = ...quarantine_flaky...`` or a class decorator, both of which hide whole files."""
    found = []
    for path in paths if paths is not None else sorted((ROOT / "tests").rglob("*.py")):
        if path == Path(__file__):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            targets = [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)] or (
                [node.target.id] if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) else [])
            if "pytestmark" in targets and "quarantine_flaky" in ast.unparse(node):
                found.append(f"{path.name}:{node.lineno}: pytestmark")
            if isinstance(node, ast.ClassDef) and any("quarantine_flaky" in ast.unparse(d) for d in node.decorator_list):
                found.append(f"{path.name}:{node.lineno}: class decorator")
    return found


def test_every_quarantine_marker_in_the_suite_names_a_reason_an_issue_and_an_expiry() -> None:
    bad = [f"{where}: {p}" for where, kws in _flaky_marker_keywords() for p in plugin.field_problems(kws)]
    assert not bad, "pytest.mark.quarantine_flaky without its required keywords:\n" + "\n".join(bad)


def test_no_quarantine_marker_in_the_suite_sits_on_a_module_or_a_class() -> None:
    assert not _hiding_declarations(), "quarantine goes on test functions only:\n" + "\n".join(_hiding_declarations())


def test_the_structural_scan_really_sees_module_and_class_level_markers(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("import pytest\npytestmark = pytest.mark.quarantine_flaky(reason='x')\n")
    (tmp_path / "k.py").write_text("import pytest\n@pytest.mark.quarantine_flaky(reason='x')\nclass TestA:\n    pass\n")
    (tmp_path / "f.py").write_text("import pytest\n@pytest.mark.quarantine_flaky(reason='x')\ndef test_a():\n    pass\n")
    found = _hiding_declarations([tmp_path / "m.py", tmp_path / "k.py", tmp_path / "f.py"])
    assert sorted(found) == ["k.py:3: class decorator", "m.py:2: pytestmark"], found


def test_a_module_or_class_level_marker_never_quarantines_and_says_why(tmp_path: Path) -> None:
    kw = f"reason={REASON!r}, issue='#873', expires={SOON!r}"
    module = f"import pytest\npytestmark = pytest.mark.quarantine_flaky({kw})\ndef test_a():\n    assert False\n"
    klass = f"import pytest\n@pytest.mark.quarantine_flaky({kw})\nclass TestA:\n    def test_a(self):\n        assert False\n"
    for name, body in (("module", module), ("class", klass)):
        done = pytest_run(make_project(tmp_path / name, {"test_w.py": body}))
        assert done.returncode == 1 and "module/class-level quarantine_flaky" in done.stdout, (name, done.stdout)


def test_a_marker_on_a_method_function_still_quarantines(tmp_path: Path) -> None:
    kw = f"reason={REASON!r}, issue='#873', expires={SOON!r}"
    body = f"import pytest\nclass TestA:\n    @pytest.mark.quarantine_flaky({kw})\n    def test_a(self):\n        assert False\n"
    assert pytest_run(make_project(tmp_path, {"test_w.py": body})).returncode == 0


def test_an_expiry_beyond_the_horizon_does_not_quarantine_and_the_structural_check_flags_it(tmp_path: Path) -> None:
    far = (dt.date.today() + dt.timedelta(days=plugin.MAX_HORIZON_DAYS + 30)).isoformat()
    done = pytest_run(make_project(tmp_path, {"test_w.py": marked(expires=far)}))
    assert done.returncode == 1 and "more than 90 days away" in done.stdout, done.stdout
    edge = (dt.date.today() + dt.timedelta(days=plugin.MAX_HORIZON_DAYS)).isoformat()
    assert pytest_run(make_project(tmp_path / "edge", {"test_w.py": marked(expires=edge)})).returncode == 0
    assert any("90 days" in p for p in plugin.field_problems({"reason": REASON, "issue": "#1", "expires": "2099-01-01"}))


def test_the_structural_check_really_catches_an_entry_without_a_reason(tmp_path: Path) -> None:
    lst = write_list(tmp_path, '[[quarantine]]\nnodeid = "tests/test_x.py::test_y"\nissue = "#1"\nexpires = "'+SOON+'"\n')
    assert any("missing required 'reason'" in p for p in plugin.entry_problems(plugin.load_entries(lst)))
    lst = write_list(tmp_path, '[[quarantine]]\nnodeid = "tests/test_x.py::test_y"\nreason = "ok"\nissue = "#1"\nexpires = "soon"\n')
    problems = plugin.entry_problems(plugin.load_entries(lst))
    assert any("at least" in p for p in problems) and any("ISO date" in p for p in problems)


def test_the_marker_is_registered_so_strict_markers_accept_it(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_w.py": marked()})
    assert pytest_run(proj, "--strict-markers").returncode == 0


def test_the_repo_conftest_registers_the_marker_for_a_plain_pytest_run() -> None:
    done = subprocess.run([sys.executable, "-m", "pytest", "--markers", "-p", "no:cacheprovider"], capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert "@pytest.mark.quarantine_flaky(reason, issue, expires)" in done.stdout, done.stdout + done.stderr
