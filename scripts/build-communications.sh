#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
./node_modules/.bin/tsc -p tsconfig.communications.json
cp .superpowers/communications-js/communications-view.js packages/communications-view.js
