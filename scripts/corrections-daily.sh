#!/usr/bin/env bash
# Daily scan of agent corrections on this machine and on every fabric machine `herdr-eng machines` lists.
# Read-only on transcripts. Each host keeps its own log (0600) next to its own transcripts; only counts come back here.
# Usage: corrections-daily.sh [RECENT]   (default 60 records per harness per host; the first run can pass a larger number)
set -u
if [ "${1:-}" = "--install" ]; then
  # Copies what the scan needs to a stable place (so a moved or deleted checkout cannot break it) and schedules 03:30 daily.
  SRC="$(cd "$(dirname "$0")/.." && pwd)"; APP="$HOME/.local/share/herdr-engineering/corrections-app"
  mkdir -p "$APP/scripts"
  (cd "$SRC" && rsync -aR --quiet herdr_engineering/__init__.py herdr_engineering/version.py herdr_engineering/corrections.py \
     herdr_engineering/session_history.py herdr_engineering/control_plane/__init__.py herdr_engineering/control_plane/redact.py \
     scripts/corrections-scan.py scripts/corrections-daily.sh "$APP/")
  if [ "$(uname)" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/com.herdr.corrections-daily.plist"
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.herdr.corrections-daily</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$APP/scripts/corrections-daily.sh</string></array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>
<key>StandardOutPath</key><string>$HOME/.local/share/herdr-engineering/state/corrections/launchd.out</string>
<key>StandardErrorPath</key><string>$HOME/.local/share/herdr-engineering/state/corrections/launchd.err</string>
</dict></plist>
PL
    launchctl bootout "gui/$(id -u)/com.herdr.corrections-daily" 2>/dev/null
    launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "scheduled daily 03:30: $PLIST"
  else
    echo "installed to $APP; schedule it with cron: 30 3 * * * /bin/bash $APP/scripts/corrections-daily.sh"
  fi
  exit 0
fi
RECENT="${1:-60}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LOGDIR="${HERDR_CORRECTIONS_LOG_DIR:-$HOME/.local/share/herdr-engineering/state/corrections}"
mkdir -p "$LOGDIR"; chmod 700 "$LOGDIR"
OUT="$LOGDIR/daily.log"
FILES="herdr_engineering/__init__.py herdr_engineering/version.py herdr_engineering/corrections.py herdr_engineering/session_history.py herdr_engineering/control_plane/__init__.py herdr_engineering/control_plane/redact.py scripts/corrections-scan.py"
REMOTE_DIR='.cache/herdr-corrections'
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

RESET="${HERDR_CORRECTIONS_RESET:-}"   # =1 deletes each host's kept corrections before scanning (after the extractor changed)
[ -n "$RESET" ] && (cd "$HERE" && python3 scripts/corrections-scan.py forget-all >/dev/null)
echo "$(stamp) local: $(cd "$HERE" && python3 scripts/corrections-scan.py scan --recent "$RECENT" 2>&1 | tail -1)" >> "$OUT"

HOSTS="$(herdr-eng machines --json 2>/dev/null | python3 -c 'import json,sys
try: print(" ".join(m["target"] for m in json.load(sys.stdin) if m.get("enabled")))
except Exception: pass')"
for host in $HOSTS; do
  if ! (cd "$HERE" && rsync -aR --quiet -e "ssh -o BatchMode=yes -o ConnectTimeout=8" $FILES "$host:$REMOTE_DIR/") 2>>"$OUT"; then
    echo "$(stamp) $host: sync failed" >> "$OUT"; continue
  fi
  echo "$(stamp) $host: $(ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" "cd $REMOTE_DIR && python3 scripts/corrections-scan.py enable >/dev/null && { [ -z '$RESET' ] || python3 scripts/corrections-scan.py forget-all >/dev/null; } && python3 scripts/corrections-scan.py scan --recent $RECENT" 2>&1 | tail -1)" >> "$OUT"
done
tail -n $((1 + $(echo $HOSTS | wc -w))) "$OUT"
