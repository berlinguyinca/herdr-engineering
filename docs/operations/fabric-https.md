# Fabric HTTPS (`https://dev.lan:<port>`)

The gateway serves HTTPS on **the same port** as plain HTTP: the first byte of a connection selects the protocol
(`0x16` = TLS handshake). `http://dev.lan:<port>` keeps working; a failed TLS handshake is dropped, never downgraded.
TLS terminates at the gateway. The origin receives `X-Forwarded-Proto: https`, `X-Forwarded-Host` and
`X-Forwarded-For` (client-supplied copies are discarded), and redirect `Location` headers that point at the
origin's loopback address are rewritten to the `https://dev.lan:<port>` address. Each TLS exchange is a single request
(`Connection: close`); WebSocket upgrades, SSE and HMR streams pass through untouched.
Leases with same-origin `routes` are TLS-terminated but not header-rewritten.

## One-time setup (gateway host)

```bash
herdr-eng devfabric ca init                  # name-constrained root, valid for dev.lan only (10 years)
herdr-eng devfabric ca issue                 # dev.lan + *.dev.lan, 397 days, EC P-256
herdr-eng devfabric gateway --bind ... --tls-cert ~/.config/herdr-engineering/ca/gateway.pem \
                                       --tls-key  ~/.config/herdr-engineering/ca/gateway.key
```

Keys live in `~/.config/herdr-engineering/ca/` (0700, keys 0600) and are never committed. The root is
**name-constrained to the domains given to `ca init`**, so trusting it on a device cannot be abused to impersonate
any other site. Renew before expiry: `herdr-eng devfabric ca status` reports `renewal_due`; re-run `ca issue` and restart
the gateway (leases survive; they live in the registry). `fabric.tls_cert` / `fabric.tls_key` in the config are
accepted instead of the flags.

## Enrolling devices

`herdr-eng devfabric ca export --out <dir>` writes `fabric-ca.pem` and `fabric-ca.mobileconfig` (public data only).
Verify the SHA-256 fingerprint it prints on the device before trusting.

| Device | Steps |
|---|---|
| macOS | Open `fabric-ca.mobileconfig` (or import the `.pem` into Keychain), then Keychain Access > the certificate > Trust > "Always Trust" for SSL. |
| iPhone / iPad | AirDrop or open the `.mobileconfig`, Settings > "Profile Downloaded" > Install. **Then** Settings > General > About > Certificate Trust Settings > enable full trust for "Herdr Fabric CA". Without this second step Safari still rejects the certificate. The profile is unsigned, so iOS labels it "Not Signed". |
| Linux / CLI | `curl --cacert fabric-ca.pem https://dev.lan:<port>/` or install into the system store. |

Devices must also resolve `dev.lan` (Tailscale split-DNS to the fabric's CoreDNS; see `deploy/fabric-stack/README.md`).

## Verification status
Automated: chain, SAN (apex + wildcard), EKU, name constraints, 397-day cap, same-port HTTP/HTTPS, redirects, SSE,
upgrade passthrough, forwarded-header spoofing, handshake failure. **Not verified by automation:** acceptance on a real
Mac and iPhone (trust UI, Safari). Run the table above and report back.
