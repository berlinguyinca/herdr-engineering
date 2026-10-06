# herdr-web-client

Structured API + live-event client for the HerdR control plane (Spec §45, §52,
§92). The web UI is a **client** of the same `/api/v1` JSON and SSE event
stream the rest of the platform consumes — it never scrapes terminal, Docker,
Slurm, or HTML.

## Modules

- `client.js` — `HerdrClient` (missions, sessions, services, hosts, activity,
  analytics, message send, session actions, health). `fetch` is injected so it
  is unit-testable with `node --test`.
- `events.js` — `connectEvents()` SSE live-update helper (injectable
  `createEventSource`; auto-reconnect, malformed-frame-safe).

## Test

```bash
node --test herdr-web-client/client.test.js
```

## Usage

```js
import { HerdrClient } from './client.js';
const client = new HerdrClient('/');          // same-origin
const { missions } = await client.listMissions();
const { sessions } = await client.listSessions();
await client.sendMessage('session_s1', 'steer: use pytest', ['artifact_a1']);
```
