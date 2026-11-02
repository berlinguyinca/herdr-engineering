# herdr-terminal-relay

Host side of the Shepherd terminal. It mirrors herdr panes to the phone through the control plane and types the phone's
input back. It makes outbound calls only: no inbound port, no ssh, no pty.

Per host:

1. herdr must be running as the same user (the relay talks to `~/.config/herdr/herdr.sock`).
2. `~/.config/herdr-engineering/host.json` must exist (`herdr-eng fleet join`); the relay uses its control plane URL and
   the host's own host token (`X-Host-Token`, the same credential the fleet routes use).
3. Nothing to configure on the control plane: the token identifies the host, and a token only works for its own host.
4. Roll it out from the checkout, per host, with `deploy/deploy-fleet-agents.sh --with-terminal-relay --hosts <host>`
   (opt-in: a normal fleet roll never installs it; `--dry-run` shows the plan). The script builds
   `~/.local/share/herdr-terminal-relay/releases/<release>/venv` on Python >= 3.12 (`uv` if present, else `python3.12`,
   else `/opt/homebrew/bin/python3.12`; it refuses with a message when there is none, because plain `python3` over
   non-login ssh may be 3.9), installs the package from the checkout, switches `.../herdr-terminal-relay/current`, and
   renders the unit or plist from this directory (`@CURRENT@` becomes that `current` path, `@USER@` the account name)
   so it runs `<current>/venv/bin/herdr-eng host terminal-relay` with no dependency on any other herdr-eng install.
   It then enables and starts it (systemd user unit, or `launchctl load` of the LaunchAgent) and waits up to 20 s for the
   relay's `terminal relay connected` log line (the first successful wait); without it the `current` symlink goes back
   to the previous release, which is restarted, and the script exits non-zero.

   - Linux needs the user's systemd instance to run without a login: `loginctl enable-linger <user>` (the script checks
     and warns). The unit uses only directives that work without user namespaces (no `PrivateTmp`/`ProtectSystem`); do
     not add them on hosts where unprivileged user namespaces are disabled, the unit would fail to start.
   - macOS: the log is `~/Library/Logs/herdr-terminal-relay.log`. launchd cannot be told not to restart on exit status
     2, so the plist throttles restarts to one per 30 s.
   - The script also warns (and goes on) when herdr does not run as that user or `~/.config/herdr/herdr.sock` is
     missing.

Exit status: 0 stopped by SIGTERM/SIGINT, 1 restartable failure (another relay holds the lock, control plane not
reachable for the first configuration), 2 only when restarting cannot help (host.json missing or incomplete, token
revoked). argparse usage errors also exit 2.

Exactly one relay runs per host (a lock file under `~/.local/share/herdr-engineering/state`); a second one exits with
status 1. Nothing is logged about screen contents, typed input or key material.
