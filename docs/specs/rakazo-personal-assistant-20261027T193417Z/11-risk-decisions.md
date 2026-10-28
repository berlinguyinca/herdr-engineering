# 11 — Risks, decisions and open questions

Decide using ADRs, not unreviewed assumptions:
D1 Canonical user/tenant identity and Herdr auth.
D2 Persistence schema and whether runtime adapter is in-process vs isolated sidecar.
D3 Memory backend namespace/auth semantics in existing OpenViking installation.
D4 Real availability and reliability semantics of scheduler/failover at HEAD.
D5 Connection broker for apps and OAuth scopes; credential migration.
D6 Design-system settings route and global launcher conventions.
D7 Voice privacy, data retention and external provider choice.
D8 Browser and computer sandbox policy on heterogeneous host fleet.
D9 Upstream Rakazo version maintenance and Apache NOTICE.
D10 Mobile approach: responsive first; native client later.

Risks: upstream churn (beta), source license compliance, duplicated identity/scheduler, accidental delegated privilege, model/tool capability mismatch, personal-data exposure, dependency drift, host migration duplicates, unwitting resource spend. Mitigate by pinned upstream SHA, adapter versioning, canonical ownership, least privilege, fault injection, explicit user consent and budget constraints.
