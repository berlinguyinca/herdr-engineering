# Pi assistant activation CLI

The CLI queues and observes strict Pi package activation through the authenticated control
plane. It does not run Pi, call an operator endpoint, or accept execution details.

```sh
herdr-eng assistant hosts list --room ROOM_ID --json

herdr-eng assistant agents create \
  --package PACKAGE_ID \
  --room ROOM_ID \
  --host HOST_ID \
  --name venue-agent \
  --idempotency-key venue-agent-create-1 \
  --model qwen3-32b \
  --json

herdr-eng assistant agents runtime-inspect RUNTIME_ID --room ROOM_ID --json
herdr-eng assistant agents runtime-status RUNTIME_ID --room ROOM_ID --json
herdr-eng assistant agents runtime-cancel RUNTIME_ID --room ROOM_ID --json
```

`--model` is optional. When omitted, the backend uses the package default. An explicit model
must be in the immutable package policy and the fresh host/room policy. The creation request
contains only `package_id`, `host_id`, `name`, `idempotency_key`, and optional
`selected_model`. There are no command, image, path, environment, credential, user, sandbox,
or grant arguments.

Host listing exposes owner-safe attestation summaries: stable host ID and label, readiness,
attestation times, Pi runtime support, strict sandbox profiles, and allowed model IDs. It
does not expose runtime paths, credentials, broker sockets, or operator controls. A host is
usable only when the backend reports `ready`; stale or unavailable hosts cannot be selected.

Creation is asynchronous. First submission returns a runtime in `queued` state; replaying
the same idempotency key and content returns the existing record. Status can move through
`queued`, `claimed`, `starting`, and `ready`, or end as `failed` or `cancelled`. `agent_id`
is absent until the trusted runner claims the activation. The credential is never returned.

`runtime-status` is a bounded projection for automation. `runtime-inspect` returns the full
owner-visible record, including checkpoint/error/lease state and a public launch-attestation
summary when one exists. Queued, claimed, or starting status is not proof that Pi launched.
Ready status depends on the operator-owned runner validating the package digest, trusted Pi
artifact, exact `task-restricted-v1` profile and limits, model, lease, and completion receipt.

Cancel requests are durable and asynchronous. Cancellation cannot erase checkpoints or
external task results. Once an activated agent is ready, ordinary work is delegated with the
existing `assistant tasks create` command; task authority remains server-enforced.

This slice is Pi-only. Docker and Apptainer packages remain definition-only. Host
attestation, claims, reports, credentials, and launch operations are private operator/runner
functions and have no user CLI commands.
