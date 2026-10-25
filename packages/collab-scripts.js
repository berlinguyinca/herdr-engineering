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
  body=$(T="$TOKEN" python3 -c 'import os,sys,json;print(json.dumps({"token":os.environ["T"],"instance_id":sys.argv[1]}))' "\${HERDR_INSTANCE_ID:-$(hostname)-$$-$i}")
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
# A room message (or handoff) that addresses this agent wakes it too: the harness gets that text on stdin
# once (only in rooms where the owner approved "execute"); its output is NOT posted back to the room.
# Default does nothing but acknowledge. Task text is a request from a verified room member (or an agent
# they started): it is never spliced into a shell command by this script.
# Started by the host runner it also gets (set in the environment, never by a task):
#   HERDR_SANDBOX=/path/collab-sandbox.sh   run every task inside that box: only the agent's own work
#                                           directory is writable, nothing else on the host is visible
#   HERDR_AUDIT=/path/NAME.log              append a hash-chained line per task: what ran, how it ended,
#                                           and which files in the work directory it added, changed or removed
${PREAMBLE(base)}
CMD="\${HERDR_AGENT_CMD:-cat >/dev/null; echo acknowledged}"
SANDBOX="\${HERDR_SANDBOX:-}"; AUDIT="\${HERDR_AUDIT:-}"
NAME=$(jget name); WORK="$HOME/herdr-agents/work/$NAME"
TMP=$(mktemp -d); mkdir "$TMP/tasks"; trap 'rm -rf "$TMP" "$HDR"' EXIT
audit() {  # audit EVENT [key=value ...]: one line in $AUDIT, each carrying the hash of the line before it
  [ -n "$AUDIT" ] || return 0
  python3 - "$AUDIT" "$@" <<'PY' || true
import hashlib, json, os, sys, time
path, event, *kv = sys.argv[1:]
prev = "0" * 64
try:
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 16384))
        last = fh.read().splitlines()[-1:]
        if last:
            prev = json.loads(last[0])["hash"]
except (OSError, ValueError, KeyError):
    pass
rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "prev": prev}
for a in kv:
    k, _, v = a.partition("=")
    rec[k] = v[:2000]
rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
os.write(fd, (json.dumps(rec, sort_keys=True) + "\\n").encode())
os.close(fd)
PY
}
snap() {  # snap DIR OUT.json: every file in the work directory with its size and mtime
  python3 - "$1" > "$2" <<'PY' || echo '{}' > "$2"
import json, os, sys
root, out = sys.argv[1], {}
for d, _, files in os.walk(root):
    for f in files:
        p = os.path.join(d, f)
        try:
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = [st.st_size, int(st.st_mtime)]
        except OSError:
            pass
print(json.dumps(out))
PY
}
diffsnap() {  # diffsnap BEFORE AFTER: "+added ~changed -removed" names, at most 40
  python3 - "$1" "$2" <<'PY' || true
import json, sys
a, b = (json.load(open(x)) for x in sys.argv[1:3])
rows = [f"+{k}" for k in b if k not in a] + [f"~{k}" for k in b if k in a and a[k] != b[k]] + [f"-{k}" for k in a if k not in b]
print(" ".join(rows[:40]) + (f" (+{len(rows) - 40} more)" if len(rows) > 40 else ""))
PY
}
[ -z "$SANDBOX" ] || mkdir -p -m 0700 "$WORK"
[ -z "$AUDIT" ] || mkdir -p -m 0700 "$(dirname "$AUDIT")"
audit worker_start agent="$NAME" sandbox="\${SANDBOX:+yes}" cmd="\${HERDR_AGENT_CMD:-acknowledge}"
echo "waiting for tasks as $NAME..."
mkdir -p "$TMP/mentions"
MC=-1   # mention cursor: -1 = from now on, so a restarted worker never replays old mentions; "off" = old server
while :; do
  case "$MC" in off) ARGS='{"seconds":20}' ;; *[!0-9-]*|'') MC=-1; ARGS='{"seconds":20,"mentions_after":-1}' ;; *) ARGS=$(printf '{"seconds":20,"mentions_after":%d}' "$MC") ;; esac
  call tasks_wait "$ARGS" > "$TMP/r.json" || { sleep 5; continue; }
  python3 - "$TMP/r.json" "$TMP" <<'PY'
import sys, json
reply = json.load(open(sys.argv[1]))
import re
result = reply["result"]
body = json.loads(result["content"][0]["text"])
if result.get("isError") and "invalid arguments" in str(body.get("error")):
    open(f"{sys.argv[2]}/mc", "w").write("off")     # a server without mention wake-up: tasks only
    body = {}
tasks = body.get("tasks", [])
def own_text(t):
    # The room keeps the literal text. The harness gets it without ONE leading "@<this agent>" (address,
    # name or old name) so the request reads as written to it; any other text stays exactly as typed.
    text, own = t["text"], {n for n in [t.get("target_address"), t.get("target_name"), *(t.get("target_aliases") or [])] if n}
    m = re.match(r"@([A-Za-z0-9._-]{1,48})\\s+(?=\\S)", text)
    if m and m.group(1).lower() in own:
        text = text[m.end():]
    return text
for t in tasks:
    open(f"{sys.argv[2]}/tasks/{t['task_id']}.txt", "w").write(own_text(t))
    print(t["task_id"])
for m in body.get("mentions", []):
    open(f"{sys.argv[2]}/mentions/{int(m['sequence']):012d}.txt", "w").write(own_text(m))
if isinstance(body.get("mention_cursor"), int) and not isinstance(body.get("mention_cursor"), bool):
    open(f"{sys.argv[2]}/mc", "w").write(str(body["mention_cursor"]))
PY
  [ ! -s "$TMP/mc" ] || MC=$(cat "$TMP/mc")
  for f in "$TMP"/tasks/*.txt; do
    [ -e "$f" ] || continue
    tid=$(basename "$f" .txt)
    call task_update "$(python3 -c 'import sys,json;print(json.dumps({"task_id":sys.argv[1],"status":"accepted"}))' "$tid")" >/dev/null || true
    status=done; rc=0
    audit task_start task_id="$tid" text_sha256="$(sha256sum "$f" | cut -d' ' -f1)" text_bytes="$(wc -c < "$f")"
    [ -z "$SANDBOX" ] || snap "$WORK" "$TMP/before.json"
    started=$(date +%s)
    # a harness whose model never answers must not stall this agent for ever
    if [ -n "$SANDBOX" ]; then
      timeout "\${HERDR_TASK_TIMEOUT:-900}" "$SANDBOX" "$WORK" "$CMD" < "$f" > "$TMP/out.txt" 2>&1 || rc=$?
    else
      timeout "\${HERDR_TASK_TIMEOUT:-900}" bash -c "$CMD" < "$f" > "$TMP/out.txt" 2>&1 || rc=$?
    fi
    [ "$rc" -eq 0 ] || status=failed
    changes=""
    if [ -n "$SANDBOX" ]; then snap "$WORK" "$TMP/after.json"; changes=$(diffsnap "$TMP/before.json" "$TMP/after.json"); fi
    audit task_end task_id="$tid" status="$status" exit="$rc" seconds="$(( $(date +%s) - started ))" output_bytes="$(wc -c < "$TMP/out.txt")" work_changes="$changes"
    args=$(python3 -c 'import sys,json;print(json.dumps({"task_id":sys.argv[1],"status":sys.argv[2],"result":open(sys.argv[3]).read()[:3500]}))' "$tid" "$status" "$TMP/out.txt")
    call task_update "$args" >/dev/null || true
    echo "$tid $status"
    rm -f "$f"
  done
  for f in "$TMP"/mentions/*.txt; do      # a mention: the harness reads it once; nothing is posted back
    [ -e "$f" ] || continue
    seq=$(basename "$f" .txt); rc=0
    audit mention_start sequence="$seq" text_sha256="$(sha256sum "$f" | cut -d' ' -f1)" text_bytes="$(wc -c < "$f")"
    if [ -n "$SANDBOX" ]; then
      timeout "\${HERDR_TASK_TIMEOUT:-900}" "$SANDBOX" "$WORK" "$CMD" < "$f" > "$TMP/out.txt" 2>&1 || rc=$?
    else
      timeout "\${HERDR_TASK_TIMEOUT:-900}" bash -c "$CMD" < "$f" > "$TMP/out.txt" 2>&1 || rc=$?
    fi
    audit mention_end sequence="$seq" exit="$rc" output_bytes="$(wc -c < "$TMP/out.txt")"
    echo "mention $seq exit $rc"
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
