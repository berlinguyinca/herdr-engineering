# Verification report: fabric URL resolution and HTTPS gateway

Date: 2026-10-28 · Repo: `berlinguyinca/herdr-engineering` · PRs #321, #323, #325, #326, #328, #329
Legend: **Automated** = a test in this repo exercises it. **Partial** = automated for the mechanism, not the real-world target.
**Not verified** = needs devices/hosts this session did not have. **Not built** = out of what shipped.

## Acceptance matrix (spec Phase 5)

| # | Item | Status | Evidence / gap |
|---|---|---|---|
| 1 | localhost URL on the local agent host | Automated | `tests/test_fabric_urls.py::test_local_agent_host_url_is_rewritten_to_https`; gateway `/resolve-urls` in `test_fabric_url_gateway.py` |
| 2 | localhost URL on a remote fabric host | Automated | `test_remote_host_url_uses_the_lease_port_not_the_target_port` |
| 3 | Isolated container namespace | **Not built** | Leases carry the machine, not a network namespace. A URL from a container resolves by host only. |
| 4 | Two hosts, same port | Automated | `test_two_hosts_same_port_source_disambiguates`; unknown source => `ambiguous`, never a guess |
| 5 | HTTP service exposed through HTTPS | Automated | `tests/test_fabric_tls_gateway.py` (HTTP and HTTPS share one port) |
| 6 | Expired / unavailable / deregistered | Automated | statuses `expired`, `unhealthy`, `unmapped`, `unavailable`; UI shows the reason instead of a dead link (`fabric-urls.test.js`) |
| 7 | IPv4 and IPv6 loopback | Partial | Resolver accepts `127.x`, `::1`, `0.0.0.0`, v4-mapped. The gateway router binds IPv4 only, so IPv6 *clients* of `dev.lan` are unsupported. |
| 8 | Path / query / fragment / encoding | Automated | resolver (byte-for-byte tail) and JS tests |
| 9 | Split streaming output | Automated | `fabric-urls-streaming.test.js` walks every prefix of a streamed URL |
| 10 | Markdown, OSC 8, code blocks, raw logs | Automated | same file; code, fences and tool output are never rewritten; inputs not mutated |
| 11 | WebSocket / SSE / HMR | Partial | SSE streams over TLS; an upgrade request passes through with forwarded headers. No real WebSocket frame exchange and no real HMR dev server was run. |
| 12 | Certificates on Mac and iPhone | **Not verified** | Chain, SAN, EKU, name constraints and the 397-day cap are tested with OpenSSL/Python. Safari/Keychain/iOS trust was not exercised; steps in `docs/operations/fabric-https.md`. |
| 13 | Unauthorized user and guest denial | Partial | `AccessPolicy` tested with injected identities and a fake tailscaled LocalAPI socket. No real shared-in guest or live tailscaled. Off unless an allow-list is configured. |
| 14 | Malicious URL, SSRF, open proxy, redirect abuse | Automated | `target_host` limited to loopback/tailnet (metadata, link-local, names rejected); lookalike hosts, userinfo, `javascript:`/`//host` canonicals refused; client X-Forwarded-* replaced; redirects only rewritten for the lease's own loopback port |
| 15 | Gateway / controller failover | Partial | Two real gateways in-process: mirror, promotion, rejoin, double-active fencing. Not run across hosts or DNS. |
| 16 | Pi / Claude / Codex / local-model compatibility | Partial | The layer works on message text from the session inspector, independent of agent. No per-agent fixtures; collab chat, notifications and artifact viewers are not wired (only the session workspace). |

## Phase scope check
* **Done:** canonical resolver; batch API; SSRF validation; browser hook with hover details, copy original/fabric URL (always visible on touch),
  inaccessible-service messaging, searchable Fabric Services page; same-port HTTPS with a name-constrained private CA and enrollment doc;
  tailnet-identity access control; stream-safe rendering and OSC 8; active/standby failover.
* **Added in the follow-up (branch `feat/fabric-public-certs`):** publicly trusted certificate for `local.metabolomics.us` (Let's Encrypt, DNS-01 on Route 53, no device enrollment),
  A-record publishing (tailnet addresses only), automatic renewal with a daily failure backoff and hot reload, stable `https://<service>.local.metabolomics.us[:port]` URLs,
  `origin_port`, network-namespace matching, legacy-name aliases (`dev.lan`), and `urls` wiring in the composer preview, attention list and agents list. See `docs/operations/fabric-public-names.md`.
* **Still not built:** Python-side `urlrewrite` text rewriting still produces `http`; `urls` is not wired into mission views, notifications or artifact viewers; `collab-view` is intentionally unwired (public guest surface);
  the dashboard does not yet know a source network namespace to send.
* **Known behavior limits:** TLS exchanges are one request per connection; same-origin `routes` leases are terminated but not header-rewritten;
  lease target port may differ from the app's loopback port when port-matching fell back (resolves as `unmapped`, never wrongly); a successful resolve is not a guarantee the browser device has enrolled the CA.

## Test results
Python fabric suites: 70 tests pass (includes 7 failover, 12 access, 8 TLS gateway, 7 CA, 59 resolver/gateway). JS: client suite 690 pass.
Pre-existing, unrelated failures (also fail on unrelated PRs): `ci-observability-view.test.js` (missing `playwright` locally) and the CI `ui-gates`
visual-baseline job (pixel diffs on the collab pages; its own header says it was never validated on an agent). PRs touching `packages/**` merged with that job red for this reason.
Follow-up test pass (branch `test/fabric-url-gaps`): CLI wiring (`tests/test_fabric_cli.py`, including that `control-plane web` hands the fabric helpers to the server),
Playwright UI tests of the session workspace (`tests/playwright/fabric-links.test.mjs`: canonical link, hover title, clipboard copy, unreachable non-link, stream-settle re-render,
gateway-down), and a failover test driven by the real background loop. Wiring tests were mutation-checked (breaking the wiring makes them fail).
The earlier one-off failover failure could not be reproduced in 18 stressed runs; its two plausible causes (the probe thread racing hand-driven steps, and re-binding a released ephemeral port) were removed
(`auto_ha=False` for hand-driven tests, no port reuse).

## Live verification of the public name (2026-10-28)
Production certificate issued once (`CN=local.metabolomics.us`, SAN apex + `*.local.metabolomics.us`, issuer Let's Encrypt, 89 days). A and wildcard A records published to the gateway's
tailnet IP only (no AAAA). An ephemeral gateway (scratch state dir, free ports) was run with the real certificate:
`curl https://docs.local.metabolomics.us:<port>/` and `curl https://local.metabolomics.us:<lease port>/` returned 200 with `ssl_verify_result=0` from this host **and from a second tailnet machine** (beast),
using only the system trust store; an unknown service name is reset with no response; `resolve-urls` returns the stable name as `canonical` and the per-port address as `port_url`.
Findings: (1) `100.100.100.100` (MagicDNS) answered SERVFAIL when queried directly while the host's normal resolver and 8.8.8.8 answered correctly; devices using only MagicDNS should be checked.
(2) A gateway whose lease range overlaps another gateway on the same host registers a lease whose port could not be bound (`op_register` ignores `router.bind`'s result) - hit here because the live
gateway holds 18000; pre-existing, not changed. (3) Not verified on real devices: iPhone/iPad/Mac browsers, and the named front on port 443 (needs the port free and CAP_NET_BIND_SERVICE).

## Rollout
Everything new is opt-in except one behavior change: lease registration now refuses `target_host` outside loopback/tailnet unless allow-listed.
HTTPS needs `devfabric ca init|issue` and `gateway --tls-cert/--tls-key`; the allow-list needs `--allow-user/--allow-tag`; failover needs `--peer`.
Order for a fleet: deploy code, `ca init`, `ca issue`, restart gateway with the cert, enroll devices, then enable the allow-list.
