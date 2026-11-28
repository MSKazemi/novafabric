# NovaFabric — experiment image
# Extras: server (Postgres + FastAPI), serve (dashboard), clickhouse (cost
#         attribution), lineage-kuzu, lineage-migration
#
# Build context must be the repo root:
#   docker build -f deploy/docker/Dockerfile -t novafabric:dev .
#
# Two-stage build: wheel compiled in builder, runtime image stays slim.
#
# Every external input is pinned (#220): each image by version tag AND digest
# (Dependabot's `docker` entry for /deploy/docker bumps both together), and the
# OPA binary by version AND per-architecture sha256 (bumped by hand; see
# docs/ci.md "Pinned build inputs"). tests/docs/test_build_inputs_are_pinned.py
# fails on an image without a digest or a download without a checksum check.

# ── uv, the installer used by both stages ───────────────────────────────────
# A named stage rather than `COPY --from=<image>` so Dependabot sees it: it
# updates `FROM` lines, and this keeps the uv binary on an audited release.
FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

# ── stage 1: build wheel ─────────────────────────────────────────────────────
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS builder

COPY --from=uv /uv /usr/local/bin/uv

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
# Third time (v0.106.0): `readme` is dynamic, produced by a hatch metadata hook that
# the backend imports at build time; without it: "Build script does not exist:
# scripts/pypi_readme_hook.py". Guarded by
# test_every_hatch_hook_script_is_copied_into_the_build_context.
COPY scripts/pypi_readme_hook.py scripts/pypi_readme_hook.py

RUN uv build --wheel --no-sources

# ── stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

COPY --from=uv /uv /usr/local/bin/uv

# pg_isready — entrypoint uses it to wait for Postgres before migrating
# curl — used to download OPA binary below
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

# OPA (Open Policy Agent) binary — policy evaluation for nova policy test/explain.
# Pinned by version and verified against the sha256 OPA publishes beside each
# release asset (opa_<arch>.sha256), so a substituted download fails the build
# instead of landing in the image. To bump: set OPA_VERSION and copy the two
# values from that release's opa_linux_amd64_static.sha256 and
# opa_linux_arm64_static.sha256. An architecture with no pinned checksum is
# refused rather than given another architecture's binary.
ARG OPA_VERSION=1.23.0
ARG OPA_SHA256_AMD64=5a3fe2aee94c370a5cdf59acca2101e2a48ae17d2cf911d45f0af6cdd3fe4e6a
ARG OPA_SHA256_ARM64=488a597d39a09eb2909663f75d6ab918bcf38cd31a67c4475eb73313938fbd6b
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64)  OPA_ARCH="linux_amd64_static"; OPA_SHA256="${OPA_SHA256_AMD64}" ;; \
      aarch64) OPA_ARCH="linux_arm64_static"; OPA_SHA256="${OPA_SHA256_ARM64}" ;; \
      *) echo "no pinned OPA checksum for architecture $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 -o /tmp/opa \
         "https://github.com/open-policy-agent/opa/releases/download/v${OPA_VERSION}/opa_${OPA_ARCH}"; \
    echo "${OPA_SHA256}  /tmp/opa" | sha256sum -c -; \
    install -m 0755 /tmp/opa /usr/local/bin/opa; \
    rm -f /tmp/opa; \
    opa version

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

# Non-root by default (a standing project anti-pattern rule; found 2026-09-04 as the
# one gap in an otherwise-aligned chain: the Helm chart already pinned
# runAsUser/runAsGroup/fsGroup 1000 while this image defined NO such user and
# owned /data as root, so the chart's pods and this image disagreed about who
# may write the data dirs). uid/gid 1000 here MUST match the chart's
# podSecurityContext -- change them together or not at all.
#
# Bind-mount note: `mkdir -p` in the entrypoint succeeds on any EXISTING
# mounted dir regardless of owner; a host dir owned by another uid surfaces
# as a loud permission error on first write, not silent breakage. Named
# volumes inherit this chown on first use.
RUN groupadd --gid 1000 nova \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin nova \
    && mkdir -p /data/capsules /data/nova /data/kuzu \
    && chown -R nova:nova /data

COPY deploy/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/data/capsules", "/data/nova", "/data/kuzu"]
EXPOSE 4321

USER nova

ENTRYPOINT ["/entrypoint.sh"]
