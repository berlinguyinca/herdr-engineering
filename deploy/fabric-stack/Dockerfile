# HerdR Engineering runtime image — fabric gateway + private web UI.
#
# Built for the master-host (bender) container stack. Every service uses
# `network_mode: host` so the containers share the host network stack: the
# gateway binds the host's tailnet IP + the whole dev:<port> range
# (18000-28999) and routes to other tailnet hosts; the web UI binds loopback
# only (private-by-default invariant).
#
# The `tailscale` CLI is intentionally NOT installed: the container relies on
# explicit `--bind` / `--url-base` from the compose command instead of
# auto-discovery (magicdns_name()/tailnet_ipv4() degrade to None safely).
#
# Build context = repository root:
#   docker build -f deploy/fabric-stack/Dockerfile .
# Pinned by digest (python:3.12-slim as resolved 2026-11-24); bump deliberately, not by a rebuild.
ARG RUNTIME_BASE=python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
FROM ${RUNTIME_BASE}

# Optional build stamp surfaced by /api/v1/health (commit=). Passed via
# `docker compose build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD)`;
# defaults to "dev" for un-stamped / local builds.
ARG GIT_COMMIT=dev
ENV HERDR_ENGINEERING_COMMIT=$GIT_COMMIT

# curl = container healthchecks; ca-certificates = HTTPS proxy targets.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package (no dev deps). [build-system] is setuptools.
COPY pyproject.toml README.md LICENSE ./
COPY deploy/fabric-stack/constraints.txt ./constraints.txt
COPY herdr_engineering/ ./herdr_engineering/
# Constraints are exported from uv.lock (a test keeps them equal), so the same commit builds the same dependencies.
RUN pip install --no-cache-dir -c constraints.txt ".[collab,review-gate,share]"

# Build-free frontend assets (ES modules + Web Components, no bundler). Copied
# into the image so the control-plane web is self-contained; serve via
# `--web-root /app/packages` (dev checkouts resolve packages/ relative to the
# package instead).
COPY packages/ ./packages/

# The control-plane worker's defaults and SQL migrations ship IN the image, so they
# roll forward and back with the image tag (deploy.sh builds from a temporary
# worktree; a relative bind mount would point into a directory that is deleted).
COPY deploy/control-plane/config.yaml ./control-plane/config.yaml
COPY deploy/control-plane/migrations/ ./control-plane/migrations/
# The fabric config (gateway routes, domains) read by fabric-gateway and herdr-web as /app/config.yaml. Baked in
# for the same reason: a relative bind mount of ../fabric-stack/config.yaml would dangle once deploy.sh removes its
# temporary worktree. Host-specific routes are not baked: they come from the host-local file the gateway mounts
# (scripts/fabric-stack.sh memory-route), read after this one.
COPY deploy/fabric-stack/config.yaml ./config.yaml

# Runtime state + config live here; both are provided at runtime by the stack
# (state -> named volume; config defaults are baked in above). Overridable per service.
ENV HERDR_ENGINEERING_STATE=/state \
    HERDR_ENGINEERING_CONFIG=/app/config.yaml

# No default healthcheck; each service in compose.yaml sets its own.
HEALTHCHECK NONE

ENTRYPOINT ["herdr-eng"]
CMD ["--help"]
