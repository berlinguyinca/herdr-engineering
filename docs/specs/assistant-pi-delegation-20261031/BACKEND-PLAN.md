# Existing AssistantService delegation lifecycle plan

Goal: finish the existing root assistant's delegation lifecycle through an injected
`pi-secretary` adapter without creating another assistant, auth system, store or connector.

1. Extend the durable `DelegationRequest` with server-derived run provenance, bounded
   context/source references, immutable idempotency, external task receipt, checkpoint,
   result/error, cancellation request and terminal timestamps.
2. Add an injected adapter protocol with `submit`, `poll` and `cancel`. Pass a sanitized
   dispatch object that omits tenant/user credentials, grants and connectors.
3. Restrict targets to configured names (`pi` by default; `pi-secretary` only when
   configured). Map the Secretary target to the fixed reviewed role metadata outside
   request bodies; accept no commands, paths, model overrides or role text.
4. Persist before dispatch. Keep failed delivery queued for retry; never fabricate
   completion. Poll through service maintenance and map only bounded queued/running/
   succeeded/needs-context/failed/cancelled receipts with artifacts.
5. Preserve existing direct API user/tenant scoping. Derive run identity inside the
   service; API bodies cannot set it. Cancellation remains owner-scoped and is terminal
   only after an adapter receipt when an external task exists.
6. Invoke maintenance from the existing server sweep and scoped delegation reads. Test
   restart persistence, idempotency, isolation, adapter failures, cancellation and the
   real Pi adapter contract.

No OAuth, database, root assistant, connector credential, shell/browser tool, personal
source read or arbitrary runtime command is added. A no-tools Secretary may return
`needs_context`; calendar/email/location/provider access remains root-owned and disabled
for the child.
