# CI agent hygiene

`wpci-agent-prune` deletes dead Woodpecker agent registrations through the API
(`https://ci.metabolomics.us`). It runs on bender every 30 minutes from the
user timer `herdr-ci-agent-prune.timer`; each deletion is logged to the journal
(`journalctl --user -u herdr-ci-agent-prune`). The token is read from
`~/.config/woodpecker/token` and is never printed; create it with mode 0600
(`install -m0600 /dev/null ~/.config/woodpecker/token`), the script refuses a
group/other-accessible token file.

Guards: if no agent has contacted the server in the last 60 minutes
(`--recent-contact-mins`; server just restarted, or a quiet night) the run
deletes nothing and exits 0 with a one-line `NOTICE` on stderr, so the unit does
not show as failed. At most 50 deletions per run (`--max-deletions`); a larger
backlog drains over successive runs. Agents missing owner/org fields are never
deleted. A 409 on one delete is skipped and counted. Exit codes: 0 ok or
guarded skip, 1 API error, 2 token file missing or not 0600, 64 bad arguments.

After a Woodpecker outage longer than 24 hours, agents that are in fact alive can look stale until they reconnect, so up to `--max-deletions`
(default 50) live agents per run may be removed first; they re-register on their own. To limit that, lower the cap: add `--max-deletions 5` to
`ExecStart` in `herdr-ci-agent-prune.service` (then `systemctl --user daemon-reload`), or `--max-deletions 0` to make every run a no-op.

Deleted: system agents (`owner_id=-1`) that never contacted the server and are
older than 1 hour, or have had no contact and no work for 24 hours. Never
deleted: any agent referenced by a pending, running or waiting workflow in
`/api/queue/info` (re-checked right before each delete).

Why: one-shot agents that die between server auth and registration (Slurm kill,
idle exit, gateway 504) leave an unnamed, capacity -1, never-contacted record.
The Slurm-side `wpci-cleanup` only handles named, job-correlated agents, so
those records piled up (164 on 2026-11-13) along with stale named ones.

Install on bender:

```sh
install -D -m0755 deploy/ci-agent-hygiene/wpci-agent-prune ~/.local/libexec/wpci-agent-prune
install -D -m0644 deploy/ci-agent-hygiene/herdr-ci-agent-prune.{service,timer} -t ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now herdr-ci-agent-prune.timer
~/.local/libexec/wpci-agent-prune          # dry run
loginctl enable-linger "$USER"             # user timer keeps running without a login session
```

Disable: `systemctl --user disable --now herdr-ci-agent-prune.timer`.
