# Fabric public name (`https://<service>.local.metabolomics.us`)

Devices need **no enrollment**: the fleet name carries a real Let's Encrypt certificate. The DNS records point at
tailnet addresses (`100.64.0.0/10`), so the name resolves publicly but is **reachable only from the tailnet**. The
private CA in [fabric-https.md](fabric-https.md) stays available for `dev.lan`; the gateway picks the certificate by SNI.

## Moving parts

| Piece | Where | What it does |
|---|---|---|
| `fabric_certs.py` | gateway | `AcmeRoute53` (certbot, DNS-01 on Route 53) and `CertManager` (SNI choice, hot reload, renewal) |
| `fabric_dns.py` | any host with AWS creds | UPSERTs **A records only**, tailnet IPv4 only, apex + wildcard |
| `fabric_named.py` | gateway | one listener (default 443) that routes `<service>.<domain>` by SNI / `Host` |
| `url_aliases` | gateway + browser + agent | legacy names (`dev.lan`) are accepted and canonicalised to the public name |

## Setup

```bash
pip install 'herdr-engineering[acme]'                     # certbot + route53 plugin + boto3
herdr-eng devfabric cert issue --domain local.metabolomics.us --staging    # rehearse against the LE staging CA first
herdr-eng devfabric cert issue --domain local.metabolomics.us              # the one production issuance
herdr-eng devfabric dns publish --domain local.metabolomics.us --dry-run   # then without --dry-run
herdr-eng devfabric gateway --acme-domain local.metabolomics.us --renew-certs
```

* `cert issue` is a no-op while a valid certificate exists (`--force` overrides and counts against Let's Encrypt rate
  limits, which cover **all of metabolomics.us**). No ACME email is registered unless you pass `--email`.
* State lives in `~/.config/herdr-engineering/acme` (0700). The process needs AWS credentials able to change
  `_acme-challenge.local.metabolomics.us` TXT records (and, for `dns publish`, the A records of the name).
  Prefer a dedicated IAM user restricted to those records over a personal key.
* `dns publish` refuses any address outside `100.64.0.0/10`. It publishes no AAAA: the router binds IPv4 only, and a
  AAAA answer would make dual-stack clients wait on an address nothing serves. With `--ip` repeated (an HA pair) clients
  round-robin and fall through when one host is down. TTL defaults to 60 s.

## Renewal

`--renew-certs` (or `fabric.renew_certs: true`) makes **one** gateway renew. In an HA pair set it on one node only; the
other picks the files up on restart. A certificate is renewed when a third of **its own** lifetime is left (about 30
days of 90). A failed attempt is not retried for 24 hours, so a broken credential cannot hammer the ACME servers. The
new certificate replaces the live TLS context without a restart; open connections finish on the old one.
`devfabric cert status` shows expiry, lifetime and whether renewal is due. Run `cert renew` from cron on the other node
if you prefer an external schedule.

## Stable service names

With `--acme-domain`, the gateway also serves `https://<name>.<domain>` on `--named-port` (default **443**). The name
is the lease label when it is a valid DNS label (`--label docs` -> `docs.local.metabolomics.us`), otherwise
`<machine>-<listening port>` (`bender-3000`). A label already taken keeps going to the older lease; the newer one
falls back to its automatic name. Unknown names are dropped without a response. TLS termination, the tailnet access
policy and header rewriting are the existing per-port path, so a named URL is as protected as a port URL.

`resolve-urls` returns the stable name as `canonical` **only while the named listener is actually bound**, with the
per-port address in `port_url`. If the port cannot be bound (443 is privileged, or something else owns it) the gateway
logs a warning and keeps using per-port URLs; it never advertises a name nothing serves.

> **Port 443 on the gateway host.** Binding 443 needs `CAP_NET_BIND_SERVICE` (or `net.ipv4.ip_unprivileged_port_start`),
> and the tailnet address's 443 must be free. If `tailscale serve` already owns it, either move that service behind the
> gateway as a named route, or run the named front on another port with `--named-port`.

## The bare names: `https://dev.lan` and `https://local.metabolomics.us`

`--apex-upstream HOST:PORT` (config `fabric.apex_upstream`) sends the bare fleet names on the named port to the
dashboard, e.g. `--apex-upstream 100.104.39.6:80` for the `herdr-cp-web` container. `https://local.metabolomics.us`
then works on any tailnet device with no enrollment. `https://dev.lan` needs a certificate for `.lan`, which no public CA
issues, so it uses the private CA: run `devfabric ca init && devfabric ca issue`, start the gateway with the CA present, and
install the root once per device (`devfabric ca export`, an iOS/macOS `.mobileconfig`). Until a device trusts it, its browser
warns on `https://dev.lan`; `http://dev.lan` keeps working. The 443 listener must be free first: on bender
`tailscale serve` holds it (see below).

## Origin port and network namespaces

A lease records `origin_port` (where the app listens) when the agent's forwarder had to bind a different port, and
`network_namespace` (`net:[inode]` of the agent). Loopback URLs are matched on the origin port. Two namespaces on one
machine that both have a `localhost:3000` are distinct leases; a caller that passes `source_namespace` gets its own
namespace's service, and without it the answer is `ambiguous` rather than a guess. Leases that predate these fields
keep matching on the target port.

## Not covered

The public guest surface (`collab-view`) is deliberately not wired to the resolver: guests never see fabric names.
Mission activity and mission story views show agent text but are not wired; the agents list resolves without a source
machine, so only a fleet-unique port links there. A source network namespace is accepted by the API but the dashboard
does not yet know one to send.
