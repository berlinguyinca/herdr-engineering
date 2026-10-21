#!/usr/bin/env bash
# Publish the loopback control plane over tailnet-only HTTPS (Tailscale terminates TLS).
# usage: collab-tailscale-serve.sh up <local-port> <https-port> | down <https-port>
# Additive: refuses to touch an https port that already has a serve entry.
set -euo pipefail
cmd="${1:-}"
case "$cmd" in
  up)
    local_port="${2:?local port}"; https_port="${3:?https port}"
    if tailscale serve status 2>/dev/null | grep -q "ts.net:${https_port} "; then
      echo "https port ${https_port} is already published; choose another" >&2; exit 1
    fi
    if ss -ltn | awk '{print $4}' | grep -qE "(^|:)${https_port}$"; then
      echo "port ${https_port} is in use by another listener; choose another" >&2; exit 1
    fi
    tailscale serve --bg --https="${https_port}" "http://127.0.0.1:${local_port}"
    tailscale serve status
    ;;
  down)
    tailscale serve --https="${2:?https port}" off
    ;;
  *) echo "usage: $0 up <local-port> <https-port> | down <https-port>" >&2; exit 2 ;;
esac
