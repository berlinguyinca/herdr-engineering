// Copy-paste bash for fleets of agents. Everything here is a pure string builder: the join token is
// the only secret and it is embedded by the caller, in the browser, once. Task text and room text
// never reach a shell: they travel through files, stdin and argv, never through string splicing.

// POSIX single-quote a value so it can sit in a shell script verbatim.
export const shq = (v) => `'${String(v).replace(/'/g, `'\\''`)}'`;

const JGET = `jget() { python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$F" "$1"; }`;

const CALL = `HDR=$(mktemp); trap 'rm -f "$HDR"' EXIT; chmod 600 "$HDR"   # the credential goes in a file, never in argv
printf 'Authorization: Agent %s\\nX-Herdr-Device: %s\\n' "$CRED" "$DEV" > "$HDR"
call() {  # call TOOL JSON_ARGS  -> the MCP JSON-RPC reply on stdout
  python3 -c 'import sys,json;print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":sys.argv[1],"arguments":json.loads(sys.argv[2])}}))' "$1" "$2" |
  curl -fsS -X POST "$BASE/mcp" -H 'content-type: application/json' -H 'accept: application/json' \\
    -H @"$HDR" --data-binary @-
}`;

const PREAMBLE = (base) => `set -euo pipefail
BASE=${shq(base)}
F="\${1:?first argument: the agent file written by register.sh}"
${JGET}
CRED=$(jget credential); DEV=$(jget device_id)
${CALL}
`;

export function registerScript({ base, token, count, prefix }) {
  return `#!/usr/bin/env bash
# Registers up to ${Number(count)} agents named ${prefix}-N. Each gets its own file in $OUT with a private credential.
# They have NO access until the room owner approves them on the dashboard.
set -euo pipefail
BASE=${shq(base)}
TOKEN=${shq(token)}
COUNT="\${1:-${Number(count)}}"
OUT="\${HERDR_AGENT_DIR:-$HOME/herdr-agents}"
umask 077; mkdir -p "$OUT"
for i in $(seq 1 "$COUNT"); do
  body=$(T="$TOKEN" python3 -c 'import os,sys,json;print(json.dumps({"token":os.environ["T"],"instance_id":sys.argv[1]}))' "$(hostname)-$$-$i")
  resp=$(curl -fsS -X POST "$BASE/api/v1/collab/agents/join" -H 'content-type: application/json' -d "$body")
  name=$(printf '%s' "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["name"])')
  printf '%s\\n' "$resp" > "$OUT/$name.json"
  echo "registered $name -> $OUT/$name.json"
done
echo "Now ask the room owner to approve them (dashboard: room > Agent requests > Approve all)."
`;
}

export function workerScript({ base }) {
  return `#!/usr/bin/env bash
# worker.sh AGENT.json — wait for tasks, run HERDR_AGENT_CMD on each, report the result.
#   HERDR_AGENT_CMD='claude -p'   # task text arrives on stdin; its stdout becomes the result
# Default does nothing but acknowledge. Task text is a request from a verified room member (or an agent
# they started): it is never spliced into a shell command by this script.
${PREAMBLE(base)}
CMD="\${HERDR_AGENT_CMD:-cat >/dev/null; echo acknowledged}"
TMP=$(mktemp -d); mkdir "$TMP/tasks"; trap 'rm -rf "$TMP" "$HDR"' EXIT
echo "waiting for tasks as $(jget name)..."
while :; do
  call tasks_wait '{"seconds":20}' > "$TMP/r.json" || { sleep 5; continue; }
  python3 - "$TMP/r.json" "$TMP" <<'PY'
import sys, json
reply = json.load(open(sys.argv[1]))
tasks = json.loads(reply["result"]["content"][0]["text"]).get("tasks", [])
for t in tasks:
    open(f"{sys.argv[2]}/tasks/{t['task_id']}.txt", "w").write(t["text"])
    print(t["task_id"])
PY
  for f in "$TMP"/tasks/*.txt; do
    [ -e "$f" ] || continue
    tid=$(basename "$f" .txt)
    call task_update "$(python3 -c 'import sys,json;print(json.dumps({"task_id":sys.argv[1],"status":"accepted"}))' "$tid")" >/dev/null || true
    status=done
    bash -c "$CMD" < "$f" > "$TMP/out.txt" 2>&1 || status=failed
    args=$(python3 -c 'import sys,json;print(json.dumps({"task_id":sys.argv[1],"status":sys.argv[2],"result":open(sys.argv[3]).read()[:3500]}))' "$tid" "$status" "$TMP/out.txt")
    call task_update "$args" >/dev/null || true
    echo "$tid $status"
    rm -f "$f"
  done
done
`;
}

export function delegateScript({ base }) {
  return `#!/usr/bin/env bash
# delegate.sh AGENT.json ROOM_ID TARGET_AGENT_ID PARENT_TASK_ID "what to do"
# An agent only delegates part of a task it accepted: PARENT_TASK_ID is that task.
${PREAMBLE(base)}
ROOM="\${2:?room id}"; TARGET="\${3:?target agent id}"; PARENT="\${4:?parent task id}"; TEXT="\${5:?task text}"
args=$(python3 -c 'import sys,json;print(json.dumps({"room_id":sys.argv[1],"target_agent_id":sys.argv[2],"parent_task_id":sys.argv[3],"text":sys.argv[4]}))' "$ROOM" "$TARGET" "$PARENT" "$TEXT")
call task_create "$args"
`;
}

export function agentsScript({ base }) {
  return `#!/usr/bin/env bash
# agents.sh AGENT.json [ROOM_ID] — list the rooms this agent can use, or the agents in one room.
${PREAMBLE(base)}
if [ -n "\${2:-}" ]; then
  call agents_list "$(python3 -c 'import sys,json;print(json.dumps({"room_id":sys.argv[1]}))' "$2")"
else
  call rooms_list '{}'
fi
`;
}

// One paste: writes the four scripts and registers the agents.
export function bundleScript(opts) {
  const files = {
    'register.sh': registerScript(opts), 'worker.sh': workerScript(opts),
    'delegate.sh': delegateScript(opts), 'agents.sh': agentsScript(opts),
  };
  const parts = ['#!/usr/bin/env bash', 'set -euo pipefail',
    'DIR="${HERDR_AGENT_DIR:-$HOME/herdr-agents}"', 'umask 077; mkdir -p "$DIR"'];
  for (const [name, body] of Object.entries(files)) {
    if (body.includes('HERDR_EOF')) throw new Error('script contains the heredoc marker');
    parts.push(`cat > "$DIR/${name}" <<'HERDR_EOF'\n${body}HERDR_EOF`, `chmod 700 "$DIR/${name}"`);
  }
  parts.push('"$DIR/register.sh"', 'echo "Scripts are in $DIR (worker.sh, delegate.sh, agents.sh)."');
  return `${parts.join('\n')}\n`;
}

// Tailnet shortcut: the dashboard owner created the agents directly; this turns the credentials the
// page already holds into the same per-agent files, with no token and no public-site call.
export function filesFromBulk(base, agents) {
  return agents.map((a) => ({
    name: `${a.name}.json`,
    text: `${JSON.stringify({ agent_id: a.agent_id, name: a.name, credential: a.credential, device_id: a.device_id, expires_at: a.expires_at }, null, 2)}\n`,
    base,
  }));
}
