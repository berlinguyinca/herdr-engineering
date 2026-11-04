#!/usr/bin/env bash
# Re-vendor pdf.js for the artifact PDF views. Usage: vendor-pdfjs.sh <empty scratch dir>
# Packs pdfjs-dist@$PDFJS from npm WITHOUT running its scripts, checks the tarball's sha512 against the registry's
# dist.integrity, and copies only the two main modules and the licence into share_static/vendor/pdfjs/ (the dashboard
# serves the same files). Not vendored: cmaps/, standard_fonts/ and wasm/ (pdf.js fetches them on demand; see the
# Decision 1 tradeoff). Then writes SHA256SUMS, which tests/control_plane/test_pdfjs_vendor.py checks.
set -euo pipefail
PDFJS=4.10.38
work=${1:?usage: vendor-pdfjs.sh <empty scratch dir>}
here=$(cd "$(dirname "$0")" && pwd)
dest="$(dirname "$here")/herdr_engineering/control_plane/share_static/vendor/pdfjs"
mkdir -p "$work" "$dest"
[ -z "$(ls -A "$work")" ] || { echo "scratch dir $work is not empty" >&2; exit 1; }
cd "$work"
npm pack "pdfjs-dist@$PDFJS" --ignore-scripts --silent >/dev/null
tarball="pdfjs-dist-$PDFJS.tgz"
want=$(npm view "pdfjs-dist@$PDFJS" dist.integrity)
got="sha512-$(openssl dgst -sha512 -binary "$tarball" | openssl base64 -A)"
[ "$want" = "$got" ] || { echo "integrity mismatch: registry $want, tarball $got" >&2; exit 1; }
mkdir x && tar -xzf "$tarball" -C x
cp x/package/build/pdf.min.mjs x/package/build/pdf.worker.min.mjs x/package/LICENSE "$dest/"
(cd "$dest" && sha256sum LICENSE pdf.min.mjs pdf.worker.min.mjs > SHA256SUMS)
echo "pdfjs-dist@$PDFJS $got"
cat "$dest/SHA256SUMS"
