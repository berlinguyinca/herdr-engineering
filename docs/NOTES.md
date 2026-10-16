# Re-adoption note — preserved worker branch (TSK-6peLA9)

The partial output of a crashed worker (commit 504eedf, previously on the
preserved branch pi-eng-orch-a-1) was re-adopted and verified complete before
integration.

## Verification performed

- Line-by-line review of the CI-entity feature in
  `herdr_engineering/control_plane/repo.py`:
  - `upsert_test_run` / `upsert_build` — TestRun*/Build* events, deep-copied
    event payloads, results list copied on write.
  - `upsert_review` / `add_review_finding` — findings upsert by id,
    ReviewFindingAdded event.
  - `upsert_pull_request` / `_record_pr_merged` — PullRequestOpened/Updated and
    the one-shot PullRequestMerged transition (old_status != merged).
  - `_resolve_mission_key` — fail-closed namespacing: rejects ':' in id or
    source, unknown non-herdr source raises KeyError, herdr source is raw.
- `tests/control_plane/test_ci_entities.py` (332 lines) is complete and passes.
- Test results:
  - `pytest tests/control_plane/` (test_ci_entities, test_repo, test_ingest,
    test_mission_namespace, test_events, test_models, test_snapshots):
    **69 passed**.
  - Full `pytest tests/`: 398 passed, 1 skipped; the only 2 failures are
    pre-existing environmental failures in `tests/test_edge.py`
    (enroll-sync tests) caused by a reachable fabric gateway in this machine's
    config — they reproduce identically on the unrelated
    ws/10e-fix1-ci-entities-review worktree and are unrelated to this branch.

No code changes were required; the branch was verified as-is. This commit
records the re-adoption so the branch carries an explicit marker on top of
504eedf.
