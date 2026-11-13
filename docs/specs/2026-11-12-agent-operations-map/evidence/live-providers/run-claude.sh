#!/bin/sh
L=/private/tmp/claude-501/lp
A=~/IdeaProjects/herdr-engineering/.claude/worktrees/agent-ae7b3d9d01511ff31/deploy/agent-ops-adapters/claude.js
mkdir -p $L/claude-cwd
CMD="sh -c 'tee -a $L/hook-in.jsonl | node $A'"
cat > $L/claude-settings.json <<EOF
{"hooks":{
 "SessionStart":[{"hooks":[{"type":"command","command":"$CMD"}]}],
 "PostToolUse":[{"hooks":[{"type":"command","command":"$CMD"}]}],
 "Stop":[{"hooks":[{"type":"command","command":"$CMD"}]}],
 "SessionEnd":[{"hooks":[{"type":"command","command":"$CMD"}]}]
}}
EOF
export HERDR_AGENT_OPS=1 HERDR_AGENT_OPS_URL=http://127.0.0.1:65157 HERDR_AGENT_OPS_API_KEY=ing-<THROWAWAY-TOKEN> HERDR_AGENT_OPS_SCOPE=org
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN
cd $L/claude-cwd
timeout 120 ~/.local/bin/claude -p "reply with the word ok" --model haiku --setting-sources project \
  --settings $L/claude-settings.json --no-session-persistence --tools "" 2>&1 | tail -20
echo "exit=$?"
