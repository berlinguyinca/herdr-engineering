#!/usr/bin/env bash
# Deploy the HerdR Engineering master-host stack (CoreDNS + fabric gateway +
# private web UI) as a single Docker Compose project on the master host.
#
# This replaces the manually-started fabric gateway process and the
# herdr-eng-web systemd unit with containers. The per-host auto-registration
# agent stays a host systemd unit (it must see host listeners via ss/lsof).
#
#   ./scripts/fabric-stack.sh            # detect IP, write .env, build+up
#   ./scripts/fabric-stack.sh --replace  # also stop the OLD host-run services
#   ./scripts/fabric-stack.sh down       # stop the stack
#   ./scripts/fabric-stack.sh status     # show container + healthz state
set -euo pipefail

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/fabric-stack" && pwd)"
ENV_FILE="$STACK_DIR/.env"
COMPOSE=(docker compose -f "$STACK_DIR/compose.yaml" --env-file "$ENV_FILE")
SERVICE_PREFIX='herdr-fabric'
OLD_GATEWAY_PATTERN='herdr-eng devfabric gateway'
OLD_WEB_UNIT='herdr-eng-web.service'

say()  { printf '\n\033[1;34m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v tailscale >/dev/null || { echo "tailscale is required (to discover the tailnet IP)" >&2; exit 1; }

write_env() {
    local ip
    ip="$(tailscale ip -4 | head -1)"
    IP="$ip"   # global: reused by gen_coredns
    [ -n "$IP" ] || { echo "could not determine this host's tailnet IPv4 (is tailscale up?)" >&2; exit 1; }
    if [ -f "$ENV_FILE" ] && grep -q '^GATEWAY_TAILNET_IP=' "$ENV_FILE" \
       && [ "$(grep '^GATEWAY_TAILNET_IP=' "$ENV_FILE" | cut -d= -f2)" = "$ip" ]; then
        say "GATEWAY_TAILNET_IP already correct ($ip) in $ENV_FILE"
    else
        say "Writing $ENV_FILE with GATEWAY_TAILNET_IP=$ip"
        cat > "$ENV_FILE" <<EOF
GATEWAY_TAILNET_IP=$ip
EOF
    fi
}

gen_coredns() {
    # Render Corefile + zone from templates with the tailnet IP. The distroless
    # CoreDNS image has no shell, so generation happens on the host.
    local gen="$STACK_DIR/coredns/generated"
    mkdir -p "$gen"
    sed "s/{{GATEWAY_TAILNET_IP}}/$IP/g" "$STACK_DIR/coredns/Corefile.tmpl" > "$gen/Corefile"
    sed "s/{{GATEWAY_TAILNET_IP}}/$IP/g" "$STACK_DIR/coredns/dev.lan.zone.tmpl" > "$gen/dev.lan.zone"
    say "Rendered CoreDNS config (dev.lan -> $IP): $gen"
}

stop_old_host_services() {
    # Old web UI: systemd user unit.
    if systemctl --user is-active "$OLD_WEB_UNIT" >/dev/null 2>&1; then
        say "Stopping + disabling old host web UI unit ($OLD_WEB_UNIT)..."
        systemctl --user stop "$OLD_WEB_UNIT"
        systemctl --user disable "$OLD_WEB_UNIT" 2>/dev/null || true
    fi
    # Old manually-started gateway process.
    if pgrep -f "$OLD_GATEWAY_PATTERN" >/dev/null 2>&1; then
        say "Stopping old host fabric gateway process (devfabric gateway)..."
        pkill -f "$OLD_GATEWAY_PATTERN" || true
        # Give it a moment to unbind the router + release gateway.lock.
        sleep 2
    fi
}

up() {
    write_env
    gen_coredns
    "${COMPOSE[@]}" build fabric-gateway
    "${COMPOSE[@]}" up -d
    "${COMPOSE[@]}" ps
}

status() {
    "${COMPOSE[@]}" ps 2>/dev/null || { echo "stack not running"; exit 0; }
    say "gateway healthz:"
    curl -fsS "http://127.0.0.1:29999/healthz" || echo "(unreachable)"
    echo
    say "coredns dev.lan resolution (local):"
    if command -v dig >/dev/null; then
        dig +short @127.0.0.1 dev.lan A
    else
        getent hosts dev.lan || echo "(no getent result)"
    fi
}

down() { "${COMPOSE[@]}" down; }

cmd="${1:-up}"
case "$cmd" in
    up)
        up
        ;;
    up-replace)
        stop_old_host_services   # must free ports 8787/29999/18000+ first
        up
        ;;
    down)
        down
        ;;
    status)
        status
        ;;
    stop-old)
        stop_old_host_services
        ;;
    *)
        echo "usage: $0 [up|up-replace|down|status|stop-old]" >&2
        exit 2
        ;;
esac

say "Next: configure Tailscale split-DNS so dev.lan resolves fleet-wide."
say "  admin console -> DNS -> Nameservers -> Add nameserver:"
say "    address: $(tailscale ip -4 | head -1)"
say "    restrict to domain: dev.lan"
say "  (see deploy/fabric-stack/README.md for the exact steps + verification)"
