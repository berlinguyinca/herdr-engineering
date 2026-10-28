# Fabric URL Resolution and HTTPS Gateway — spec and decisions

## Decisions

**D1 One detector, render-time rewrite.** The browser linkifier is the only URL detector for display.
`renderMarkdown(..., {urls})` gains a `urls.resolve(href)` hook modelled on `references.resolve`: the
href comes from the resolver, never from the text. Stored text, logs and commands are never mutated.
Code spans/fences stay non-linkified (they hold commands/logs); a resolved URL there gets no link.
The server exposes `POST /resolve-urls` (batch, keyed by exact URL string + session + host).

**D2 Source-anchored resolver** `fabric_urls.py` (new, pure, no I/O). Leases are filtered to the *source
machine first*; a fleet-wide unique match is used only when the source is unknown. Ambiguity returns
`status=ambiguous`, never a guess. Existing `urlrewrite.*` / `op_resolve_loopback` / `op_rewrite` keep their
semantics for current callers. Output fields: original, canonical (https), host, port, service, status,
reason, access class. The emitted port is whatever the lease says.

**D3 Statuses:** `resolved`, `unmapped` (no lease), `ambiguous`, `expired`/`unhealthy`, `not_loopback`
(untouched), `denied` (policy), `invalid`. Access classes: `private-fabric` (default), `restricted`.
External URLs are never rewritten without an explicit mapping.

**D4 Same-port HTTPS.** Each router listener peeks the first byte: `0x16` → TLS handshake (ssl server
context) then the existing pipe; anything else → current plaintext path, so `http://dev.lan:<port>` keeps
working. TLS-terminated requests get `X-Forwarded-Proto: https`/`Forwarded`, and Location rewrite emits https.

**D5 Private CA.** `herdr-eng devfabric ca init|issue|export`. Leaf SANs: `dev.lan`, `*.dev.lan`, gateway
MagicDNS name; EKU serverAuth; validity ≤ 398 days (Apple limit); key never in repo (`~/.config/herdr-engineering/ca/`,
0600). Enrollment doc covers macOS keychain and the iOS profile *plus* the manual Full Trust toggle.

**D6 SSRF/open-proxy controls.** `target_host` allowlist at registration: loopback and addresses of the
registering machine's fabric/tailnet only; link-local, metadata (169.254.0.0/16, fd00:ec2::/…), multicast,
unspecified and unresolved names are rejected. Redirects pointing off the lease are not rewritten to anything new.

**D7 Data-plane auth.** Tailscale `whois` on the peer IP, checked against a configurable allow-list of tailnet
users/tags (guest/shared nodes denied by default). Falls back to deny when whois is unavailable and enforcement is on.

**D8 Failover (last PR).** Active/standby gateways on a shared registry with lease-fenced lock; CoreDNS
returns both IPs with health-based removal. The report states what was actually proven.

## PR plan
1. Spec + audit + resolver (`fabric_urls.py`) + SSRF validation + tests.
2. Browser integration (`urls.resolve`, hover/copy, services view) + `/resolve-urls`.
3. TLS listener + private CA + enrollment docs.
4. Data-plane auth.
5. Streaming split-chunk + terminal/OSC 8 integration.
6. Failover + final verification report.

## Acceptance matrix owner / verifiability
Tests 12 (Mac/iPhone trust) and the real-fleet parts of 11/15 need physical devices and hosts and will be
marked **unverified** in the report unless run by the user.

## Known limitation (found in PR 1)
Leases record the *target* port, which for loopback-only apps is the fabric agent's tailnet forwarder
port. It equals the app's loopback port only when port-matching succeeded (`fabric_agent._adopt` falls
back to an ephemeral port otherwise). The resolver matches on the lease's target port, so in that
fallback case the URL the agent printed resolves as `unmapped`, never to a wrong service. Fix in PR 2:
agents register an `origin_port` and the resolver prefers it.

## PR 1 status
Shipped: `fabric_urls.resolve_url`, `validate_target_host`, `POST /resolve-urls`, target-host
validation in `op_register` (operator CIDR allow-list via `allowed_target_networks`).
Canonical scheme stays `http` (`public_scheme`) until PR 3 binds a TLS listener — advertising https
before it exists would hand out dead links.
Deployment note: registration of non-loopback, non-tailnet targets (e.g. LAN IPs via `--host`) is now
refused unless allow-listed; no such registrant exists in-repo (agents use the tailnet IP).

## PR 2 status (browser integration)
Shipped: `renderMarkdown(..., {urls})` hook (resolver supplies the href, re-validated with `parseWebUrl`;
code spans/fences never rewritten); `fabric-urls.js` cache + batch resolver; control-plane
`POST /api/v1/fabric/resolve-urls` and `GET /api/v1/fabric/services`; hover details, copy-original /
copy-fabric buttons (always visible on touch devices); unresolved loopback URLs render as a non-link with the
reason; searchable `#/fabric-services` page. Wired into the session workspace (source host = session host).
Not yet wired: collab chat, notifications, artifacts, terminal OSC 8 (PR 5 covers terminal/streaming;
remaining renderers call the same `urls` option).
Deferred from the PR 1 note: `origin_port` on leases is still open.

## PR 3 / PR 4 status
PR 3 merged: same-port HTTPS + name-constrained private CA. PR 4: `fabric_access.AccessPolicy` (tailscaled
LocalAPI whois, user/tag allow-list, fail closed, loopback trusted) enforced in the router before TLS/HTTP.
