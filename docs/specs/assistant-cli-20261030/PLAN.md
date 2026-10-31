# Assistant CLI initial slice plan

Goal: expose the existing authenticated collaboration agent/task contracts through
`herdr-eng assistant` without inventing authority or a capability registry.

1. Add failing CLI/client tests for credential loading, safe endpoint validation,
   versioned JSON envelopes, agents, room-scoped capabilities, task create/list/get,
   agent inbox/update, and approval proposal request/list.
2. Implement a standard-library HTTP client with redirects disabled, bounded JSON
   responses, Bearer/Agent authentication from environment or a private token file,
   and no automatic mutation retries.
3. Register the nested `assistant` commands in the existing parser. Derive capability
   output from current room action grants and label the future role registry unavailable.
4. Bind approval proposal commands only to the additive backend contract
   `/rooms/{room}/approval-proposals`; a pending proposal never grants authority.
5. Verify focused tests, existing CLI tests, Ruff, compilation, and diff cleanliness;
   document setup, examples, security boundaries, and current limitations.

No backend authority expansion, dependency addition, agent creation, live outreach,
or dashboard work is part of this slice.
