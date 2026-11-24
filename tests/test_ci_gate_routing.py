"""Tests for CI gate routing: when the unit gates run, when they may be skipped, and what proves `main`.

Both halves exist because of issue #719. `main` ran no tests at all - unit-gates triggered on pull_request only, and with a
`when: {path: ...}` filter the pipeline is never even created, so the context could never be required and a PR whose unit-gates
failed merged anyway. The path decision now lives in scripts/ci/unit-gates-applicable.sh (which always reports), and
scripts/ci/main-cron-gates.sh proves main on a schedule without paying a suite per quiet interval.

These drive the real shell scripts against real throwaway git repositories, because the thing being guaranteed is a routing
decision made from git state - a Python-level mock would test the mock.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
APPLICABLE = ROOT / "scripts" / "ci" / "unit-gates-applicable.sh"
CRON = ROOT / "scripts" / "ci" / "main-cron-gates.sh"
UI_GATES_YML = (ROOT / ".woodpecker" / "ui-gates.yml").read_text()
UNIT_GATES_YML = (ROOT / ".woodpecker" / "unit-gates-shard-1.yml").read_text()   # the aggregate unit-gates.yml has no steps of its own
UNIT_GATES_AGGREGATE_YML = (ROOT / ".woodpecker" / "unit-gates.yml").read_text()
CRON_YML = (ROOT / ".woodpecker" / "main-cron.yml").read_text()

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    out = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a `main` branch and one commit touching every interesting area."""
    r = tmp_path / "repo"
    (r / "herdr_engineering").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "docs").mkdir()
    (r / "scripts" / "ci").mkdir(parents=True)
    (r / "herdr_engineering" / "cli.py").write_text("x = 1\n")
    (r / "tests" / "test_cli.py").write_text("def test_x():\n    assert True\n")
    (r / "docs" / "readme.md").write_text("hello\n")
    (r / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    return r


def change(repo: Path, path: str, branch: str = "feature") -> str:
    """Commit a one-line change to `path` on `branch`, and return that branch's head sha."""
    if branch != git(repo, "rev-parse", "--abbrev-ref", "HEAD"):
        git(repo, "checkout", "-q", "-b", branch)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"touch {path}")
    return git(repo, "rev-parse", "HEAD")


def applicability(repo: Path, sha: str, event: str = "pull_request", base: str = "main",
                  gate: str | None = None, event_var: str = "CI_PIPELINE_EVENT") -> subprocess.CompletedProcess[str]:
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("CI_")}  # a runner's CI_* must not leak in
    env = {**base_env, event_var: event, "CI_COMMIT_SHA": sha, "CI_TARGET_BRANCH": "main",
           "HERDR_APPLICABILITY_BASE": base}
    args = ["bash", str(APPLICABLE), *([gate] if gate else [])]
    return subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True)


# ------------------------------------------------------------------ applicability: what must run ------------------------------------

@pytest.mark.parametrize("path", [
    "herdr_engineering/new_module.py",       # product code
    "herdr_engineering/deep/nested/thing.py",
    "tests/test_new.py",                     # a test nobody has run yet
    "packages/herdr-web-client/app.js",      # client
    "scripts/ci/unit-gates.sh",              # the gate itself
    "scripts/ci/anything-new.sh",            # a new helper must not slip through unproven
    "deploy/control-plane/deploy.sh",        # the deploy scripts the suite exercises
    "pyproject.toml",                        # dependency and pytest config
    ".woodpecker/unit-gates.yml",            # the routing config
    ".woodpecker/unit-gates-shard-2.yml",    # a shard workflow
    "scripts/ci/unit-shard.py",              # the shard split
    "scripts/ci/main-moved-check.sh",
    ".woodpecker/unit-gates-main.yml",
    ".woodpecker/structural-gates.yml",
    ".woodpecker/artifact-handoff.yml",
    "scripts/ci-pipeline-freshness.sh",      # not under scripts/ci/
    "scripts/ci/integrate-main.sh",
    # Bug-hunt: these used to skip the required check because the unit gate was an allow-list. Default is now RUN.
    "herdr_artifact_host/server.py",
    "ansible/site.yml",
    "skills/herdr/SKILL.md",                 # a skill's markdown is not a doc: tests read it
    "scripts/install.sh",
    "package.json",
    "package-lock.json",
    "schemas/new.json",
    "a_brand_new_top_level_dir/anything.py", # drift guard: an unknown path must run the suite
])
def test_a_change_that_can_affect_the_suite_runs_it(repo, path):
    result = applicability(repo, change(repo, path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "running the suite" in result.stdout


# ------------------------------------------------------------------ applicability: what may be skipped --------------------------------

@pytest.mark.parametrize("path", [
    "docs/operations/ci-gates.md",
    "CHANGELOG.md",
    "README.md",
    "docs/herdr_engineering_notes.md",   # contains a covered prefix; must not match on substring
])
def test_a_change_that_cannot_affect_the_suite_is_skipped(repo, path):
    result = applicability(repo, change(repo, path))
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "skipping the suite" in result.stdout


@pytest.mark.parametrize("path", [".gitignore", ".github/CODEOWNERS", "LICENSE", "docs/adr/x.png"])
def test_the_explicit_exempt_list_is_skipped(repo, path):
    assert applicability(repo, change(repo, path)).returncode == 3


def test_every_tracked_path_outside_the_exempt_list_runs_the_suite():
    """Drift guard: ask the real matcher about every tracked file; only docs and the known-inert files may be skipped."""
    script = (
        f'set -- unit; src="$(sed -n "/^gate=/,/^label=/p" {APPLICABLE})"; eval "$src"; '
        f'eval "$(sed -n "/^herdr_covered_path()/,/^}}/p" {APPLICABLE})"; path="$1"; herdr_covered_path'
    )
    skipped = [p for p in _tracked() if subprocess.run(["bash", "-c", script, "x", p], capture_output=True).returncode != 0]
    unexpected = [p for p in skipped if not (p.startswith(("docs/", ".github/", ".autospec/")) or "/" not in p)]
    assert not unexpected, f"paths that skip the required unit gate: {unexpected[:10]}"
    assert any(p.startswith("herdr_artifact_host/") for p in _tracked()), "vacuous: no tracked files found"


def test_a_branch_identical_to_its_base_has_nothing_to_prove(repo):
    git(repo, "checkout", "-q", "-b", "no-op")
    result = applicability(repo, git(repo, "rev-parse", "HEAD"))
    assert result.returncode == 3
    assert "identical to the merge base" in result.stdout


# ------------------------------------------------------------------ applicability: never silently green -------------------------

@pytest.mark.parametrize("event", ["push", "cron", "tag", "deployment"])
def test_events_that_are_already_a_request_to_prove_something_always_run(repo, event):
    # A docs-only push to main still has to be proven: that is the whole point of #719.
    result = applicability(repo, change(repo, "docs/whatever.md"), event=event)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_base_that_cannot_be_resolved_runs_the_suite_instead_of_guessing(repo):
    result = applicability(repo, git(repo, "rev-parse", "HEAD"), base="origin/no-such-branch")
    assert result.returncode == 0, "a guard that cannot work out the diff must not hand back a green"
    assert "fail open" in result.stderr


def test_the_decision_uses_the_merge_base_not_the_branch_tip(repo):
    """main moved after the branch was cut; that must not make an unrelated branch look applicable."""
    change(repo, "docs/only-docs.md", branch="feature")
    tip = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    change(repo, "herdr_engineering/from_main.py", branch="main")   # main moved, on a covered path
    git(repo, "checkout", "-q", "feature")
    result = applicability(repo, tip)
    assert result.returncode == 3, "the base's own changes are not this PR's responsibility"


# ------------------------------------------------------------------ the cron proof ------------------------------------------

@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal checkout whose scripts/ci/unit-gates.sh is a stub, so the cron logic can be tested without the suite."""
    root = tmp_path / "tree"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(CRON, root / "scripts" / "ci" / "main-cron-gates.sh")
    stub = root / "scripts" / "ci" / "unit-gates.sh"
    stub.write_text('#!/bin/bash\necho ran >> "$RUN_LOG"\nexit "${FAKE_GATE_EXIT:-0}"\n')
    stub.chmod(0o755)
    (root / "README.md").write_text("x\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def run_cron(tree: Path, state: Path, ran: Path, gate_exit: str = "0") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HERDR_CI_STATE": str(state), "RUN_LOG": str(ran), "FAKE_GATE_EXIT": gate_exit}
    return subprocess.run(["bash", str(tree / "scripts" / "ci" / "main-cron-gates.sh")],
                          cwd=tree, env=env, capture_output=True, text=True)


def test_an_unproven_commit_on_main_is_proven(tree, tmp_path):
    state, ran = tmp_path / "state", tmp_path / "ran"
    result = run_cron(tree, state, ran)
    assert result.returncode == 0, result.stderr
    assert ran.exists(), "the gate must actually run for a commit with no proof"
    assert "proven green" in result.stdout


def test_a_commit_already_proven_green_is_not_re_run(tree, tmp_path):
    state, ran = tmp_path / "state", tmp_path / "ran"
    assert run_cron(tree, state, ran).returncode == 0
    before = ran.read_text()
    result = run_cron(tree, state, ran)
    assert result.returncode == 0, result.stderr
    assert ran.read_text() == before, "a quiet schedule must not pay for the suite again"
    assert "already proven green" in result.stdout


def test_a_new_commit_on_main_is_proven_even_when_an_older_one_was(tree, tmp_path):
    state, ran = tmp_path / "state", tmp_path / "ran"
    run_cron(tree, state, ran)
    (tree / "README.md").write_text("new commit\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "second")
    result = run_cron(tree, state, ran)
    assert result.returncode == 0, result.stderr
    assert ran.read_text().count("ran") == 2


def test_a_red_gate_earns_no_memo(tree, tmp_path):
    """Otherwise one failure buys a free pass for every later interval at the same commit."""
    state, ran = tmp_path / "state", tmp_path / "ran"
    result = run_cron(tree, state, ran, gate_exit="1")
    assert result.returncode != 0
    assert not (state / "main-unit-gates-green.txt").exists()


def test_a_red_gate_leaves_the_previous_memo_alone(tree, tmp_path):
    """A failure at a new commit must not discard the proof of the commit before it."""
    state, ran = tmp_path / "state", tmp_path / "ran"
    run_cron(tree, state, ran)
    proven = (state / "main-unit-gates-green.txt").read_text()
    (tree / "README.md").write_text("new commit\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "second")
    result = run_cron(tree, state, ran, gate_exit="1")
    assert result.returncode != 0
    assert (state / "main-unit-gates-green.txt").read_text() == proven


def test_a_commit_already_proven_green_short_circuits_before_the_gate(tree, tmp_path):
    """The memo is the whole economy of the cron, so it has to be consulted before any install work."""
    state, ran = tmp_path / "state", tmp_path / "ran"
    run_cron(tree, state, ran)
    assert run_cron(tree, state, ran, gate_exit="1").returncode == 0, "a proven commit must not even reach the gate"


def test_a_state_dir_that_cannot_be_written_still_runs_the_gate(tree, tmp_path):
    """Fail-open: no memo means no idea what is proven, and 'unknown' must never mean 'green'."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        result = run_cron(tree, blocked / "state", tmp_path / "ran")
    finally:
        blocked.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ran").exists(), "an unreadable memo must not skip the suite"
    assert "not writable" in result.stderr


# ------------------------------------------------------------------ the wiring that makes it count ------------------------------

def test_unit_gates_is_no_longer_path_filtered():
    """The trap this whole change exists to spring: a filtered pipeline never reports, so it can never be required."""
    for text in (UNIT_GATES_YML, UNIT_GATES_AGGREGATE_YML):
        triggers = yaml.safe_load(text)["when"]
        assert [t for t in triggers if t.get("event") == "pull_request"], "unit-gates must still trigger on PRs"
        offenders = [t for t in triggers if "path" in t or "include" in t]
        assert not offenders, f"a path filter here re-creates the never-reports problem: {offenders}"


def test_the_skip_is_reported_rather_than_silently_absent():
    assert "unit-gates-applicable.sh" in UNIT_GATES_YML
    assert ".ci-unit-gates-not-applicable" in UNIT_GATES_YML
    assert "nothing to run" in UNIT_GATES_YML, "a skipped run must say why, in the log"


def test_the_gate_script_is_still_reproducible_from_a_clean_checkout():
    assert "bash scripts/ci/unit-gates.sh" in UNIT_GATES_YML


def test_the_cron_pipeline_triggers_on_cron_only():
    events = {t.get("event") for t in yaml.safe_load(CRON_YML)["when"]}
    assert events == {"cron"}, f"a cron pipeline must not double-run PRs or pushes: {events}"
    assert "main-cron-gates.sh" in CRON_YML


def test_the_cron_pipeline_runs_on_the_heavy_pool_and_never_overlaps_itself():
    cron, unit = yaml.safe_load(CRON_YML), yaml.safe_load(UNIT_GATES_YML)
    assert cron["labels"] == unit["labels"] == {"cluster": "hive-heavy"}, "cron runs landed on 4-CPU nodes without the label"
    assert cron["concurrency"]["limit"] == 1 and cron["concurrency"]["group"]


def test_both_scripts_are_valid_bash():
    for script in (APPLICABLE, CRON):
        out = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert out.returncode == 0, f"{script.name}: {out.stderr}"


def test_cron_without_a_state_dir_fails_loudly_instead_of_guessing(tree, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("HERDR_CI_STATE", "XDG_STATE_HOME")}
    env.update(RUN_LOG=str(tmp_path / "ran"), HOME=str(tmp_path / "home"))
    result = subprocess.run(["bash", str(tree / "scripts" / "ci" / "main-cron-gates.sh")],
                            cwd=tree, env=env, capture_output=True, text=True)
    assert result.returncode == 2 and "HERDR_CI_STATE is not set" in result.stderr
    assert not (tmp_path / "ran").exists() and not (tmp_path / "home").exists()


def test_the_cron_pipeline_takes_its_state_dir_from_the_runner_with_runtime_escaping():
    commands = yaml.safe_load(CRON_YML)["steps"][1]["commands"]
    export = next(c for c in commands if "HERDR_CI_STATE" in c)
    assert "$${WPCI_CI_DIR" in export, "Woodpecker substitutes single-$ vars at parse time; the runtime var needs $$"
    assert "/home/" not in CRON_YML


# ------------------------------------------------------------------ ui-gates: the same decision, a different path list -----------

@pytest.mark.parametrize("path", [
    "packages/herdr-web-client/app.js",
    "tests/playwright/ui-gates-lib.mjs",
    "tests/visual/baseline.png",
    "docs/assets/screenshots/home.png",
    "scripts/ci/ui-gates.sh",
    "scripts/ci-pipeline-freshness.sh",
    "package.json",
    "package-lock.json",
    "herdr_engineering/control_plane/server.py",
    ".woodpecker/ui-gates.yml",              # the routing config
    "scripts/ci/unit-gates-applicable.sh",   # the decision itself
])
def test_a_change_that_can_affect_the_ui_gates_runs_them(repo, path):
    result = applicability(repo, change(repo, path), gate="ui")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ui-gates applicability" in result.stdout and "running the" in result.stdout


@pytest.mark.parametrize("path", [
    "docs/operations/ci-gates.md",
    "CHANGELOG.md",
    "herdr_engineering/cli.py",                  # python outside control_plane: unit-gates' business, not the browser's
    "tests/test_cli.py",
    "scripts/ci/unit-gates.sh",
    "docs/packages/notes.md",                    # contains a covered prefix; must not match on substring
])
def test_a_change_that_cannot_affect_the_ui_gates_is_skipped(repo, path):
    result = applicability(repo, change(repo, path), gate="ui")
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "ui-gates applicability" in result.stdout and "skipping" in result.stdout


def test_the_two_gates_decide_independently(repo):
    sha = change(repo, "herdr_engineering/cli.py")
    assert applicability(repo, sha).returncode == 0
    assert applicability(repo, sha, gate="ui").returncode == 3


def test_ui_gates_manual_and_push_events_always_run(repo):
    sha = change(repo, "docs/whatever.md")
    for event in ("manual", "push"):
        assert applicability(repo, sha, event=event, gate="ui").returncode == 0


@pytest.mark.parametrize("gate", ["unit", "ui"])
@pytest.mark.parametrize("event", ["manual", "push"])
def test_woodpecker_event_variable_manual_and_push_always_run(repo, gate, event):
    """Woodpecker exports CI_PIPELINE_EVENT and no CI_EVENT; reading only CI_EVENT turned a manual run green without running."""
    sha = change(repo, "docs/whatever.md")
    result = applicability(repo, sha, event=event, gate=gate)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "always runs the suite" in result.stdout


@pytest.mark.parametrize("gate", ["unit", "ui"])
def test_woodpecker_event_variable_pull_request_can_still_skip(repo, gate):
    result = applicability(repo, change(repo, "docs/whatever.md"), event="pull_request", gate=gate)
    assert result.returncode == 3, (result.stdout, result.stderr)


def test_legacy_ci_event_variable_is_still_honoured(repo):
    sha = change(repo, "docs/whatever.md")
    assert applicability(repo, sha, event="manual", gate="ui", event_var="CI_EVENT").returncode == 0


@pytest.mark.parametrize("path", ["pyproject.toml", "scripts/ci/journal.sh", "scripts/ci/cache-trust.sh"])
def test_ui_gates_dependencies_route_to_run(repo, path):
    result = applicability(repo, change(repo, path), gate="ui")
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_ui_gates_unresolvable_base_fails_open(repo):
    result = applicability(repo, git(repo, "rev-parse", "HEAD"), base="origin/no-such-branch", gate="ui")
    assert result.returncode == 0 and "fail open" in result.stderr


def test_an_unknown_gate_is_an_error_not_a_skip(repo):
    result = applicability(repo, git(repo, "rev-parse", "HEAD"), gate="nope")
    assert result.returncode not in (0, 3)


def test_ui_gates_is_no_longer_path_filtered():
    """A filtered pipeline never reports, so ci/woodpecker/pr/ui-gates could never be required."""
    triggers = yaml.safe_load(UI_GATES_YML)["when"]
    assert [t for t in triggers if t.get("event") == "pull_request"]
    assert [t for t in triggers if t.get("event") == "manual"], "a manual run must still run the gate"
    offenders = [t for t in triggers if "path" in t or "include" in t]
    assert not offenders, f"a path filter here re-creates the never-reports problem: {offenders}"


def test_ui_gates_skip_is_reported_rather_than_silently_absent():
    steps = {s["name"]: s for s in yaml.safe_load(UI_GATES_YML)["steps"]}
    assert list(steps) == ["checkout", "freshness", "integrate-main", "applicability", "ui-gates"]
    applicability_cmds = steps["applicability"]["commands"]
    assert "rc=0; bash scripts/ci/unit-gates-applicable.sh ui || rc=$?" in applicability_cmds
    # the marker is touched only on rc 3; any other non-zero rc fails the step instead of skipping
    decision = next(c for c in applicability_cmds if c.startswith("if ["))
    assert 'if [ "$rc" = 3 ]; then touch .ci-ui-gates-not-applicable;' in decision
    assert 'elif [ "$rc" != 0 ]; then exit "$rc"; fi' in decision
    assert [c for c in applicability_cmds if "touch" in c] == [decision]
    gate_cmds = steps["ui-gates"]["commands"]
    guard = next(c for c in gate_cmds if c.startswith("if ["))
    assert gate_cmds.index(guard) < gate_cmds.index("bash scripts/ci/ui-gates.sh")
    assert "[ -f .ci-ui-gates-not-applicable ]" in guard
    assert "nothing to run" in guard and guard.rstrip().endswith("exit 0; fi")


# ------------------------------------------------------------------ ui-gates: the covered list cannot drift from what the gate uses ----

import re  # noqa: E402  (kept next to the tests that use it)


def _ui_covered(path: str) -> bool:
    """Ask the real script's own matcher, not a copy of its list."""
    script = (
        f'path="$1"; set -- ui; src="$(sed -n "/^gate=/,/^label=/p" {APPLICABLE})"; eval "$src"; path="$path"; '
        f'eval "$(sed -n "/^herdr_covered_path()/,/^}}/p" {APPLICABLE})"; herdr_covered_path'
    )
    return subprocess.run(["bash", "-c", script, "x", path], capture_output=True).returncode == 0


def _tracked(*patterns: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", "--", *patterns], cwd=ROOT, capture_output=True, text=True, check=True)
    return [p for p in out.stdout.splitlines() if p]


def test_ui_gate_covered_helper_agrees_with_the_script():
    assert _ui_covered("packages/app.js") and not _ui_covered("CHANGELOG.md")


def test_every_ui_source_tree_and_root_ui_config_is_covered():
    """New UI sources (web client, static member/share pages, browser-test configs) must trigger the gate."""
    must_cover = _tracked(
        "packages", "tests/playwright", "tests/visual", "docs/assets/screenshots",
        "herdr_artifact_host/member_static", "herdr_artifact_host/share_static", "schemas",
        "playwright*.config.*", "eslint*.config.*", "tsconfig*.json", "package.json", "package-lock.json",
    )
    assert must_cover, "the tracked-file query found nothing; the test would pass vacuously"
    missed = [p for p in must_cover if not _ui_covered(p)]
    assert not missed, f"UI paths the gate ignores (add them to the `ui` list in unit-gates-applicable.sh): {missed[:10]}"


def test_every_file_ui_gates_sh_runs_or_sources_is_covered():
    script = (ROOT / "scripts" / "ci" / "ui-gates.sh").read_text()
    sourced = [f"scripts/ci/{m}" for m in re.findall(r'\. "\$\(dirname "\$0"\)/([\w.-]+\.sh)"', script)]
    assert sourced, "expected ui-gates.sh to source helper scripts"
    pkg = (ROOT / "package.json").read_text()
    referenced = re.findall(r"(?:tests|packages)/[\w./*!()-]+", pkg.split('"test:ui-gates"')[1].split("\n")[0])
    referenced += re.findall(r"node (tests/[\w./-]+)", pkg)
    missed = [p for p in sourced + referenced + ["scripts/ci/ui-gates.sh"] if not _ui_covered(p.split("*")[0].split("!")[0] or p)]
    assert not missed, f"the gate runs these but a change to them would skip it: {missed}"


def test_ui_gates_workflow_files_are_covered():
    for path in (".woodpecker/ui-gates.yml", "scripts/ci/unit-gates-applicable.sh", "scripts/ci/ui-gates.sh"):
        assert _ui_covered(path), path
