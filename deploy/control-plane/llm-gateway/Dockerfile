# Gateway image: a reviewed Linux inferweave release binary and curl, nothing else.
# Build only from a release that has passed review (for example a pinned Whiteale release
# of a merged commit); never from a macOS, debug or unreviewed binary.
ARG BASE=debian:bookworm-slim
FROM ${BASE}
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=0755 inferweave /usr/local/bin/inferweave-bin
# The image entrypoint is the gateway binary; compose passes `gateway serve ...`.
ENTRYPOINT ["/usr/local/bin/inferweave-bin"]
USER 1000:1000
