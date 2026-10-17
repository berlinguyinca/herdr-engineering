#!/usr/bin/env bash
# Install this host's companion. Policy/allowed roots live in herdr-eng config.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERDR_ENG="$(command -v herdr-eng)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
python3 - "$REPO_ROOT/scripts/herdr-eng-build-cleanup.service" "$UNIT_DIR/herdr-eng-build-cleanup.service" "$HERDR_ENG" <<'PY'
import pathlib, sys
source, destination, binary = sys.argv[1:]
# systemd ExecStart uses its own quoting and specifiers, not shell quoting.
binary = binary.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
pathlib.Path(destination).write_text(pathlib.Path(source).read_text().replace('__HERDR_ENG__', '"' + binary + '"'))
PY
systemctl --user daemon-reload
systemctl --user enable herdr-eng-build-cleanup.service
systemctl --user restart herdr-eng-build-cleanup.service
systemctl --user is-active herdr-eng-build-cleanup.service
