# Shepherd phone app: tailnet discovery and zero-typing sign-in

The tailnet is the trust boundary. A phone on the operator's private tailnet finds the server and signs in with nothing typed.
Config is YAML only (no environment variables). App-side behaviour: [shepherd-onboarding-contract.md](shepherd-onboarding-contract.md).

## `GET /.well-known/herdr-shepherd`

Unauthenticated, secret-free, served by the rakazo bridge (only when `control_plane.rakazo_bridge.enabled`). Response (`Cache-Control: no-store`):

```json
{"name": "herdr-shepherd", "bridge_url": "https://local.example.ts.net", "api_version": 1,
 "auth_modes": ["tailnet", "password"], "tailnet": true}
```

- `bridge_url`: `public_url` from config, returned only when `tailnet` is true (null otherwise, and when unset). The https URL the app uses for everything else.
- `tailnet`: true only when the REAL caller address is a tailnet node per whois. Never leaks user ids, tokens or room names.
- `auth_modes`: what THIS caller can use: `tailnet` (only if `tailnet_open_owner` is on, `tailnet` is true and, when `tailnet_open_owner_users` is set, the user is listed), always `password`.

## Who the caller is (and what whois does not prove)

The socket peer address is looked up with the fabric-trust machinery (`fabric_access.TailscaleWhois`; only 100.64.0.0/10 and
fd7a:115c:a1e0::/48 addresses are looked up, IPv4-mapped forms are unwrapped first). Refused outright: loopback, this host's own
addresses, tagged devices (no user) and any lookup error (fail closed).

`X-Forwarded-For` (last entry) is believed only when the peer is inside `trusted_proxies` (IPv4-mapped forms included). Absent, empty,
malformed or repeated `X-Forwarded-For` headers mean "no client": the request is refused. Rate limiting and attribution use the same
address whois checked. Verdicts are cached 30 s, errors 2 s; one lookup per address is in flight at a time.

**Whois identifies the last-hop node, not necessarily the operator.** Any user on the tailnet passes unless `tailnet_open_owner_users` is
set, and a relay or proxy that does NOT set `X-Forwarded-For` (or is not in `trusted_proxies`) lends its own identity to everyone who
reaches it. Behind `tailscale serve` or an untrusted proxy every peer is loopback, so `tailnet` is false: use a tailnet-reachable
listener, or list the proxy in `trusted_proxies` if it sets a truthful `X-Forwarded-For`.

## Config (`control_plane.rakazo_bridge`)

| Key | Default | Meaning |
|---|---|---|
| `tailnet_open_owner` | `false` | `POST /api/auth/open` gives the first operator account a session to a whois-identified tailnet caller (still needs an app Origin from `open_owner_origins`). Needs an operator account. |
| `tailnet_open_owner_users` | empty | Optional list of tailnet login names (`user@domain`, case-insensitive). Empty or absent allows any tailnet user; when set, the whois user must be listed. |
| `public_url` | none | https URL reported as `bridge_url` to tailnet callers |
| `name` | `herdr-shepherd` | display name reported as `name` |
| `whois_socket` | tailscaled LocalAPI | absolute path of a whois sidecar socket (`herdr-eng fabric whois-proxy`), same as `collab.fabric_trust.whois_socket` |

**Precedence:** when `tailnet_open_owner` is on, whois decides every `/api/auth/open`, even if `open_owner` is also on. The address-range rule
(loopback + tailnet range, no whois) applies only when `open_owner` is on and `tailnet_open_owner` is off. Password sign-in is unchanged.
