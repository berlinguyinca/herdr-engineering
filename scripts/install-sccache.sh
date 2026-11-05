#!/usr/bin/env bash
#
# Per-user, checksum-pinned sccache installer for the fabric build cache.
# Never needs root. Installing sccache changes nothing until
# fabric_build_cache.enabled is set (the feature stays default-off).
#
#   scripts/install-sccache.sh            install sccache 0.19.0 to ~/.local/bin
#
# Exit: 0 installed or already satisfied; 1 refused/failed (nothing installed).
#
# Test-only overrides (the source override is ignored unless HERDR_TEST_MODE=1):
#   SCCACHE_INSTALL_DEST      destination dir (default ~/.local/bin)
#   SCCACHE_INSTALL_PLATFORM  "<os>-<arch>" e.g. linux-x86_64 (default: uname)
#   SCCACHE_CHECKSUMS         checksum file (default: scripts/sccache-checksums.txt)
#   SCCACHE_SOURCE_DIR        directory holding the asset instead of github.com
set -euo pipefail

SCCACHE_VERSION="0.19.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKSUMS="${SCCACHE_CHECKSUMS:-$HERE/sccache-checksums.txt}"
DEST="${SCCACHE_INSTALL_DEST:-$HOME/.local/bin}"
BASE_URL="https://github.com/mozilla/sccache/releases/download/v${SCCACHE_VERSION}"

fail() { printf 'sccache install: %s\n' "$*" >&2; exit 1; }

version_ge() { # $1 >= $2 (dotted)
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]
}

installed_version() {
  local bin="$1" out
  [ -x "$bin" ] || return 1
  out="$("$bin" --version 2>/dev/null | head -1)" || return 1
  printf '%s' "$out" | sed -n 's/^sccache v\{0,1\}\([0-9][0-9.]*\).*/\1/p'
}

platform="${SCCACHE_INSTALL_PLATFORM:-$(uname -s | tr 'A-Z' 'a-z')-$(uname -m)}"
case "$platform" in
  linux-x86_64)                  triple="x86_64-unknown-linux-musl" ;;
  linux-aarch64|linux-arm64)     triple="aarch64-unknown-linux-musl" ;;
  darwin-arm64|darwin-aarch64)   triple="aarch64-apple-darwin" ;;
  darwin-x86_64)                 triple="x86_64-apple-darwin" ;;
  *) fail "unsupported platform '$platform' (supported: linux x86_64/aarch64, macOS arm64/x86_64); nothing installed" ;;
esac

have="$(installed_version "$DEST/sccache" || true)"
if [ -z "$have" ] && [ -z "${SCCACHE_INSTALL_DEST:-}" ]; then
  have="$(installed_version "$(command -v sccache 2>/dev/null || true)" || true)"
fi
if [ -n "$have" ] && version_ge "$have" "$SCCACHE_VERSION"; then
  printf 'sccache %s already installed (>= %s); skipping\n' "$have" "$SCCACHE_VERSION"
  exit 0
fi

asset="sccache-v${SCCACHE_VERSION}-${triple}.tar.gz"
[ -f "$CHECKSUMS" ] || fail "checksum file missing: $CHECKSUMS; refusing to install"
expected="$(awk -v a="$asset" '$1 !~ /^#/ && $2 == a {print $1}' "$CHECKSUMS")"
printf '%s' "$expected" | grep -Eq '^[0-9a-f]{64}$' \
  || fail "no pinned SHA-256 for $asset in $CHECKSUMS; refusing to install"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [ -n "${SCCACHE_SOURCE_DIR:-}" ]; then
  [ "${HERDR_TEST_MODE:-0}" = "1" ] || fail "SCCACHE_SOURCE_DIR is test-only (set HERDR_TEST_MODE=1)"
  cp "$SCCACHE_SOURCE_DIR/$asset" "$tmp/$asset" || fail "test asset missing: $SCCACHE_SOURCE_DIR/$asset"
else
  command -v curl >/dev/null 2>&1 || fail "curl is required"
  curl -fsSL --retry 2 -o "$tmp/$asset" "$BASE_URL/$asset" || fail "download failed: $BASE_URL/$asset"
fi

if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$tmp/$asset" | awk '{print $1}')"
else
  actual="$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')"
fi
[ "$actual" = "$expected" ] || fail "SHA-256 mismatch for $asset (expected $expected, got $actual); nothing installed"

# Extract only the sccache binary, never trusting archive paths beyond that.
member="$(tar -tzf "$tmp/$asset" | grep -E '(^|/)sccache$' | head -1 || true)"
[ -n "$member" ] || fail "archive has no sccache binary; nothing installed"
mkdir "$tmp/x"
tar -xzf "$tmp/$asset" -C "$tmp/x" "$member"
mkdir -p "$DEST"
install -m 0755 "$tmp/x/$member" "$DEST/sccache.new.$$"
mv -f "$DEST/sccache.new.$$" "$DEST/sccache"
printf 'installed sccache %s (%s) to %s/sccache\n' "$SCCACHE_VERSION" "$triple" "$DEST"
