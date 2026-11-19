# CI gates

Four Woodpecker pipelines run on `ci.metabolomics.us` for this repository. All four are configured in `.woodpecker/`, all four
trigger on `manual` and `pull_request`, and the step logic lives in `scripts/ci/*.sh` so every gate is reproducible from a clean
checkout without touching CI.

| pipeline | config | script | trigger on `pull_request` | typical wall time |
|---|---|---|---|---|
| `structural-gates` | `.woodpecker/structural-gates.yml` | `scripts/ci/structural-gates.sh` | **always** | ~1.5 s |
| `artifact-handoff` | `.woodpecker/artifact-handoff.yml` | `scripts/ci/artifact-handoff-gates.sh` | **always** | ~1 min |
| `unit-gates` | `.woodpecker/unit-gates.yml` | `scripts/ci/unit-gates.sh` | **always** (`applicability` step skips in seconds when no covered path changed) | ~55 min, seconds when skipped |
| `ui-gates` | `.woodpecker/ui-gates.yml` | `scripts/ci/ui-gates.sh` | **always** (`applicability` step skips in seconds when no UI path changed) | ~15 min, seconds when skipped |

`structural-gates` is also the only pipeline that triggers on `event: push, branch: main`.

## The path filter, and the trap it sets

A Woodpecker `when: {path: {include: [...]}}` filter means the pipeline is never **created** on a pull request that touches none of
those paths, so its `ci/woodpecker/pr/<name>` context never reports. GitHub waits for a required check that never reports, so a
path-filtered check made *required* parks every unrelated pull request on *Expected - Waiting for status to be reported*.

`unit-gates` (#719) and `ui-gates` no longer have a path filter. Both trigger on every `pull_request` (and `manual`) and an
`applicability` step runs `scripts/ci/unit-gates-applicable.sh [unit|ui]`, which diffs the PR against its merge base and exits
0 (run the gate), 3 (no covered path changed: drop `.ci-<gate>-not-applicable`; the gate step exits 0 in seconds saying why) or
fails the pipeline. It fails open: a diff it cannot work out runs the gate. The covered paths live in that one script (the `unit` and
`ui` cases); a manual run (read from `CI_PIPELINE_EVENT`, falling back to legacy `CI_EVENT`) always runs the gate. **All four pipelines now report on every pull request, so all four are safe to
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

## Known-flaky gates

`tests/control_plane/test_assistant_hub_slack_questions.py` is timing-coupled and reddens `unit-gates` intermittently on more than
one test (issue #703). A single red `unit-gates` on a pull request whose diff cannot reach that file should be re-checked rather
than assumed to be caused by the diff - and note that `structural-gates` failing is never in that category, because it reads only
tracked data files.
