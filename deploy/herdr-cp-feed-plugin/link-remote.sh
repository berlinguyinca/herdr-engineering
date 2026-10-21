#!/usr/bin/env bash
# Link the herdr-cp-feed plugin to a control plane from a fresh host.
#
# Usage (run ON the target host, or via ssh):
#   ./link-remote.sh [PLUGIN_DEST]
#
#   PLUGIN_DEST        default: ~/.local/share/herdr-cp-feed-plugin
#
# This copies the plugin directory into a stable location, points the plugin
# and links it with herdr. The plugin finds the control plane through
# ~/.config/herdr-engineering/host.json (written by `herdr-eng fleet join`).
set -euo pipefail

DEST="${1:-$HOME/.local/share/herdr-cp-feed-plugin}"

if ! command -v herdr >/dev/null 2>&1; then
  echo "ERROR: herdr binary not found on this host. Install herdr first." >&2
  exit 1
fi

# Copy plugin source (this script lives inside the plugin directory).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$DEST"
for f in herdr-plugin.toml package.json emit.js enrich.js git-metadata.js host-config.js inventory.js session-model.js snapshot.js; do
  if [ -f "$SCRIPT_DIR/$f" ]; then
    cp "$SCRIPT_DIR/$f" "$DEST/$f"
  else
    echo "ERROR: missing plugin file $SCRIPT_DIR/$f" >&2
    exit 1
  fi
done
chmod +x "$DEST/emit.js" 2>/dev/null || true

echo "Plugin copied to: $DEST"

# Link with herdr.
if herdr plugin link "$DEST" >/tmp/herdr-cp-link.log 2>&1; then
  echo "Linked: $(herdr plugin list 2>/dev/null | grep -i 'cp-feed' || true)"
else
  echo "WARNING: 'herdr plugin link' reported an issue; see /tmp/herdr-cp-link.log" >&2
  cat /tmp/herdr-cp-link.log >&2
fi

if [ ! -f "$HOME/.config/herdr-engineering/host.json" ]; then
  echo "NOTE: no host.json yet; run \`herdr-eng fleet join\` for this host on the control-plane host." >&2
fi

echo
echo "Done. Verify with: herdr plugin list"
