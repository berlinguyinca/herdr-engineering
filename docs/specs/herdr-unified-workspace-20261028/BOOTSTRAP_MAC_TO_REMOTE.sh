#!/usr/bin/env bash
set -euo pipefail
# Run ON THE MAC, after replacing REMOTE with your known SSH alias.
REMOTE="${1:?Usage: ./BOOTSTRAP_MAC_TO_REMOTE.sh <remote-ssh-alias> <repo-absolute-path>}"
REPO="${2:?Provide an absolute remote repository path}"
ZIP="$HOME/Downloads/herdr-unified-workspace-20261028.zip"
[[ -f "$ZIP" ]] || { echo "Missing Mac ZIP: $ZIP" >&2; exit 1; }
[[ "$REPO" = /* ]] || { echo "Remote repository path must be absolute" >&2; exit 1; }
# Transfer to the user's home first; quote user-controlled paths.
scp -- "$ZIP" "$REMOTE:~/herdr-unified-workspace-20261028.zip"
ssh "$REMOTE" bash -s -- "$REPO" <<'REMOTE_SCRIPT'
set -euo pipefail
repo="$1"
cd "$repo"
git rev-parse --show-toplevel >/dev/null
spec='docs/specs/herdr-unified-workspace-20261028'
if [[ -e "$spec" ]]; then echo "Spec destination exists; inspect it instead of overwriting: $spec" >&2; exit 2; fi
mkdir -p "$spec"
python3 - "$HOME/herdr-unified-workspace-20261028.zip" "$spec" <<'PY2'
import sys,zipfile,pathlib
src=pathlib.Path(sys.argv[1]);target=pathlib.Path(sys.argv[2]).resolve()
with zipfile.ZipFile(src) as z:
 for i in z.infolist():
  p=pathlib.PurePosixPath(i.filename)
  if p.is_absolute() or '..' in p.parts or i.is_dir() and not p.parts: raise SystemExit('unsafe zip path')
  out=(target/p).resolve()
  if not out.is_relative_to(target): raise SystemExit('zip path traversal')
 z.extractall(target)
print('Extracted',len(z.namelist()),'entries')
PY2
(cd "$spec" && sha256sum -c SHA256SUMS)
echo "Spec installed in $repo/$spec"
REMOTE_SCRIPT
