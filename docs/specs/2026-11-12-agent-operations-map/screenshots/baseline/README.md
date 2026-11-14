# Baseline screenshots (current dashboard, before agent-ops-map)

Files: `<page>-<light|dark>-<1440|820|390>.png` (viewports 1440x900, 820x1180, 390x844; viewport-sized, not full page). 72 PNGs.

Seeded server (SPA, hash routes):
`BURST=0 WORK_CENTER=1 RELATED_SEED=1 PR_TRAIL_SEED=1 AGENT_EVOLUTION_SEED=1 PI_ACTIVITY_SEED=1 LLM_SEED=1 CI_OFFLINE=1 python3 tests/playwright/server.py`
(needs nh3, slack_sdk, etc.; run with a venv outside the repo). Theme set via localStorage `herdr.theme` plus colorScheme.

| page prefix | route | notes |
|---|---|---|
| overview | #/overview | Work Command Center is on (WORK_CENTER=1), shown on Overview |
| attention | #/attention | |
| agents | #/herd | |
| missions | #/missions | |
| handoffs | #/handoffs | |
| llm-usage | #/llm | Usage / Models |
| hosts | #/hosts | |
| collab-seeded | #/collab | renders "collaboration network is not enabled" (capability off) |
| teams-seeded | #/teams | renders "Teams are not turned on" |
| team-map-seeded | #/team-map | stuck on "Loading first update / Enter a project" (needs unified_workspace_v1, absent here) |
| collab-real, team-map-real | real_server.py root | `python3 tests/playwright/real_server.py '{"graph_nodes":0}'`; serves the standalone collab sign-in page, not the SPA; both routes show the same sign-in screen (no token entered) |

Failed to render meaningfully: collab-seeded, teams-seeded, team-map-seeded, collab-real, team-map-real (empty/disabled states, captured as-is). No separate Sessions page exists in NAV; no dedicated Work Command Center route (it is part of #/overview).
