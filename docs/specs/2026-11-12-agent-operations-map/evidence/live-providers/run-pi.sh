#!/bin/sh
L=/private/tmp/claude-501/lp
mkdir -p $L/pi-agent $L/pi-home
export PI_CODING_AGENT_DIR=$L/pi-agent HOME=$L/pi-home PI_OFFLINE=1
export HERDR_AGENT_OPS=1 HERDR_AGENT_OPS_URL=http://127.0.0.1:65157 HERDR_AGENT_OPS_API_KEY=ing-<THROWAWAY-TOKEN> HERDR_AGENT_OPS_SCOPE=org
cd $L
timeout 90 ~/.local/bin/pi --offline --no-extensions --no-mcp --no-skills --no-session \
  -e ~/IdeaProjects/herdr-engineering/.claude/worktrees/agent-ae7b3d9d01511ff31/deploy/agent-ops-adapters/pi.js \
  -p "reply with the word ok" 2>&1 | tail -30
echo "exit=$?"
ls -la $L/pi-agent
