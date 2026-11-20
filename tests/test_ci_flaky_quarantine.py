"""The unit gate's retry of failed tests and its flaky quarantine (scripts/ci/pytest-rerun.sh, scripts/ci/herdr_flaky_plugin.py).

After a failing fast pass the failed tests, and only those, run once more. A test that passes then is logged as RETRIED with its first
failure and listed in a machine-readable file; a test that fails twice is a real failure and the pass still fails. Quarantined tests
still run and report but cannot fail the gate, and the structural test at the bottom fails for any entry without a reason.
Everything runs real pytest (xdist included) in real temporary projects; nothing is mocked.
"""

from __future__ import annotations

import ast
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


def sh(script: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{CI / "pytest-parallel.sh"}"; . "{CI / "pytest-rerun.sh"}"; {script}'],
        capture_output=True, text=True, cwd=cwd, timeout=300,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", **env},
    )


def make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
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
    assert "retrying ONLY those once" in out, out
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


# --- the quarantine -----------------------------------------------------------------------------------------------------------

def pytest_run(proj: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "herdr_flaky_plugin", *args], capture_output=True, text=True,
                          cwd=proj, timeout=300,
                          env={**os.environ, "PYTHONPATH": f"{CI}{os.pathsep}{os.environ.get('PYTHONPATH', '')}", **env})


REASON = "waits on the wall clock, fails about one run in twenty on a loaded host"


def marked(reason: str = REASON, issue: str = "#873", expires: str = "2099-01-01") -> str:
    kw = ", ".join(f"{k}={v!r}" for k, v in (("reason", reason), ("issue", issue), ("expires", expires)) if v is not None)
    return f"import pytest\n@pytest.mark.flaky({kw})\ndef test_wobbly():\n    assert False, 'wobbled'\n"


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


ENTRY = 'nodeid = "test_w.py::test_wobbly"\nreason = "{r}"\nissue = "#873"\nexpires = "2099-01-01"\n'


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
    for entry in entries:
        path = str(entry.get("nodeid", "")).split("::")[0]
        if not (ROOT / path).is_file():
            problems.append(f"{entry.get('nodeid')}: no such test file (stale entry; delete it)")
    assert not problems, "flaky quarantine entries are incomplete:\n" + "\n".join(problems)


def _flaky_marker_keywords() -> list[tuple[str, dict[str, object]]]:
    found = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        if path == Path(__file__):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            target = node.func if isinstance(node, ast.Call) else node
            if isinstance(target, ast.Attribute) and target.attr == "flaky" and ast.unparse(target).endswith("mark.flaky"):
                kws = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg and isinstance(k.value, ast.Constant)} \
                    if isinstance(node, ast.Call) else {}
                found.append((f"{path.relative_to(ROOT)}:{node.lineno}", kws))
    return found


def test_every_flaky_marker_in_the_suite_names_a_reason_an_issue_and_an_expiry() -> None:
    bad = [f"{where}: {p}" for where, kws in _flaky_marker_keywords() for p in plugin.field_problems(kws)]
    assert not bad, "pytest.mark.flaky without its required keywords:\n" + "\n".join(bad)


def test_the_structural_check_really_catches_an_entry_without_a_reason(tmp_path: Path) -> None:
    lst = write_list(tmp_path, '[[quarantine]]\nnodeid = "tests/test_x.py::test_y"\nissue = "#1"\nexpires = "2099-01-01"\n')
    assert any("missing required 'reason'" in p for p in plugin.entry_problems(plugin.load_entries(lst)))
    lst = write_list(tmp_path, '[[quarantine]]\nnodeid = "tests/test_x.py::test_y"\nreason = "ok"\nissue = "#1"\nexpires = "soon"\n')
    problems = plugin.entry_problems(plugin.load_entries(lst))
    assert any("at least" in p for p in problems) and any("ISO date" in p for p in problems)


def test_the_marker_is_registered_so_strict_markers_accept_it(tmp_path: Path) -> None:
    proj = make_project(tmp_path, {"test_w.py": marked()})
    assert pytest_run(proj, "--strict-markers").returncode == 0


def test_the_repo_conftest_registers_the_marker_for_a_plain_pytest_run() -> None:
    done = subprocess.run([sys.executable, "-m", "pytest", "--markers", "-p", "no:cacheprovider"], capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert "@pytest.mark.flaky(reason, issue, expires)" in done.stdout, done.stdout + done.stderr
