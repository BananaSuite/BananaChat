# The recommended image for agent sandboxes: the default Python image plus the
# tools coding agents expect (git, a C toolchain, Node.js, ripgrep, jq).
#
# Build it on the compute server and allow it in the runner (the runner never
# pulls images itself):
#   docker build -t bananachat-agent:1 compute/sandbox-image   (or podman build)
#   BC_SANDBOX_IMAGES=bananachat-agent:1
# Sandboxes still run with no network, a read-only root and uid 1000; nothing
# here needs root at run time.
ARG BASE=mirror.gcr.io/library/python:3.12-slim
FROM ${BASE}

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git build-essential nodejs npm ripgrep jq less procps file unzip zip xz-utils patch \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /bin/bash agent \
 && git config --system init.defaultBranch main \
 && git config --system user.name "BananaChat agent" \
 && git config --system user.email "agent@localhost" \
 && git config --system safe.directory '*'

ENV HOME=/workspace \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    npm_config_cache=/tmp/npm-cache \
    npm_config_update_notifier=false

USER 1000:1000
WORKDIR /workspace
