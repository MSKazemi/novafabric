# NovaFabric — experiment image
# Extras: server (Postgres + FastAPI), serve (dashboard), lineage-kuzu, lineage-migration
#
# Build context must be the repo root:
#   docker build -f deploy/docker/Dockerfile -t novafabric:dev .
#
# Two-stage build: wheel compiled in builder, runtime image stays slim.

# ── stage 1: build wheel ─────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src/ src/

RUN uv build --wheel --no-sources

# ── stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# pg_isready — entrypoint uses it to wait for Postgres before migrating
# curl — used to download OPA binary below
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

# OPA (Open Policy Agent) binary — policy evaluation for nova policy test/explain
ARG OPA_VERSION=1.16.2
RUN ARCH=$(uname -m) && \
    case "$ARCH" in \
      x86_64)  OPA_ARCH="linux_amd64_static" ;; \
      aarch64) OPA_ARCH="linux_arm64_static"  ;; \
      *)        OPA_ARCH="linux_amd64_static" ;; \
    esac && \
    curl -fsSL "https://github.com/open-policy-agent/opa/releases/download/v${OPA_VERSION}/opa_${OPA_ARCH}" \
         -o /usr/local/bin/opa && \
    chmod +x /usr/local/bin/opa

WORKDIR /app

COPY --from=builder /build/dist/ ./dist/
RUN WHL=$(ls dist/novafabric-*.whl) \
    && uv pip install --system "${WHL}[server,serve,lineage-kuzu,lineage-migration]" \
    && rm -rf dist/

RUN mkdir -p /data/capsules /data/nova /data/kuzu

COPY deploy/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/data/capsules", "/data/nova", "/data/kuzu"]
EXPOSE 4321

ENTRYPOINT ["/entrypoint.sh"]
