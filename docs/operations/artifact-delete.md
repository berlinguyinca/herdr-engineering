# Deleting, purging and restoring artifacts

`retract` only hides an artifact. Deleting removes it for good, in two steps that the owner controls.

| Step | What happens | Reversible |
| --- | --- | --- |
| Delete (tombstone) | The artifact leaves every list. A reason and a hold window (default 30 days) are recorded. Bytes stay. | Yes, until the bytes are purged |
| Purge | Object bytes are deleted. Hashes, version rows, provenance and the audit trail stay. | No |

The worker purges a tombstone automatically when its hold window ends. The owner can skip the wait with **purge now**.
Bytes that another artifact (live or still restorable) also uses are never deleted; they are reported as kept.
A tombstone with a legal hold has no purge date and is never purged, not even by purge now, unless the owner passes the
explicit legal-hold override. A retracted artifact that is restored stays retracted.

## Who may do it

Only an owner: the dashboard owner / operator API key listed under `control_plane.artifact_delete` (below), or the
operator of an open (keyless) listener. Creators, members, other operators and read-only collaborators keep only
`retract`; the server answers them with a plain 404 that does not depend on any id. With no owner configured on a keyed
server, nobody can delete.

```yaml
control_plane:
  artifact_delete:
    owner_key_digests: ["0123456789abcdef"]   # first 16 hex digits of sha256(<operator API key>)
```

Compute the digest without printing the key: `printf %s "$HERDR_API_KEY" | sha256sum | cut -c1-16`. Restart the control
plane after changing the list. Every step is an append-only `artifact_lifecycle_events` row (actor, reason, request key).

## API

All routes need the owner key, `Content-Type: application/json`, and a same-origin `Origin` when one is sent.
At most 200 ids per request; results are per id, in order. A request key makes a replay change nothing.

* `POST /api/v1/artifacts/delete` with `{"ids": [...], "mode": "tombstone"|"purge_now", "reason": "...",
  "hold_days"?: 0-3650, "request_key"?: "...", "confirm"?: "<count>", "override_legal_hold"?: true}`.
  `purge_now` must carry `confirm`, the decimal count of distinct ids; a mismatch is refused (400) and nothing happens.
  Results: `tombstoned`, `deleted`, `already_purged`, `not_found`, or `refused` with a fixed `reason` (`legal_hold`).
* `POST /api/v1/artifacts/restore` with `{"ids": [...], "request_key"?: "..."}`: `restored`, `not_found`, or `refused`
  (`not_tombstoned`, `purged`).
* `GET /api/v1/artifacts/tombstoned` lists tombstones with their purge date; `GET /api/v1/artifacts/delete-access`
  tells the caller whether it is an owner.

A dead registry database is always `503 {"error": "artifact store unavailable"}`; the driver's text stays in the log.

## CLI

```
herdr-eng artifacts delete ART_ID... [--reason TEXT] [--hold-days N]
herdr-eng artifacts delete ART_ID... --purge-now --yes [--reason TEXT] [--override-legal-hold]
herdr-eng artifacts restore ART_ID...
```

Common options: `--control-plane URL`, `--api-key-env NAME` (default `HERDR_API_KEY`; the key is read from that
variable and never printed), `--request-key KEY`. One JSON object per id is printed; ids beyond 200 go in further
batches. `--purge-now` without `--yes` is refused before any request.

## Dashboard

On the Artifacts page an owner sees a checkbox per row, **Delete** (tombstone) and **Delete permanently**. The latter
opens a field that wants the number of selected artifacts typed in. The view selector switches to **Deleted
artifacts**, where tombstones can be selected and **Restore**d; purged ones are listed but cannot be selected. The list
shows the artifacts the signed-in key can read, so use the CLI for artifacts made by other keys.
