# Artifact host image: ONLY the standalone herdr_artifact_host package.
# No control-plane code (herdr_engineering) is copied or installed.
#
# Build context = repository root:
#   docker build -f deploy/artifact-host/Dockerfile .
ARG PYTHON_BASE=python:3.13-slim-bookworm

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
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin artifact \
 && mkdir /state && chown 10001:10001 /state
WORKDIR /state
USER 10001:10001
HEALTHCHECK NONE
ENTRYPOINT ["herdr-artifact-host"]
CMD ["--help"]
