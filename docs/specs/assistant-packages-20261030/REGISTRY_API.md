# Assistant package registry API v1

The registry stores private, immutable assistant definitions for one authenticated
room owner. Registration does not create an agent, launch a runtime, grant a
capability, approve a model, transfer knowledge, or authorize an external action.

## Routes

`POST /api/v1/collab/rooms/{room_id}/assistant-packages`

The body contains exactly `{"manifest": <assistant-package-v1>}`. Identity and room
ownership come from the bearer credential. Agent credentials and non-owner room
members are refused. The server normalizes the manifest with the shared validator,
hashes its canonical JSON, and records the normalized definition.

The first registration returns HTTP 201. Repeating the same normalized name,
version, and content returns HTTP 200 with `replayed: true`. Reusing a name and
version for different content returns HTTP 409. The returned package includes:

- `package_id`: `package_` plus the canonical SHA-256 hex digest;
- the normalized `manifest` and `digest`;
- `runtime_kind`;
- `runtime_launch_supported: false`;
- `capabilities_are_requests: true`;
- `status: definition-only` and `created_at`.

`GET /api/v1/collab/rooms/{room_id}/assistant-packages` lists the authenticated
owner's definitions in deterministic name/version order. The targeted form
`GET /api/v1/collab/rooms/{room_id}/assistant-packages/{package_id}` returns one.

## Privacy and limits

Package events are event-sourced for restart recovery but excluded from shared room
feeds, SSE/MCP reads, and room change cursors. Project audit exposes only the action and
room metadata, never the package ID, digest, or manifest. Another member of the same room cannot
list, inspect, or register packages under the owner's identity.

Each room owner may retain at most 100 definitions per room and register at most 20
new definitions per ten minutes. Reads and replays recheck the current authenticated
user and room-owner role. Malformed manifests and unknown request fields produce
bounded errors that do not reflect submitted credentials or private content.

Docker and Apptainer packages remain definitions only. Pi definitions also remain
unlaunchable through this API until a separate, attested runtime binding proves the
requested sandbox profile can be enforced. No route in this contract starts a process.
