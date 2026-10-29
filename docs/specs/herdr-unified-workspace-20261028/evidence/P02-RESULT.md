# P02 Result: Persistent Work Graph and reconciliation

Branch `feat/uw-p02-work-graph`, PR #387 (draft until gates are green; not merged).
Status: **implemented, local gates green; CI status recorded at the end of this file.**

## What shipped

- Migrations `0033_event_trace_id.sql` (nullable `trace_id` on `fabric_events`) and
  `0034_work_graph.sql` (`wg_event`, `wg_assertion`, `wg_entity`, `wg_alias`, `wg_edge`,
  `wg_edge_interval`, `wg_fact`, `wg_layout`, `wg_cursor`, `wg_gap`). Idempotent, additive,
  each carries `-- DOWN:`. Numbered 0033/0034 because open PR #380 holds 0031 and #366 holds 0022-0028;
  re-checked against origin/main and all open PRs at the time of writing.
- `work_graph_core.py` (pure fold), `work_graph_store.py` (asyncpg, ACL-in-SQL),
  `work_graph_sync.py` (ingest worker + reconciler), `work_graph_service.py` (sync facade + HTTP handler).
- API under `/api/v1/graph/work/*` (entity, list, search, snapshot by root, project/issue/task-centric,
  facts, `as_of`, cursor paging, layout GET/PUT with ETag/If-Match, status, operator reconcile/rebuild).
  Contract: `docs/contracts/work-graph-v1.md`. Behind `control_plane.unified_workspace.enabled` (default off):
  flag off is 404 and capability is not advertised; flag on without Postgres is 503.
- `contracts/PI_INGEST_FIELDS.md` lists the Pi-side session-ingest field names.

## Test evidence (real output)

Real Postgres (private initdb server by default; also run against postgres:16 in docker):

```
tests/control_plane/test_work_graph_{core,store,sync,api}.py   42 passed in 15.14s
A14 entities=1022 edges=3023 load=3.17s snapshot300=0.206s page200=0.019s snapshot_queries=4
work-graph tests + test_workspace_contracts_*                  114 passed
postgres:16 (WG_TEST_PG_DSN): store 10, sync 12, api 6         all passed
ruff check (new files)                                         clean
```

Acceptance mapping:

| Item | Test(s) |
|---|---|
| A07 history survives close | `test_a07_history_survives_when_agents_and_rooms_close`, `test_edge_uniqueness_and_no_deletes` |
| A09 ACL before traversal | `test_a09_member_sees_only_their_rooms_and_nothing_leaks_through_neighbors_counts_or_search`, `test_a09_org_scope_and_private_scope`, API door tests |
| A12 drop/late/repeat | `test_a12_*` (core and sync) |
| A14 performance | `test_a14_a_thousand_node_graph_stays_bounded` (4 SQL per snapshot, not per node) |
| Clean-replay equality (gate) | `test_replay_from_clean_rebuilds_the_identical_graph`, `test_postgres_projection_equals_the_pure_projection`, `test_replay_in_any_order_gives_the_same_graph` |
| Evidence links | `test_every_node_edge_and_fact_links_to_a_source_event`, `test_every_row_links_to_a_ledgered_source_event` |
| No orphan artifacts | `test_no_orphan_artifacts_after_reconciliation`, `test_orphan_check_works_in_both_directions` |
| Migration idempotent + DOWN | `test_migrations_are_idempotent_and_the_down_script_undoes_exactly_the_up` (DOWN run in a rolled-back transaction) |

## Gate verdicts

- Clean replay equality: PASS. `clear_projection` then replay yields a byte-identical `dump()`
  (entities, aliases, edges, intervals, facts); layouts are preserved by design.
- Orphan check: PASS. Zero artifact_version without a registry artifact and vice versa after reconcile.
- Historical facts never deleted: PASS (tombstones and closed intervals only).

## Regression run

`pytest tests/control_plane -k "graph or collab or artifact or assistant or lineage or events or sink or envelope or ingest or test_db or contextual or workspace"`:
see the section "Regression result" below. The full `pytest -q` and `scripts/ci/unit-gates.sh` were not run locally; CI is the authority.

## Rollback

1. Set `control_plane.unified_workspace.enabled: false` (default). Routes return 404, worker does not start.
2. Optional schema rollback: run the `-- DOWN:` sections of 0034 then 0033 (tables are additive, nothing else reads them).
3. The only change to existing paths is `Event.trace_id`, written only when set; writers work without the column (tested).

## Deviations

- `scripts/dev/uw-stack.sh up` failed: ports 31900/31901/31080 are held by another user's `herdr-eng` process, not touched.
  I used my own postgres:16-alpine container (`herdr-uw-p02-pg`, 127.0.0.1:31433), removed at the end.
- P01 vocabulary test was changed to `test_live_graph_vocabulary_is_the_p02_extension` because P02 adds node types
  `project team artifact_version external_action` and edge types `contains derived_from discussed_in approved_by notified_via`.

## Open follow-ups

- Room-derived artifact read grants are a P03 decision; artifacts use only the registry gate, so members need explicit grants.
- A project is visible via its rooms or named grantees; a manager in no room does not see it.
- Alias conflicts are ignored (first wins), not surfaced.
- GitHub enrichment covers only issues the graph already references; catalog project to HCN project mapping not done.
- Guest-hidden helper events advancing the cursor are not re-tested.
- PG tests skip when no Postgres binaries exist (repo convention).
- Migration numbers must be re-checked against main and open PRs before merge (#380 uses 0031).
- Layout shared-default write is allowed for operators and members who can see the root.
