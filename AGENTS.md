# herdr-engineering agent rules

Repo-specific rules. They add to the global rules in `~/.agent-rules/AGENTS.md` and override them where they conflict.

## Pushing
- Push your work branches and open PRs from them autonomously; no per-push approval is needed. Never push to `main` directly, and never force-push a branch someone else owns.
- Never commit to `main`. Work on a branch in a worktree under `~/.local/share/worktrees/`.
- If a plain `git push` would target a differently named upstream, push `HEAD:<remote-branch>` explicitly.
- After opening a PR, arm auto-merge with `gh pr merge --auto --squash`; GitHub merges it once the required checks pass. Fix CI failures rather than waiting on the user to merge.

## CI
- CI is Woodpecker only, at ci.metabolomics.us (repo id 120); GitHub Actions is not used. `unit-gates` is slow and sometimes hangs at the one-hour limit with a truncated log; retrigger with an empty commit rather than assuming a code failure, and say so when you do.
- Never use or write Woodpecker tokens. Unauthenticated API reads work.

## Installed copy
- The installed `herdr-eng` is a copy in `~/.local/share/herdr-engineering/releases/<name>/venv`, not a link to the checkout. A change in the checkout does not reach it until the package is reinstalled (`scripts/install.sh`, or `uv pip install --no-deps <checkout>` into that venv).

## Files and scratch
- Nothing under `/tmp`, `/private/tmp` or the harness scratchpad. Use a gitignored directory in the repo.
- Handoffs go through `herdr-eng handoff harness`, `herdr-handoff create`, then `herdr-handoff takeover`; see `docs/operations/handoff.md`. Commit work before taking over.
