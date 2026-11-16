# CI gates

Four Woodpecker pipelines run on `ci.metabolomics.us` for this repository. All four are configured in `.woodpecker/`, all four
trigger on `manual` and `pull_request`, and the step logic lives in `scripts/ci/*.sh` so every gate is reproducible from a clean
checkout without touching CI.

| pipeline | config | script | trigger on `pull_request` | typical wall time |
|---|---|---|---|---|
| `structural-gates` | `.woodpecker/structural-gates.yml` | `scripts/ci/structural-gates.sh` | **always** | ~1.5 s |
| `artifact-handoff` | `.woodpecker/artifact-handoff.yml` | `scripts/ci/artifact-handoff-gates.sh` | **always** | ~1 min |
| `unit-gates` | `.woodpecker/unit-gates.yml` | `scripts/ci/unit-gates.sh` | **only when code changes** (path filter) | ~55 min |
| `ui-gates` | `.woodpecker/ui-gates.yml` | `scripts/ci/ui-gates.sh` | **only when UI code changes** (path filter) | ~15 min |

`structural-gates` is also the only pipeline that triggers on `event: push, branch: main`.

## The path filter, and the trap it sets

`unit-gates` and `ui-gates` restrict their `pull_request` trigger with a `path:` include list:

```yaml
when:
  - event: manual
  - event: pull_request
    path:
      include: ['herdr_engineering/**', 'tests/**', 'packages/**', 'pyproject.toml', 'scripts/ci/unit-gates.sh', '.woodpecker/unit-gates.yml']
```

The intent is stated above the filter: a push to a PR branch must not build twice. The consequence is that **a documentation-only
pull request never runs `unit-gates` at all** - it reports `artifact-handoff` and nothing else.

That matters the moment a check is made *required* for merge. GitHub waits for a required check that will never be reported, so a
path-filtered check made required would park every documentation pull request on *Expected — Waiting for status to be reported*
with no way to satisfy it. **Only `structural-gates` and `artifact-handoff` are safe to require as things stand**, because those two
are the only ones that report on every pull request.

To require `unit-gates`, first make it always report - trigger it unconditionally and skip fast inside `scripts/ci/unit-gates.sh`
when the diff is documentation-only. Do not solve the hang by removing the path filter alone; that makes every documentation change
pay ~55 minutes.

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
