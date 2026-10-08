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
FROM python:3.12-slim

# curl = container healthchecks; ca-certificates = HTTPS proxy targets.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package (no dev deps). [build-system] is setuptools.
COPY pyproject.toml README.md LICENSE ./
COPY herdr_engineering/ ./herdr_engineering/
RUN pip install --no-cache-dir .

# Build-free frontend assets (ES modules + Web Components, no bundler). Copied
# into the image so the control-plane web is self-contained; serve via
# `--web-root /app/packages` (dev checkouts resolve packages/ relative to the
# package instead).
COPY packages/ ./packages/

# Runtime state + config live here; both are provided at runtime by the stack
# (state -> named volume, config -> bind-mount). Overridable per service.
ENV HERDR_ENGINEERING_STATE=/state \
    HERDR_ENGINEERING_CONFIG=/app/config.yaml

# No default healthcheck; each service in compose.yaml sets its own.
HEALTHCHECK NONE

ENTRYPOINT ["herdr-eng"]
CMD ["--help"]
