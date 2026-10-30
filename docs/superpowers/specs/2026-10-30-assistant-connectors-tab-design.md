# Assistant Connectors tab (Gmail, Calendar, GitHub, more later)

Status: design, awaiting approval. Date: 2026-10-30. Owner: the operator (berlinguyinca).

## Why

The personal assistant is useful in proportion to what it can reach. Today Gmail/Calendar access exists only as a
terminal workflow (a Google OAuth client file, `herdr-eng google authorize`, a token file, a deploy-mounted directory).
That is fine for a developer once; it is wrong for something the operator wants to switch on casually, for GitHub and other
apps next, and eventually for family members. The operator wants to open the dashboard, go to the personal assistant,
click **Connectors**, and press **Enable Gmail**, **Enable Calendar**, and so on, with one central place to see and manage
everything the assistant is connected to.

## Goals

- A **Connectors** tab inside the personal-assistant area of the dashboard: one card per app (Gmail, Google Calendar,
  GitHub, later Slack/Notion/...) with status (not connected / connected as X / needs attention), an Enable button that runs the
  provider's own sign-in, and Disconnect.
- Gmail: search and read mail; the assistant can **draft** an email after the operator approves, the draft lands in a
  **Drafts** view where the operator reads it and decides. **Sending is a later feature**, but nothing in the design should make
  it hard (the operator said: "we want sending later and we should be able to see drafts and say send").
- Calendar: read events; **create events after approval**.
- **Blocked calendars:** per person, a list of calendars the assistant must never touch (example given: "don't touch my
  boss's calendar").
- Connections belong to a **person**. Each person connects their own accounts and keeps their own blocked list. What one
  person connects is private to them.
- **Reminders** (added on approval, 2026-10-30): the person can ask the assistant "remind me to X at 3pm" (or "tomorrow
  morning", or a few minutes before a calendar event) and it will. A reminder is stored per person, delivered at its time as a
  phone push notification and as a message in the assistant chat, and listed (with Cancel) in a Reminders list under the
  personal assistant. Calendar events the assistant creates accept a reminder lead time ("10 minutes before"). *The
  operator said only "please add reminders as well"; this reading is the designer's guess (assistant reminders delivered
  through the push work already live, plus event reminder offsets). Not included unless asked: Google Tasks, recurring
  reminders, snooze.*
- One generic connector model so adding an app means adding a provider, not a new page and new plumbing.

## Reminders: behaviour

- Created by the assistant tool `set_reminder {text, when}` (not sensitive: it only notifies the person themself, so no
  approval card), listed by `list_reminders`, removed by `cancel_reminder`. `when` is resolved in the person's timezone
  (assistant notification settings already carry one); an unclear time makes the assistant ask rather than guess.
- A server-side scheduler fires due reminders once (a missed one, after a restart, fires late and says so), sends the push
  (title "Reminder", the text, tap opens the assistant chat) and adds the message to the chat. Quiet hours do not hold back a
  reminder the person asked for by time.
- Stored in Postgres with the person key, fire time, state (pending/fired/cancelled). Reminders never contain credentials.
- No Google account is needed for reminders; they work before any connector is enabled.

## Non-goals (for this version)

- Sending email. (Designed for; not built.)
- The phone app. The Rakazo app has not been forked yet; native Connectors/Drafts screens are a separate future spec
  (`future/2026-10-30-phone-app-fork-connectors-and-drafts.md`). The connector service must be a plain HTTP service the
  fork can later call.
- Family members connecting accounts through the dashboard (see "The one hard limit" below).
- Deleting or editing mail, changing or deleting existing events, sharing calendars.
- Hosting a third party's tokens (Composio and similar). Tokens stay on the operator's machine.

## Anti-goals (a failure even if it technically works)

- Any token, client secret or refresh token appearing in a page, a log line, an API response, a tool result, git, or
  chat.
- The assistant reading or touching a blocked calendar, even once, even by accident (for example through a "list all
  calendars" call).
- An email draft or a calendar event created without the operator's approval, or a draft sent without an explicit Send.
- Mail or calendar text being obeyed as instructions (it is written by other people).

## Constraints

- The dashboard is served open on the tailnet (single-operator mode: the open listener acts as the operator). There is
  no per-person sign-in on the dashboard.
- Google requires an OAuth client created in Google Cloud once, and requires the redirect address to be HTTPS on a public
  top-level domain (not an IP). The valid-certificate dashboard address is `https://local.metabolomics.us`
  (tailnet-only, reachable from the operator's browser); the callback is
  `https://local.metabolomics.us/api/v1/connectors/google/callback`.
- Config is YAML only, no new environment variables. Secrets never go to GitHub.
- The dashboard has visual-regression and accessibility gates; a new tab must ship with its baselines and pass the
  accessibility ratchet.
- Existing building blocks: assistant tools and grants, approval cards (dashboard and phone), `draft_email`, collaboration rooms,
  the GitHub App (shared credential), and the read-only Google code already merged (#502, #503).

## What the operator decided

1. Location: dashboard, Personal assistant, a **Connectors** tab; click Enable per app.
2. Gmail and Calendar are needed with write actions after approval (drafts, events), not read-only.
3. Blocked calendars are required.
4. Connections are per person.
5. Sending later; drafts are viewable and sendable by the operator; a room-like place for drafts was floated
   ("maybe have a room").
6. Phone app: not now. Dashboard only. The fork is planned, not started.

## The one hard limit, stated plainly

Google makes the operator create the "app" (OAuth client) once in Google Cloud. Everything after that happens in the
dashboard: the operator pastes the client id/secret into a Connectors settings form once, then Enable/Disconnect.

Per-person connections need a way to know *who is clicking*. The dashboard cannot tell the operator from a family
member (open listener). So in this version the dashboard manages **the operator's own** connections; the data model keys
every connection by person so family members can connect through their own signed-in surface later (the phone app fork,
or a dashboard login), without a data migration.

## Design decisions made by the designer (the operator did not choose these; change any)

- **Storage:** connection credentials (client secret, refresh tokens) are encrypted at rest (authenticated symmetric
  encryption via the `cryptography` package already installed) in Postgres, with the key in a root-readable file named in
  YAML and mounted read-only. Never in YAML values, never returned by any endpoint (only "connected as ..." and scopes).
- **Scopes requested:** Gmail `gmail.readonly` plus `gmail.compose` (the narrowest scope that can create drafts; Google
  bundles "send" into it, so *our code* is what enforces no sending until the feature exists); Calendar `calendar.readonly`
  plus `calendar.events`. Requested together at Enable time so the operator is not sent back to Google twice.
- **Blocked calendars:** the assistant cannot see a blocked calendar at all: server-side filter by calendar id on every
  read and write, and the calendar list the model sees omits them. Default calendar is the person's primary one. A blocked
  id is also refused if the model supplies it directly.
- **Approvals:** reuse the existing approval cards (dashboard and phone) for "create draft" and "create event"; the
  approval shows the exact content. Tools that write are sensitive and never pre-authorized.
- **Drafts view:** a Drafts list under the personal assistant: each draft shows recipients, subject, body, "Open in Gmail",
  Discard; **Send is present but disabled** with "coming soon". (A collaboration-room rendering is a later option.)
- **Result handling:** mail/calendar text returned to the model is marked as untrusted data, as in the merged read-only
  implementation.
- **GitHub card:** v1 is a read-only status card for the existing Herdr GitHub App and personal token (which installations,
  which permissions, what is missing). Enable/Manage for GitHub is a link to GitHub's own install/permissions page, not a new
  OAuth flow.
- **Provider interface:** `id`, `name`, `scopes`, `connect_url(person)`, `handle_callback(person, params)`,
  `status(person)`, `disconnect(person)`, `tools`. Google is the first provider; GitHub is a status-only provider.
- **Migration from #503:** the file-based token path is replaced by the store; the mounted directory can be removed once
  the new flow ships (builder's call on keeping it as a fallback for one release).

## Answers to questions raised by the builder check

Settled from the codebase and conversation:

- **Who is the person?** The assistant already identifies a person as `(tenant_id, user_id)`. The dashboard operator is
  `default/open-listener`; each phone sign-in is `default/rakazo:<hash>`. A connection is keyed by that pair, so a tool
  call resolves to exactly the calling user's connection, never another person's. No new people table.
- **Existing assistant machinery:** tools are declared in `TOOL_CATALOG` (assistant.py) with a `sensitive` flag; grants and
  approvals already gate sensitive tools; `draft_email` exists and today saves a draft in the assistant's own store, not in
  Gmail. The builder should confirm and decide whether Drafts reads Gmail's drafts folder (recommended: Gmail is the source of
  truth, so Discard and Open in Gmail work and a later Send acts on the real draft) or the local store.
- **Callback host and OAuth safety:** the valid-certificate address `https://local.metabolomics.us` already fronts the
  dashboard. Enable must be started from that address (Google rejects any other redirect). The callback is reachable
  without a signed-in user, so it is protected by an unguessable one-time `state` stored server-side with the person and an
  expiry, plus PKCE; a callback without a matching state is refused.

Designer's calls (the operator did not choose these):

- **Blocked calendars, precisely:** blocking is by the calendar's real id. The alias `primary` is resolved to its real id
  before comparing, so a person can block their primary calendar. One filter function is the single choke point used by list,
  read, free/busy and create. Free/busy ("find me a slot") excludes blocked calendars. Events on an *allowed* calendar are
  shown even if they mention people from a blocked one. A blocked id supplied by the model is refused with a generic error.
- **Encryption key:** a new read-only mount like the existing secret directories, created by `deploy.sh`. If the key file is
  missing or wrong, the Connectors tab says "not set up" and nothing starts failing; no connection can be saved or used.
  Key rotation is out of scope for this version.
- **Google "Testing" apps lose refresh tokens after 7 days.** The tab must treat "needs attention / sign in again" as a
  normal, one-click state, not an error. Recommended for the operator: after creating the OAuth client, set its publishing
  status to "In production" (an unverified personal app shows a warning screen once but its tokens do not expire weekly).
  Choosing Testing instead is acceptable if weekly re-sign-in is tolerated.
- **Gates:** the tab's states (not set up, not connected, connected, needs attention, settings form) are built first, then
  the visual baselines and accessibility ratchet are recorded in the same change.
- **Existing #503 mount:** nothing in production uses it yet (no token was ever created), so no migration is needed;
  removing it is part of this change.

## What the builder decides

Table and endpoint shapes, the exact UI layout within the established card style, test strategy, how the settings form
for the client id/secret is presented, polling vs push for status, naming.

## Acceptance (what "done" means)

An operator with a Google OAuth client created can, entirely from the dashboard: paste the client once, click Enable Gmail
and Enable Calendar, approve at Google, see "connected as <address>"; ask the assistant to find an email and a free slot; have
it draft a reply that appears in Drafts after approval; have it propose an event that is created only after approval;
add a calendar to the blocked list and see the assistant fail to find or touch it; click Disconnect and see the tokens
gone. No secret appears in any response or log. The tab passes the dashboard accessibility and visual gates.
