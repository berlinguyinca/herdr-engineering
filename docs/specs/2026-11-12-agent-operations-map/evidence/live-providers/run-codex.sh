#!/bin/sh
L=${SCRATCH:-/tmp/ao-live}
A=${REPO:-$HOME/IdeaProjects/herdr-engineering}/deploy/agent-ops-adapters/codex.js
mkdir -p $L/codex-cwd
export HERDR_AGENT_OPS=1 HERDR_AGENT_OPS_URL=http://127.0.0.1:65157 HERDR_AGENT_OPS_API_KEY=ing-<THROWAWAY-TOKEN> HERDR_AGENT_OPS_SCOPE=org
cd $L/codex-cwd
touch $L/codex-marker
timeout 150 /opt/homebrew/bin/codex exec --skip-git-repo-check -s read-only \
  -c "notify=[\"node\",\"$A\",\"notify\"]" "reply with the word ok" 2>&1 | tail -15
echo "exit=$?"
find ~/.codex/sessions -name 'rollout-*.jsonl' -newer $L/codex-marker
