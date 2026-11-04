# HANDOFF (rolling; newest section first)

## Spec 00 audit — DONE (commit bda65484)
Completed: `AUDIT.md` (repo root) + `audit/{A,B,C}-*.md`. No source changes.
Not done: nothing; read-only.
Risks: see AUDIT.md "Findings that change the spec" (no sccache on bender, no Slurm runner, no S3 endpoint, logical-vs-physical writes, no SSE resume ids).
Baseline: `python3 -m pytest tests/test_build_coord.py tests/test_build_cleanup.py tests/test_build_maintenance.py tests/test_cleanup_deploy.py -q` → 63 passed.
Next: 01-measure.
