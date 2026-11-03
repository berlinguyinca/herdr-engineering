# Memory Explorer (read-only)

One `Memory` page in the dashboard (`#/memory`, nav group Code, hidden until the server advertises `memory_v1`) plus three
authenticated GET endpoints. Phase P04 of the unified Hindsight memory work; implements the "first PR minimum" of the
dashboard UX contract: search, detail drawer, integration status. No graph, quality, timeline or replay views, and no write actions.

## Endpoints
- `GET /api/v1/memory/search?q=&scope=&project=&kind=&status=&harness=&host=&limit=&offset=`
- `GET /api/v1/memory/{memory_id}`
- `GET /api/v1/memory/status`

## Configuration (`control_plane.memory` in the fleet config)
```yaml
memory:
  enabled: true
  command: [herdr-memory]          # optional; the canonical CLI, CONTRACT.md v1
  viewers:                         # deny by default: an unlisted API-key principal gets 403
    "key:<12-hex digest>": {projects: [herdr-engineering], scopes: [global, infra]}   # projects may be "*"
```
Only `viewer` and `operator` keys can read. Disabled answers 404.

## Behaviour
- `memory_explorer.py` is the single adapter: it runs `herdr-memory recall|get|status` and parses the one JSON object. No Hindsight
  credentials reach the browser and the control plane holds no memory store.
- Every record is authorized by its bank on the server (`<project>-facts|summaries`, `<project>-mission-<id>`, `herdr-global`,
  `herdr-infra`). An unreadable memory is dropped from searches and answers 404 on detail, identical to a missing id.
  Personal/Rakazo/assistant banks are never reachable, even for a `"*"` viewer. Filtering on a project or scope the viewer cannot read is a 403.
- Text is credential-redacted and labelled untrusted; provenance is whitelisted to the contract keys and never inferred.
- Audit: one metadata-only `memory_explorer` event per read (action, outcome, actor, counts, scopes, memory id); never query or memory text.
- CLI missing, slow or not contract v1: `503 memory service unavailable`, no sample data.
- Links to missions, sessions, pull requests and handoffs appear only when that id is in the memory's own provenance or evidence refs.
  Commits have no dashboard page and stay as text.

## Known gaps (contract v1 does not provide them)
- Per-harness/host integration state: shown as UNKNOWN. Retrieval history: shown as unknown. Revision history: shown only if `get` returns `revisions`.
- Superseded/retracted memories are excluded by `recall`, so a status filter for them finds nothing (the UI says so).
- Paging re-asks `recall` for `offset+limit+1` results (bounded at 200), since contract v1 recall has no cursor.
- The `--project` flag on `recall` is assumed; bank authorization on the server does not depend on it.

## Tests
`tests/control_plane/test_memory_explorer_api.py` (mocked adapter), `packages/memory-view.test.js`, and
`tests/playwright/memory.test.mjs` (real control plane, mocked adapter; writes `docs/evidence/memory-explorer/*.png`).
