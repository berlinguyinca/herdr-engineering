# Personal Assistant Pi delegation adapter plan

Goal: dispatch the existing root AssistantService's queued `pi-secretary` delegations to one
preconfigured no-tools Pi target without creating identity, auth, storage or scheduling systems.

1. Define a narrow injected transport over the existing Pi task contract: create, inspect and
   cancel. The adapter accepts no credentials, commands, paths, mounts or model overrides.
2. Validate the operator-owned binding to one room, agent and immutable Secretary role reference.
3. Submit only the root delegation's stored task text with a deterministic idempotency key; map Pi
   task states back to queued/running/done/failed/cancelled and preserve bounded result text.
4. Make cancellation task-scoped; it must not grant authority or stop unrelated agents.
5. Test target denial, scope isolation, idempotent submission, state mapping and cancellation with
   synthetic data. AssistantService owns persistence, authorization, audit and connector context.
