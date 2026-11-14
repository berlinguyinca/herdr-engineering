# After screenshots: web Agent Operations map

Route: `#/agent-ops` (nav entry "Agent Operations", shown only while the server advertises `agent_ops_v1`).

Server (real control plane, seeded through the real `POST /api/v1/ingest`):

    python tests/playwright/agent-ops-server.py        # prints PORT=<n>; open http://127.0.0.1:<n>/index.html#/agent-ops
    WORK_CENTER=1 python tests/playwright/agent-ops-server.py   # also advertises work_center_v1 (Overview shows the task-lineage card)

Files: `map-{light,dark}-{1440,820,390}.png`, `map-replay-light-1440.png` (scrubbed to event 13 of 21),
`map-reduced-motion-light-1440.png` (prefers-reduced-motion: no particles, no animation, map still complete).
The fixture is org-scoped, so agents appear in the "No room" cluster.
