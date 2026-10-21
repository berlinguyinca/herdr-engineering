# P2 plan — harness connectivity (not started)

Gate: P1 security tests (`tests/control_plane/test_collab.py`) green and the
P1 blockers in `audit.md` resolved for any real-user enablement.

1. **Local bridge** (`herdr_engineering/collab_bridge.py`, new): outbound-only
   client of the P1 API (enroll, rotate, poll/stream events, post). No inbound
   port, no PTY access. Credential in the OS keyring. Reuse `herdr_adapter`
   only for *enumerating* local agent status, never `send_input` for peer text.
2. **MCP adapter** for Claude Code and Pi: tools `herdr.rooms.list`,
   `herdr.messages.send`, `herdr.agents.list` map 1:1 onto P1 routes; peer text
   is returned wrapped as untrusted data. Tasks/approvals tools wait for step 3.
3. **Delegated tasks** (new event types in the same log, no new queue): goal,
   allowed artifact refs, permitted operations, budget, TTL, hop depth (default
   3), max turns, cycle detection on `correlation_id`/`causation_id`, accept/
   decline, cancel, human approval for cross-user chaining.
4. **A2A gateway**: stateless translation of task/message/artifact to the log;
   no second lifecycle store.
5. Tests: Claude A asks Claude B (declines/answers), Pi joins via scoped grant,
   loop budget stops recursion, no terminal text crosses users.
