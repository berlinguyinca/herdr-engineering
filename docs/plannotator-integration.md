# Plannotator reviews in HerdR

HerdR stores the review subject, launch identity, advertised URL, heartbeat and
upstream decision in its existing PostgreSQL event log. The launcher must receive
a durable registration acknowledgement before starting the upstream process.
Restarting the control plane reconstructs reviews from those events. Plans and
feedback are redacted before persistence; the plan digest identifies the exact
original bytes supplied by the launcher.

Plannotator owns its annotation UI and review decisions. HerdR does not implement
an alternative approve endpoint. An authenticated host reports a CLI JSON result;
its receipt is explicitly `host_reported`, not a cryptographic upstream attestation.
The launch instance, commit association and plan digest must match the registration.
Retries of the same outcome are safe; conflicting terminal decisions are rejected.

## Launch on the session's host

Install the official Plannotator CLI and make it available on PATH. The integration
was verified with version **0.28.24**, including an actual fixture plan review and
its upstream JSON result. No JavaScript runtime is needed for the release binary.

The `herdr-plannotator` entry point is installed with this package. It discovers the canonical origin repository and full HEAD commit from the actual project workspace, applies shared-project overrides, and preserves explicit flags. Mission/session associations remain explicit. Inherited Git directory/config overrides cannot redirect that discovery. Run from the project workspace:

```bash
herdr-plannotator plan \
  --plan-file ./plan.md \
  --repository owner/backend \
  --project-id shared-product \
  --mission-id mission-id \
  --session-id session-id \
  --url-host bender.lan

herdr-plannotator code \
  --cwd /path/to/worktree \
  --repository owner/frontend \
  --project-id shared-product \
  --mission-id mission-id \
  --url-host bender.lan
```

`--url-host` is the host reachable from the user's browser. It does not specify a
port: `PLANNOTATOR_PORT=0` asks upstream to allocate one, and
`PLANNOTATOR_READY_FILE` reports the actual URL. An explicit URL host enables
upstream's remote listening mode; without one, reviews bind to loopback for local
use. Reachability/firewall or a trusted reverse proxy must be configured separately.
The launcher does not proxy arbitrary URLs through HerdR.

The control-plane URL comes from `--control-plane`,
`HERDR_ENGINEERING_CONTROL_PLANE_URL`, or the existing `http://dev.lan` default.
Authentication reuses `HERDR_ENGINEERING_CONTROL_PLANE_KEY` or the private
`~/.config/herdr-engineering/control-plane-key` file. It sends `X-API-Key` and
refuses HTTP redirects. Credentials are never passed as command-line arguments.

Plan reviews use a private immutable input copy and upstream
`annotate <file> --gate --json`; code reviews use `review --json` in the selected
worktree. The commit is an association with that checkout's HEAD; uncommitted code
may also be displayed by upstream and is not represented as a committed artifact.
The final JSON is returned to the invoking agent, which remains responsible for
acting on review feedback. This bridge does not automatically steer a mission.

A heartbeat means the launcher is alive, not that the human has made progress.
After 90 seconds without one, HerdR reports stale transport. It does not invent a
failed or stalled review. After a decision, the upstream process exits: stored
plan, feedback and outcome remain available even though its temporary UI closes.

If the decision upload fails, the launcher retains a private receipt under
`$XDG_STATE_HOME/herdr-engineering/review-receipts/` (default
`~/.local/state/herdr-engineering/review-receipts/`). Replay it after connectivity
returns:

```bash
herdr-plannotator replay --receipt-file /path/to/rev_ID.json
```

## HTTP contract

All routes use the control plane's normal authentication and write protections.

- `GET /api/v1/reviews`: review summaries and freshness.
- `GET /api/v1/reviews/{id}`: the persisted subject and outcome.
- `POST /api/v1/reviews`: register `review_id`, `instance_id`, `kind` (`plan` or
  `code`), `title`, optional `plan`, `repository`, `project_id`, `revision`,
  `mission_id`, `session_id`, `host_id`, and `review_url`.
- `POST /api/v1/reviews/{id}/heartbeat`: bound launch identity plus optional current
  `review_url`.
- `POST /api/v1/reviews/{id}/result`: bound identity plus upstream JSON `result`
  (`decision` and `feedback` or `message`).
- `POST /api/v1/reviews/{id}/failure`: bound identity plus launcher `error`.

The bound identity is `instance_id`, `revision` and `plan_digest` returned by
registration. Ready URLs must be HTTP(S), without embedded credentials, query or
fragment. Repository/project identity and review identity are independent of
runtime endpoints; separate frontend/backend repos can share `project_id`.

Upstream contracts:
[CLI](https://github.com/backnotprop/plannotator/blob/v0.28.24/apps/hook/server/index.ts),
[readiness](https://github.com/backnotprop/plannotator/blob/v0.28.24/packages/server/shared-handlers.ts),
[code-review JSON](https://github.com/backnotprop/plannotator/blob/v0.28.24/apps/hook/server/review-output.ts),
[dynamic ports](https://github.com/backnotprop/plannotator/blob/v0.28.24/packages/shared/port-range.ts).
