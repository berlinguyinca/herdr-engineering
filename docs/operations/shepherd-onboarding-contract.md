# Shepherd app onboarding contract (app side)

Server side: [shepherd-discovery.md](shepherd-discovery.md). Goal: a phone on the operator's tailnet onboards with zero typing.

## Probe order

1. **Baked-in default URL.** The app ships with a default bridge URL (the operator's `public_url`, e.g. `https://local.example.ts.net`). No prompt.
2. **Well-known probe.** `GET <url>/.well-known/herdr-shepherd`, short timeout (3 s), no auth, no cookies. Check `api_version` is one the app supports; on a bad version show "Update the app".
3. **Use the flag.**
   - `tailnet: true` and `"tailnet"` in `auth_modes`: call `POST <bridge_url or url>/api/auth/open` with `Origin: rakazo://` (or the configured app origin), empty JSON body. A 200 returns `{token}`: store it in the keychain and go to the home screen. Nothing is shown to the user.
   - `tailnet: true` but no `"tailnet"` in `auth_modes`: the server has not enabled it; show the password sign-in (this is the only screen with typing).
   - `tailnet: false`: show the **Connect Tailscale** screen.
4. Use `bridge_url` from the response for all later calls when it is non-null (it may differ from the probed URL).

## Connect Tailscale screen (tailnet is false)

Static copy, no inputs: "This server is only reachable from your Tailscale network." A single primary button **Open Tailscale** (deep link to the Tailscale app, App Store link if absent) and a secondary **Try again** that re-runs the probe. The probe is re-run automatically when the app returns to the foreground. No URL field, no token field, no allowlist questions.

## Failure handling

- Probe timeout or network error: same **Connect Tailscale** screen (an off-tailnet phone usually cannot reach the host at all).
- `POST /api/auth/open` 403: the server refused this caller (whois could not identify it): show **Connect Tailscale**. 429: wait `Retry-After` and retry once silently.
- A stored token that stops working (401): discard it and repeat the probe flow; never prompt for it.
- Never log tokens. The well-known response carries none.
