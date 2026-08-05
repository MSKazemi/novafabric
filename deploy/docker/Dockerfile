# NovaFabric — experiment image
# Extras: server (Postgres + FastAPI), serve (dashboard), clickhouse (cost
#         attribution), lineage-kuzu, lineage-migration
#
# Build context must be the repo root:
#   docker build -f deploy/docker/Dockerfile -t novafabric:dev .
#
# Two-stage build: wheel compiled in builder, runtime image stays slim.

# ── stage 1: build wheel ─────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build
# README.md is REQUIRED, not cosmetic: pyproject.toml declares
# `readme = "README.md"`, and hatchling hard-fails the wheel build with
# "OSError: Readme file does not exist" if it is missing from the build
# context. Adding the `readme` field without this COPY broke the image build.
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
# The wheel force-includes alembic/ as novafabric/migrations/registry (see
# [tool.hatch.build.targets.wheel.force-include]). Without it here the build
# fails with "Forced include not found: /build/alembic" — and the entrypoint's
# `nova db upgrade` would have no migrations to run. Pre-existing breakage: the
# force-include landed on 2026-07-24 without a matching COPY, so this image had
# been unbuildable since then; found independently while verifying ADR-0222.
COPY alembic/ alembic/

# Same story, second time. v0.99.0 added force-includes for the canonical JSON
# Schemas that runtime validators load (BL-037) and again did not add the COPY,
# so this image has been unbuildable since that release with:
#   FileNotFoundError: Forced include not found: /build/schemas/export-manifest.schema.json
# A guard now exists — tests/packaging_metadata/test_dockerfile_force_includes.py
# asserts every force-include source is present in the build context, so a third
# instance fails in the test suite rather than at release time.
COPY schemas/ schemas/

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
# `clickhouse` added by ADR-0222. clickhouse-connect used to arrive
# unconditionally via [project.dependencies]; now that it is extras-only, the
# image would silently lose ClickHouse cost attribution without naming it here
# — even though the compose `prod` profile runs a ClickHouse container and sets
# NOVA_CLICKHOUSE_URL, which would leave the dashboard CostTab stuck in stub
# mode. duckdb, pyarrow and python-louvain also left core in ADR-0222 but need
# no new extra here: `serve` pins all three (it owns the Live Topology
# Dashboard's cluster store, Arrow ADS encoder and Louvain pass), and
# `lineage-migration` pins pyarrow as well.
RUN WHL=$(ls dist/novafabric-*.whl) \
    && uv pip install --system \
       "${WHL}[server,serve,clickhouse,lineage-kuzu,lineage-migration]" \
    && rm -rf dist/

RUN mkdir -p /data/capsules /data/nova /data/kuzu

COPY deploy/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/data/capsules", "/data/nova", "/data/kuzu"]
EXPOSE 4321

ENTRYPOINT ["/entrypoint.sh"]
