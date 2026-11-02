# herdr-terminal-relay

Host side of the Shepherd terminal. It mirrors herdr panes to the phone through the control plane and types the phone's
input back. It makes outbound calls only: no inbound port, no ssh, no pty.

Per host:

1. herdr must be running as the same user (the relay talks to `~/.config/herdr/herdr.sock`).
2. `~/.config/herdr-engineering/host.json` must exist (`herdr-eng fleet join`); the relay uses its control plane URL and
   the host's ingest key.
3. On the control plane, that ingest key must be listed for this host's own id in `control_plane.architecture.agent_hosts`.
   Without it the relay gets 403 and backs off to one try per 30 s, logging once a minute.
4. Install one unit and start it as the user that runs herdr:
   - Linux: copy `herdr-terminal-relay.service` to `~/.config/systemd/user/`, then
     `systemctl --user enable --now herdr-terminal-relay`.
   - macOS: copy `com.herdr.terminal-relay.plist` to `~/Library/LaunchAgents/`, then
     `launchctl load ~/Library/LaunchAgents/com.herdr.terminal-relay.plist`.

   - Linux needs the user's systemd instance to run without a login: `loginctl enable-linger <user>`. The unit uses
     only directives that work without user namespaces (no `PrivateTmp`/`ProtectSystem`); do not add them on hosts where
     unprivileged user namespaces are disabled, the unit would fail to start.
   - macOS: replace `USER` in the plist's log path (launchd does not expand `~`); the log is
     `~/Library/Logs/herdr-terminal-relay.log`. launchd cannot be told not to restart on exit status 2, so the plist
     throttles restarts to one per 30 s.

Exit status: 0 stopped by SIGTERM/SIGINT, 1 restartable failure (another relay holds the lock, control plane not
reachable for the first configuration), 2 only when restarting cannot help (host.json missing or incomplete, token
revoked). argparse usage errors also exit 2.

Exactly one relay runs per host (a lock file under `~/.local/share/herdr-engineering/state`); a second one exits with
status 1. Nothing is logged about screen contents, typed input or key material.
