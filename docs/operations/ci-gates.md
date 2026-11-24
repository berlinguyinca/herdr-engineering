# CI gates

Four Woodpecker pipelines run on `ci.metabolomics.us` for this repository. All four are configured in `.woodpecker/`, all four
trigger on `manual` and `pull_request`, and the step logic lives in `scripts/ci/*.sh` so every gate is reproducible from a clean
checkout without touching CI.

| pipeline | config | script | trigger on `pull_request` | typical wall time |
|---|---|---|---|---|
| `structural-gates` | `.woodpecker/structural-gates.yml` | `scripts/ci/structural-gates.sh` | **always** | ~1.5 s |
| `artifact-handoff` | `.woodpecker/artifact-handoff.yml` | `scripts/ci/artifact-handoff-gates.sh` | **always** | ~1 min |
| `unit-gates` | `.woodpecker/unit-gates.yml` (aggregate; the work is in `unit-gates-shard-{1,2,3}.yml`) | `scripts/ci/unit-gates.sh` | **always** (`applicability` step skips in seconds when no covered path changed) | ~55 min, seconds when skipped |
| `ui-gates` | `.woodpecker/ui-gates.yml` | `scripts/ci/ui-gates.sh` | **always** (`applicability` step skips in seconds when no UI path changed) | ~15 min, seconds when skipped |

`structural-gates` is also the only pipeline that triggers on `event: push, branch: main`.

## Sharded unit gate

The pull-request unit gate is three workflows, `unit-gates-shard-1.yml` .. `unit-gates-shard-3.yml`, that run in parallel on three
`hive-heavy` agents. Each has the full pipeline (checkout, freshness, integrate-main, applicability, `unit-gates.sh` with
`HERDR_UNIT_SHARD=<i>/3`, `main-moved-check`). `scripts/ci/unit-shard.py` assigns every `tests/**/test_*.py` / `*_test.py` file to a
shard by `sha256(path)`, so the split needs no shared state and a new test file is picked up by itself; the slow-marked file and the
client (node) tests have one owner each (the slow tail follows its file, client tests run in shard 1). `tests/test_ci_unit_shards.py`
proves the shards partition the real suite (no test dropped, none twice) and that exactly one shard owns the slow tail.

`.woodpecker/unit-gates.yml` stays as the **aggregate**: it `depends_on` the three shards and has one step. Woodpecker names the
status context after the workflow file, so `ci/woodpecker/pr/unit-gates` (the required check) is unchanged. It runs after the shards
whatever their result (`runs_on: [success, failure]`) and reads their states from the Woodpecker API: success only when every shard
succeeded, red otherwise (and red if the API cannot be read). Without `runs_on` a red shard left the context `pending` forever. The shards also report their own `ci/woodpecker/pr/unit-gates-shard-N`
contexts (informational, not required). To change the shard count, add or remove a shard file, update `depends_on` in the aggregate and
the `/N` in each shard's `HERDR_UNIT_SHARD`, and `SHARDS` in the test. Coverage is not enforced in this gate (no `--cov` anywhere), so
there is nothing to combine. `unit-gates-main.yml` (push to main) stays a single unsharded run.

`main-moved-check` (last step of each shard) re-fetches `origin/main` after the tests: unchanged is one fetch; moved in uncovered paths
passes; moved in covered paths with a clean merge passes with a loud WARNING (the push-to-main run proves the combination; set
`HERDR_MAIN_MOVED_POLICY=fail` to fail instead); a merge that no longer conflicts-free fails naming the files.

## The path filter, and the trap it sets

A Woodpecker `when: {path: {include: [...]}}` filter means the pipeline is never **created** on a pull request that touches none of
those paths, so its `ci/woodpecker/pr/<name>` context never reports. GitHub waits for a required check that never reports, so a
path-filtered check made *required* parks every unrelated pull request on *Expected - Waiting for status to be reported*.

`unit-gates` (#719) and `ui-gates` no longer have a path filter. Both trigger on every `pull_request` (and `manual`) and an
`applicability` step runs `scripts/ci/unit-gates-applicable.sh [unit|ui]`, which diffs the PR against its merge base and exits
0 (run the gate), 3 (no covered path changed: drop `.ci-<gate>-not-applicable`; the gate step exits 0 in seconds saying why) or
fails the pipeline. It fails open: a diff it cannot work out runs the gate. The covered paths live in that one script (the `unit` and
`ui` cases); a manual run (read from `CI_PIPELINE_EVENT`, falling back to legacy `CI_EVENT`) always runs the gate. The `ui` list is locked by `tests/test_ci_gate_routing.py`: every tracked UI source tree (web client, member/share static pages, schemas, browser-test configs) and every script the gate runs must be matched by it, so a new UI directory fails that test until it is listed. **All four pipelines now report on every pull request, so all four are safe to
require.** Do not reintroduce a `path:` filter in a pipeline whose context is required.

## Why `main` is gated by `structural-gates` and not by `unit-gates`

Nothing used to run on a push to `main`: all four pipelines triggered on `pull_request` and `manual` only, so a commit that broke
`main` was never detected by `main`. It surfaced as a red pipeline on the next unrelated pull request that rebased onto it. See
issue #691.

Running `unit-gates` on every push to `main` is not viable: `main` takes roughly 85 commits a day (251 on the busiest recent day)
and `unit-gates` takes ~55 minutes, which is ~78 runner-hours a day and would starve pull-request CI. `structural-gates` answers
the questions whose failures actually reached `main` - every tracked YAML/JSON/TOML file parses, no tracked file carries an
unresolved conflict marker, and `memory_install.PINNED_REVISION` equals `lock/upstreams.yaml` - in about 1.3 seconds over ~2,900
tracked files. Roughly 85 runs a day therefore costs about two minutes of runner time.

It is not a cheaper `unit-gates` and it does not replace any part of it. `unit-gates.sh` runs whole suites on purpose, on the
grounds that a scoped gate hides the breakage a change causes elsewhere; that principle is unchanged.

## Reproducing a gate locally

```sh
bash scripts/ci/structural-gates.sh   # needs python3 with venv; installs pyyaml into a throwaway venv
bash scripts/ci/unit-gates.sh         # needs python3, node 22, git; builds its own venv
```

Both journal their output the same way CI does (`scripts/ci/journal.sh`), so a local run produces a readable log file even when the
terminal is closed. Read the server's copy of a finished run with:

```sh
python3 scripts/ci/wp_log.py berlinguyinca/herdr-engineering <pipeline-number> --failures
```

## Worker crashes in the parallel pytest pass

On a heavily loaded host pytest-xdist workers can die ("node down: Not properly terminated") and xdist 3.8.0 then ends with an
INTERNALERROR KeyError, pytest exit 3 (pipeline 1710, load 155 on 4 CPUs). `unit-gates.sh` handles this in two ways
(`scripts/ci/pytest-rerun.sh`, `scripts/ci/pytest-parallel.sh`):

- `auto` workers are bounded by the CPUs the 1-minute load average leaves free (never below 1; an explicit `HERDR_PYTEST_WORKERS`
  wins; `HERDR_LOADAVG_FILE` overrides `/proc/loadavg`).
- When the parallel pass exits 3 or its output has a worker-crash signature, it is rerun once on `HERDR_PYTEST_RERUN_WORKERS`
  workers (default 2; `0` = serial, ~90 min) and the log says so. A genuine test failure, a watchdog hang (124) and a pass are
  never rerun, and a rerun's result is final. A test failure whose own output happens to print a crash signature costs one more
  run, nothing more.
- When the fast pass fails with ordinary test failures (exit 1, no crash signature), the **test files of the failed and errored tests**
  are run once more, serially and whole (so a test polluted by an earlier test of its own module fails again instead of passing
  alone). A failed test that **passes** on the retry (a recorded pass: a skip or a test that did not run does not count) is logged as
  `RETRIED: <id> ... first failure: <original summary>`, repeated as `RETRIED-JSONL {...}` log lines (the log is the archive until the
  pipeline publishes the file) and written to `pytest-retried.jsonl` (`HERDR_PYTEST_RETRIED_FILE`; one JSON object per line: `nodeid`,
  `first_failure`, `at`, `pipeline`, `commit`; rewritten each run, empty when nothing was retried). Everything else fails the gate:
  `FAILED TWICE (real failure)`, `NOT PASSED ON RETRY`, `FAILED ON RETRY` (a test that passed in the fast pass failed when its file
  reran). More than `HERDR_PYTEST_RETRY_MAX` (25, must be a number) failures are not retried; `HERDR_PYTEST_RETRY=0` disables the
  retry. A crash rerun's result is final. A pass that failed at session level (coverage floor, `INTERNALERROR`) is not retried:
  rerunning files cannot fix it and a green retry must not hide it. Only the fast pass has a retry: a slow tail run on its own
  (once it is a separate step) gets none. **Limits**: only the files of failed tests are rerun, so pollution coming from a test in ANOTHER file can still make a retry pass green (the chronic counter then warns); session-level detection is a grep of the pass's log, so a conftest `pytest_sessionfinish` that sets `exitstatus` is not covered; `HERDR_PYTEST_RETRY_MAX` counts failed test ids, not files; pseudo ids such as an xdist `gw1` are never retried (nothing left means the failure stands). **Follow-up**: publish `pytest-retried.jsonl` as a pipeline artifact (a `.woodpecker/`
  change, outside this file set).
- **Chronic flakes.** A test that needed a retry in `HERDR_PYTEST_CHRONIC_RUNS` (3) distinct runs within `HERDR_PYTEST_CHRONIC_DAYS`
  (14) days prints a loud `CHRONIC FLAKE` warning and carries `chronic_runs` in its `RETRIED-JSONL` row (a retry must not hide a flake
  forever); it fails the gate only with `HERDR_PYTEST_CHRONIC_FAIL=1`, so it cannot fail an unrelated PR by default. The counter is on only for `push`/`cron` pipelines (others log `RETRY HISTORY: off (untrusted event)` and touch nothing). It is
  `pytest-retried-history.jsonl` in `HERDR_CI_STATE` or the file `HERDR_PYTEST_RETRY_HISTORY` (explicit paths may be created); the default
  `/home/wohlgemuth/woodpecker/ci/state/herdr` (the `main-cron-gates.sh` state dir) is used only if it already exists. Updates run under `flock`. If that directory cannot be written the counter is
  off for the run, with a `RETRY HISTORY:` note in the log, and the gate is not failed for it.

### Quarantining a known-flaky test

A quarantined test **still runs and is reported** (a `QUARANTINED flaky tests` section in the pytest output) but a failure of it does
not fail the gate. Declare it either on the test, `@pytest.mark.quarantine_flaky(reason="...", issue="#873", expires="2026-12-31")`, or in
`tests/flaky-quarantine.toml` (`[[quarantine]]` with `nodeid`, `reason`, `issue`, `expires`; a node id also covers every
parametrisation). All three of reason (15+ characters), issue (`#N` or a URL) and an ISO expiry date at most 90 days away are required. The marker is
`quarantine_flaky` (not `flaky`, which pytest-rerunfailures and the `flaky` plugin own) and goes on the test **function** only: a
module-level `pytestmark` or a class decorator would hide a whole file, so it never quarantines and the structural test fails:
`tests/test_ci_flaky_quarantine.py` fails the structural gate for an incomplete entry or marker, and an incomplete one never quarantines.
Past its expiry date an entry stops quarantining and the test fails the gate again, so fix it or renew the date deliberately.
The plugin is `scripts/ci/herdr_flaky_plugin.py`.

## Reading a unit gate while it runs, and when it overruns

A CI step that the platform stops keeps only what it had already printed. `--durations=40` and `pytest_file_times` both report at
teardown, so for the two runs this section exists for - pipeline 1754's single-pass gate (6829 s, tail truncated) and pipeline 1978's
shard 3 (`killed` at 1950 s) - neither reported anything, and the next session had to re-measure from zero (#937).

`scripts/ci/pytest-plugins/pytest_ci_progress.py` is loaded on every pass (`herdr_pytest_report_load` in `scripts/ci/pytest-parallel.sh`,
so it covers the fast pass, the xdist rerun and the slow tail) and prints, as the pass runs:

```
[ci-progress] shard=3/3 t=612.4s done=1420 pass=1380 fail=2 skip=38 err=0 workers=8/8 running=[gw3:tests/x.py::test_y(41s)] slow=[tests/z.py::test_w 88s]
```

* cadence: every `HERDR_PYTEST_PROGRESS_EVERY_SECONDS` (60) or `HERDR_PYTEST_PROGRESS_EVERY_TESTS` (200), whichever comes first, plus a
  final record. A wall-clock tick is used, not only report arrivals, because a wedged pass produces no reports - the run worth reading is
  the one that has gone quiet;
* a stall is reported **once**, then the plugin writes nothing at all (no line, no state-file read, no fsync) until a test finishes again.
  This is a safety property, not tidiness: `scripts/ci/run-watched.py` decides a pass is wedged from two signals only - no byte on the
  pipe and no CPU rise in the tree (`run-watched.py:266-292`) - and both are reset by anything the plugin does. A line every interval
  would keep a deadlocked pass alive forever and re-create pipeline 1362's 8 h 20 m run. The one line (`quiet=true`) names what each worker
  was inside when the pass went quiet, and the silence after it belongs to the watchdog. `tests/test_ci_pytest_progress.py` runs a hung
  pass under `run-watched.py` and requires exit 124;
* `running=` names the test each worker is inside and for how long. A finished test's report reaches the controller only at the end, and
  xdist's forwarded `pytest_runtest_logstart` (`xdist/dsession.py:308-315`) carries no worker identity, so the controller cannot tell from
  it who is inside what: each worker writes the test it started to `$HERDR_PYTEST_PROGRESS_DIR/<pass-id>/worker-<id>.json` and the
  controller reads those files (default `.tmp/ci-progress`, gitignored). `workers=` counts what has reported, over what xdist will
  actually start - the transport list, so `-n auto --maxprocesses=8` says 8 and not the uncapped auto count - because a worker that has
  started nothing yet is invisible and a killed pass must not read as an idle one;
* `<pass-id>` is the xdist test-run id, taken from the worker's own `workerinput` or the controller's session plugin (or `--testrunuid`
  when pinned), or `local-<pid>` for a pass without workers. Never from `PYTEST_XDIST_TESTRUNUID`: a pytest that a test starts in a
  subprocess inherits that variable from the worker which started it, and a nested controller reading it would clean and read its parent's
  directory while filing its own records under the parent's id, so the parent's live workers would read as vanished. The id is needed
  because `herdr_pytest_run_gate` in `scripts/ci/pytest-rerun.sh` can have the slow tail running beside the fast pass in one progress
  directory: filed together, the serial pass's `main` reads as a fifth xdist worker and either pass's cleanup erases the other's evidence
  mid-run. Only the pass that owns the run writes records - a worker inherits `HERDR_PYTEST_PROGRESS_DIR`, and if it appended its own
  records every test would be counted twice;
* `slow=` lists the finished tests over 1 s, longest first; sub-second tests are left out because they push the answer off the line;
* the same records go to `$HERDR_PYTEST_PROGRESS_DIR/progress.jsonl`, one JSON object per line;
* the lines are written to a duplicate of descriptor 1 taken when the plugin loads, so pytest's own capture cannot hold them until
  teardown.

Past `HERDR_PYTEST_PROGRESS_BUDGET_SECONDS` the same view repeats as `[ci-budget]` lines (and `kind: "budget"` records), once per interval.
**A budget stops nothing.** `unit-gates.sh` has no flat cap by design ("a slow but live run is never killed") and
`scripts/ci/run-watched.py` owns stopping a wedged pass; the budget's whole job is that the last thing a step prints before the platform
stops it names what it was inside. The defaults come from measured walls (`herdr_pytest_progress_defaults`):

| run | default budget | what CI measured (pipelines 1958-2012, since the shards landed) |
| --- | --- | --- |
| a shard (`HERDR_UNIT_SHARD` set) | 900 s | shard medians 346-611 s, maximum 741 s; shard 3 was killed at 1950 s (pipeline 1978) |
| unsharded (`unit-gates-main`, any local run) | 1800 s | `unit-gates-main` 999-1405 s; the pre-shard single-pass gate 6829-8118 s (pipelines 1754, 1941, 1942, 1954) |

Set it to `0` to have no budget. `tests/test_ci_pytest_progress.py` runs real pytest passes (real xdist workers, real subprocesses) and
asserts the records name a test that is still running, that a budget exceedance leaves both the dump and the original verdict, and that
the plugin changes no result.

**Why the overruns stopped being the common case.** The 6800 s class was a single unsharded pass, and `#919` (2026-11-21) split the gate
into three shards; across pipelines 1958-2012 no shard exceeded 741 s. Two things #937 asked for are therefore already partly answered:
`#919` removed the flat 6861 s exposure for pull requests, and `#907` ("size unit-gates workers from the Slurm allocation", closed
unmerged 2026-11-21) is superseded rather than needed: `scripts/ci/cpu-quota.py` now takes the real budget from the cgroup quota, and the
load average only shaves `auto` down to a floor of half the visible CPUs and never below 4 (`herdr_free_cpus`), so the squeeze pipeline
1804 hit - `-n 1` on a shared node - cannot recur. Re-measured after `#919`: shards finish in 346-611 s median, which is what 8 workers
look like; `-n 1` would be minutes-long shards.

## Known-flaky gates

`tests/control_plane/test_assistant_hub_slack_questions.py` is timing-coupled and reddens `unit-gates` intermittently on more than
one test (issue #703). A single red `unit-gates` on a pull request whose diff cannot reach that file should be re-checked rather
than assumed to be caused by the diff - and note that `structural-gates` failing is never in that category, because it reads only
tracked data files.
