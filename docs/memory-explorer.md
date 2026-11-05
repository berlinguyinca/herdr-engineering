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

## Private dashboard sign-in (Memory only)

Memory can reuse the existing collaboration Cognito pool and Hosted UI without
changing authentication for other dashboard pages. Configure the host-only
`control_plane.memory.cognito` policy with one canonical HTTPS `origin`, an
optional absolute `sync_dir` (default `/etc/herdr/collab-sync`), and optional
`trusted_proxy_ips` containing exact socket peer IP addresses. The pool, allowed
clients, required group, Hosted UI domain and web client come from the existing
`control_plane.collab` configuration. No new app client, service, or credentials
are minted by this feature.

The browser uses authorization code plus PKCE and the configured origin's `/`
callback, which must already be registered with Cognito. Tokens and pending PKCE
state live only in a separate namespace in tab session storage. Access tokens
are sent only to same-origin HTTPS Memory requests; redirects are refused. An
HTTP dashboard can still use its other pages, but Memory sign-in requires the
configured private HTTPS dashboard origin. There is no public/Funnel fallback.

`GET /api/v1/memory/auth/config` exposes only the existing public sign-in
identifiers at that origin. It grants no memory access. The other Memory GET
routes validate the access token, signature, issuer, expiry, client, token use,
required group and a fresh synced membership file on every request. Memory does
not inherit collaboration's open-signup or administrator privileges. A verified
human's principal ID is the existing Cognito username (or subject when username
is absent), exactly as in collaboration; only an explicit `memory.viewers` entry
for that ID grants the listed engineering projects/scopes. Verified but unlisted
people receive 403; missing or invalid authentication receives 401. Personal,
assistant and Rakazo banks remain excluded.

The HTTPS boundary compares the full configured scheme and authority. Actual TLS
is accepted directly; a TLS-terminating proxy needs an explicitly configured
socket peer IP and that fixed external HTTPS origin. Forwarded headers never
establish identity, transport or origin. Do not add a proxy peer unless direct
untrusted callers cannot reach the backend through that peer with a forged Host.
Missing proxy configuration fails closed. Duplicate security headers, ambiguous
GET bodies, malformed origins/ports and foreign origins are refused. Configure
canonical ports: omit default `:443`, and do not zero-pad a port (for example,
`:09447`); these spellings are rejected rather than normalized differently by
the server and browser.

Existing native API keys still need their own explicit viewer entry and retain
their scoped access. An explicitly configured client API key takes precedence over
browser sign-in, so only that key is sent and its project/scope bounds apply.
A request combining API-key and bearer credentials is
refused. `single_operator`/`open-listener`, an operator proxy, an email header,
and a posted user ID grant no Memory access. Origin restrictions do not turn a
Cognito identity into a global dashboard operator.

Cognito settings and collaboration identifiers are captured at server startup;
restart the web process after changing them. Viewer mappings retain the existing
Memory policy reload behavior. Runtime CLI/profile activation and its validated
viewer schema are supplied separately by the dashboard runtime change (#620).
This authentication slice does not deploy, modify the central configuration,
create a viewer grant, enable automatic summary retention, or migrate old data.
