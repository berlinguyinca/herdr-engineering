# Shepherd phone app: tailnet discovery and zero-typing sign-in

The tailnet is the trust boundary. A phone on the operator's private tailnet finds the server and signs in with nothing typed.
Config is YAML only (no environment variables). App-side behaviour: [shepherd-onboarding-contract.md](shepherd-onboarding-contract.md).

## `GET /.well-known/herdr-shepherd`

Unauthenticated, secret-free, served by the rakazo bridge (only when `control_plane.rakazo_bridge.enabled`). Response (`Cache-Control: no-store`):

```json
{"name": "herdr-shepherd", "bridge_url": "https://local.example.ts.net", "api_version": 1,
 "auth_modes": ["tailnet", "password"], "tailnet": true}
```

- `bridge_url`: `public_url` from config (null when unset). The https URL the app uses for everything else.
- `tailnet`: true only when the REAL caller address is a tailnet node per whois. Never leaks user ids, tokens or room names.
- `auth_modes`: what THIS caller can use: `tailnet` (only if `tailnet_open_owner` is on and `tailnet` is true), always `password`.

## Who the caller is

The socket peer address is looked up with the fabric-trust machinery (`fabric_access.TailscaleWhois`; only 100.64.0.0/10 and
fd7a:115c:a1e0::/48 addresses are looked up, loopback never qualifies). `X-Forwarded-For` (last entry) is believed only when the
peer is inside `trusted_proxies`. Any whois error means "not a tailnet member" (fail closed). Verdicts are cached for 30 s.
Behind `tailscale serve` or a proxy that is not in `trusted_proxies` every peer is loopback, so `tailnet` is false: point the app
at a tailnet-reachable listener, or list the proxy in `trusted_proxies` if it sets a truthful `X-Forwarded-For`.

## Config (`control_plane.rakazo_bridge`)

| Key | Default | Meaning |
|---|---|---|
| `tailnet_open_owner` | `false` | `POST /api/auth/open` gives the first operator account a session to a whois-identified tailnet caller (still needs an app Origin from `open_owner_origins`). Needs an operator account. |
| `public_url` | none | https URL reported as `bridge_url` |
| `name` | `herdr-shepherd` | display name reported as `name` |
| `whois_socket` | tailscaled LocalAPI | absolute path of a whois sidecar socket (`herdr-eng fabric whois-proxy`), same as `collab.fabric_trust.whois_socket` |

`open_owner` is unchanged (address-range rule, no whois). Password sign-in is unchanged.
