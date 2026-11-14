# CI agent hygiene

`wpci-agent-prune` deletes dead Woodpecker agent registrations through the API
(`https://ci.metabolomics.us`). It runs on bender every 30 minutes from the
user timer `herdr-ci-agent-prune.timer`; each deletion is logged to the journal
(`journalctl --user -u herdr-ci-agent-prune`). The token is read from
`~/.config/woodpecker/token` and is never printed.

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
```
