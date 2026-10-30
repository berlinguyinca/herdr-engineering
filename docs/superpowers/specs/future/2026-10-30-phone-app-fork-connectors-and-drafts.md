# Future: our own phone app (fork of Rakazo), Connectors and Drafts on the phone

Status: idea, not scheduled. Date: 2026-10-30. Depends on:
[the assistant Connectors tab](../2026-10-30-assistant-connectors-tab-design.md).

## Context

The operator plans to fork the official Rakazo app and make it theirs. The fork has not been cloned yet. Today the phone
talks to the Herdr bridge as an unmodified custom-server client, which limits what it can show (no push through the
official app's project, no Connectors or Drafts screens).

## What this would add

- A native **Connectors** screen: the same cards, Enable/Disconnect, and blocked-calendar list as the dashboard tab,
  using the connector service's plain HTTP endpoints. Enable opens the provider's sign-in in the system browser and returns
  to the app.
- A **Drafts** room: email drafts the assistant prepared, shown for reading, edit requests, and an explicit **Send**
  (sending ships here or earlier once the operator turns it on).
- **Per-person connections for family members**: each signed-in bridge account connects their own apps, which the
  dashboard cannot offer because it has no per-person sign-in.
- **Our own push**: with our own Expo/EAS project and credentials, notifications no longer depend on the official app's
  project (today's push relies on the official app's token and may be rejected if that project enforces enhanced push
  security).
- Branding, server address baked in, no official-server screens.

## Open questions for when this starts

- Fork or thin wrapper? How do we track upstream changes?
- Apple developer account and distribution (TestFlight vs ad hoc).
- Whether the bridge should keep supporting the official app alongside the fork.
