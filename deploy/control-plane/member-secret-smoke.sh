#!/usr/bin/env bash
# Host smoke test for the member-share secret hand-off across two uids (run as root on the worker host).
#
#   member-secret-smoke.sh <scratch-dir> <collab-public-uid>
#
# The gid is verified against the WORKER, not just the argument: set WORKER_CONTAINER to the running worker container
# (its HERDR_COLLAB_CERT_UID is read with `docker exec`), or WORKER_GID to the value you configured as
# control_plane.collab.members.secrets_gid. The script fails when that is not exactly <collab-public-uid>.
# Python must import herdr_engineering for root and for <collab-public-uid>: on the host that means PYTHON and
# PYTHONPATH of a checkout (or venv) readable by both; the worker image's python is not reachable from the host
# without a mount, so for a pure-image setup mount the repo's herdr_engineering read-only and set PYTHONPATH.
#
# Writes a secret exactly as the worker does (root, group = the collab-public uid, dir 0770, file 0640), then reads
# and deletes it as <collab-public-uid>:<collab-public-uid> and checks that an unrelated uid (65534) can do neither.
# Uses only the scratch dir; touches no live outbox, database or service.
set -euo pipefail
DIR="${1:?usage: $0 <scratch-dir> <collab-public-uid>}"
UID_PUB="${2:?usage: $0 <scratch-dir> <collab-public-uid>}"
PYTHON="${PYTHON:-python3}"
case "$UID_PUB" in ''|*[!0-9]*) echo "collab-public uid must be a number" >&2; exit 2;; esac
if [ "$UID_PUB" = 0 ] || [ "$UID_PUB" = 65534 ]; then
  echo "uid $UID_PUB is root/nobody: the test would prove nothing; pass the HERDR_COLLAB_CERT_UID of collab-public" >&2; exit 2
fi
if [ -n "${WORKER_CONTAINER:-}" ]; then
  WORKER_GID="$(docker exec "$WORKER_CONTAINER" printenv HERDR_COLLAB_CERT_UID)" || { echo "cannot read the worker's environment" >&2; exit 2; }
fi
[ -n "${WORKER_GID:-}" ] || { echo "set WORKER_CONTAINER or WORKER_GID: the worker's resolved secrets gid must be checked" >&2; exit 2; }
[ "$WORKER_GID" = "$UID_PUB" ] || { echo "FAIL: the worker resolves gid '$WORKER_GID', collab-public runs as $UID_PUB" >&2; exit 1; }
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
