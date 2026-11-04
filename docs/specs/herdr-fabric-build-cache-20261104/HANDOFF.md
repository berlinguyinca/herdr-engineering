# HANDOFF (rolling; newest section first)

## Spec 02 config -- DONE
Completed: `fabric_build_cache` section in config.py DEFAULTS (all off, mode local_only, allowed_hosts []); `build_cache_config.py` (`validate`, frozen dataclasses, StrEnums, `check_credentials_file`, `public_view`); `load_config` validates after merge + env expansion; example YAML block with per-flag defaults. Fails closed: unknown keys, non-bool types, ranges, mode implications, URL-shaped endpoint_ref, tls_required=false only for `*.local-test`, secret-named keys, relative credentials_file. Warnings (tuple) for gpu_for_cpu_builds and cleanup.dry_run=false. Disabled => effective_mode local_only, writes/slurm effective flags false.
Not done: no consumer wired yet (nothing calls `check_credentials_file`; no CLI/API exposure of `public_view`); allowed_hosts not enforced anywhere yet; central fleet-file layer (B in audit) untouched.
Risks: observability defaults are false here (contract example shows true) to keep everything default-off; legacy `${VAR}` expansion still applies to config values (pre-existing); the no-PyYAML fallback parser cannot read lists/null, so the section needs PyYAML; bad section now makes `load_config` raise for that file only when the section is invalid.
Files: herdr_engineering/build_cache_config.py (new), herdr_engineering/config.py, config/herdr-engineering.example.yaml, tests/test_build_cache_config.py (new), this file.
Tests: `python3 -m pytest tests/test_build_cache_config.py tests/test_config.py tests/test_fabric_yaml_only_config.py tests/test_host_config.py tests/test_build_coord.py tests/test_build_metrics.py -q -p no:cacheprovider` -> 172 passed. ruff clean on touched files (pre-existing lint elsewhere ignored).
Next: 03-local.

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
