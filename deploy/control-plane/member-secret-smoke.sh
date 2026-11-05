#!/usr/bin/env bash
# Host smoke test for the member-share secret hand-off across two uids (run as root on the worker host).
#
#   member-secret-smoke.sh <scratch-dir> <collab-public-uid>
#
# Writes a secret exactly as the worker does (root, group = the collab-public uid, dir 0770, file 0640), then reads
# and deletes it as <collab-public-uid>:<collab-public-uid> and checks that an unrelated uid (65534) can do neither.
# Uses only the scratch dir; touches no live outbox, database or service.
set -euo pipefail
DIR="${1:?usage: $0 <scratch-dir> <collab-public-uid>}"
UID_PUB="${2:?usage: $0 <scratch-dir> <collab-public-uid>}"
PYTHON="${PYTHON:-python3}"
[ "$(id -u)" = 0 ] || { echo "run as root (the worker is root)" >&2; exit 2; }
command -v setpriv >/dev/null || { echo "setpriv (util-linux) is required" >&2; exit 2; }
OUT="$(mktemp -d "$DIR/outbox.XXXXXX")"; chmod 0755 "$OUT"
trap 'rm -rf "$OUT"' EXIT
export OUT UID_PUB

"$PYTHON" - <<'PY'
import os
from pathlib import Path
from herdr_engineering.control_plane.artifact_member_shares import (
    SECRET_DIR_MODE_SHARED, SECRET_FILE_MODE_SHARED, SECRETS_DIR)
from herdr_engineering.control_plane.collab_bridge_files import link_new_beneath
import json
assert link_new_beneath(Path(os.environ["OUT"]), [SECRETS_DIR], "tkt_smoke.json",
                        json.dumps({"ticket": "tkt_smoke", "principal": "p", "url": "https://x/s/t", "at": 1}).encode(),
                        mode=SECRET_FILE_MODE_SHARED, create_dirs=True, dir_mode=SECRET_DIR_MODE_SHARED,
                        gid=int(os.environ["UID_PUB"]))
PY

as() { setpriv --reuid "$1" --regid "$1" --clear-groups "${@:2}"; }

# an unrelated uid can neither read nor delete
if as 65534 cat "$OUT/secrets/tkt_smoke.json" 2>/dev/null; then echo "FAIL: an unrelated uid read the secret" >&2; exit 1; fi
if as 65534 rm "$OUT/secrets/tkt_smoke.json" 2>/dev/null; then echo "FAIL: an unrelated uid deleted the secret" >&2; exit 1; fi

# collab-public reads and deletes it (take_secret is what collab-public runs)
as "$UID_PUB" "$PYTHON" - <<'PY'
import os
from pathlib import Path
from herdr_engineering.control_plane.artifact_member_intents import take_secret
out = Path(os.environ["OUT"])
assert take_secret(out, "tkt_smoke", "p") == "https://x/s/t", "collab-public could not read the secret"
assert take_secret(out, "tkt_smoke", "p") is None
assert not (out / "secrets" / "tkt_smoke.json").exists(), "collab-public could not delete the secret"
PY
echo "OK: the secret crossed root -> uid $UID_PUB and was deleted; uid 65534 was refused"
