"""Sharded unit gate: scripts/ci/unit-shard.py, the shard wiring in pytest-parallel.sh / pytest-rerun.sh / unit-gates.sh, the
.woodpecker/unit-gates-shard-N.yml workflows, the aggregate `unit-gates` workflow and scripts/ci/main-moved-check.sh.

Real pytest runs in throwaway projects and real git repositories; nothing is mocked.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SHARD = ROOT / "scripts" / "ci" / "unit-shard.py"
PARALLEL = ROOT / "scripts" / "ci" / "pytest-parallel.sh"
RERUN = ROOT / "scripts" / "ci" / "pytest-rerun.sh"
GATE = (ROOT / "scripts" / "ci" / "unit-gates.sh").read_text()
RECHECK = ROOT / "scripts" / "ci" / "main-moved-check.sh"
WOODPECKER = ROOT / ".woodpecker"
SHARDS = 3  # keep in step with the number of .woodpecker/unit-gates-shard-N.yml files

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


def shard_cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SHARD), *args], cwd=cwd, capture_output=True, text=True)


def listed(cwd: Path, *args: str) -> list[str]:
    done = shard_cli(cwd, *args)
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


# ------------------------------------------------------------------ the partition over the real suite --------------------------------

def test_the_shards_partition_every_test_file_of_the_real_suite() -> None:
    every = sorted(str(p.relative_to(ROOT)) for pat in ("test_*.py", "*_test.py") for p in (ROOT / "tests").rglob(pat))
    assert len(every) > 200, "the discovery found no suite"
    seen: list[str] = []
    for i in range(1, SHARDS + 1):
        files = listed(ROOT, "files", f"{i}/{SHARDS}")
        assert files, f"shard {i} is empty"
        seen += files
    assert sorted(seen) == sorted(set(every)), "a test file is in no shard or in two shards"
    assert len(seen) == len(set(seen))


def test_the_check_command_proves_the_partition_and_a_slow_file_exists() -> None:
    done = shard_cli(ROOT, "check", str(SHARDS))
    assert done.returncode == 0, done.stderr
    assert "partition ok" in done.stdout


def test_the_shards_are_reasonably_balanced() -> None:
    sizes = [len(listed(ROOT, "files", f"{i}/{SHARDS}")) for i in range(1, SHARDS + 1)]
    assert max(sizes) <= 1.5 * min(sizes), sizes


def test_the_slow_file_runs_in_exactly_one_shard() -> None:
    owners = [i for i in range(1, SHARDS + 1) if listed(ROOT, "slow-files", f"{i}/{SHARDS}")]
    assert len(owners) == 1
    assert "tests/control_plane/test_postgres_recovery.py" in listed(ROOT, "slow-files", f"{owners[0]}/{SHARDS}")


def test_the_assignment_is_deterministic_and_independent_of_the_working_order(tmp_path: Path) -> None:
    assert listed(ROOT, "files", f"2/{SHARDS}") == listed(ROOT, "files", f"2/{SHARDS}")
    proj = tmp_path / "p"
    (proj / "tests" / "sub").mkdir(parents=True)
    for name in ("a", "b", "c", "d", "e"):
        (proj / "tests" / f"test_{name}.py").write_text("def test_x():\n    pass\n")
    (proj / "tests" / "sub" / "test_f.py").write_text("def test_x():\n    pass\n")
    (proj / "tests" / "helper.py").write_text("")
    first = [listed(proj, "files", f"{i}/2") for i in (1, 2)]
    assert sorted(first[0] + first[1]) == sorted(str(p.relative_to(proj)) for p in (proj / "tests").rglob("test_*.py"))
    assert not set(first[0]) & set(first[1])


@pytest.mark.parametrize("bad", ["0/3", "4/3", "x/3", "1/0", "1", "1/3/5", ""])
def test_a_bad_shard_spec_is_an_error_not_an_empty_shard(bad: str) -> None:
    done = shard_cli(ROOT, "files", bad)
    assert done.returncode == 2 and "shard" in done.stderr


def test_a_shard_with_a_missing_tests_directory_fails_loudly(tmp_path: Path) -> None:
    assert shard_cli(tmp_path, "files", "1/2").returncode == 2


# ------------------------------------------------------------------ real pytest runs under the shard wiring --------------------------

# enough files that every one of the three shards owns fast tests (a shard with none would exit 5, nothing collected)
FAST_NAMES = ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j")


def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow\n")
    for name in FAST_NAMES:
        (proj / "tests" / f"test_{name}.py").write_text(f"def test_{name}():\n    open(__file__ + '.ran', 'w').write('x')\n")
    (proj / "tests" / "test_slowpoke.py").write_text("import pytest\n\n@pytest.mark.slow\ndef test_slowpoke():\n    open(__file__ + '.ran', 'w').write('x')\n")
    return proj


def gate(proj: Path, shard: str | None, **env: str) -> subprocess.CompletedProcess[str]:
    exports = f"export HERDR_UNIT_SHARD={shard}; " if shard else ""
    script = (f'set -uo pipefail; {exports}. "{PARALLEL}"; . "{RERUN}"; herdr_pytest_parallel_load; herdr_pytest_shard_load || exit 9; '
              f'herdr_pytest_run_gate env env "{{FAST}}" "{{SLOW}}"; echo "gate-status=$?"').replace("{FAST}", "not slow").replace("{SLOW}", "slow")
    return subprocess.run(["bash", "-c", script], cwd=proj, capture_output=True, text=True, timeout=300,
                          env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
                               "HERDR_PYTEST_WORKERS": "2", "HERDR_PYTEST_RETRY": "0", "HERDR_PYTEST_RETRIED_FILE": "", **env})


def ran(proj: Path) -> set[str]:
    """Names of the tests that really executed (each writes a marker next to its file), then clears the markers."""
    marks = sorted((proj / "tests").glob("*.ran"))
    names = {m.name.removeprefix("test_").removesuffix(".py.ran") for m in marks}
    for m in marks:
        m.unlink()
    return names


def test_every_test_runs_in_exactly_one_shard_and_the_tail_only_where_the_slow_file_lives(tmp_path: Path) -> None:
    proj = project(tmp_path)
    per_shard = []
    for i in (1, 2, 3):
        done = gate(proj, f"{i}/3", HERDR_PYTEST_SLOW_CONCURRENT="0")
        assert "gate-status=0" in done.stdout, done.stdout + done.stderr
        per_shard.append(ran(proj))
    assert set().union(*per_shard) == {*FAST_NAMES, "slowpoke"}
    assert sum(len(s) for s in per_shard) == len(FAST_NAMES) + 1, per_shard


def test_a_shard_without_slow_tests_does_not_fail_on_the_empty_tail(tmp_path: Path) -> None:
    proj = project(tmp_path)
    for i in (1, 2, 3):
        slow_here = bool(listed(proj, "slow-files", f"{i}/3"))
        done = gate(proj, f"{i}/3")
        assert "gate-status=0" in done.stdout, done.stdout + done.stderr
        assert ("slowpoke" in ran(proj)) == slow_here


def test_without_a_shard_the_gate_still_runs_everything(tmp_path: Path) -> None:
    proj = project(tmp_path)
    done = gate(proj, None)
    assert "gate-status=0" in done.stdout, done.stdout + done.stderr
    assert ran(proj) == {*FAST_NAMES, "slowpoke"}


def test_a_failing_test_in_a_shard_fails_that_shard(tmp_path: Path) -> None:
    proj = project(tmp_path)
    owner = next(i for i in (1, 2, 3) if "tests/test_a.py" in listed(proj, "files", f"{i}/3"))
    (proj / "tests" / "test_a.py").write_text("def test_a():\n    assert False\n")
    done = gate(proj, f"{owner}/3")
    assert "gate-status=0" not in done.stdout


def test_a_bad_shard_variable_fails_the_gate_loudly(tmp_path: Path) -> None:
    done = gate(project(tmp_path), "7/3")
    assert done.returncode == 9


def test_the_gate_script_loads_the_shard_and_runs_the_client_tests_in_shard_one_only() -> None:
    assert "herdr_pytest_shard_load" in GATE
    assert GATE.index("herdr_pytest_parallel_load") < GATE.index("herdr_pytest_shard_load") < GATE.index("herdr_pytest_run_gate")
    assert re.search(r"HERDR_UNIT_SHARD", GATE) and "client tests" in GATE


# ------------------------------------------------------------------ the Woodpecker workflows ----------------------------------------

def doc(name: str) -> dict:
    return yaml.safe_load((WOODPECKER / name).read_text())


@pytest.mark.parametrize("i", range(1, SHARDS + 1))
def test_each_shard_workflow_has_the_full_pr_pipeline_for_its_slice(i: int) -> None:
    d = doc(f"unit-gates-shard-{i}.yml")
    assert d["labels"] == {"cluster": "hive-heavy"}
    assert {"event": "pull_request"} in d["when"] and {"event": "manual"} in d["when"]
    assert d["concurrency"]["group"] == "${CI_WORKFLOW_NAME}-${CI_COMMIT_SOURCE_BRANCH}"
    names = [s["name"] for s in d["steps"]]
    assert names[:5] == ["checkout", "freshness", "integrate-main", "applicability", "unit-gates"]
    assert names[-1] == "main-moved-check"
    gate_step = next(s for s in d["steps"] if s["name"] == "unit-gates")
    assert gate_step["environment"]["HERDR_UNIT_SHARD"] == f"{i}/{SHARDS}"
    assert "bash scripts/ci/unit-gates.sh" in "\n".join(gate_step["commands"])
    assert ".ci-unit-gates-not-applicable" in "\n".join(gate_step["commands"])


def test_there_are_exactly_as_many_shard_workflows_as_the_constant_says() -> None:
    assert sorted(p.name for p in WOODPECKER.glob("unit-gates-shard-*.yml")) == [f"unit-gates-shard-{i}.yml" for i in range(1, SHARDS + 1)]


def test_the_aggregate_keeps_the_one_required_context_name_and_waits_for_every_shard() -> None:
    d = doc("unit-gates.yml")
    assert (WOODPECKER / "unit-gates.yml").exists(), "the required status context ci/woodpecker/pr/unit-gates is named after this file"
    assert d["depends_on"] == [f"unit-gates-shard-{i}" for i in range(1, SHARDS + 1)]
    assert {"event": "pull_request"} in d["when"]
    assert not [t for t in d["when"] if "path" in t or "include" in t], "a path filter would make the required context never report"
    assert "runs_on" not in d, "default: skipped (not green) when any shard failed"
    assert len(d["steps"]) == 1


# ------------------------------------------------------------------ the mid-run freshness re-check -----------------------------------

def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout.strip()


def commit(work: Path, path: str, text: str) -> str:
    (work / path).parent.mkdir(parents=True, exist_ok=True)
    (work / path).write_text(text)
    git(work, "add", "-A")
    git(work, "commit", "-qm", f"edit {path}")
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def pr(tmp_path: Path) -> dict[str, Path]:
    """A bare origin, a seed clone that moves main, and `ws`: a CI workspace holding a PR head that integrate-main already merged."""
    bare, seed, ws = tmp_path / "o.git", tmp_path / "seed", tmp_path / "ws"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    commit(seed, "herdr_engineering/a.py", "a = 1\n")
    commit(seed, "docs/r.md", "r\n")
    git(seed, "push", "-q", "origin", "main")
    git(seed, "checkout", "-q", "-b", "feat")
    head = commit(seed, "herdr_engineering/feat.py", "f = 1\n")
    git(seed, "push", "-q", "origin", "feat")
    git(seed, "checkout", "-q", "main")
    ws.mkdir()
    git(ws, "init", "-q", "-b", "feat")
    git(ws, "remote", "add", "origin", str(bare))
    git(ws, "fetch", "-q", "--no-tags", "origin", head)
    git(ws, "checkout", "-q", head)
    git(ws, "fetch", "-q", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    return {"seed": seed, "ws": ws, "head": Path(head)}


def recheck(ws: Path, head: Path, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("CI_", "HERDR_"))}
    return subprocess.run(["bash", str(RECHECK)], cwd=ws, capture_output=True, text=True,
                          env={**base, **GIT_ENV, "CI_PIPELINE_EVENT": "pull_request", "CI_COMMIT_SHA": str(head), **env})


def test_recheck_is_a_cheap_no_op_when_main_did_not_move(pr) -> None:
    done = recheck(pr["ws"], pr["head"])
    assert done.returncode == 0 and "main unchanged" in done.stdout


def test_recheck_is_skipped_outside_a_pull_request(pr) -> None:
    done = recheck(pr["ws"], pr["head"], CI_PIPELINE_EVENT="push")
    assert done.returncode == 0 and "not a pull request" in done.stdout


def test_recheck_passes_when_main_moved_only_in_paths_the_suite_does_not_cover(pr) -> None:
    commit(pr["seed"], "docs/new.md", "x\n")
    git(pr["seed"], "push", "-q", "origin", "main")
    done = recheck(pr["ws"], pr["head"])
    assert done.returncode == 0 and "no covered path" in done.stdout


def test_recheck_warns_loudly_when_covered_paths_moved_but_the_merge_is_clean(pr) -> None:
    commit(pr["seed"], "herdr_engineering/other.py", "o = 1\n")
    git(pr["seed"], "push", "-q", "origin", "main")
    done = recheck(pr["ws"], pr["head"])
    assert done.returncode == 0
    assert "WARNING" in done.stdout and "unit-gates-main" in done.stdout


def test_recheck_fails_on_covered_movement_when_the_policy_says_fail(pr) -> None:
    commit(pr["seed"], "herdr_engineering/other.py", "o = 1\n")
    git(pr["seed"], "push", "-q", "origin", "main")
    done = recheck(pr["ws"], pr["head"], HERDR_MAIN_MOVED_POLICY="fail")
    assert done.returncode == 1 and "main moved" in done.stderr


def test_recheck_always_fails_when_main_moved_into_a_conflict(pr) -> None:
    commit(pr["seed"], "herdr_engineering/feat.py", "f = 2\n")
    git(pr["seed"], "push", "-q", "origin", "main")
    done = recheck(pr["ws"], pr["head"])
    assert done.returncode == 1
    assert "no longer merges cleanly" in done.stderr and "herdr_engineering/feat.py" in done.stderr


def test_recheck_fails_when_origin_main_cannot_be_fetched(pr) -> None:
    git(pr["ws"], "remote", "set-url", "origin", "/nonexistent/origin.git")
    done = recheck(pr["ws"], pr["head"])
    assert done.returncode == 1 and "could not fetch" in done.stderr


def test_recheck_rejects_an_unknown_policy(pr) -> None:
    done = recheck(pr["ws"], pr["head"], HERDR_MAIN_MOVED_POLICY="maybe")
    assert done.returncode == 2
