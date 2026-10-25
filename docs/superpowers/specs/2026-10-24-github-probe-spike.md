# GitHub probe spike (Task G0), 2026-10-24

Read-only. Only GraphQL queries were run (no mutations, no scratch repo/issue). The credential was
the operator's `gh` login (`gh api graphql`, which handles auth itself; the token was never printed
or stored). That is the same user's account but not necessarily the control plane's configured
`github.token`; costs are per-query and do not depend on which token, but the repo universe
(360 repos, 78 with open issues, 6,612 open issues, 48 archived, 81 with issues disabled) is this
account's.

## Verdict

**GO for G2-G4**, with three amendments below (watermark overlap, probe covers closes, no extra
query shape needed). Fallback (50/page or aliased probes) is **not needed**. Driver: **asyncpg via
the existing `_LoopRunner` pattern; no new dependency, no SQLite volume.**

## (a) Probe cost at 100 repos/page

Query (repos listing, existing fields plus the probe):

```graphql
query($c:String){ viewer{ repositories(first:100, after:$c,
  affiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER],
  ownerAffiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER],
  orderBy:{field:NAME,direction:ASC}){
  totalCount pageInfo{hasNextPage endCursor}
  nodes{ id nameWithOwner url isPrivate isArchived hasIssuesEnabled viewerPermission
    primaryLanguage{name}
    issues(states:OPEN){totalCount}
    newest: issues(first:1, orderBy:{field:UPDATED_AT,direction:DESC}){nodes{updatedAt}} }}}
  rateLimit{cost remaining nodeCount resetAt} }
```

| Variant | GraphQL `cost` | `nodeCount` |
|---|---|---|
| 100/page, current query (no probe) | 1 | 100 |
| 100/page + probe field | **1** | 200 |
| 50/page + probe field | 1 | 100 |
| full crawl, 360 repos, 100/page + probe | 4 pages x 1 = **4** | 200/page |

The probe adds no points (cost is 1 per page; nodeCount doubles but stays far below the 500,000 node
limit). Every repo with open issues returned a `newest` node (86 repos had one; 0 repos had open
issues and no node). Note the probe is deliberately **without `states:`**, so it reflects the newest
`updatedAt` over open and closed issues: a close bumps it (see b), so the probe also detects
closures and no separate "closed" probe is needed. `remaining` fluctuated between runs (3846,
3829, 3318, 3814) and `resetAt` differed between two responses, so the token is shared with other
consumers; do not infer per-call cost from `remaining` deltas, use `cost`.

Current sweep for comparison: 4 listing pages + sum(ceil(open/100)) over repos with open issues.
With 78 such repos that is at least 82 calls per 300 s; with the probe, an idle sweep is 4.

## (b) Do comment / label / body edit bump `updatedAt`?

**Verified by observation, not by experiment** (no writes allowed). Method: for the 8 most recently
active repos with open issues, read the 25 most recently updated issues (open and closed), 144
issues in total, 8 points. For each issue compare `updatedAt` with the times of its timeline items
(`IssueComment`, `LabeledEvent`, `UnlabeledEvent`, `ClosedEvent`, `ReopenedEvent`, `AssignedEvent`,
`UnassignedEvent`, `RenamedTitleEvent`) and `userContentEdits` (body edits).

| Event kind | Events seen | Event later than `updatedAt` (+2 s) | Issues where it was the latest event | ...and `updatedAt` within 5 s of it |
|---|---|---|---|---|
| IssueComment | 406 | 0 | 41 | 33 |
| LabeledEvent | 407 | 4 (all by 3.0 s, one issue pair) | 20 | 18 |
| UnlabeledEvent | 52 | 0 | 4 | 4 |
| BodyEdit | 131 | 0 | 8 | 8 |
| ClosedEvent | 97 | 0 | 79 | 75 |
| AssignedEvent | 17 | 0 | 0 | n/a |
| ReopenedEvent / RenamedTitle | 2 / 3 | 0 | 0 | n/a |

Reading: no comment, unlabel, body edit or close was ever later than the issue's `updatedAt`, and
when one is the newest event `updatedAt` equals it to within seconds in nearly every case (the
remainder are issues with later activity of kinds not sampled, e.g. reactions, cross references,
project moves). That is consistent with each of these bumping `updatedAt`, but is correlation over
existing data, not a controlled write, so the status is **verified by observation**; a controlled
confirmation would need a scratch repo and was not permitted. Assign/unassign, reopen and retitle
have too few samples to claim anything (assign: 17 events, never later than `updatedAt`).

Anomaly: 4 `LabeledEvent`s (on `InferWeave/inferweave` #1253 and #1250) are timestamped 3.0 s
after `updatedAt`. So `updatedAt` can trail an event's own `createdAt` slightly (label applied by
automation within the same second burst). **Amendment to G3/G4:** compare against the watermark
with an overlap, not a strict `>`: re-read issues whose `updatedAt >= watermark - 10 s` (idempotent
upserts make the overlap free), and keep the repo "changed" if the probe's newest `updatedAt` differs
from stored at all (equality, not ordering, is the probe test, as the spec already says).

## (c) Closed-since read

Works. Query:

```graphql
query($o:String!,$n:String!,$s:DateTime!){ repository(owner:$o,name:$n){
  issues(first:100, states:CLOSED, filterBy:{since:$s},
         orderBy:{field:UPDATED_AT,direction:DESC}){
    totalCount pageInfo{hasNextPage endCursor} nodes{number updatedAt closedAt} }}
  rateLimit{cost remaining} }
```

On `InferWeave/inferweave` (250 closed issues): `since=2026-10-23T00:00:00Z` returned 3 issues, all
with `updatedAt >= since`, ordered `updatedAt DESC`, cost 1; `since=2026-10-24T12:00:00Z` returned 0,
cost 1. `filterBy.since` filters on `updatedAt` (not `closedAt`): a closed issue later edited
reappears, which is harmless for reconciliation. Also valid with `states:OPEN`. The existing
`issues(states:OPEN){totalCount}` remains the drift check for deletes/transfers (neither moves
through CLOSED), as G4 already specifies.

## (d) Postgres driver and `control_plane.pg_dsn`

- Installed: **`asyncpg>=0.29`** (`pyproject.toml` `dependencies`; image `deploy/fabric-stack/Dockerfile`
  runs `pip install ".[collab,review-gate]"` from that file, so web and worker share it).
  **psycopg is not a dependency anywhere** (no mention in pyproject or the Dockerfile); adding it
  means a new package in the image.
- Usage today: `cli.py` (~line 2533) reads `cp.get("pg_dsn")` from the mounted central fleet file
  (never env), then `_maybe_pg_source(dsn)` and `DurableEventSink(dsn=dsn)`. In `control-plane-web`,
  `DurableEventSink` already uses asyncpg synchronously from request threads: it runs coroutines on
  a dedicated event-loop thread (`_LoopRunner` in `control_plane/bridge.py`), serialised by a
  `threading.RLock` because asyncpg allows one operation per connection, reconnecting lazily when
  `conn.is_closed()`. `pg_dsn` can be `None` (collab-only mode, tests), in which case no sink exists.
- The web container has no writable volume (so SQLite would need a new named volume in
  `compose.yaml`).

**Decision:** G1 uses **asyncpg through `_LoopRunner`** with its own connection and lock, mirroring
`DurableEventSink`: a small synchronous `GitHubStore` class (`load()`, `upsert_repos()`,
`upsert_issues()`, `tombstone()`, `bump_revision()`, advisory-lock `try_lock()/unlock()` on a
dedicated session connection). No new dependency, no new volume, no SQLite. This **amends ruling 4's
"psycopg sync adapter"** wording: there is no psycopg to use. Requirements for G1 that follow:
(1) `pg_dsn` unset must degrade to today's in-memory behaviour (store = None, status shown as
unpersisted), as the sink does; (2) the migration lands in `deploy/control-plane/migrations/` (run
by the worker at startup, baked into the image), and the web monitor must tolerate the table not
existing yet during a roll; (3) use a Postgres session advisory lock held on the store's dedicated
connection (a pooled connection would drop it); (4) jsonb must be passed as `str` (asyncpg default,
see `tests/control_plane/test_events.py`).

## Consequences for G2-G6

- G2: ship as specced; one extra aliased field, no cost change; idle sweep drops to
  ceil(repos/100) calls (4 here).
- G3: stop rule becomes `updatedAt < watermark - 10 s` (overlap), not "not newer than".
- G4: use the CLOSED/`since` query above (verified). The 24 h full reconciliation stays as the net.
- G6: a point budget is easy to meet; at today's size a changed repo costs 1 point per 100 open issues
  plus 1 for closed-since.
