# Public artifacts side: library feed, recipient page, agent push

Date: 2026-11-05. Status: draft for approval. Owner: Gert Wohlgemuth.

## Intent

`artifacts.local.metabolomics.us` is the public face of Herdr artifacts. The dashboard stays inside
Tailscale. The public side is a separate container (share-public / collab-public) behind the edge.
It serves lab members (Cognito group `local.dev`) and anyone holding a secret share link.

Today the public pages are a separate minimal style, there is no place for a member to see "my artifacts",
and an opened public link is forgotten. This design gives each person one library, makes the public pages
look like the dashboard, and lets agents connected to `mcp.local.metabolomics.us` push artifacts into it.

## Goals

1. A signed-in member sees one feed of every artifact available to them:
   - **Assigned**: shared with them (lab group or direct share).
   - **Opened via link**: public links they opened while signed in (claimed).
   - **Mine**: artifacts they own, including agent-pushed ones that they accepted.
2. Opening a public link while signed out works with no account, and reminds the visitor to sign in to
   claim it into their library.
3. All public pages (library feed, recipient landing page, sign-in step) match the dashboard style.
4. Agents connected to `mcp.local.metabolomics.us` can create and publish artifacts for their member.
   Pushed artifacts wait in a **Pending** state until the member accepts them.

## Non-goals

- No dark mode on the public side (decided: light only, like the dashboard).
- No thumbnails or card grid (layout C rejected).
- Claiming a link grants no extra access. Visibility is still decided by the share (audience, expiry, revocation).
- No change to the share model from PR #631 (lab audience, public secret token, owner approval, 30-day default
  expiry, max 90, read-once semantics unless the builder finds this must change).
- The dashboard is not changed.

## Anti-goals

- An agent push must never make anything visible to anyone but its owner. No auto-sharing, no auto-public.
- A signed-out visitor must never see another person's library or any list of artifacts.
- No external requests from the pages (CSP stays `default-src 'none'`-style; fonts and CSS are same-origin).

## Constraints

- CSP on the member pages is strict (`default-src 'none'`, X-Frame-Options DENY). Keep it strict.
  Inter is bundled on our own host with a system-font fallback. No CDN.
- Design tokens come from `packages/herdr-design-system/theme.css` (background #f7f7f8, surface #fff,
  border #d9d9e3, text #353740, secondary #6e6e80, accent #10a37f, 8px radius, Inter, JetBrains Mono).
  Reuse the file or copy the light tokens into share_static; do not fork values by hand.
- Existing code: `herdr_engineering/control_plane/share_static/` (viewer.html, view.html, share.js, share.css,
  lab-signin.js with PKCE), `packages/member-artifacts-app.js`, `member-artifacts-view.js`,
  `member-artifacts.css`, served by `member_web.py`. Member tools: `collab_member_tools.py`
  (`artifact_create`, `artifact_upload_chunk`, `artifact_publish`, `artifact_delete`).
- Repo rules: TDD, no mocks, real dependencies, E2E only through `npx playwright test`. Four test layers.
  80%+ coverage.

## Decided parts

### Library feed (layout B)

- One list, newest first. Each row: title, who shared it, when, source badge (Assigned / Opened via link /
  Mine), and for opened-via-link rows the expiry.
- Filter chips: All, Assigned to me, Opened via link, Mine. A Pending chip appears only when something is pending.
- Search box over titles and sharer names.
- Expired or revoked items stay in the feed, greyed out with an "Expired" or "Revoked" tag. They show title,
  sharer and date only. No content, no open action.
- Header shows the signed-in identity and a Sign out action.

### Recipient landing page

- Slim top bar: "Shared by NAME", artifact title, time left, and a "Sign in to keep this" button when signed out.
- Signed out: a notice under the bar: "Sign in with your lab account to add this to your library. Signing in
  does not change who can see it."
- Signed in: the bar only; opening the link records it in the library as Opened via link.
- The artifact content renders below, unchanged. The notice is not a wall; it never blocks viewing.

### Sign-in step

- Centred card: "Sign in to Artifacts", one line saying shared links need no account, one button
  "Continue with lab sign-in" (existing Cognito PKCE flow in `lab-signin.js`).
- After sign-in the visitor returns to where they were (the link or the library).

### Agent push

- Agents connected to `mcp.local.metabolomics.us` use the member tools to create, upload and publish.
  The artifact is owned by the member the agent belongs to.
- A pushed artifact is created in state **Pending**. It is visible only to its owner, under a Pending chip,
  with Accept and Discard actions. Accepting moves it to Mine. Discarding deletes it.
- Sharing is a separate, later, human action through the existing share intents and owner approval.
- Pushing is bounded by the existing member limits (size, count, rate).

## Left to the builder (my calls, mark if changed)

- Badge colours and exact wording (the mockup is a guide, not a spec).
- Whether to import the dashboard token file or generate a copy at build time.
- How "claimed" links are stored (a library table keyed by member and share).
- Whether Pending is a new artifact state or a flag on the existing record.
- Pagination or infinite scroll for long feeds.

## Open prerequisites to confirm

- The member tools are behind the members feature, which is OFF in the current deploy. Agents on mcp.local
  need it ON or a narrower switch for agent push only. Enabling it has its own config prerequisites
  (share and collab_artifacts enabled, api_keys set).
- The agent's identity must map to a Cognito member so ownership in the feed lines up.
- mcp.local.metabolomics.us does not serve yet: PR #645 (edge server_name) is unmerged.

## Scope of delivery (decided by the user)

Done means implemented, deployed, integrated in the Herdr dashboard, and hosted under the right URLs:

- **Dashboard (Tailscale only, `bender.tail0c50da.ts.net:9447`)** gets the same functionality as the public library:
  the unified feed with source badges, search and filters, folders (create, rename, delete, file), the Pending
  inbox for agent pushes (accept or discard), claimed links, and sharing. It uses the dashboard's own sign-in and
  design system. It is never routed publicly.
- **Public side (`artifacts.local.metabolomics.us`)** serves the restyled library, recipient page and sign-in step
  through the share-public / collab-public containers behind the edge.
- **Agents (`mcp.local.metabolomics.us`)** push artifacts and search the index. This needs PR #645 merged, the edge
  redeployed, the members feature switched on with its prerequisites, and agent configs repointed from `localdev.mcp`.
- **Assistant filing** is delivered too, as the second phase with its own design pass after phase 1 is live.
- Phase 1 (library, link claiming, search, filters, manual folders, agent push with Pending, dashboard
  integration) ships and deploys first. Phase 2 (suggest, then auto-file) follows.
- Merges and deploys stay behind the user's go-ahead, as before.

Both surfaces use the same backend services, so the dashboard and the public library cannot disagree.

## Search, filter and folders (this project)

- Search covers title, sharer, folder names and discovered metadata (file type, size, dates, project, instrument,
  batch where the artifact carries them). Filters combine with the source chips.
- Folders are personal and per member. An artifact can be in several folders at once: a folder is a view, not a
  place, so filing never copies, moves or re-shares content and never changes who can see an artifact.
- Members create, rename and delete folders and file artifacts into them by hand. Deleting a folder deletes no artifact.
- Folders may be topic-based or metadata-based; both are the same object. Automatic filing is the next project.
- The agent harness can search the same index the search box uses, through a member MCP tool (builder to specify).

## Second project, not built here: assistant filing

Recorded so the first project leaves room for it. Design it separately after the library ships.

- The assistant suggests categories and folders for each artifact. The member accepts or corrects each
  suggestion. Those accepts and corrections are the learning data.
- Once a measured bar is met (my guess: the member's recent acceptance rate), the assistant files automatically.
  Every automatic filing stays visible, explained and undoable, and never changes visibility.
- Learning is per member (confirmed by the user). A lab-wide taxonomy is a later question.
- It follows the repo's assistant pattern: the assistant runs with the caller's own permissions and only proposes;
  nothing executes without the owner's say-so (see `control_plane/intel_assistant.py`).
- Data model rule for this project: keep artifact-to-folder links as rows with a source field
  (manual now, suggested and automatic later), so the second project needs no migration of meaning.

## Facts found in the code (read-only check)

- Public share links are not read-once: only the URL shown to the owner is shown once. A live link can be re-opened
  by token (`share_public.py`). Lab shares need a Cognito session against the allow-list (`share_lab.py`).
- No member route serves artifact bytes by id. `member_web.py` offers head, history and shares metadata only.
  The library needs a new signed-in view route.
- `MemberService.mine` (`GET /api/member/me`, collab-public) lists owned artifacts, direct ACL shares
  (`shared_with_me`) and links the member created. It does not list lab-audience shares, so Assigned needs a new listing.
- An agent belongs to a member through the agents feed owner (`CollabService.member_check`); no new identity work.
- There is no pending state on artifacts today. Per-member count and byte limits default to unlimited. No pending cap.
- This repo has no coverage gate or CI workflows, and `member-artifacts-app.js` has no test file.

## Calls made while writing (change any you disagree with)

- Claim: signing in while viewing a link records it automatically; no extra click. The record snapshots title and
  sharer at claim time; expiry and revocation are read live. A revoked row shows only title, sharer, date.
- Library opens a claimed item through the new signed-in route, authorised by the share's live state, not by
  replaying the token. The token never goes into library storage.
- Sign-in return path: the link path is held in sessionStorage across the Cognito redirect, not in a query string.
  Needs the security review (opus, approved for security reviews).
- Dedupe: one row per artifact; badge priority Mine, then Assigned, then Opened via link.
- Pending: applies to artifacts created through agent (MCP) tools only; a human creating one in the web page skips it.
  An agent may leave at most 25 Pending per member; further pushes are refused with a clear message. Pending items
  never expire; only the owner accepts or discards. Pending items count toward the member's limits.
- Feed data comes from collab-public (the existing member web); no new container.
- Tests cover all new code in the four layers; this project does not add a repo-wide coverage gate (flagged for you).

## Success

A lab member signs in, sees their assigned, claimed and own artifacts in one feed, opens a public link in a
private window and is invited to sign in, and an agent push lands as Pending that only the owner can see.
