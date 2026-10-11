#!/usr/bin/env bash
# Link the herdr-cp-feed plugin to a control plane from a fresh host.
#
# Usage (run ON the target host, or via ssh):
#   ./link-remote.sh [CONTROL_PLANE_URL] [PLUGIN_DEST]
#
#   CONTROL_PLANE_URL  default: http://100.104.39.6  (bender tailnet IP:80)
#                      use http://dev.lan where split-DNS resolves it
#   PLUGIN_DEST        default: ~/.local/share/herdr-cp-feed-plugin
#
# This copies the plugin directory into a stable location, points the plugin
# at the control plane via its config-dir cp-url file, and links it with herdr.
set -euo pipefail

CP_URL="${1:-http://100.104.39.6}"
DEST="${2:-$HOME/.local/share/herdr-cp-feed-plugin}"

if ! command -v herdr >/dev/null 2>&1; then
  echo "ERROR: herdr binary not found on this host. Install herdr first." >&2
  exit 1
fi

# Copy plugin source (this script lives inside the plugin directory).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$DEST"
for f in herdr-plugin.toml emit.js; do
  if [ -f "$SCRIPT_DIR/$f" ]; then
    cp "$SCRIPT_DIR/$f" "$DEST/$f"
  else
    echo "ERROR: missing plugin file $SCRIPT_DIR/$f" >&2
    exit 1
  fi
done
chmod +x "$DEST/emit.js" 2>/dev/null || true

echo "Plugin copied to: $DEST"
echo "Control-plane URL: $CP_URL"

# Link with herdr.
if herdr plugin link "$DEST" >/tmp/herdr-cp-link.log 2>&1; then
  echo "Linked: $(herdr plugin list 2>/dev/null | grep -i 'cp-feed' || true)"
else
  echo "WARNING: 'herdr plugin link' reported an issue; see /tmp/herdr-cp-link.log" >&2
  cat /tmp/herdr-cp-link.log >&2
fi

# Point the plugin at the control plane via its config-dir cp-url file.
PLUGIN_ID="herdr.cp-feed"
CFG_DIR="$HOME/.config/herdr/plugins/config/$PLUGIN_ID"
mkdir -p "$CFG_DIR"
printf '%s\n' "$CP_URL" > "$CFG_DIR/cp-url"
echo "Wrote control-plane URL to: $CFG_DIR/cp-url"

echo
echo "Done. Verify with: herdr plugin list"
