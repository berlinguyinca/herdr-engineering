#!/usr/bin/env bash
# worker.sh AGENT.json — wait for tasks, run HERDR_AGENT_CMD on each, report the result.
#   HERDR_AGENT_CMD='claude -p'   # task text arrives on stdin; its stdout becomes the result
# Default does nothing but acknowledge. Task text is a request from a verified room member (or an agent
# they started): it is never spliced into a shell command by this script.
set -euo pipefail
BASE='__HERDR_BASE__'
F="${1:?first argument: the agent file written by register.sh}"
jget() { python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$F" "$1"; }
CRED=$(jget credential); DEV=$(jget device_id)
HDR=$(mktemp); trap 'rm -f "$HDR"' EXIT; chmod 600 "$HDR"   # the credential goes in a file, never in argv
printf 'Authorization: Agent %s\nX-Herdr-Device: %s\n' "$CRED" "$DEV" > "$HDR"
call() {  # call TOOL JSON_ARGS  -> the MCP JSON-RPC reply on stdout
  python3 -c 'import sys,json;print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":sys.argv[1],"arguments":json.loads(sys.argv[2])}}))' "$1" "$2" |
  curl -fsS -X POST "$BASE/mcp" -H 'content-type: application/json' -H 'accept: application/json' \
    -H @"$HDR" --data-binary @-
}

CMD="${HERDR_AGENT_CMD:-cat >/dev/null; echo acknowledged}"
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
