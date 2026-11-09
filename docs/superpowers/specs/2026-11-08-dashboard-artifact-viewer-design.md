# Dashboard artifact viewer: open an artifact and see it first

Status: draft for approval. Amends `2026-11-06-artifact-host-app-design.md`: this reverses its "dashboard is read-only,
no write path" (two narrow writes are added: create-share and revoke-share) and its "untrusted HTML is inert in the dashboard" (scripts now run, in an
opaque-origin sandbox).

## Why

Operators open artifacts in the dashboard to investigate and share them, the way Claude shows an artifact. Today
`/library` item pages show metadata with an inert, script-free preview. The artifact itself, running, should be the first
thing seen, with sharing at hand.

## Goals

1. Opening an artifact in the dashboard shows the rendered artifact first, behaving exactly as on the public share page
   (scripts run). No second sign-in, no prompt, no token visible to the operator.
2. A slim top bar over the artifact: back to library, title, version picker (latest by default), share, open public
   page, download, fullscreen. Metadata (owner, folders, topics, share list, version history) lives in a drawer, closed
   by default.
3. Fullscreen button: fullscreens the frame container, Esc exits; the artifact stays sandboxed.
4. From the dashboard the operator can make an artifact public, private or group-shared, as in the public view. The
   share dialog shows the current state (private, group or public) and the active links, and lets the operator change
   it. A new share is live immediately (no owner approval) and appears in the owner's member page like any other share;
   a share the operator revokes appears there as "revoked by operator", so the owner is not surprised.

## Non-goals / anti-goals

- The write surface is exactly two routes on the internal listener: create-share and revoke-share (below). No edit,
  delete, filing or approve from the dashboard; those stay on the member page. The operator cannot approve a pending
  share, cannot alter a share's audience, lists, expiry or token after creation, and cannot see an existing token.
- No path for artifact script to reach the dashboard origin, its cookies, storage, DOM or APIs. A flaw here must expose
  at most the artifact being viewed.
- No change to the public face or to member sign-in. No dark mode.

## Design (decisions made with the owner are marked; the rest are my calls, change any)

- **Frame (owner: same as public).** Iframe `sandbox="allow-scripts allow-downloads"`, never `allow-same-origin`, so the
  artifact runs in an opaque origin, as on the public page.
- **Content policy (my call).** The internal listener serves artifact bytes with the same sandbox CSP the public `/s/`
  pages use (`share_http.sandbox_csp`), instead of today's script-free `CONTENT_CSP`. The cp-web proxy accepts exactly
  that policy shape (sandbox with only `allow-scripts allow-downloads`, no other `allow-*`) and still 502s on anything
  else, and on missing nosniff or type.
- **Authenticating subresources (my call).** An opaque-origin frame sends no cookies, so its CSS/JS/images cannot use
  the operator's session. Opening an artifact makes the dashboard mint a signed, short-lived (minutes) content URL for
  that one item and version, and the frame loads from under that path (same idea as public `/s/<token>/`). The operator
  never sees it; an expired URL makes the frame reload quietly. The signing key lives in cp-web, is never sent to the
  host's public role, and the URL grants read of that item/version only.
- **Share states (owner: public, private or group, "just like in the public view").** The host's share audiences are
  `public` and `lab`; a `lab` share is limited by `allow_emails` and `allow_groups` (`herdr_artifact_host/share_store.py`,
  `migrations/0001_init.sql`). The dashboard maps them as: public = a live `public` share; group = a live `lab` share;
  private = no live share. Changing state is revoke plus create (a new token); the dashboard never mutates a share's
  audience or lists in place.
- **Create-share (owner: dashboard creates, operator publishes directly).** POST for (item, version) with `audience`
  (`public` or `lab`), `allow_emails`, `allow_groups` (lab needs at least one; public takes none; emails and groups are
  validated exactly as member shares validate them), and expiry (default 30 d, max 90). The share is created on the
  owner's behalf as live immediately, no owner approval, and an event records the dashboard operator as the actor.
  Share tokens never come back in any list/read response; only the create response returns the new link once.
- **Revoke-share.** POST for (item, share) revokes any live share on that artifact, whoever created it (owner, agent or
  operator). It sets the share revoked, records the operator as the revoker and writes a `revoked` event whose actor is
  the operator and whose detail reason is `operator`. It cannot revoke a pending share, un-revoke, or touch any other
  column. Making an artifact private revokes each live share through this route.
- **Both write routes** take the one separate write key (never the read key, so a leaked read key can neither create nor
  revoke), connect as the `operator_share` database role, and are the only non-GET routes. The relay allows exactly
  these two POST paths; everything else stays GET-only.
- **Dialog (owner).** Opening the share dialog shows the artifact's current state (private, group or public), the active
  links with audience, group/email lists, expiry and creator (never a token or URL; existing links are known only by
  metadata, a link is shown once when created), and lets the operator choose public, private or group. Group needs
  at least one group (picked) or email (free text).
- **Group picker source (needs verification by the builder).** A read-only internal GET route returns the group names the
  host knows so cp-web can offer them. From the code read for this amendment: the host's directory is the synced
  `members.json` (`DirectorySnapshot`, `herdr_artifact_host/cognito.py`) whose `groups` object is keyed by group name, and
  `members_config.group` names the one required group; `share_lab` matches a share's `allow_groups` exactly and
  case-sensitively against the Cognito `cognito:groups` claim. It is NOT yet verified that `members.json` lists more
  than the one required group, nor that the internal container mounts that directory. The builder must check both; if
  the directory has only the required group or is not mounted, the route returns the union of the required group and every
  group already used in `shares.allow_groups` (a database read), and the dashboard still accepts a free-text group
  name validated like member shares, with a plain note in the UI that unknown names match nobody.
- **Member page.** A share revoked by the operator shows as "revoked by operator" in the owner's share list and the
  owner can still see and revoke their own shares as before.
- **Tests that must change.** The GET-only internal-API tests and the relay tests that pin it widen to exactly two POST
  routes (and one new GET route); the proxy CSP tests change from "pure sandbox" to "exactly the public sandbox shape".

## What a flaw would cost

Untrusted artifact script would run next to the operator dashboard. The opaque origin contains it; the signed URL limits
what it can fetch to one artifact. A leaked write key could revoke every live share (a denial of service for owners,
not a disclosure) or publish any artifact with a ready version; the `operator_share` role is the second limit: it can
insert shares and events and flip a live share to revoked, nothing else, enforced by column grants and a trigger. I would have Opus 5.5 review this before it ships, as with the host.

## Left to the builder

Bar visuals and drawer layout (follow `library-view.js` conventions, light only), exact endpoint and header names, the
signing scheme (HMAC with expiry and item/version in the payload is enough), key rotation, and how fullscreen degrades
where the Fullscreen API is unavailable.

## Settled during the builder check (my calls, change any)

- **Database role.** `operator_share` has INSERT on `shares` and `share_events`, UPDATE on only the columns `state`,
  `revoked_at`, `revoked_by` of `shares` (new `revoked_by` column), SELECT where the rules need it, no DELETE. A trigger
  allows only the transition live -> revoked, so UPDATE on `state` cannot approve, revive or un-revoke.
- **Operator identity.** The actor recorded is the dashboard operator as cp-web knows them (the signed-in dashboard user,
  not a shared label). Sharing works for any item with a ready version; it is refused for purged or retracted versions,
  removed members' items and items with no ready version.
- **Content route.** Same dashboard origin. A dedicated path prefix, verified by signature and exempt from session auth,
  covers the whole bundle by prefix so multi-file artifacts with relative references load. Negative tests: tampered,
  expired, wrong item, wrong version, path traversal, and the exemption does not reach any other route.
- **Bar states.** "Open public page" is disabled when the item has no live public share. "Share" opens the dialog
  described above (expiry default 30 d, max 90); after create the result link is shown and copied.
- **Download.** The version's primary file through the existing GET content route; no new route.
- **Old inert preview.** Replaced. The script-free `CONTENT_CSP` path and its proxy rule are removed, not kept alongside.
