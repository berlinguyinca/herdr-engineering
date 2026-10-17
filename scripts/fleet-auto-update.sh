#!/usr/bin/env bash
# Install daily unattended updates on the controller only.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENABLE=1
if [ "${1:-}" = "--no-enable" ] && [ "$#" = 1 ]; then
  ENABLE=0
elif [ "$#" != 0 ]; then
  echo "Usage: scripts/fleet-auto-update.sh [--no-enable]" >&2
  exit 2
fi
PYTHON="${HERDR_ENG_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "Controller Python is not executable: $PYTHON (set HERDR_ENG_PYTHON)" >&2
  exit 1
fi
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
"$PYTHON" - "$REPO_ROOT" "$UNIT_DIR" "$PYTHON" <<'PY'
import os
import pathlib
import sys

root, unit_dir, executable = map(pathlib.Path, sys.argv[1:])

def quote(value, *, command=False):
    value = str(value)
    if any(char in value for char in "\n\r\0"):
        raise SystemExit("Unit paths and environment must not contain newlines or NUL")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'

path = os.pathsep.join([
    str(executable.absolute().parent),
    str(root / ".venv" / "bin"),
    str(pathlib.Path.home() / ".local" / "bin"),
    "/usr/local/bin", "/usr/bin", "/bin", os.environ.get("PATH", ""),
])
replacements = {
    "__PYTHON__": quote(executable.absolute(), command=True),
    # WorkingDirectory takes the raw path, unlike ExecStart/Environment.
    "__REPO__": str(root).replace("%", "%%"),
    "__REPO_ENV__": quote("HERDR_ENGINEERING_REPO=" + str(root)),
    "__PATH_ENV__": quote("PATH=" + path),
}
for name in ("herdr-eng-fleet-update.service", "herdr-eng-fleet-update.timer"):
    content = (root / "scripts" / name).read_text()
    for marker, value in replacements.items():
        content = content.replace(marker, value)
    (unit_dir / name).write_text(content)
print(f"Installed daily fleet update units in {unit_dir}")
PY
if [ "$ENABLE" = 1 ]; then
  systemctl --user daemon-reload
  systemctl --user enable --now herdr-eng-fleet-update.timer
  systemctl --user is-active herdr-eng-fleet-update.timer
fi
