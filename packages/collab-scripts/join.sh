#!/usr/bin/env bash
# HerdR collaboration: put agents of the chosen family on THIS machine, ready for the room you picked.
#
#   curl -fsSL __HERDR_BASE__/api/v1/collab/scripts/join.sh | HERDR_JOIN_TOKEN='hcj_...' bash -s -- [options]
#
# The join token (made on the dashboard, short-lived, limited uses) fixes WHAT is created: the family, the name
# prefix, how many agents at most, the room and the permissions asked for. Options say HOW it runs here:
#   --count N       register N agents (default 1; never more than the token allows)
#   --start         start a worker for each agent right away (background processes, logs in ~/herdr-agents/logs)
#   --herdr         run the workers in herdr (implies --start): workspace `herd-PREFIX`, one tab per agent named after it,
#                   helpers of an agent as splits of its tab; the dashboard links them and keeps the names equal
#   --tools         let the harness use its tools on task text (default: text answers only; codex needs this)
#   --cmd 'COMMAND' run COMMAND for each task instead of the family's own (task text arrives on stdin)
#   --no-sandbox    do not run tasks inside bubblewrap (default: boxed whenever bwrap works)
#   --status        show the agents in ~/herdr-agents and whether their workers run
#   --stop          stop the background workers started by this script
#   --help          this text
# Agents have NO access until the room owner approves them on the dashboard (room > Agents > Agent requests).
# Task text is written by room members: it goes to the harness on stdin and is never spliced into a shell command.
main() {
  set -euo pipefail
  BASE='__HERDR_BASE__'
  DIR="${HERDR_AGENT_DIR:-$HOME/herdr-agents}"
  COUNT=1; START=0; USE_HERDR=0; TOOLS=0; CMD_OVERRIDE=""; SANDBOX_ON=1; MODE=join
  while [ $# -gt 0 ]; do
    case "$1" in
      --count) COUNT="${2:?--count needs a number}"; shift 2 ;;
      --start) START=1; shift ;;
      --herdr) USE_HERDR=1; START=1; shift ;;
      --tools) TOOLS=1; shift ;;
      --cmd) CMD_OVERRIDE="${2:?--cmd needs a command}"; shift 2 ;;
      --no-sandbox) SANDBOX_ON=0; shift ;;
      --status) MODE=status; shift ;;
      --stop) MODE=stop; shift ;;
      --help|-h) sed -n '2,22p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true; echo "usage: curl -fsSL $BASE/api/v1/collab/scripts/join.sh | HERDR_JOIN_TOKEN=... bash -s -- --count N [--start] [--herdr] [--tools]"; return 0 ;;
      *) echo "join.sh: unknown option $1 (try --help)" >&2; return 2 ;;
    esac
  done
  case "$COUNT" in ''|*[!0-9]*) echo "join.sh: --count must be a number" >&2; return 2 ;; esac
  [ "$COUNT" -ge 1 ] && [ "$COUNT" -le 50 ] || { echo "join.sh: --count must be 1-50" >&2; return 2; }
  for need in curl python3; do command -v "$need" >/dev/null 2>&1 || { echo "join.sh: $need is required" >&2; return 1; }; done
  umask 077

  if [ "$MODE" = status ]; then
    shopt -s nullglob
    for f in "$DIR"/*.json; do
      n=$(basename "$f" .json); pid=$(cat "$DIR/run/$n.pid" 2>/dev/null || true)
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "$n  running (pid $pid)"; else echo "$n  not running as a background worker"; fi
    done
    return 0
  fi
  if [ "$MODE" = stop ]; then
    shopt -s nullglob
    for p in "$DIR"/run/*.pid; do
      pid=$(cat "$p" 2>/dev/null || true)
      case "$pid" in ''|*[!0-9]*) rm -f "$p"; continue ;; esac
      if ps -p "$pid" -o args= 2>/dev/null | grep -q 'worker\.sh'; then kill "$pid" 2>/dev/null && echo "stopped $(basename "$p" .pid) (pid $pid)"; fi
      rm -f "$p"
    done
    echo "Workers started in herdr panes keep running: close their workspaces in herdr."
    return 0
  fi

  TOKEN="${HERDR_JOIN_TOKEN:-}"
  [ -n "$TOKEN" ] || { echo "join.sh: set HERDR_JOIN_TOKEN (the dashboard shows the whole command with it filled in)" >&2; return 1; }
  mkdir -p "$DIR" "$DIR/logs" "$DIR/run"
  chmod 700 "$DIR"

  # The helper scripts come from the same server, so they always match it.
  fetch() { curl -fsS "$BASE/api/v1/collab/scripts/$1" -o "$DIR/$2.new" </dev/null && chmod 700 "$DIR/$2.new" && mv -f "$DIR/$2.new" "$DIR/$2"; }
  fetch worker.sh worker.sh

  BOX=0
  if [ "$SANDBOX_ON" = 1 ]; then
    if command -v bwrap >/dev/null 2>&1 && bwrap --ro-bind / / --unshare-pid --die-with-parent /usr/bin/true >/dev/null 2>&1 \
        && [ "$DIR" = "$HOME/herdr-agents" ]; then
      fetch sandbox.sh collab-sandbox.sh; BOX=1
    else
      echo "join.sh: bubblewrap is not usable here (or a custom --dir is set): tasks will NOT be boxed. Install bubblewrap, or only start text-only agents for people you trust." >&2
    fi
  fi

  FAMILY=""; NAMES=(); PREFIXES=(); PARENTS=(); IDS=()
  for i in $(seq 1 "$COUNT"); do
    body=$(T="$TOKEN" python3 -c 'import os,sys,json;print(json.dumps({"token":os.environ["T"],"instance_id":sys.argv[1]}))' "$(hostname)-$$-$i")
    resp=$(mktemp); code=$(curl -sS -o "$resp" -w '%{http_code}' -X POST "$BASE/api/v1/collab/agents/join" -H 'content-type: application/json' -d "$body" </dev/null) || code=000
    if [ "$code" != 200 ] && [ "$code" != 201 ]; then
      echo "join.sh: the server refused agent $i (HTTP $code): the token is wrong, expired, revoked or used up." >&2; rm -f "$resp"; break
    fi
    name=$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["name"])' "$resp")
    fam=$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1])).get("harness_type",""))' "$resp")
    pre=$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1])).get("name_prefix") or "")' "$resp")
    par=$(python3 -c 'import sys,json;d=json.load(open(sys.argv[1])).get("parent") or {};print(d.get("instance_id") or "")' "$resp")
    aid=$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["agent_id"])' "$resp")
    case "$name" in ''|*[!a-z0-9-]*) echo "join.sh: unexpected agent name from the server" >&2; rm -f "$resp"; return 1 ;; esac
    mv -f "$resp" "$DIR/$name.json"; chmod 600 "$DIR/$name.json"
    FAMILY="${FAMILY:-$fam}"; NAMES+=("$name"); PREFIXES+=("$pre"); PARENTS+=("$par"); IDS+=("$aid")
    echo "registered $name"
  done
  [ "${#NAMES[@]}" -gt 0 ] || return 1

  if [ "$START" = 1 ]; then
    CMD="$CMD_OVERRIDE"; SKILL_CMD=""
    if [ -z "$CMD" ]; then
      if [ "$TOOLS" = 1 ]; then
        case "$FAMILY" in
          claude-code) CMD='claude -p' ;;
          codex) CMD='codex exec --skip-git-repo-check -' ;;
          opencode) CMD='opencode run'; SKILL_CMD='opencode run' ;;
          pi) CMD='pi -p --no-session -- "Task from a room member: $(cat)"'
              SKILL_CMD='pi -p --no-session -- "$(cat)"' ;;   # only for tasks the server marked as a skill call
          *) CMD='' ;;
        esac
      else
        case "$FAMILY" in
          claude-code) CMD="claude -p --tools '' --strict-mcp-config --mcp-config '{\"mcpServers\":{}}' --disable-slash-commands"
                       SKILL_CMD="claude -p --tools '' --strict-mcp-config --mcp-config '{\"mcpServers\":{}}'" ;;
          opencode) CMD='opencode run --pure'; SKILL_CMD='opencode run --pure' ;;
          pi) CMD='pi -p --no-session --no-tools -- "Task from a room member: $(cat)"'
              SKILL_CMD='pi -p --no-session --no-tools -- "$(cat)"' ;;
          codex) echo "join.sh: codex cannot be limited to text answers; add --tools to start codex agents (they can then run commands, inside the sandbox when available)." >&2; return 1 ;;
          *) CMD='' ;;
        esac
      fi
      bin=""; case "$FAMILY" in claude-code) bin=claude ;; codex) bin=codex ;; opencode) bin=opencode ;; pi) bin=pi ;; esac
      if [ -n "$bin" ] && ! command -v "$bin" >/dev/null 2>&1; then
        echo "join.sh: $bin is not installed on this machine, so the workers were NOT started. Registered agents are in $DIR." >&2; return 1
      fi
    fi
    HOSTID=$(python3 -c 'import json,os,sys
try: print(json.load(open(os.path.expanduser("~/.config/herdr-engineering/host.json")))["host_id"])
except Exception: print("")' </dev/null)
    [ -n "$HOSTID" ] || HOSTID=$(hostname -s | tr 'A-Z' 'a-z')
    pane_of() { python3 -c 'import sys,json;r=json.load(sys.stdin)["result"];p=r.get("root_pane") or r["pane"];print(p["pane_id"],p.get("tab_id",""))'; }
    for idx in "${!NAMES[@]}"; do
      name="${NAMES[$idx]}"; prefix="${PREFIXES[$idx]}"; parent="${PARENTS[$idx]}"; aid="${IDS[$idx]}"
      f="$DIR/$name.json"; envs=()
      [ -z "$CMD" ] || envs+=("HERDR_AGENT_CMD=$CMD")
      [ -z "$SKILL_CMD" ] || envs+=("HERDR_SKILL_CMD=$SKILL_CMD")
      if [ "$BOX" = 1 ]; then envs+=("HERDR_SANDBOX=$DIR/collab-sandbox.sh" "HERDR_AUDIT=$DIR/audit/$name.log"); fi
      if [ "$USE_HERDR" = 1 ] && command -v herdr >/dev/null 2>&1 && [ -n "$prefix" ]; then
        # workspace herd-PREFIX; a tab per agent named after it; a helper splits its parent's tab
        tab=""; own_tab=1
        if [ -n "$parent" ] && [ "${parent#pane:$HOSTID:}" != "$parent" ]; then
          read -r pane tab < <(herdr pane split "${parent#pane:$HOSTID:}" --direction right --no-focus </dev/null | pane_of); own_tab=0
        else
          ws=$(herdr workspace list </dev/null | PFX="herd-$prefix" python3 -c 'import os,sys,json
for w in json.load(sys.stdin)["result"]["workspaces"]:
    if w.get("label") == os.environ["PFX"]: print(w["workspace_id"]); break')
          if [ -n "$ws" ]; then read -r pane tab < <(herdr tab create --workspace "$ws" --label "$name" --no-focus </dev/null | pane_of)
          else read -r pane tab < <(herdr workspace create --label "herd-$prefix" --no-focus </dev/null | pane_of); fi
        fi
        [ "$own_tab" = 0 ] || { [ -z "$tab" ] || herdr tab rename "$tab" "$name" </dev/null >/dev/null; }
        herdr pane rename "$pane" "$name" </dev/null >/dev/null
        lbody=$(T="$TOKEN" A="$aid" I="pane:$HOSTID:$pane" python3 -c 'import os,json;print(json.dumps({"token":os.environ["T"],"agent_id":os.environ["A"],"instance_id":os.environ["I"]}))')
        curl -fsS -X POST "$BASE/api/v1/collab/agents/join-link" -H 'content-type: application/json' -d "$lbody" >/dev/null </dev/null \
          || echo "join.sh: could not link $name to its herdr pane (it still works; link it on the AI agents page)" >&2
        line=""; for e in "${envs[@]}"; do line="$line$(printf '%q' "$e") "; done
        herdr pane run "$pane" "env $line$(printf '%q' "$DIR/worker.sh") $(printf '%q' "$f")" </dev/null >/dev/null
        echo "started $name in herdr (workspace herd-$prefix, pane $pane)"
      else
        [ "$USE_HERDR" = 0 ] || echo "join.sh: herdr is not installed (or the server sent no name prefix); starting $name as a background process." >&2
        if command -v setsid >/dev/null 2>&1; then runner=(setsid nohup); else runner=(nohup); fi
        env "${envs[@]}" "${runner[@]}" "$DIR/worker.sh" "$f" </dev/null >>"$DIR/logs/$name.log" 2>&1 &
        echo $! > "$DIR/run/$name.pid"
        echo "started $name (pid $!, log $DIR/logs/$name.log)"
      fi
    done
  fi

  echo
  echo "Done: ${#NAMES[@]} agent(s) in $DIR. They can do nothing until the room owner approves them (dashboard: room > Agents > Agent requests)."
  [ "$START" = 1 ] || echo "Workers are not running yet: re-run with --start, or start one with: $DIR/worker.sh $DIR/${NAMES[0]}.json"
  echo "Later: ... bash -s -- --status    or    ... bash -s -- --stop"
}
main "$@"
