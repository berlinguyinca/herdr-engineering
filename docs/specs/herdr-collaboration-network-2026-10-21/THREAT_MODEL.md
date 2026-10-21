# Security threat model and mandatory controls

## Trust boundaries
1. Human browser ↔ collaboration server (session, SSO, CSRF, XSS, permission checks)
2. Local agent bridge ↔ central server (mutual agent identity, replay protection, TLS, revocation)
3. User A ↔ user B (cross-tenant and project isolation)
4. Agent-generated text ↔ agent tools/execution (prompt injection and privilege escalation)
5. Artifact/media ↔ previews and model prompts (malicious payloads, hidden instructions and data leakage)
6. Server ↔ git/CI/object storage/memory (scoped service identities)

## Threats and mitigations
- Agent prompt injection in room chat: treat as untrusted content; explicit tool authorization + human approval for sensitive effects; limit available context and tools.
- Cross-user file exfiltration: deny local filesystem access; artifact allowlists, classification tags, project ACLs and content scanning; egress control for sandboxed runners.
- Credential exfiltration: never mount SSH/Git/API credentials into peer-controlled execution; redact logs and exceptions, short-lived tokens, scoped delegation.
- Tenant ID spoofing: identity from auth token; server joins on authorized org/project; database policy checks; negative tests on every resource and artifact reference.
- Unbounded inter-agent recursion: hop/turn/token budgets, cycle detection, cancellation and kill switches.
- Malicious guest invite or link: invited named principals, short expiry, MFA where required, read/comment default, audit download/export.
- Device compromise / stolen agent token: binding to device if supported, rotation, revoke, audit anomalous activity, no privilege amplification.
- Insecure WebSocket: authenticated upgrade + origin checks, authorization per room subscription/event, immediate revocation.
- Artifact conflicts: immutable versions, compare-and-swap, explicit review; never silent overwrite.
- SSRF/metadata leakage: no free-form URLs fetched by collaboration server, allowlist outbound integrations and block metadata networks.
- Supply-chain contamination: pin vetted dependencies, SCA, SAST, SBOM and reproducible container image process.
- University/restricted data: data classification and legal/institutional approvals before enabling external sharing or nonapproved model contexts.

## Non-negotiable pre-release tests
- User A cannot enumerate user B's rooms/artifacts/agents, even via IDs, search, event streams, notifications or cached previews.
- A guest cannot trigger tool execution, read terminal buffers, retrieve internal paths or follow private artifact links.
- A forged agent event cannot impersonate an owner or project admin.
- Revocation blocks subsequent REST, stream and agent-bridge actions.
- Prompt injection saying 'ignore your instructions' cannot override task ACL, permitted tools, or approval rules.
- Parallel artifact proposal does not overwrite either author's work.
- Client retries don't duplicate task execution or published artifact versions.
- Existing local single-user CLI/socket operations still work after feature is enabled/disabled.

## Release gate
Threat modeling review by a security-qualified engineer, authorization integration tests, external guest penetration testing, secrets handling review and documented deployment hardening. Do not market as 'no security exposure'; aim for least privilege and verifiable isolation with residual risk documented.
