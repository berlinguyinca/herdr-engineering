#!/usr/bin/env bash
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
#   HERDR_SKILL_CMD='...'                    runs a task the SERVER marked as a skill call
#                                           (skill: {name}) using the harness-native prompt. Without it, or for
#                                           anything unmarked, HERDR_AGENT_CMD runs as usual.
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
SKILL_CMD="${HERDR_SKILL_CMD:-}"
SANDBOX="${HERDR_SANDBOX:-}"; AUDIT="${HERDR_AUDIT:-}"
HARNESS=$(jget harness_type)
if [ -z "$SKILL_CMD" ]; then
  case "$HARNESS" in
    pi) SKILL_CMD='pi -p --no-session -- "$(cat)"' ;;
    claude-code) SKILL_CMD='claude -p' ;;
    codex) SKILL_CMD='codex exec --skip-git-repo-check -' ;;
    opencode) SKILL_CMD='opencode run' ;;
  esac
fi
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
os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode())
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
report_skills() {
  skills=$(python3 - "$HARNESS" <<'PY'
import json, os, re, sys
from pathlib import Path
harness = sys.argv[1]
home = Path.home()
roots = [home / ".agents" / "skills", home / ".claude" / "skills"]
if harness == "pi" and os.environ.get("PI_CODING_AGENT_DIR"):
    roots.append(Path(os.environ["PI_CODING_AGENT_DIR"]) / "skills")
if harness == "codex":
    roots.append(home / ".codex" / "skills")
    if os.environ.get("CODEX_HOME"):
        roots.append(Path(os.environ["CODEX_HOME"]) / "skills")
if harness == "opencode":
    roots.append(home / ".config" / "opencode" / "skills")
seen, out = set(), []
pattern = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
for root in roots:
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        continue
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir() or not pattern.fullmatch(entry.name):
            continue
        try:
            manifest = (entry / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if not re.search(r"(?m)^description:\s*\S", manifest):
            continue
        skill = "skill:" + entry.name
        if skill not in seen:
            seen.add(skill); out.append(skill)
        if len(out) >= 100:
            break
    if len(out) >= 100:
        break
print(json.dumps({"skills": sorted(out)}, separators=(",", ":")))
PY
  )
  call agent_capabilities "$skills" >/dev/null 2>&1 || true
}
report_skills
[ -z "$SANDBOX" ] || mkdir -p -m 0700 "$WORK"
[ -z "$AUDIT" ] || mkdir -p -m 0700 "$(dirname "$AUDIT")"
audit worker_start agent="$NAME" sandbox="${SANDBOX:+yes}" cmd="${HERDR_AGENT_CMD:-acknowledge}"
echo "waiting for tasks as $NAME..."
mkdir -p "$TMP/mentions"
MC=-1   # mention cursor: -1 = from now on, so a restarted worker never replays old mentions; "off" = old server
while :; do
  case "$MC" in off) ARGS='{"seconds":20}' ;; *[!0-9-]*|'') MC=-1; ARGS='{"seconds":20,"mentions_after":-1}' ;; *) ARGS=$(printf '{"seconds":20,"mentions_after":%d}' "$MC") ;; esac
  call tasks_wait "$ARGS" > "$TMP/r.json" || { sleep 5; continue; }
  python3 - "$TMP/r.json" "$TMP" "$HARNESS" <<'PY'
import sys, json
reply = json.load(open(sys.argv[1]))
import re
result = reply["result"]
body = json.loads(result["content"][0]["text"])
if result.get("isError") and "invalid arguments" in str(body.get("error")):
    open(f"{sys.argv[2]}/mc", "w").write("off")     # a server without mention wake-up: tasks only
    body = {}
tasks = body.get("tasks", [])
harness = sys.argv[3]
def own_text(t):
    # The room keeps the literal text. The harness gets it without ONE leading "@<this agent>" (address,
    # name or old name) so the request reads as written to it; any other text stays exactly as typed.
    text, own = t["text"], {n for n in [t.get("target_address"), t.get("target_name"), *(t.get("target_aliases") or [])] if n}
    m = re.match(r"@([A-Za-z0-9._-]{1,48})\s+(?=\S)", text)
    if m and m.group(1).lower() in own:
        text = text[m.end():]
    return text
def save(path, t):
    # Only the SERVER's mark makes a skill call, and only when the text really is "/skill:<that name>" then
    # whitespace or nothing: the worker translates the canonical call to the harness-native skill prompt. The
    # whole prompt stays ONE argument after "--", so an "@" or "-" inside the args is never a file or a flag.
    text, sk = own_text(t), t.get("skill")
    name = sk.get("name") if isinstance(sk, dict) else None
    if isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
        m = re.fullmatch(r"/skill:" + re.escape(name) + r"(?:\s+(.*))?", text, re.S)
        if m:
            args = (m.group(1) or "").strip()
            prefix = {"pi": f"/skill:{name}", "claude-code": f"/{name}",
                      "codex": f"${name}", "opencode": f"@{name}"}.get(harness, f"/skill:{name}")
            text = prefix + (f" {args}" if args else "")
            open(path[:-4] + ".skill", "w").write(name)
    open(path, "w").write(text)
for t in tasks:
    save(f"{sys.argv[2]}/tasks/{t['task_id']}.txt", t)
    print(t["task_id"])
for m in body.get("mentions", []):
    save(f"{sys.argv[2]}/mentions/{int(m['sequence']):012d}.txt", m)
if isinstance(body.get("mention_cursor"), int) and not isinstance(body.get("mention_cursor"), bool):
    open(f"{sys.argv[2]}/mc", "w").write(str(body["mention_cursor"]))
PY
  [ ! -s "$TMP/mc" ] || MC=$(cat "$TMP/mc")
  for f in "$TMP"/tasks/*.txt; do
    [ -e "$f" ] || continue
    tid=$(basename "$f" .txt)
    call task_update "$(python3 -c 'import sys,json;print(json.dumps({"task_id":sys.argv[1],"status":"accepted"}))' "$tid")" >/dev/null || true
    status=done; rc=0; run="$CMD"; skill=""
    if [ -e "${f%.txt}.skill" ]; then skill=$(cat "${f%.txt}.skill"); rm -f "${f%.txt}.skill"; [ -z "$SKILL_CMD" ] || run="$SKILL_CMD"; fi
    audit task_start task_id="$tid" text_sha256="$(sha256sum "$f" | cut -d' ' -f1)" text_bytes="$(wc -c < "$f")" skill="$skill"
    [ -z "$SANDBOX" ] || snap "$WORK" "$TMP/before.json"
    started=$(date +%s)
    # a harness whose model never answers must not stall this agent for ever
    if [ -n "$SANDBOX" ]; then
      timeout "${HERDR_TASK_TIMEOUT:-900}" "$SANDBOX" "$WORK" "$run" < "$f" > "$TMP/out.txt" 2>&1 || rc=$?
    else
      timeout "${HERDR_TASK_TIMEOUT:-900}" bash -c "$run" < "$f" > "$TMP/out.txt" 2>&1 || rc=$?
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
    seq=$(basename "$f" .txt); rc=0; run="$CMD"; skill=""
    if [ -e "${f%.txt}.skill" ]; then skill=$(cat "${f%.txt}.skill"); rm -f "${f%.txt}.skill"; [ -z "$SKILL_CMD" ] || run="$SKILL_CMD"; fi
    audit mention_start sequence="$seq" text_sha256="$(sha256sum "$f" | cut -d' ' -f1)" text_bytes="$(wc -c < "$f")" skill="$skill"
    if [ -n "$SANDBOX" ]; then
      timeout "${HERDR_TASK_TIMEOUT:-900}" "$SANDBOX" "$WORK" "$run" < "$f" > "$TMP/out.txt" 2>&1 || rc=$?
    else
      timeout "${HERDR_TASK_TIMEOUT:-900}" bash -c "$run" < "$f" > "$TMP/out.txt" 2>&1 || rc=$?
    fi
    audit mention_end sequence="$seq" exit="$rc" output_bytes="$(wc -c < "$TMP/out.txt")"
    echo "mention $seq exit $rc"
    rm -f "$f"
  done
done
