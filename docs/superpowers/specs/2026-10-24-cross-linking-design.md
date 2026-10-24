# Cross-linking design

Status: draft for operator review, 2026-10-24. Statements marked **(guess)** were not verified in code or live data.

## Goal

"All pages need to allow proper cross linking." From any entity the operator is looking at (mission, agent, host, service, CI run, review, pull request, project, LLM usage), the neighbouring entities are one click away, and the same entity is written and linked the same way everywhere, including in agent chat and summaries.

Constraints carried over:

- The dashboard is browsed from any fabric machine, so every link is a relative hash link that works on whichever host serves it.
- Remote text is untrusted. A link is only ever built from a known entity kind and an id that is percent-encoded; it never comes from free text as an `href`.
- The public collaboration site never gets these links or the relations endpoint.
- Existing routes keep working.

## What exists today

Hash links found per page (`grep '#/…'` over `packages/`, 2026-10-24):

| Page module | Links out to |
| --- | --- |
| Overview | hosts, services, missions, agents, CI, reviews, topology, activity |
| Topology | services, missions, hosts, agents, CI, reviews |
| Agents list and session workspace | missions, agents, LLM usage, collaboration |
| Mission catalog and mission detail (live) | missions only (the detail has "Related reviews / Related CI" links and a recorded-agents list) |
| Host detail | hosts only |
| Services | services, hosts, agents |
| CI and reviews (`integrations-view`) | missions, agents |
| Projects (`github-view`) | nothing |
| LLM | LLM, agents |

So a host page cannot reach the agents or services on it, a mission page does not show its pull requests or its LLM usage, a CI run does not link to the host that ran it, and the Projects page links nowhere. Other pieces already in place:

- `relatedIntegrationLinks` (`app.js`): "Related reviews", "Related CI" and "CI for this revision" links by `?mission=`, `?session=`, `?repository=` filters.
- The shared safe linkifier (`packages/conversation-markdown.js`): turns `[text](url)` and bare http(s) URLs into links, allows `#/…` internal targets. It does **not** recognise bare references such as a session id or `#123`.
- The lineage graph (`control_plane/graph.py`, `GET /api/v1/graph/...`): a typed graph with `neighborhood` queries, built from lineage events. It is the right store for typed relations but covers only entities that emit lineage events **(guess: not missions, sessions, hosts and CI runs yet)**.
- The repo already holds the foreign keys that make most relations: session → mission, host, branch, repository; service → host and session; CI run → repository, branch, revision; test run, pull request and review → mission; usage record → session and mission.

## Proposal

### 1. One place that knows how to link

A small module `packages/entity-links.js` is the only code that builds an internal link:

```js
entityHref(kind, id, params?)   // kind: mission|session|host|service|ci|review|pr|project|llm-session
entityLabel(kind, record)       // short human label, never the raw id first
```

It owns the route table (today scattered through `app.js` and each view), encodes the id once (`encodeURIComponent`), and returns `null` for an unknown kind or a missing id so a caller can render plain text instead of a dead link. Existing `#/…` strings in the views move to it as each page is touched; it is not a big-bang rewrite.

### 2. A server-side relations answer

`GET /api/v1/related?kind=<kind>&id=<id>` on the **operator listener only** returns the neighbours of one entity, computed from the foreign keys above (not from the lineage graph in version 1):

```yaml
kind: session
id: <id>
relations:
  mission:   [{id, label, state}]
  host:      [{id, label, state}]
  service:   [...]
  ci:        [{id, label, state, repository, branch}]
  review:    [...]
  pr:        [{id, label, state, url}]        # url is an external forge link, shown as external
  agents:    [{id, label, relation}]          # parent, child, handoff
  llm:       {requests, tokens_out, problems} # a summary; the link goes to #/llm/session/<id>
partial: [source...]                          # sources that could not be read
```

Each relation list is capped (20) with a `more` count. Labels are the same bounded, redacted strings the list pages already show. A source that is unavailable is named in `partial`, and the panel says so instead of showing an empty list that looks like "none".

### 3. A Related panel on every detail page

A shared `renderRelated(relations)` renders "Related" as grouped link lists at the top of the mission, agent, host, service, CI run and review detail pages (and the LLM session page), replacing the ad-hoc link rows. Groups with nothing are omitted, not shown empty; the whole panel says "Related items could not be loaded" when the request fails. It is a plain list of anchors, keyboard- and screen-reader-friendly, and works at phone width.

The relation matrix, which is the acceptance test for "proper cross linking" (a row links to each column marked):

| From \ To | Mission | Agent | Host | Service | CI run | Review | PR | LLM usage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Mission | | yes | yes | | yes | yes | yes | yes |
| Agent | yes | yes (parent, child, handoff) | yes | yes | yes | yes | yes | yes |
| Host | | yes | | yes | | | | yes |
| Service | | yes | yes | | | | | |
| CI run | yes | yes | yes (runner host, if reported) | | | yes | yes | |
| Review | yes | yes | | | yes | | yes | |
| Project (repository) | yes | yes | | | yes | yes | yes | |

"yes" means the relation is derived from an existing key. Where the data to derive a link does not exist yet (a CI run's runner host is reported only by some providers **(guess)**), the cell stays empty rather than guessing.

### 4. Inline references in text

The shared linkifier gains **reference tokens** so agent chat, summaries and mission text link entities without the author writing a URL. A reference is recognised only in a closed, explicit syntax, matched against the entities that exist at render time:

- `session:<id>`, `mission:<id>`, `host:<id>`, `ci:<repo>#<run>`, `pr:<owner/repo>#<n>`;
- a bare `owner/repo#123` is linked to the forge only when `owner/repo` is a known project.

The id charset is bounded; an unknown id stays plain text. The link target comes from `entityHref`, never from the text. This builds on `LINK_SYNTAX_MAX` and the bounded regexes in `conversation-markdown.js`, with the same adversarial-input tests.

### 5. Backlinks and breadcrumbs

- Every detail page shows where you came from (a back link) when the previous hash is a dashboard page, using `history`, not the referrer.
- Entity lists that filter by a related entity (`#/ci?mission=…`) show the filter as a removable chip, so the destination makes the link's context visible.

## Security and privacy

- Operator listener only. A regression test asserts `/api/v1/related`, `entity-links.js` and the new panel module return 404 on the public port.
- No link target is taken from remote text. `entityHref` accepts only the closed kind list, encodes ids, and external forge URLs are built from structured `repository` and number fields, shown with `rel="noopener noreferrer"` and an "external" mark.
- The relations answer carries labels and ids only: no log text, no paths beyond what list pages already show.
- Reference tokens match only existing entities, so a malicious message cannot probe which ids exist beyond what the reader's page already lists.

## Test plan

- `entity-links.js` unit tests: every kind builds the expected hash; unknown kind or empty id gives `null`; ids with `/`, `%`, `:`, quotes and unicode are encoded and round-trip through the router's `decodeURIComponent`.
- Python tests for `/api/v1/related`: each kind's relations from seeded repo data, caps and `more`, `partial` when a source fails, 400 for a bad kind, 404 for an unknown id, operator-only, public-listener 404.
- Linkifier tests: reference tokens link only known entities; look-alikes (`session:` with spaces, overlong ids, `javascript:` after a token) do not; the existing adversarial timing test still passes.
- UI (node unit plus Playwright): from a seeded mission, click through to an agent, its host, a CI run and back, asserting each arrives at the right page; the matrix above is walked by a test that visits one seeded entity per row and checks the expected link groups; 390 px width without horizontal scroll; empty, partial and error states.

## Build plan (PR-sized)

1. **Route table and helper.** `entity-links.js`, route table extracted from `app.js`, tests. No behaviour change.
2. **Relations endpoint.** `control_plane/relations.py`, the route, caps, partial handling, public-404 test.
3. **Related panel.** `renderRelated`, wired into mission, agent, host and service detail; remove the ad-hoc link rows they replace.
4. **CI, review, project and LLM pages.** Panel plus the Projects page links; filter chips.
5. **Inline references** in the shared linkifier, for chat, summaries and mission text.
6. **(Later)** move relations onto the lineage graph once missions, sessions, hosts and CI runs emit lineage events, so deeper "what led to this" traces are possible.

## Decisions needed from the operator

Defaults stand until you answer; each can change on request.

1. **Relations source.** Default: computed from the foreign keys the repo already holds (step 2). Alternative: wait for lineage coverage and use the graph, which gives richer paths but needs the emitters first.
2. **Reference tokens.** Default: an explicit `kind:id` syntax only. Alternative: also recognise bare session ids and run numbers, which links more but risks false positives and probing.
3. **Where "Related" sits.** Default: a panel at the top of detail pages. Alternative: a sticky side column on wide screens (better for 4K), a larger layout change.
4. **Projects page.** Default: add links (repository → missions, CI runs, reviews, PRs). Say if Projects should be reworked more than linked.

## Built notes (2026-10-24)

Built as PRs #237 (helper), #238 (`/api/v1/related`), #239 (panel on the pages) and this one (inline references). Decisions used the defaults above.

What was built:

- `packages/entity-links.js`: `entityHref`, `entityLabel`, `forgePrUrl`. Ids are percent-encoded once, and `' ( ) ! *` are encoded too so an id is safe in any attribute quoting. An id over 300 characters gives `null`.
- `GET /api/v1/related?kind=&id=` (`control_plane/relations.py`), operator listener only; kinds mission, session, host, service, ci, review, pr, project. 400 for a bad kind or missing id, 404 for an unknown entity. Lists are capped at 20 with a `more` count; a source that fails is named in `partial`.
- `packages/related-panel.js` (`renderRelated`, `mountRelated`): on the mission (including a live-only Pi mission, which shows "Nothing else is linked"), agent, host, service and LLM session pages, on the selected CI run, and on a selected project in the Projects page. The older "Related reviews / Related CI" filter links stay as the panel footer. The CI and reviews filter note now links back to the entity it filters by.
- Inline references: `renderMarkdown(text, { references })`. `session:<id>`, `mission:<id>`, `host:<id>`, `ci:<owner/repo>#<run>`, `pr:<owner/repo>#<n>`. The resolver (`packages/entity-refs.js`) links only entities in a lazily loaded index of sessions, missions and hosts; the target comes from `entity-links.js`, never from the text, and a throwing or non-`#/` resolver result stays plain text. `conversation-markdown.js` imports nothing from the dashboard and the public collaboration site passes no resolver, so it never links these. Wired into the agent conversation and summaries in the session workspace.

Deviations and not built:

- **CI runs and pull requests have no detail pages**, so their links go to the filtered list (`#/ci?search=<run>&repository=<id>`, `#/reviews?search=<id>`). A pull request in the panel links to the forge (external mark, `noopener noreferrer`) when the repository and number are structured fields.
- **CI to host** is filled only when the pipeline row reports a `host_id`; none of the current providers do, so the cell is usually empty, as the matrix allows.
- **CI, review and PR relations to sessions** are matched on `repository_id` and branch, since no direct key exists. A session without a repository id is never linked to a CI run.
- **Mission LLM link:** the LLM page has no mission filter, so a mission shows the usage summary without a link; an agent links to `#/llm/session/<id>`.
- **Bare `owner/repo#123`** is not linked (default decision 2: explicit `kind:id` only). The reference index has no CI-run or PR existence check; `ci:` and `pr:` link to the filtered list when the shape is `owner/repo#number`.
- **Breadcrumbs and back links** (design section 5) are not built; the filter note is the only backlink.
- **The route table** lives in `entity-links.js`; `app.js` keeps its own `ROUTES` (the router) rather than importing it. No big-bang rewrite.
- **Relations on the lineage graph** (step 6) not started.
