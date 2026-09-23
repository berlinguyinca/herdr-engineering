# Implementation Roadmap

The sequence is intentionally front-loaded with reconciliation, compatibility and stable contracts. UI layers are built only after the underlying ownership/data contracts are known.

```mermaid
flowchart TD
  A[0010 Repo + reconciliation] --> B[0020 Upstream audit]
  B --> C[0030 Native Herdr contracts]
  C --> D[0040 Fleet + Ansible]
  B --> E[0050 Powerpack lifecycle]
  C --> E
  D --> E
  E --> F[0060 Web/mobile]
  F --> G[0070 Browser + Plannotator]
  D --> H[0080 Artifact workspace]
  E --> H
  C --> I[0090 Session journal]
  F --> I
  H --> I
  C --> J[0100 Pi/AutoSpec integration]
  I --> J
  D --> K[0110 Transparent dev fabric]
  F --> K
  G --> K
  F --> L[0120 Tests]
  I --> L
  J --> L
  F --> M[0130 CI]
  I --> M
  L --> M
  G --> N[0140 Candidate comparison]
  J --> N
  K --> N
  L --> N
  M --> N
  I --> O[0150 Unified UX]
  K --> O
  L --> O
  M --> O
  N --> O
  I --> P[0160 Attention]
  M --> P
  O --> P
  E --> Q[0170 Ops/security/rollback]
  I --> Q
  K --> Q
  L --> Q
  M --> Q
  P --> Q
  Q --> R[0180 Fleet validation + release]
```

## Milestone slices

### Milestone A — Trusted foundation (0010–0050)

Outcome: repository, real compatibility data, stable Herdr adapter, reproducible fleet and safely pinned Powerpack. Nothing flashy is required yet; this prevents later UI from being built on guessed APIs.

### Milestone B — Usable cockpit (0060–0100)

Outcome: phone/tablet access, browser/review, shared evidence, session search/timeline, and semantic Pi/AutoSpec correlation. At this point HerdR Engineering is already useful for daily operations.

### Milestone C — Distributed IDE (0110–0150)

Outcome: transparent dev services, cross-language tests, CI/queue state, parallel candidate comparison, coherent navigation. This is where “everything on every machine feels local” becomes tangible.

### Milestone D — Production operations (0160–0180)

Outcome: attention routing, robust security/observability/update/rollback and full fleet release validation.

## Parallelism policy

Implementation agents may work in parallel only when the manifest dependencies are satisfied and each agent owns an isolated Git worktree. Cross-phase interface changes require a written contract/ADR before dependent agents implement against them.
