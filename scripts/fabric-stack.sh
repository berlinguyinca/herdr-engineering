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
#   ./scripts/fabric-stack.sh status     # show container + healthz state (+ Hindsight memory, when deployed)
#   ./scripts/fabric-stack.sh memory-route enable [PORT]|disable|status
#                                        # route https://memory.<domain> and https://memory.dev.lan to the loopback
#                                        # Hindsight API (default port 8888) through the gateway. Writes the host-local
#                                        # ~/.config/herdr-engineering/fabric/config.yaml (survives deploys; the shipped
#                                        # config stays routes-off), then prints the gateway restart to run
set -euo pipefail

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/fabric-stack" && pwd)"
# The compose project is shared with the control plane: one file, one .env.
PROJECT_DIR="$(cd "$STACK_DIR/../control-plane" && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
COMPOSE=(docker compose -f "$PROJECT_DIR/compose.yaml" --env-file "$ENV_FILE")
SERVICE_PREFIX='herdr-fabric'
OLD_GATEWAY_PATTERN='herdr-eng devfabric gateway'
OLD_WEB_UNIT='herdr-eng-web.service'

say()  { printf '\n\033[1;34m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v tailscale >/dev/null || { echo "tailscale is required (to discover the tailnet IP)" >&2; exit 1; }

check_fabric_token_dir() {   # create it 0700 when missing, then verify it; never prints token content
  local d="$HOME/.config/herdr-engineering/fabric-gateway" mode
  if [ ! -e "$d" ] && [ ! -L "$d" ]; then
    mkdir -p "$(dirname "$d")" && mkdir -m 700 "$d" || return 1
  fi
  if [ -L "$d" ] || [ ! -d "$d" ]; then
    echo "$d (the fabric-gateway token mount) must be a real directory (not a symlink or a file)" >&2; return 1
  fi
  if [ "$(stat -c %u "$d")" != "$(id -u)" ]; then
    echo "$d (the fabric-gateway token mount) must be owned by the deploying user (uid $(id -u)); docker creates it root-owned when it is missing at compose time" >&2; return 1
  fi
  mode="$(stat -c %a "$d")"
  if [ "$mode" != 700 ]; then
    echo "$d (the fabric-gateway token mount) must be mode 0700 (it is $mode): chmod 700 $d" >&2; return 1
  fi
}

write_env() {
    local ip
    ip="$(tailscale ip -4 | head -1)"
    IP="$ip"   # global: reused by gen_coredns
    [ -n "$IP" ] || { echo "could not determine this host's tailnet IPv4 (is tailscale up?)" >&2; exit 1; }
    if [ -f "$ENV_FILE" ] && grep -q '^GATEWAY_TAILNET_IP=' "$ENV_FILE" \
       && [ "$(grep '^GATEWAY_TAILNET_IP=' "$ENV_FILE" | cut -d= -f2)" = "$ip" ]; then
        say "GATEWAY_TAILNET_IP already correct ($ip) in $ENV_FILE"
    else
        say "Setting GATEWAY_TAILNET_IP=$ip in $ENV_FILE"
        # Keep the control plane's credentials: replace only our line.
        touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
        grep -v '^GATEWAY_TAILNET_IP=' "$ENV_FILE" > "$ENV_FILE.tmp" || true
        echo "GATEWAY_TAILNET_IP=$ip" >> "$ENV_FILE.tmp"
        mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
    fi
}

gen_coredns() {
    # Render Corefile + zone from templates with the tailnet IP. The distroless
    # CoreDNS image has no shell, so generation happens on the host.
    local gen="$STACK_DIR/coredns/generated"
    mkdir -p "$gen"
    sed "s/{{GATEWAY_TAILNET_IP}}/$IP/g" "$STACK_DIR/coredns/Corefile.tmpl" > "$gen/Corefile"
    # Optional standby gateway (HA pair): GATEWAY_PEER_TAILNET_IP adds a second A record for dev.lan and *.dev.lan.
    # Clients try both addresses; the one without a listener refuses and they fall through to the active gateway.
    local peer_apex="" peer_wild=""
    if [ -n "${GATEWAY_PEER_TAILNET_IP:-}" ]; then
        peer_apex="    IN A   ${GATEWAY_PEER_TAILNET_IP}"
        peer_wild="*   IN A   ${GATEWAY_PEER_TAILNET_IP}"
    fi
    sed -e "s/{{GATEWAY_TAILNET_IP}}/$IP/g" -e "s|{{PEER_APEX_RECORD}}|$peer_apex|" -e "s|{{PEER_WILDCARD_RECORD}}|$peer_wild|" \
        "$STACK_DIR/coredns/dev.lan.zone.tmpl" > "$gen/dev.lan.zone"
    say "Rendered CoreDNS config (dev.lan -> $IP${GATEWAY_PEER_TAILNET_IP:+, $GATEWAY_PEER_TAILNET_IP}): $gen"
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

ensure_local_dir() {   # the gateway bind-mounts it; if docker made it, it is root-owned and memory-route cannot write
    local d="$HOME/.config/herdr-engineering/fabric"
    if [ -L "$d" ] || { [ -e "$d" ] && [ ! -d "$d" ]; }; then
        echo "$d must be a real directory (not a symlink or a file)" >&2; exit 1
    fi
    if [ -d "$d" ] && [ "$(stat -c %u "$d")" != "$(id -u)" ]; then
        echo "$d must be owned by $(id -un); fix with: sudo chown $(id -un) $d" >&2; exit 1
    fi
    mkdir -p "$d" || { echo "cannot create $d" >&2; exit 1; }
}

up() {
    ensure_local_dir
    # The compose file bind-mounts the token directory read-only into fabric-gateway and herdr-web; docker would
    # create a missing source root-owned 0755, and nothing would fix it afterwards.
    check_fabric_token_dir || exit 1
    write_env
    gen_coredns
    "${COMPOSE[@]}" build fabric-gateway
    # The optional Hindsight memory containers belong to compose profile `memory`, managed by deploy.sh --with-memory.
    # While they exist, never ask compose to remove "orphans": this script's env file carries no memory variables.
    local orphans=(--remove-orphans)
    if docker inspect herdr-hindsight >/dev/null 2>&1 || docker inspect herdr-hindsight-db >/dev/null 2>&1; then
        orphans=()
        warn "Hindsight memory containers present: not using --remove-orphans (managed by deploy.sh --with-memory)"
    fi
    "${COMPOSE[@]}" up -d ${orphans[@]+"${orphans[@]}"}
    "${COMPOSE[@]}" ps
}

memory_status() {
    # Read-only. UNKNOWN when docker cannot answer; never a guess.
    local name state
    for name in herdr-hindsight-db herdr-hindsight; do
        state="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$name" 2>&1)" || {
            case "$state" in *"No such"*) continue ;; *) echo "$name: UNKNOWN (docker did not answer)"; continue ;; esac; }
        echo "$name: $state"
    done
}

memory_route() {
    # The shipped deploy/fabric-stack/config.yaml (baked into the image) keeps the memory routes OFF. They live in
    # their own host-local file, the gateway's third HERDR_ENGINEERING_CONFIG entry, under fabric.local_routes (the
    # gateway appends them to the baked fabric.routes), so a deploy never loses them, a changed shipped route still
    # reaches this host, and no other host-local file is touched.
    local shipped="$STACK_DIR/config.yaml" mark='# herdr-memory-route'
    local dir="$HOME/.config/herdr-engineering/fabric" port="${2:-}"; [ -n "$port" ] || port=8888
    local route_file="$dir/memory-route.yaml"
    case "${1:-status}" in
        enable)
            case "$port" in ''|*[!0-9]*) echo "usage: $0 memory-route enable [PORT]  (control_plane.memory.port, default 8888)" >&2; exit 2 ;; esac
            ensure_local_dir
            local lines
            lines="$(sed -n '/^  routes:/,/^  [a-z_]*:/p' "$shipped" | grep "$mark\$" | sed -E "s/^( *)# - /\1- /; s/:[0-9]+\"/:$port\"/")" \
                && [ -n "$lines" ] || { echo "no '$mark' lines in $shipped" >&2; rm -f "$route_file.new"; exit 1; }
            { printf '%s\n' "# Written by scripts/fabric-stack.sh memory-route; appended to fabric.routes by the gateway." \
                  "fabric:" "  local_routes:"; printf '%s\n' "$lines"; } >"$route_file.new" \
                && mv "$route_file.new" "$route_file" \
                || { echo "could not write $route_file" >&2; rm -f "$route_file.new"; exit 1; }
            say "memory routes enabled in $route_file (https://memory.<domain> and https://memory.dev.lan -> 127.0.0.1:$port)"
            say "apply: docker restart herdr-fabric-gateway   (it re-reads the file; leases return when agents re-register)"
            ;;
        disable)
            rm -f "$route_file" || { echo "could not remove $route_file" >&2; exit 1; }
            say "memory routes disabled (removed $route_file); apply: docker restart herdr-fabric-gateway"
            ;;
        status)
            if [ -f "$route_file" ]; then grep -E '^ *- "' "$route_file"; else echo "(memory routes off: no $route_file)"; fi
            ;;
        *) echo "usage: $0 memory-route enable [PORT]|disable|status" >&2; exit 2 ;;
    esac
}

status() {
    "${COMPOSE[@]}" ps 2>/dev/null || { echo "stack not running"; exit 0; }
    say "hindsight memory (optional profile):"
    memory_status
    say "gateway healthz:"
    local tokfile="${HERDR_ENGINEERING_FABRIC_TOKEN_FILE:-$HOME/.config/herdr-engineering/fabric-token}" auth=()
    if [ -r "$tokfile" ] && [ -s "$tokfile" ]; then   # the full payload needs the bearer; without it only the minimal reply
        auth=(-H "Authorization: Bearer $(tr -d '[:space:]' <"$tokfile")")
    fi
    curl -fsS "${auth[@]}" "http://127.0.0.1:29999/healthz" || echo "(unreachable)"
    echo
    say "coredns dev.lan resolution (local):"
    if command -v dig >/dev/null; then
        dig +short @127.0.0.1 dev.lan A
    else
        getent hosts dev.lan || echo "(no getent result)"
    fi
}

down() { "${COMPOSE[@]}" stop coredns fabric-gateway herdr-web; }  # not the control plane

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
    memory-route)
        memory_route "${2:-status}" "${3:-}"
        exit 0
        ;;
    *)
        echo "usage: $0 [up|up-replace|down|status|stop-old|memory-route enable [PORT]|disable|status]" >&2
        exit 2
        ;;
esac

say "Next: configure Tailscale split-DNS so dev.lan resolves fleet-wide."
say "  admin console -> DNS -> Nameservers -> Add nameserver:"
say "    address: $(tailscale ip -4 | head -1)"
say "    restrict to domain: dev.lan"
say "  (see deploy/fabric-stack/README.md for the exact steps + verification)"
