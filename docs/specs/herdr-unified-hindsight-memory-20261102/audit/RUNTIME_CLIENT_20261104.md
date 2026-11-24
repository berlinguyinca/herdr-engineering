# Private dashboard runtime acceptance — November 4

The existing Memory Explorer now has a tested canonical-client deployment path.
This extends the existing deploy coordinator and API; no additional registry,
memory engine, transport, UI or listener is introduced.

## Component checks

- herdr-memory PR #15: client 0.1.7, merge
  7d8499af11d3ef825ecf7fd3bba3189b0f56e5f7. Offline schema/URL validation
  performs no routing, credential reads, networking or cache/outbox creation.
  Full suite: 165 passed, four optional integrations skipped, 20 subtests passed.
  Final trimmed-default compatibility checks also passed.
- Canonical CLI and complete dashboard images built successfully. The client
  image reports 0.1.7/contract 1 and contains no profile, Git credentials or ML engine.
- Runtime/profile/image/installer tests: 122 passed, one ownership fixture
  skipped because it requires root. Independent security reviews found no
  remaining high/medium issues in the bounded profile and lifecycle review.
- Deployment/backend/runtime regressions: 234 passed, one skipped. Three actual
  deploy dry-run cases passed after fixing the fleet memory schema to accept
  validated Explorer fields.

## Real backend canary

An independent validator used the dashboard image, canonical CLI and existing
authenticated HTTPS Hindsight backend. A private fixture API used synthetic
API keys with a canary-project viewer. Production viewer policy was unchanged.

- Runtime files were root-owned, 0600, in a private directory. Cache was separate.
- Backend health, API status, search and authenticated assets returned 200.
- Status was healthy. A new synthetic nonce was retrieved with revision and provenance.
- No key returned 401. An unlisted viewer and forbidden personal/global/infra scopes returned 403.
- No host port was published. The fixture API bound container loopback only.
- The synthetic note was deleted and the owned API server stopped.

## Network prerequisite and cleanup

Default Docker and control-plane network canaries could not reach the gateway,
while host access was healthy. Container DNS differed intermittently.

The operator explicitly approved a temporary rule for one owned container's /32
source on the control-plane bridge to the existing private gateway's TCP 443.
A test-container-only hosts entry used the verified gateway address while
preserving the HTTPS hostname, SNI and certificate verification. Timed cleanup
was scheduled before applying the rule.

With these temporary prerequisites the full canary passed. The rule was removed
and verified absent before the container was removed. No permanent firewall,
production DNS, Tailscale configuration, viewer grant or public exposure changed.

Production activation still requires an explicitly approved, narrowly scoped
container-to-gateway path, working private DNS and a configured viewer. Do not
allow the whole shared Docker subnet or disable TLS as a shortcut. Production
access remains denied by default.

Protected detailed reports and reproduction scripts remain under the operator's
private memory evaluation directory. No raw transcripts were stored in memory.
