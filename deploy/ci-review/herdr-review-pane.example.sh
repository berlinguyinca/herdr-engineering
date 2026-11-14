#!/usr/bin/env bash
# Example herdr pane command (not installed by anything): tail the always-on review log. Run it in a herdr pane on
# the review host. One line per review: repo, pr, sha, tier, state, model, duration; the JSONL has tokens and cost.
#   journalctl form (the unit's stderr):       journalctl --user -fu herdr-always-on-review -o cat
#   state-dir form (one JSON line per review):  tail -F ~/.local/state/herdr-review/reviews.jsonl
set -euo pipefail
state_dir="${1:-$HOME/.local/state/herdr-review}"
if command -v jq >/dev/null; then
  tail -n 20 -F "$state_dir/reviews.jsonl" | jq -r --unbuffered \
    '[.ts, .repo + "#" + (.pr|tostring), .sha[0:8], .tier, .state, .model, (.duration_s|tostring) + "s",
      (if .dry_run then "DRY" else "POSTED" end), (.why // "")] | join("  ")'
else
  tail -n 20 -F "$state_dir/reviews.jsonl"
fi
