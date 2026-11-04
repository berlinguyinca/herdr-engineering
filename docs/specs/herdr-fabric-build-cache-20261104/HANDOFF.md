# HANDOFF (rolling; newest section first)

## Spec 01 measure -- DONE (code complete; Bender baseline NOT run)
Completed: opt-in `build-coordinate --record` (fork + wait4, locks held for child life, SIGINT/SIGTERM forwarded to child only, exit code exact, signal -> 128+sig); `build_metrics.py` (typed `build_started`/`build_finished`, cmd_kind allowlist, argv/env never recorded, sccache delta, `/proc` IO sampling, diskstats delta, `unavailable` list, rotation at 5 MiB, `summarize()`); `--status` now also shows a recorded-build summary; `scripts/build_bench.py` (cold only inside its own `--copy` temp dir); slots doc/help drift fixed (min(4, cores-4), floor 2).
Not done: the exit criterion "repeatable Bender cold/warm baseline" (no ssh/run against real projects by design; run `scripts/build_bench.py` on Bender next); cleanup-path instrumentation (build_maintenance untouched); `build-gate.ts` fleet fingerprint untouched (still may state old slot text); no type-checker is configured in pyproject/CI for Python, so none was run; `ruff format` not applied (repo files are not ruff-formatted); pre-existing ruff I001 in cli.py:1067 left alone.
Risks: `sccache --show-stats` can start a sccache server; `proc_io_write_bytes` is logical and a lower bound (short-lived processes missed); disk delta is device/host wide and includes other activity; fake `sccache` used only in the parser-contract test; existing tests hard-code `/home/wohlgemuth/IdeaProjects/herdr-engineering` on sys.path (they pass, but may import the other checkout in 2 subprocess tests).
Files: herdr_engineering/build_metrics.py (new), herdr_engineering/build_coord.py, herdr_engineering/cli.py, scripts/build_bench.py (new), tests/test_build_metrics.py (new), docs/build-gate.md, this file.
Tests: `python3 -m pytest tests/test_build_coord.py tests/test_build_metrics.py tests/test_build_cleanup.py tests/test_build_maintenance.py tests/test_cleanup_deploy.py -q -p no:cacheprovider` -> 94 passed (63 baseline + 31 new), exit 0. `ruff check` on new files clean.
Next: 02-config.

## Spec 00 audit — DONE (commit bda65484)
Completed: `AUDIT.md` (repo root) + `audit/{A,B,C}-*.md`. No source changes.
Not done: nothing; read-only.
Risks: see AUDIT.md "Findings that change the spec" (no sccache on bender, no Slurm runner, no S3 endpoint, logical-vs-physical writes, no SSE resume ids).
Baseline: `python3 -m pytest tests/test_build_coord.py tests/test_build_cleanup.py tests/test_build_maintenance.py tests/test_cleanup_deploy.py -q` → 63 passed.
Next: 01-measure.
