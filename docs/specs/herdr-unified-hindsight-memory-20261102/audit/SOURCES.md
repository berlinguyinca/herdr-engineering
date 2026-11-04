# Source audit trail, 2026-11-02

Read-only GitHub connector paths fetched directly:
- https://github.com/berlinguyinca/herdr-engineering/blob/main/README.md
- https://github.com/berlinguyinca/herdr-engineering/blob/main/scripts/install.sh
- https://github.com/berlinguyinca/herdr-engineering/blob/main/herdr_engineering/control_plane/rakazo_hindsight.py
- https://github.com/berlinguyinca/herdr-engineering/blob/main/docs/operations/assistant-hub.md
- https://github.com/berlinguyinca/herdr-engineering/blob/main/docs/operations/handoff.md
- https://github.com/berlinguyinca/herdr-engineering/blob/main/docs/specs/herdr-unified-workspace-20261028/audit/MATRIX.md
- https://github.com/berlinguyinca/herdr-engineering/blob/main/docs/specs/herdr-unified-workspace-20261028/audit/SMOKE.md
- https://github.com/berlinguyinca/pi-engineering/blob/main/README.md
- https://github.com/berlinguyinca/pi-engineering/blob/main/src/blackhole/interactiveMemory.ts
- https://github.com/berlinguyinca/pi-engineering/blob/main/src/blackhole/OpenViking.ts
- https://github.com/berlinguyinca/herdr-handoff/blob/main/README.md
- https://github.com/berlinguyinca/herdr-handoff/blob/main/install/install.sh
- https://github.com/berlinguyinca/herdr-fleet/blob/main/README.md

Code search on these repos for Hindsight, memory, install, OpenViking. Not an exhaustive checkout / AST audit.

Unresolved:
- `berlinguyinca/herdr-memory` GET README.md returned 404 to connector; ask on-host git remotes, lock entries, package installation metadata, `pip show herdr-memory`, container labels, source checkout, CI builds; don't invent path or assume deletion.
- Current live Hindsight server version, health, bank inventory, token scopes, deployment SHA, backup status, retention data, and detailed endpoint schemas were not verified.
- Current open PR list / branch diff / running host sessions not verified.
- Real Codex and OpenCode installed versions and hook surfaces not verified.

Mandatory implementation-audit command ideas (run only in authorized checked-out repos; read-only first):
```bash
git status --short; git remote -v; git log -1 --format='%H %cI'
rg -n 'Hindsight|herdr.memory|OpenViking|Blackhole|memory_search|retain|recall|forget' .
rg -n 'harness|MCP|skills|install|handoff|memory' scripts/ install/ packages/ src/ 2>/dev/null
rg -n 'herdr-memory|hindsight' lock/ deploy/ docs/ 2>/dev/null
```
Produce `audit/LIVE_EVIDENCE.md` with URLs, exact SHAs, API responses redacted, container health, and permissions. Never read or echo secret values. Record UNKNOWN instead of assuming.
