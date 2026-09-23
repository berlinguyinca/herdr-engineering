# Mandatory Upstream Audit

Before production code, inspect current HerdR plugin APIs, marketplace, official examples, socket/event/session APIs, persistence, file/browser/mobile plugins and packaging.

Research leads to independently verify:
- StructuPath/herdr-browser
- eyalev/herdr-web
- plannotator/herdr-plannotator
- StructuPath/herdr-swarm
- serhii-chernenko/herdr-worktreeinclude
- JonasBaeumer/herdr-file-annotator
- itisbryan/herdr-gh-checks
- cdowell09/herdr-pr-board
- quinnjr/herdr-notifications
- 0x5c0f/herdr-insight
- jomarmontuya/herdr-file-viewer
- MatheusBBarni/herdr-tasks
- husniadil/herdr-dispatch
- barnuri/herdr-telegram-notifications
- powerfooI/roamgate
- tyler-jewell/herdr-plugins

For each verify existence/canonical repo, manifest/API compatibility, maintenance, license, platforms, install hooks, permissions, ports/network/credential behavior and overlap. Search for better current alternatives.

Classify ADOPT / OPTIONAL / ADAPT / HOLD / REJECT and write `docs/upstream-audit.md`.
