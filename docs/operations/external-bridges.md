# External bridges (Telegram first)

A bridge is a small process that joins ONE collaboration room as an external agent (`external-agents.md`) and relays chat
between that room and an outside system. It is outbound-only, holds the provider's secret itself, and shows up in the
Rakazo app as an agent bot (tagged "external") and as a messaging installation. The control plane stores no provider
secret and has no provider code.

## Set up Telegram
1. Create a bot with BotFather and save its token in a file only you can read (`chmod 600`). Find the numeric chat id(s)
   that may be bridged.
2. A room owner creates an invite with `post` allowed (dashboard: Tabs > Invite a remote agent, or
   `POST /api/v1/collab/rooms/{room}/externals/invites`). Put the one-time `hcj_...` code in a file or pipe it to stdin;
   it is never an argument.
3. Write the bridge config (YAML only, no environment variables):

       control_plane_url: https://local.metabolomics.us
       state_file: /var/lib/herdr-bridge/telegram.json      # holds the credential: kept mode 0600
       telegram:
         token_file: /etc/herdr-bridge/telegram.token
         chat_ids: [123456789]                              # ONLY these chats are bridged

4. `herdr-eng relay join --config bridge.yaml --code-file code.txt`, then `herdr-eng relay run --config bridge.yaml`
   (run it under systemd; it exits on a 401/403, meaning revoked or expired).

## Behaviour
- Telegram text from an allowed chat is posted to the room as `[telegram] <name>: <text>` (idempotent per update id, so a
  retry never double-posts). Other chats are ignored. The name is a label, not an identity the room trusts.
- Room chat from anyone else is sent to the chat as `user:<id>: <text>`; the bridge's own posts are never echoed back.
- A read-only invite drops inbound Telegram messages (logged) and still relays room chat out.
- Presence: a heartbeat every 30 s (the app shows the bot as running). The secret is rotated daily.
- Revoke from the app (Settings > messaging installations > remove) or the dashboard: the next request is a 401.

## Limits worth knowing
- Rotation does not extend the invite: credentials live at most 7 days (`credential_ttl_seconds`), so a long-running bridge
  needs a fresh invite weekly. The relay logs a warning in the last day. If that is too short, raising the cap is a
  deliberate change to `collab_external.py`, not a bridge setting.
- External agents are chat-only: no tasks, shell, files or terminal. A bridge cannot be told to "do" something.
- A room holds at most 25 externals, and an owner can create 10 invites an hour.
- Not exercised against the real Telegram API yet; the tests use a fake Bot API against a real control plane.

## Adding another provider
Implement `Adapter` (`name`, `poll(timeout) -> list[Inbound]`, `send(text)`) in `herdr_engineering/external_bridge/` and
register it in `config.py`. Voice and desktop hosts fit the same shape: the host does speech-to-text and text-to-speech
itself and exchanges text with the room.
