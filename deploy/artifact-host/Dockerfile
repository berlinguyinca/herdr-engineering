# Artifact host image: ONLY the standalone herdr_artifact_host package.
# No control-plane code (herdr_engineering) is copied or installed.
#
# Build context = repository root:
#   docker build -f deploy/artifact-host/Dockerfile .
FROM python:3.13-slim

ARG GIT_COMMIT=dev
ENV ARTIFACT_HOST_COMMIT=$GIT_COMMIT \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# A throwaway pyproject scoped to the host package keeps herdr_engineering and
# its dependencies out of the image.
COPY herdr_artifact_host/ ./herdr_artifact_host/
RUN printf '%s\n' \
    '[build-system]' \
    'requires = ["setuptools>=68"]' \
    'build-backend = "setuptools.build_meta"' \
    '[project]' \
    'name = "herdr-artifact-host"' \
    'version = "0.1.0"' \
    'requires-python = ">=3.12"' \
    'dependencies = []' \
    '[project.scripts]' \
    'herdr-artifact-host = "herdr_artifact_host.cli:main"' \
    '[tool.setuptools.packages.find]' \
    'include = ["herdr_artifact_host*"]' \
    '[tool.setuptools.package-data]' \
    'herdr_artifact_host = ["share_static/*"]' \
    > pyproject.toml \
 && pip install . \
 && rm -rf build ./*.egg-info

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin artifact \
 && mkdir /state && chown 10001:10001 /state
USER 10001:10001

HEALTHCHECK NONE
ENTRYPOINT ["herdr-artifact-host"]
CMD ["--help"]
