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
