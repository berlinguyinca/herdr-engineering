#!/bin/sh
# Real Pi model turn against a throwaway local control plane (server.py). Uses the operator's own Pi config in
# place (read-only; no session saved). Run Pi with stdin closed: `pi -p` waits for stdin EOF when stdin is not a TTY.
# Usage: TOK=<throwaway> PORT=<server.py port> REPO=<repo checkout> ./run-pi-real.sh
HERDR_AGENT_OPS=1 HERDR_AGENT_OPS_URL=http://127.0.0.1:$PORT HERDR_AGENT_OPS_API_KEY=ing-$TOK HERDR_AGENT_OPS_SCOPE=org \
  timeout 100 pi --no-session --no-extensions --no-mcp --no-skills --model metabolomics/qwen3.8-flash_next \
  -e "$REPO/deploy/agent-ops-adapters/pi.js" -p "reply with the word ok" </dev/null
