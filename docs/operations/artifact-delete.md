# Deleting, purging and restoring artifacts

`retract` only hides an artifact. Deleting removes it for good, in two steps that the owner controls.

| Step | What happens | Reversible |
| --- | --- | --- |
| Delete (tombstone) | The artifact leaves every list. A reason and a hold window (default 30 days) are recorded. Bytes stay. | Yes, until the bytes are purged |
| Purge | Object bytes are deleted. Hashes, version rows, provenance and the audit trail stay. | No |

The worker purges a tombstone automatically when its hold window ends. The owner can skip the wait with **purge now**.
Bytes that another artifact (live or still restorable) also uses are never deleted; they are reported as kept.

## Who may do it

Only an owner: an operator API key whose digest is listed under `control_plane.artifact_delete`. Creators, members,
other operators and read-only collaborators keep only `retract` (and `archive`, a reversible hide that loses nothing);
the server answers them with a plain 404 that does not depend on any id. This includes the older per-artifact routes
`POST /api/v1/artifacts/{id}/tombstone` and `.../restore` of a tombstoned artifact, which are owner-only too. With no
owner configured on a keyed server, nobody can delete.

```yaml
control_plane:
  artifact_delete:
    owner_key_digests: ["0123456789abcdef"]   # first 16 hex digits of sha256(<operator API key>)
    allow_keyless_owner: false                # default; true only for a keyless listener you fully trust
```

Compute the digest without printing the key: `printf %s "$HERDR_API_KEY" | sha256sum | cut -c1-16`. Restart the control
plane after changing the settings. A keyless listener has no keys to list and can be reached by DNS rebinding, so its
operator is an owner only with `allow_keyless_owner: true`. Its actions are audited as `keyless:operator`.

## Rules the server enforces

* **Hold minimum.** A restorable delete keeps the bytes for at least one day (`hold_days` 1 to 3650, default 30). A
  shorter hold is refused; to delete at once use purge now with its typed count.
* **Legal hold** is a durable flag on the artifact (`artifacts.legal_hold`). It survives restore. Purging, restoring or
  re-deleting a held artifact needs the explicit override (`override_legal_hold` / `--override-legal-hold`), which
  releases the hold and is recorded (`legal_hold_overridden`). The worker never purges a hold.
* **Request keys.** A key already used for another action on the same artifact is refused with
  `refused: request_key_reused`. Replaying the same action with the same key returns the first result.
* **Purge order.** The purge record (versions marked purged, keys queued in `artifact_pending_deletes`) commits first;
  objects are deleted afterwards and re-checked for other references at that moment. A failed delete leaves orphaned
  bytes that the sweep retries, never a restorable artifact without bytes. An artifact that has a purge record can never
  be restored, even if it had no versions.
* **Shared bytes** are never deleted while any other artifact (live or still restorable), registered blob (any scope),
  staged collaborator upload or legacy storage key uses them. Purge takes a per-digest advisory lock; a version commit
  does not, so a commit that references the same bytes in the instant between the purge's check and its delete can lose
  them (residual race, narrow).
* **Shares.** Deleting an artifact revokes its pending and live share links and removes their bundles in the same
  request (`shares_revoked` in the result; a failure shows as `null` and a repeat of the delete retries). New shares
  need an active artifact. Bundles already copied elsewhere cannot be recalled.
* **What stays readable.** A tombstoned artifact is hidden from lists and answers 404 to everyone on reads, renders,
  reports, links, shares and annotations, and refuses every write (new versions, merges, uploads, collaborator writes).
  The owner can read its lifecycle history and tombstone listing; restore first to inspect it.
* **Retraction is separate.** A retracted artifact that is tombstoned or purged stays retracted (also after a restart);
  restoring puts back the state it had before the tombstone (retracted or archived).
* **Annotations.** Purging blanks the author identity and email of the artifact's annotations (set to `purged`). The
  comment text and anchors stay as part of the audit record.
* **Audit.** Every step, and every refused attempt on an existing artifact, is an append-only lifecycle event.
  Attempts by a non-owner are logged with the key digest (no artifact is touched).
* **Failures.** Errors are reported per id (`error` / `refused`); a bulk request always returns every id's result. If
  every id fails because the store is down, the answer is the fixed 503.

Parked (not done): route names `tombstoned` and `delete-access` shadow artifacts with those literal ids; the tombstone
listing is capped at 200 and has no pagination; bulk purges run synchronously and `_key_shared` scans have no GIN index
(a 200-artifact purge is slow and holds the store connection); the CLI redacts the key itself but not URL-encoded forms.

## API

All routes need the owner key, `Content-Type: application/json`, and a same-origin `Origin` when one is sent.
At most 200 ids per request; results are per id, in order. A request key makes a replay change nothing.

* `POST /api/v1/artifacts/delete` with `{"ids": [...], "mode": "tombstone"|"purge_now", "reason": "...",
  "hold_days"?: 1-3650, "request_key"?: "...", "confirm"?: "<count>", "override_legal_hold"?: true}`.
  `purge_now` must carry `confirm`, the decimal count of distinct ids; a mismatch is refused (400) and nothing happens.
  Results: `tombstoned`, `deleted`, `already_purged`, `not_found`, `error`, or `refused` with a fixed `reason` (`legal_hold`, `request_key_reused`).
* `POST /api/v1/artifacts/restore` with `{"ids": [...], "request_key"?: "...", "override_legal_hold"?: true}`:
  `restored`, `not_found`, `error`, or `refused` (`not_tombstoned`, `purged`, `legal_hold`, `request_key_reused`).
* `GET /api/v1/artifacts/tombstoned` lists tombstones with their purge date; `GET /api/v1/artifacts/delete-access`
  tells the caller whether it is an owner.

A dead registry database is always `503 {"error": "artifact store unavailable"}`; the driver's text stays in the log.

## CLI

```
herdr-eng artifacts delete ART_ID... [--reason TEXT] [--hold-days N]
herdr-eng artifacts delete ART_ID... --purge-now --yes [--reason TEXT] [--override-legal-hold]
herdr-eng artifacts restore ART_ID... [--override-legal-hold]
```

Common options: `--control-plane URL`, `--api-key-env NAME` (default `HERDR_API_KEY`; the key is read from that
variable and never printed), `--request-key KEY`. One JSON object per id is printed; ids beyond 200 go in further
batches. `--purge-now` without `--yes` is refused before any request.

## Dashboard

On the Artifacts page an owner sees a checkbox per row, **Delete** (tombstone) and **Delete permanently**. The latter
opens a field that wants the number of selected artifacts typed in. The view selector switches to **Deleted
artifacts**, where tombstones can be selected and **Restore**d; purged ones are listed but cannot be selected. The list
shows the artifacts the signed-in key can read, so use the CLI for artifacts made by other keys.
