# CI and reviews in HerdR

CI and Reviews are native navigation pages with the same outcome tabs, project grouping and detail treatment as Missions and Sessions. Overview contains compact summaries. Related links carry repository/revision or explicit mission/session references; a port or working-directory basename is never proof of association.

## Woodpecker

The connector targets the installed Woodpecker v3.18 API. It discovers the authenticated user's repositories, checks recent pipelines in a single background poll, retains last-known runs on failure, and reports discovery progress, per-repository errors, freshness and authentication failure separately. A pending/running pipeline is not declared stalled just because time passes. Logs use the stored log API, are bounded and redacted, and retain a link to the full provider output.

The Woodpecker address and personal access token live in the `ci` section of the central fleet config (`herdr-eng fleet config init` asks for them; file mode 0600). The token inherits its user's access; use an appropriately restricted account. The browser never receives it.

`deploy/control-plane/deploy.sh` mounts the central file read-only into the web container; no separate credential file or compose override is needed.

Retry/cancel are disabled unless `control_plane.ci_actions: true` is set in the central fleet config. When enabled, they still require HerdR's operator role and Woodpecker push permission. The correct upstream operations are POST restart and POST cancel; cancel does not delete pipeline history. Runner inventory is optional and requires additional upstream privileges. Missing runner permissions do not disable repository monitoring.

Shared projects can span repositories: `HERDR_ENGINEERING_PROJECT_REPOS_FILE` points to a JSON object mapping canonical identities to a common project ID, for example `{"github.com/team/frontend":"product", "github.com/team/backend":"product"}`. Defaults preserve the full forge/repository identity. Provider numeric IDs distinguish equal repository names on different forges.

## Plannotator

Plannotator owns the review UI and decisions. HerdR retains review subjects, immutable plan/revision links, runtime addresses, host liveness and outcome receipts in its existing PostgreSQL event log. The actual upstream CLI runs on the host owning the work; no arbitrary repository commands are executed by the web server. Its dynamic ready URL is advertised to HerdR and opened through the native review inspector.

The host launcher and exact usage are documented alongside the implementation. A missing/expired launcher heartbeat means the runtime is stale, not that a reviewer has stopped thinking. A changes-requested decision is a review outcome; it is not an execution failure or an approval for a different revision.

## Verification boundaries

Automated contract tests verify denied permissions, correct action HTTP methods, log decoding/redaction, snapshot failure retention, single-flight progress, durable receipt replay and native UI outcomes. Live validation uses read-only CI calls. Retry/cancel are never fired simply to test connectivity, and test plan approval is not approval of product code.

## Stability, tests and rendered pipelines

The CI page includes retained run outcomes and measured durations, success rate (successful / passed-or-failed runs), latest test inventories by repository and branch, count changes and history, current failing tests with recurrence/first failure/last pass, and slow tests. Inconsistent outcomes require both passed and failed observations on the same revision; they are investigation evidence rather than a claim that environments were identical. Repository, branch, period and search filters scope the reports independently of outcome tabs.

Selecting a run from the original run list gives its pipeline the page width and temporarily hides the retained metrics overview. The project-health tree can instead share the width with the selected pipeline. Reported dependencies render as arrows with parallel branches; when the provider omits dependencies, workflow groups explicitly state that execution order is unknown. Failed steps are red, successful steps green, and running, queued, skipped and blocked steps retain text labels. Select a step for bounded logs. The test table shows the latest report attempt with search and pagination; report attempts remain inspectable.

Publish structured JUnit reports using the [CI report upload guide](ci-test-reports.md). Missing reports remain distinct from zero tests. History is a bounded observation window, not a complete provider backfill: up to 200 reports and 2,000 run observations, with a 16 MiB budget per kind. Report publication and observed run changes use the existing durable event sink when configured; without it, the UI explicitly identifies memory-only storage. Durable-sink failures leave observations retryable and surface a partial-history warning. Restart recovery replays retained evidence from the event log.

## Project health heatmap and PR tree

The CI page starts with a project-by-time heatmap. Choose **Last 24 hours**, **Last 24 runs**, **7 days**, **14 days**, or **30 days**. Hourly history covers the exact trailing 24 hours split at UTC hour boundaries; edge hours can be partial. Daily windows include today. Last 24 runs means the latest 24 matching provider runs for each project, with no date cutoff; its columns are ordinal positions rather than shared dates.

Default-branch builds are selected initially. Pull requests and All runs are explicit scopes; a PR targeting main is not silently counted as a main-branch push. The provider must report a default branch. Search, branch, route repository/mission/session context, and failures-only filtering remain explicit. Saved window/scope/search preferences and tree expansion survive navigation; a selected last-run cell stays pinned to its provider run when newer runs arrive.

Green means successful completed runs without failures; red means failed runs without successes; amber means both. Active runs have a blue marker. Canceled, skipped and unknown outcomes remain distinct from pass/fail. Complete empty buckets represent no activity; incomplete buckets are marked with question marks or dashed coverage borders. Hover or focus a cell for exact counts and UTC boundaries, or open the accessible exact-count table. Coverage issues identify provider errors, incomplete inventory, pagination validation or safety limits.

Select a cell to narrow the tree. Expand **project → PR or branch → run**, then choose a run to open the existing pipeline. Workflow and step details load for the selected run, not for every project. PR association requires explicit provider evidence; missing identity is labeled unavailable. Parent rows distinguish the latest outcome within the selection from failures anywhere in that selection. Closed tree groups do not render run descendants; open groups page 100 rows at a time. Use Tree + pipeline, Tree only or Pipeline only to allocate space; the original run list opens the wide inspector.

`GET /api/v1/ci/history` is a viewer-readable background projection separate from durable JUnit report storage. It reads bounded pages from Woodpecker, revalidates pagination boundaries and exposes per-repository coverage. The history cache is **memory-only** and rebuilds after a restart; this does not remove durably stored test reports. Limits are 500 repositories, 60 pages/3,000 runs per repository, 20,000 total runs and 16 MiB of serialized run rows. Limits describe serialized data rather than total process memory. Safety limits, provider errors and missing dates never imply complete history. Existing completed prefixes normally refresh their head page; retained nonterminal pages are rechecked, and older outcomes are rescanned after five minutes. Live provider detail remains the authority for an individual run.
