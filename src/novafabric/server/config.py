"""Server configuration model for `nova server start`.

ADR-0029: YAML config at ~/.config/novafabric/server.yaml or --config path.
Environment-variable overrides follow the NOVAFABRIC_SERVER_* convention.
Secrets (postgres_dsn, offline_key_path) are NEVER in the YAML body —
they must be supplied via env vars only.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from novafabric.server.saml import SamlConfig


class InsecureBindError(ValueError):
    """``insecure_no_auth`` combined with a non-loopback bind host (ADR-0184).

    Raised unless the operator also confirms with ``i_know_this_is_public``.
    Subclasses ``ValueError`` so pydantic surfaces it as a validation error.
    """


def _is_loopback_host(host: str) -> bool:
    """True for loopback bind hosts (127.0.0.0/8, ::1, 'localhost')."""
    name = host.strip().lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def check_insecure_bind(config: "ServerConfig") -> None:
    """Refuse insecure-no-auth on a non-loopback bind without confirmation.

    ADR-0184: anonymous admin on a network-reachable interface must be a
    doubly-explicit choice. Raises :class:`InsecureBindError` when
    ``insecure_no_auth`` is set, the bind host is non-loopback, and
    ``i_know_this_is_public`` is not.
    """
    if (
        config.insecure_no_auth
        and not _is_loopback_host(config.host)
        and not config.i_know_this_is_public
    ):
        raise InsecureBindError(
            f"Refusing to start: insecure-no-auth with non-loopback bind host "
            f"{config.host!r} would expose anonymous admin to the network. "
            f"Pass --i-know-this-is-public to confirm, or drop "
            f"--insecure-no-auth (ADR-0184)."
        )


class OidcConfig(BaseModel):
    issuer_url: str = ""
    audience: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.issuer_url and self.audience)


class ScimConfig(BaseModel):
    """SCIM 2.0 provisioning flag (ADR-0139). Default off — endpoints 404."""

    enabled: bool = False
    # ADR-0139 D3: operator-declared IdP group displayName → RBAC role map.
    # Empty by default (no group grants any role). Validated against the six
    # roles; the SCIM Group route wiring resolves membership through it.
    group_role_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("group_role_map")
    @classmethod
    def _validate_group_role_map(cls, value: dict[str, str]) -> dict[str, str]:
        # Reuse the resolver's validation so config and runtime agree on the
        # six-role vocabulary; raises on any unknown target role.
        from novafabric.server.scim_group_mapping import GroupRoleMapping

        GroupRoleMapping.from_config(value)
        return value

    def role_mapping(self) -> Any:
        """Build the validated :class:`GroupRoleMapping` for this config."""
        from novafabric.server.scim_group_mapping import GroupRoleMapping

        return GroupRoleMapping.from_config(self.group_role_map)


class RateLimitClassConfig(BaseModel):
    """One limit class: sustained ``rate`` (tokens/second) + ``burst`` capacity."""

    rate: float = Field(gt=0)
    burst: int = Field(ge=1)


class WorkspaceQuotaConfig(BaseModel):
    """One workspace's budget inside ``quota.workspaces`` (ADR-0208 D3).

    Same four keys and semantics as the global :class:`QuotaConfig`
    (``0`` = unlimited); validation mirrors ``_hard_at_least_soft``.
    """

    max_capsules_soft: int = Field(default=0, ge=0)
    max_capsules_hard: int = Field(default=0, ge=0)
    max_bytes_soft: int = Field(default=0, ge=0)
    max_bytes_hard: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _hard_at_least_soft(self) -> "WorkspaceQuotaConfig":
        for kind in ("capsules", "bytes"):
            soft = getattr(self, f"max_{kind}_soft")
            hard = getattr(self, f"max_{kind}_hard")
            if soft and hard and hard < soft:
                raise ValueError(
                    f"quota.workspaces.<slug>.max_{kind}_hard ({hard}) must be"
                    f" >= max_{kind}_soft ({soft})"
                )
        return self

    @property
    def any_limit(self) -> bool:
        """True when at least one of the four limits is non-zero."""
        return any(
            (
                self.max_capsules_soft,
                self.max_capsules_hard,
                self.max_bytes_soft,
                self.max_bytes_hard,
            )
        )


class QuotaConfig(BaseModel):
    """Storage quotas (ADR-0179 second slice, experimental).

    ``0`` means unlimited. Warn-then-reject enforcement at the capsule-ingest
    routes lives in ``novafabric.server.quotas``; it activates only when
    ``rate_limits.enabled`` is true and at least one limit is non-zero.

    ``workspaces`` (ADR-0208 D3, experimental) — additive per-workspace-slug
    budgets checked against **metered** counters alongside the unchanged
    global check. An absent/empty map keeps behavior byte-identical.
    """

    max_capsules_soft: int = Field(default=0, ge=0)
    max_capsules_hard: int = Field(default=0, ge=0)
    max_bytes_soft: int = Field(default=0, ge=0)
    max_bytes_hard: int = Field(default=0, ge=0)
    workspaces: dict[str, WorkspaceQuotaConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _hard_at_least_soft(self) -> "QuotaConfig":
        for kind in ("capsules", "bytes"):
            soft = getattr(self, f"max_{kind}_soft")
            hard = getattr(self, f"max_{kind}_hard")
            if soft and hard and hard < soft:
                raise ValueError(
                    f"quota.max_{kind}_hard ({hard}) must be >= "
                    f"quota.max_{kind}_soft ({soft})"
                )
        return self


class UsageConfig(BaseModel):
    """``server.usage`` block (ADR-0208 P1, experimental).

    All of it inert unless ``server.rate_limits.enabled`` (the existing
    experimental master switch); ``metering_enabled`` is the accounting
    kill-switch that keeps rate limits on while metering is off. Defaults are
    the normative numbers from ``design/spec/usage-metering-v0.md``.
    """

    metering_enabled: bool = True
    #: ``api_requests`` accumulator flush interval (never a per-request write).
    flush_interval_s: float = Field(default=60.0, gt=0)
    #: LRU bound on the accumulator map (ADR-0179 bucket-map discipline).
    accumulator_max_entries: int = Field(default=10_000, ge=1)
    #: Monthly rollup retention (chargeback rows).
    rollup_retention_months: int = Field(default=24, ge=1)
    #: Raw ledger retention after a period is finalized.
    ledger_retention_months: int = Field(default=3, ge=1)


class IngestConfig(BaseModel):
    """``server.ingest`` block (ADR-0203 P1, experimental).

    Bounds the capsule-ingest surface (``POST /v0/capsules``): request-size
    cap, disk-spool chunk size, and zip-bomb guards. All byte/entry keys and
    ``zip_max_ratio`` accept ``0`` = disabled (escape hatch, discouraged).
    Defaults are the normative numbers from
    ``design/spec/ingest-hardening-v0.md``.
    """

    #: Cap on the request body of POST /v0/capsules. 0 = unlimited.
    max_upload_bytes: int = Field(default=268_435_456, ge=0)  # 256 MiB
    #: Read/decompress chunk size; peak per-request memory is O(this).
    spool_chunk_bytes: int = Field(default=1_048_576, ge=65_536)  # 1 MiB
    #: Max members in the archive (central directory AND streamed count).
    zip_max_entries: int = Field(default=10_000, ge=0)
    #: Cap on total decompressed bytes across all members. 0 = disabled.
    zip_max_uncompressed_bytes: int = Field(default=2_147_483_648, ge=0)  # 2 GiB
    #: Max compression ratio (uncompressed/compressed), per member (past the
    #: 1 MiB floor) and for the archive total. 0 = disabled.
    zip_max_ratio: float = Field(default=100.0, ge=0)


class BulkConfig(BaseModel):
    """``server.bulk`` block (ADR-0206 P1, experimental).

    ``max_items`` bounds one ``POST /v0/capsules/bulk-delete`` request
    (default 100, hard ceiling 1000 — spec ``bulk-ops-pagination-v0``).
    """

    max_items: int = Field(default=100, ge=1, le=1000)


class PaginationConfig(BaseModel):
    """``server.pagination`` block (ADR-0206 P1, experimental).

    ``legacy_offset_cursors`` — accept v0 ``{"offset": N}`` cursors on the
    capsules list route for one deprecation cycle (ADR-0188). Flipped to
    ``False`` at sunset; a legacy cursor then decodes as ``invalid_cursor``.
    """

    legacy_offset_cursors: bool = True


class RateLimitsConfig(BaseModel):
    """``server.rate_limits`` block (ADR-0179, experimental).

    Additive and **disabled by default** — an absent block or ``enabled: false``
    means the limiter is fully inert and upgrading changes zero behavior.
    Per-class defaults are the normative budgets from
    ``design/spec/rate-limiting-quotas-v0.md``.
    """

    enabled: bool = False
    ingest: RateLimitClassConfig = Field(
        default_factory=lambda: RateLimitClassConfig(rate=100, burst=200)
    )
    read: RateLimitClassConfig = Field(
        default_factory=lambda: RateLimitClassConfig(rate=50, burst=100)
    )
    admin: RateLimitClassConfig = Field(
        default_factory=lambda: RateLimitClassConfig(rate=10, burst=20)
    )
    # Storage quotas (second slice) — warn-then-reject at capsule ingest;
    # enforcement in novafabric.server.quotas. Absent ⇒ no quota checks.
    quota: QuotaConfig | None = None
    # Rejections of one key within one window that trigger the audit event.
    audit_threshold_rejections: int = Field(default=100, ge=1)
    audit_window_seconds: int = Field(default=60, ge=1)


class WebhooksConfig(BaseModel):
    """``server.webhooks`` block (ADR-0205, experimental).

    Additive and **disabled by default** — an absent block or
    ``enabled: false`` means no dispatch worker thread starts and server
    behavior is byte-identical to a build without the feature. The CRUD
    routes remain mounted (registry rows only); ping/redeliver require the
    dispatcher and answer 409 while it is disabled.
    """

    enabled: bool = False
    queue_max: int = Field(default=1000, ge=1)
    max_attempts: int = Field(default=5, ge=1, le=10)
    timeout_s: float = Field(default=5.0, gt=0)
    delivery_retention_days: int = Field(default=30, ge=1)
    delivery_retention_rows: int = Field(default=10_000, ge=1)
    allow_insecure_url: bool = False
    # SSRF guard opt-out: permit webhook targets on private/link-local/reserved
    # networks. Default false (secure); self-hosted deployments that legitimately
    # post to internal hosts set this. Loopback is always allowed.
    allow_internal_targets: bool = False


class ObservabilityConfig(BaseModel):
    """Self-observability surface controls (ADR-0182, experimental).

    ``metrics_exempt`` — when True, ``/metrics`` is served without auth.
    Default False (gated): only exempt when the listener is loopback-only or
    the scrape network is isolated (ADR-0182 D1).

    ``self_tracing`` — ADR-0182 D5: opt-in, **default OFF**. When enabled the
    server emits one OTel span per HTTP request (route template, method,
    status, duration — never bodies, auth headers, or tenant identifiers)
    into ``self_tracing_endpoint``. This is explicitly *not* telemetry: spans
    never leave the deployment.

    ``self_tracing_endpoint`` — OTLP/HTTP traces URL. ``None`` means the local
    serve OTLP ingest (``http://127.0.0.1:4321/api/otlp/v1/traces``).
    Non-loopback hosts are refused at startup unless
    ``NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE=1`` (no-phone-home guarantee).
    """

    metrics_exempt: bool = False
    self_tracing: bool = False
    self_tracing_endpoint: str | None = None


class UnpartitionedStoreError(ValueError):
    """More than one organization exists while the capsule store is shared.

    Raised unless the operator confirms with ``i_accept_shared_capsule_store``.
    Subclasses ``ValueError`` so pydantic surfaces it as a validation error,
    matching :class:`InsecureBindError`.
    """


def check_multi_org_shared_store(
    config: "ServerConfig", org_count: int
) -> None:
    """Refuse to serve multiple organizations from one shared capsule store.

    ADR-0178 layered organizations/workspaces over the ``tenant_id`` RLS key,
    which protects **metadata rows** — not **capsule bytes**. The capsule
    directory is resolved without reference to the auth context, so
    ``GET /capsules`` enumerates every organization's capsules to any
    authenticated reader, and ``GET /capsules/{run_id}`` serves any of them by
    id. The tenant-scoped metadata write is best-effort and, on the shipped
    auth path, does not fire at all.

    Rather than let that gap be discovered in production, a deployment that
    has actually created more than one organization must acknowledge it. This
    narrows a *claim* — it does not fix the isolation gap; the remediation
    options are in ``design/security-reviews/2026-07-18-capsule-store-tenant-
    isolation.md`` and are gated on Security-Architect review.

    Single-organization deployments — the default, and the only shape the
    bootstrap creates — are unaffected.
    """
    if org_count > 1 and not config.i_accept_shared_capsule_store:
        raise UnpartitionedStoreError(
            f"Refusing to start: {org_count} organizations exist, but the "
            f"capsule store is NOT tenant-partitioned — any authenticated "
            f"reader can list and fetch every organization's capsules "
            f"(ADR-0178, see design/security-reviews/"
            f"2026-07-18-capsule-store-tenant-isolation.md). Server mode is "
            f"single-tenant-safe only today. Pass "
            f"--i-accept-shared-capsule-store (or set "
            f"NOVAFABRIC_SERVER_I_ACCEPT_SHARED_CAPSULE_STORE=1) to run "
            f"anyway, or keep a single organization."
        )


class StepUpConfig(BaseModel):
    """Step-up (fresh-auth) gate for destructive actions (ADR-0246 slice 1).

    Off by default (experimental); flipping the server-mode default on is
    deferred until the ADR-0228 authorization model lands. Freshness applies
    to credentials carrying ``auth_time``/``iat``; API keys and the local
    token are exempt in this slice.
    """

    enabled: bool = False
    max_age_seconds: int = Field(default=300, ge=1)


class TlsConfigError(ValueError):
    """The TLS configuration is unusable (missing/unreadable material)."""


class TlsConfig(BaseModel):
    """First-party TLS on the server listener (ADR-0241 slice 1).

    ADR-0029 documented ``tls.enabled`` / ``tls.cert_path`` / ``tls.key_path``
    without an implementation; this block makes them real. ``enabled: true``
    requires both paths; :func:`check_tls_config` (run at launch, not at model
    construction) verifies the files exist and the private key is owner-only
    (not group/world readable) — the same posture as ``offline_key_path``.
    mTLS client-certificate verification is a later ADR-0241 slice.
    """

    enabled: bool = False
    cert_path: str | None = None
    key_path: str | None = None


def check_tls_config(tls: TlsConfig) -> None:
    """Fail closed on unusable TLS material. Call before binding a listener."""
    if not tls.enabled:
        return
    if not tls.cert_path or not tls.key_path:
        raise TlsConfigError(
            "tls.enabled requires both tls.cert_path and tls.key_path "
            "(NOVA_TLS_CERT_PATH / NOVA_TLS_KEY_PATH)"
        )
    cert = Path(tls.cert_path)
    key = Path(tls.key_path)
    for name, path in (("tls.cert_path", cert), ("tls.key_path", key)):
        if not path.is_file():
            raise TlsConfigError(f"{name} does not exist: {path}")
    mode = key.stat().st_mode & 0o077
    if mode != 0:
        raise TlsConfigError(
            f"tls.key_path {key} is group/world-accessible "
            f"(mode {oct(key.stat().st_mode & 0o777)}); chmod 600 it"
        )


class ServerConfig(BaseModel):
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=7433, ge=1, le=65535)
    # "sqlite" or "postgres"
    backend: str = Field(default="sqlite")
    # SQLite db path — None means default (~/.novafabric/registry.db)
    db_path: str | None = None
    # OIDC — issuer_url="" means auth disabled
    oidc: OidcConfig = Field(default_factory=OidcConfig)
    # SAML SSO (ADR-0138, experimental partial) — absent block ⇒ backend disabled
    saml: SamlConfig | None = None
    # SCIM provisioning (ADR-0139) — disabled by default; also requires the
    # NOVAFABRIC_SCIM_TOKEN secret before the /scim/v2 endpoints activate.
    scim: ScimConfig = Field(default_factory=ScimConfig)
    # Self-observability surface (ADR-0182, experimental) — /metrics gating.
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    # API rate limiting (ADR-0179, experimental) — disabled by default.
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    # Ingest hardening (ADR-0203, experimental) — safe defaults apply when
    # the block is absent.
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    # Bulk capsule operations (ADR-0206, experimental) — batch-size bound.
    bulk: BulkConfig = Field(default_factory=BulkConfig)
    # Usage metering (ADR-0208, experimental) — inert unless
    # rate_limits.enabled; metering_enabled is the accounting kill-switch.
    usage: UsageConfig = Field(default_factory=UsageConfig)
    # Keyset-pagination cursor policy (ADR-0206, experimental).
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)

    # Webhook subscription registry (ADR-0205, experimental) — disabled by
    # default; off ⇒ no dispatch worker thread, byte-identical behavior.
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)

    # Step-up fresh-auth gate for destructive actions (ADR-0246, experimental).
    step_up: StepUpConfig = Field(default_factory=StepUpConfig)
    # First-party listener TLS (ADR-0241 slice 1) — off by default; the
    # reverse-proxy posture remains supported.
    tls: TlsConfig = Field(default_factory=TlsConfig)

    # ADR-0184 (experimental): explicit opt-out from local-token auth.
    # True restores the pre-0184 anonymous-admin behaviour when OIDC is off.
    insecure_no_auth: bool = False
    # ADR-0184: second confirmation required to combine insecure_no_auth with
    # a non-loopback bind host (see check_insecure_bind).
    i_know_this_is_public: bool = False

    # Enterprise-hardening (2026-07-24): the RFC 8628 device-grant demo flow
    # (/v0/auth/device/code, /token, /approve) mints short-lived HS256 tokens
    # that the real verifier (JWKS/RS256 or the opaque local token) never
    # honours — it is local/testing scaffolding only, and /approve is
    # unauthenticated. It is therefore OFF by default and must be explicitly
    # enabled (env NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT) so no production
    # deployment exposes an unauthenticated role-approval surface.
    demo_device_grant: bool = False

    # ADR-0178 (2026-07-18): the capsule store is NOT tenant-partitioned — it
    # is one directory shared by every org, and `GET /capsules` lists all of
    # it to any authenticated reader. Isolation exists only in the metadata
    # store (RLS), and that write is best-effort. Server mode is therefore
    # single-tenant-safe only. Set this to acknowledge the limitation and run
    # more than one organization anyway (see check_multi_org_shared_store).
    i_accept_shared_capsule_store: bool = False

    # Secrets — only from env vars, never from YAML
    postgres_dsn: str | None = Field(default=None, exclude=True)
    offline_key_path: str | None = Field(default=None, exclude=True)
    scim_token: str | None = Field(default=None, exclude=True)
    # ADR-0184: resolved local auth token (env NOVAFABRIC_SERVER_TOKEN →
    # token file → fresh). Never from YAML; resolved lazily when unset.
    local_token: str | None = Field(default=None, exclude=True)

    # ------------------------------------------------------------------ #

    @model_validator(mode="after")
    def _apply_env_overrides(self) -> "ServerConfig":
        """Apply NOVAFABRIC_SERVER_* environment variable overrides."""
        if val := os.environ.get("NOVAFABRIC_SERVER_HOST"):
            self.host = val
        if val := os.environ.get("NOVAFABRIC_SERVER_PORT"):
            self.port = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_BACKEND"):
            self.backend = val
        if val := os.environ.get("NOVAFABRIC_SERVER_DB_PATH"):
            self.db_path = val
        if val := os.environ.get("NOVAFABRIC_POSTGRES_DSN"):
            self.postgres_dsn = val
        if val := os.environ.get("NOVAFABRIC_OFFLINE_KEY_PATH"):
            self.offline_key_path = val
        if val := os.environ.get("NOVAFABRIC_SERVER_STEP_UP_ENABLED"):
            self.step_up.enabled = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_STEP_UP_MAX_AGE_SECONDS"):
            self.step_up.max_age_seconds = int(val)
        if val := os.environ.get("NOVA_TLS_ENABLED"):
            self.tls.enabled = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVA_TLS_CERT_PATH"):
            self.tls.cert_path = val
        if val := os.environ.get("NOVA_TLS_KEY_PATH"):
            self.tls.key_path = val
        if val := os.environ.get("NOVAFABRIC_SERVER_SCIM_ENABLED"):
            self.scim.enabled = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_METRICS_EXEMPT"):
            self.observability.metrics_exempt = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_SELF_TRACING"):
            self.observability.self_tracing = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_SELF_TRACING_ENDPOINT"):
            self.observability.self_tracing_endpoint = val
        # ADR-0179 rate-limit overrides (NOVAFABRIC_SERVER_RATE_LIMITS_*)
        if val := os.environ.get("NOVAFABRIC_SERVER_RATE_LIMITS_ENABLED"):
            self.rate_limits.enabled = val.lower() in ("1", "true", "yes", "on")
        for cls_name in ("ingest", "read", "admin"):
            prefix = f"NOVAFABRIC_SERVER_RATE_LIMITS_{cls_name.upper()}"
            if val := os.environ.get(f"{prefix}_RATE"):
                getattr(self.rate_limits, cls_name).rate = float(val)
            if val := os.environ.get(f"{prefix}_BURST"):
                getattr(self.rate_limits, cls_name).burst = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_RATE_LIMITS_AUDIT_THRESHOLD_REJECTIONS"):
            self.rate_limits.audit_threshold_rejections = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_RATE_LIMITS_AUDIT_WINDOW_SECONDS"):
            self.rate_limits.audit_window_seconds = int(val)
        # ADR-0203 ingest-hardening overrides (NOVAFABRIC_SERVER_INGEST_*)
        if val := os.environ.get("NOVAFABRIC_SERVER_INGEST_MAX_UPLOAD_BYTES"):
            self.ingest.max_upload_bytes = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_INGEST_SPOOL_CHUNK_BYTES"):
            self.ingest.spool_chunk_bytes = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_INGEST_ZIP_MAX_ENTRIES"):
            self.ingest.zip_max_entries = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_INGEST_ZIP_MAX_UNCOMPRESSED_BYTES"):
            self.ingest.zip_max_uncompressed_bytes = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_INGEST_ZIP_MAX_RATIO"):
            self.ingest.zip_max_ratio = float(val)
        # ADR-0208 usage-metering overrides (NOVAFABRIC_SERVER_USAGE_*)
        if val := os.environ.get("NOVAFABRIC_SERVER_USAGE_METERING_ENABLED"):
            self.usage.metering_enabled = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_USAGE_FLUSH_INTERVAL_S"):
            self.usage.flush_interval_s = float(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_USAGE_ACCUMULATOR_MAX_ENTRIES"):
            self.usage.accumulator_max_entries = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_USAGE_ROLLUP_RETENTION_MONTHS"):
            self.usage.rollup_retention_months = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_USAGE_LEDGER_RETENTION_MONTHS"):
            self.usage.ledger_retention_months = int(val)
        # ADR-0206 bulk-ops / pagination overrides
        if val := os.environ.get("NOVAFABRIC_SERVER_BULK_MAX_ITEMS"):
            # Route through the model so the 1–1000 bound holds for env too.
            self.bulk = BulkConfig(max_items=int(val))
        if val := os.environ.get(
            "NOVAFABRIC_SERVER_PAGINATION_LEGACY_OFFSET_CURSORS"
        ):
            self.pagination.legacy_offset_cursors = val.lower() in (
                "1", "true", "yes", "on",
            )

        # ADR-0205 webhook overrides (NOVAFABRIC_SERVER_WEBHOOKS_*; the spec
        # additionally names NOVAFABRIC_WEBHOOKS_QUEUE_MAX for the queue bound).
        if val := os.environ.get("NOVAFABRIC_SERVER_WEBHOOKS_ENABLED"):
            self.webhooks.enabled = val.lower() in ("1", "true", "yes", "on")
        if val := (
            os.environ.get("NOVAFABRIC_WEBHOOKS_QUEUE_MAX")
            or os.environ.get("NOVAFABRIC_SERVER_WEBHOOKS_QUEUE_MAX")
        ):
            self.webhooks.queue_max = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_WEBHOOKS_MAX_ATTEMPTS"):
            self.webhooks.max_attempts = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_WEBHOOKS_TIMEOUT_S"):
            self.webhooks.timeout_s = float(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_WEBHOOKS_DELIVERY_RETENTION_DAYS"):
            self.webhooks.delivery_retention_days = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_WEBHOOKS_DELIVERY_RETENTION_ROWS"):
            self.webhooks.delivery_retention_rows = int(val)
        if val := os.environ.get("NOVAFABRIC_SERVER_WEBHOOKS_ALLOW_INSECURE_URL"):
            self.webhooks.allow_insecure_url = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SCIM_TOKEN"):
            self.scim_token = val
        if val := os.environ.get("NOVAFABRIC_SERVER_INSECURE_NO_AUTH"):
            self.insecure_no_auth = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_I_KNOW_THIS_IS_PUBLIC"):
            self.i_know_this_is_public = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_I_ACCEPT_SHARED_CAPSULE_STORE"):
            self.i_accept_shared_capsule_store = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_TOKEN"):
            self.local_token = val
        if val := os.environ.get("NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT"):
            self.demo_device_grant = val.lower() in ("1", "true", "yes", "on")
        return self

    @model_validator(mode="after")
    def _refuse_unconfirmed_public_insecure(self) -> "ServerConfig":
        """ADR-0184: runs after env overrides (validators run in definition order)."""
        check_insecure_bind(self)
        return self

    @property
    def scim_active(self) -> bool:
        """SCIM is live only when explicitly enabled AND a token is configured."""
        return bool(self.scim.enabled and self.scim_token)


_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "novafabric" / "server.yaml"


def load_config(config_path: Path | None = None) -> ServerConfig:
    """Load ServerConfig from a YAML file (or the default path) plus env overrides."""
    path = config_path or _DEFAULT_CONFIG_PATH
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        # Strip secret keys that must not come from YAML
        for secret_key in ("postgres_dsn", "offline_key_path", "scim_token", "local_token"):
            raw.pop(secret_key, None)
        return ServerConfig.model_validate(raw)
    return ServerConfig()
