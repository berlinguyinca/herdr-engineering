# P00 routes inventory (existing; source-verified, not smoke-tested)

HCN, `/api/v1/collab/` (dispatch: collab.py CollabService.dispatch): me, config; agents (list/connect/join/join-link/join-tokens/link/rename/revoke/external/*); rooms, rooms/{id}; projects (+rooms, members); rooms/{id}/{members,invites,externals,agents,agent-requests,profile,workspaces,missions,architecture-links,xlinks,federation/shares,messages,comments,consultations,forks,flow,tasks,tasks/inbox,delegations,proposals,steering,events,events/stream,state,attention,progress,analytics}; graph/*; fleet/* (operator). Also /mcp (JSON-RPC) and /a2a.
Artifacts, `/api/v1/fabric/artifacts*`: list, {id}, download, versions(+{vid}), reviews, acl, acl/revoke, artifact-jobs, publications/requests, publications/{id}/approve|revoke.
Attention, `/api/v1/fabric/attention` (+ack/snooze/clear); rooms/{id}/attention.
Assistant, `/api/v1/` assistants, assistant-runs, -approvals, -memory, -routines, -connectors, -delegations (capability assistant_v1).
Health/capabilities: `/api/v1/health` (nav-capabilities.js gate).
Pi: CLI only (`pi-engineering herdr ...`, `mission submit|observe|cancel`, `plan --store`); HTTP calls to /collab/agents/join, /agents/external/{heartbeat,lineage}, /steering/pending|ack.

## Missing for the spec (to be added by EXTENDING the above)
teams list/summary; project/issue/task-centric graph reads (root + as_of); layout document GET/PUT; artifacts-by-{project,issue,task}; unified approvals queue; pi room history/list payloads (rooms/messages cursor reads exist and likely suffice).
