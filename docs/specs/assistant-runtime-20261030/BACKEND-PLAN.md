# Pi assistant activation backend plan

Goal: turn a private immutable Pi package into one persistent room agent only after a
fresh operator attestation and a runner receipt, without accepting host execution details.

1. Add failing lifecycle tests for strict host attestations, owner-only activation
   requests, immutable idempotency, package/host/model/profile/digest matching, and
   public credential privacy.
2. Fold private host and activation events into restart-safe state. Exclude all private
   payloads from room feeds, room cursors and generic participant tools.
3. Add owner routes for compatible host discovery, request/list/inspect and cancellation.
   Requests carry only package ID, host ID, display name, idempotency key and optional
   selected model. The server fixes room access to `read` and `execute`.
4. Add operator routes for attestation, queue polling, atomic claim, heartbeat, launch
   receipt, and cancellation polling. Claim returns a credential and claim token once;
   only their hashes persist. Revalidate current owner, package, host nonce, model and
   room policy before minting the agent.
5. Revoke room execution immediately when cancellation is requested. Mark a running
   process cancelled only after its runner receipt. Expire stale leases without replaying
   a launch and revoke the associated agent.
6. Verify hydration, lost-response replay, stale/forged nonce and token refusal, host
   expiry, checkpoint monotonicity, cancellation, and legacy collaboration regressions.

The backend never accepts commands, argv, paths, mounts, environment variables, secrets,
credentials or action lists. Docker and Apptainer packages remain unlaunchable. A queued,
claimed or starting record is not evidence that a process ran; `ready` requires a matching
launch attestation from the runner.
