# herdr-engineering agent rules

Repo-specific rules. They add to the global rules in `~/.agent-rules/AGENTS.md` and override them where they conflict.

## Pushing
- Never push this repository, and never open a PR from it, unless the user explicitly asks in that message. Approval for one branch does not extend to another.
- Never commit to `main`. Work on a branch in a worktree under `~/.local/share/worktrees/`.
- If a plain `git push` would target a differently named upstream, push `HEAD:<remote-branch>` explicitly.

## CI
- CI is Woodpecker at ci.metabolomics.us (repo id 120), plus the GitHub `verify` jobs. `unit-gates` is slow and sometimes hangs at the one-hour limit with a truncated log; retrigger with an empty commit rather than assuming a code failure, and say so when you do.
- Never use or write Woodpecker tokens. Unauthenticated API reads work.

## Installed copy
- The installed `herdr-eng` is a copy in `~/.local/share/herdr-engineering/releases/<name>/venv`, not a link to the checkout. A change in the checkout does not reach it until the package is reinstalled (`scripts/install.sh`, or `uv pip install --no-deps <checkout>` into that venv).

## Files and scratch
- Nothing under `/tmp`, `/private/tmp` or the harness scratchpad. Use a gitignored directory in the repo.
- Handoffs go through `herdr-eng handoff harness`, `herdr-handoff create`, then `herdr-handoff takeover`; see `docs/operations/handoff.md`. Commit work before taking over.
