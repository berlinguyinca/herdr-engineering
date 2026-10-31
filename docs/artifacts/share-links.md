# Artifact share links

`herdr-eng artifacts share` turns one pinned version of a private artifact into a link that someone outside the
registry can open. Nothing is exposed until the owner approves it, and approval shows exactly what will be exposed.

## Audiences

- **private** (default): no share exists; only the registry's normal access rules apply.
- **lab**: the link opens only after Cognito sign-in, and only for the emails and groups on the allow-list
  (`--email`, `--group` or `--room`, at least one required; `--room ROOM` adds the sign-in emails of the people in that room as they are when you create the share, nobody who joins later). Recipients see the Cognito sign-in page, then the artifact.
  They need a Cognito account; `--check-pool` warns about emails that have none (never blocks). Emails must be
  ASCII. A sign-in lasts until the recipient's Cognito access token expires (about 1 hour with the pool's
  defaults; `control_plane.share.session_seconds`, default 4 h, is only an upper cap), then they open the link
  again and sign in. A link pasted without its trailing slash (`/s/<token>`) is redirected to `/s/<token>/`.
- **public**: anyone with the link. No allow-list is accepted. Public links expire after 30 days unless
  `--expires-in-days` says otherwise. The server applies the default (30 days, configurable as
  `control_plane.share.default_public_days`), and a public share may not expire later than 90 days from
  creation. Lab shares accept 1 to 3650 days.

The URL is a **bearer secret**: whoever holds it can open the share (for lab, they must also sign in as an allowed
recipient). Do not paste it in public channels you do not mean it for. It is printed once, at approval.

## Flow

```
$ herdr-eng artifacts share create A1 V3 --audience lab --email bob@example.org --expires-in-days 14
Share share_3f9a1c07b2e48d6150aa9c3e (pending) of artifact A1 version V3
  audience: lab
  allowed: bob@example.org
  entry: index.html
  exposes: 41 files, 3.2 MiB
  expires: 2026-11-13T09:00:00+00:00
  WARNING: index.html references https://cdn.example.com/x.js
Not live yet. Review, then: herdr-eng artifacts share approve A1 share_3f9a1c07b2e48d6150aa9c3e

$ herdr-eng artifacts share approve A1 share_3f9a1c07b2e48d6150aa9c3e
(prints the same summary)
Type 'yes' to expose exactly the files above: yes
Share is LIVE. Anyone holding this link can open it; treat it like a password.

    https://.../s/<token>

This link will not be shown again. ...
```

Commands (all take `--control-plane` and `--api-key-env`, the NAME of the variable holding the key, default
`HERDR_API_KEY`; the key is never printed):

| Command | Purpose |
| --- | --- |
| `share create ARTIFACT VERSION --audience lab\|public [--email E]... [--group G]... [--room ROOM] [--expires-in-days N] [--entry PATH] [--title T] [--check-pool [--pool-id ID \| --fleet-config FILE]] [--json]` | Make a pending share and print its exposure summary. |
| `share show ARTIFACT SHARE_ID [--json]` | Re-print the share and, while pending, the current exposure summary. |
| `share approve ARTIFACT SHARE_ID [--yes] [--json]` | Print the summary, confirm, go live, print the URL once. |
| `share list ARTIFACT` | JSON list of the artifact's shares. |
| `share events ARTIFACT SHARE_ID [--limit N]` | JSON audit events (newest N). |
| `share revoke ARTIFACT SHARE_ID` | Kill the link now. Repeating it is harmless. |

Everything the server sends (file names, warnings, errors) is shown with control and bidi characters escaped
(`\x1b`, `\u202e`) and long values cut off, so artifact content cannot redraw the terminal. With `--json` on
create, show, approve and revoke, stdout is exactly one JSON document (on approve the link is its `url` field)
and the human summary and prompts go to stderr; `list` and `events` always print JSON. Token fields are never printed.

Approval needs `--yes`, or a typed `yes` on a terminal. With no terminal and no `--yes` the command refuses
(exit 1) and sends nothing; it never auto-confirms. Exit codes: 0 ok, 2 usage or validation error (nothing sent),
1 server or transport error (the server's short message is shown; a 409 means already approved or revoked, a 410
means the share expired).

## What is exposed

A frozen copy of the version's file bundle, taken at approval. Later edits to the artifact do not change a live
share. The bundle should be self-contained: no external CDNs or remote assets (the summary warns about references
it finds, and they will not load). Scripts run sandboxed. A PDF is served as is. Approval fails if the version
changed in a way that breaks the summary (`show` reports why).

## Limits

- 512 MiB and 20,000 files per share.
- 2 GiB of live share bytes in total across the server.
- Expiry: lab 1 to 3650 days; public defaults to 30 and is capped at 90. Expired shares stop working and cannot be approved.

## Revocation

`share revoke` removes the link's state and bundle immediately. The audit trail (`share events`) stays.
Revoke when a link is pasted somewhere it should not be, then create a new share.
