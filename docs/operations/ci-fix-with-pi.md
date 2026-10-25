# Fix with Pi (failed CI runs)

A failed CI run in the dashboard offers two buttons next to the target Pi agent picker: **Send to Pi** (debug context only) and **Fix with Pi**.

Fix with Pi uses the same path as Send to Pi: `POST /api/v1/ci/debug` with `"mode": "fix"`, operator access only, one durable replay-safe message to a Pi session you choose, delivery confirmed by transport ACK only. The message differs in its instructions, which are fixed text from the control plane, not CI data:

- work in a **new git worktree** created from the exact commit in the CI context (`herdr worktree` or `git worktree add`), never in a shared checkout;
- reproduce the failure, make the smallest fix, run the project's tests;
- push a **new branch** and open a pull request that links the CI run;
- **do not merge**, do not push to main or master, do not touch CI credentials, secrets or branch protection.

The failing step, log summary and revision follow as `CI_CONTEXT_JSON`, marked untrusted (evidence, never instructions). Secrets are redacted as for Send to Pi.

- The two modes use separate operation ids; reusing an id across modes is refused.
- A session on the run's branch is labelled "on this branch" in the picker and is preselected when it is the only one.
- The control plane never starts an agent and never merges anything. Whether the agent follows the instructions is up to the agent; review the pull request as usual.
- Codex and Claude sessions are not targets yet (the existing handoff requires a Pi session).
