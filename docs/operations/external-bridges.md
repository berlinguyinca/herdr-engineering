# External bridges

A bridge is a small outbound-only process that joins ONE collaboration room as an external agent (`external-agents.md`) and
relays chat between that room and an outside system. It holds any provider secret itself; the control plane stores none and
has no provider code. In the Rakazo app the bridge shows up as an agent bot (tagged "external", running while its heartbeat is
fresh) and as a messaging installation that can be listed and revoked.

No provider adapter ships yet. What exists is the generic part, in `herdr_engineering/external_bridge/`:
- `client.py`: `BridgeClient` joins with the invite code, sends heartbeats, reads room chat from a cursor, posts
  (idempotent per key) and replaces its secret daily. State, including the credential, is one file kept mode 0600.
- `relay.py`: `Relay` moves messages both ways through an `Adapter` (`name`, `poll(timeout) -> list[Inbound]`, `send(text)`),
  never echoes its own posts, drops inbound messages when the invite is read-only, and stops on a 401/403.
- `config.py`: YAML only (`control_plane_url`, `state_file`) and `read_secret()` for provider tokens kept in 0600 files.

## Join
A room owner creates an invite (dashboard: Tabs > Invite a remote agent, or
`POST /api/v1/collab/rooms/{room}/externals/invites`), with `post` allowed if the bridge should speak. Then:

    herdr-eng relay join --config bridge.yaml --code-file code.txt      # or pipe the code on stdin; never an argument
    herdr-eng relay status --config bridge.yaml

where `bridge.yaml` is `control_plane_url: https://...` and `state_file: /path/state.json`.

## Limits
- Rotation does not extend the invite: credentials live at most 7 days, so a long-running bridge needs a fresh invite weekly.
  The relay logs a warning in the last day. Raising the cap is a deliberate change in `collab_external.py`.
- External agents are chat-only: no tasks, shell, files or terminal, and nothing from before they joined.
- A room holds at most 25 externals; an owner can create 10 invites an hour.

## Adding a provider
Write an adapter for the system you want (it decides which outside senders are allowed and how text is delivered), build
`Relay(BridgeClient(state_file), adapter).run()` around it, and keep its secrets in files read with `read_secret()`.
