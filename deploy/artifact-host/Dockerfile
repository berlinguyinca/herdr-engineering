# Artifact host image: ONLY the standalone herdr_artifact_host package.
# No control-plane code (herdr_engineering) is copied or installed.
#
# Build context = repository root:
#   docker build -f deploy/artifact-host/Dockerfile .
# Pinned by digest (python:3.13-slim-bookworm); bump deliberately with the manifests' versions.
ARG PYTHON_BASE=python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

# Build stage: the source tree lives in /src, never shadowing site-packages.
FROM ${PYTHON_BASE} AS build
WORKDIR /src
COPY deploy/artifact-host/pyproject.toml ./pyproject.toml
COPY herdr_artifact_host/ ./herdr_artifact_host/
RUN pip install --no-cache-dir --prefix=/install .

FROM ${PYTHON_BASE}
ARG GIT_COMMIT=dev
ENV ARTIFACT_HOST_COMMIT=$GIT_COMMIT \
    PYTHONUNBUFFERED=1
COPY --from=build /install /usr/local
# The blob volume mounts at /var/lib/artifact-host/blobs: created here and owned by the runtime user so a fresh named
# volume inherits that ownership. The root filesystem is read-only at run time (compose); /tmp is a tmpfs there.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin artifact \
 && mkdir /state /var/lib/artifact-host /var/lib/artifact-host/blobs \
 && chown 10001:10001 /state /var/lib/artifact-host /var/lib/artifact-host/blobs
WORKDIR /state
USER 10001:10001
# Health is per role (compose `healthcheck:` runs `herdr-artifact-host healthcheck <role>`); none is baked in.
HEALTHCHECK NONE
ENTRYPOINT ["herdr-artifact-host"]
CMD ["--help"]
