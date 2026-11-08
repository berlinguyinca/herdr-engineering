# Dashboard artifact viewer: open an artifact and see it first

Status: draft for approval. Amends `2026-11-06-artifact-host-app-design.md`: this reverses its "dashboard is read-only,
no write path" (one narrow write is added) and its "untrusted HTML is inert in the dashboard" (scripts now run, in an
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
4. The dashboard operator can create a share link and it is live immediately (no owner approval). It appears in the
   owner's member page like any other share.

## Non-goals / anti-goals

- No other dashboard write: no revoke, delete, filing, approve, edit. Those stay on the member page.
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
- **Share creation (owner: dashboard creates, operator publishes directly).** One new endpoint on the internal listener,
  POST create-share for (item, version) with the expiry rules of member shares (default 30 d, max 90). It is the only
  non-GET route and takes its own write key, separate from the read key (a leaked read key cannot create a share).
  The share is created on the owner's behalf as live, and an event records the dashboard operator as the actor. The
  relay allows exactly this one extra POST path; everything else stays GET-only. Share tokens still never come back
  in any list/read response; only the create response returns the new link once.
- **Tests that must change.** The GET-only internal-API tests and the relay tests that pin it widen to exactly one POST
  route; the proxy CSP tests change from "pure sandbox" to "exactly the public sandbox shape".

## What a flaw would cost

Untrusted artifact script would run next to the operator dashboard. The opaque origin contains it; the signed URL limits
what it can fetch to one artifact. I would have Opus 5.5 review this before it ships, as with the host.

## Left to the builder

Bar visuals and drawer layout (follow `library-view.js` conventions, light only), exact endpoint and header names, the
signing scheme (HMAC with expiry and item/version in the payload is enough), key rotation, and how fullscreen degrades
where the Fullscreen API is unavailable.
