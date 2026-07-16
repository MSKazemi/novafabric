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


class QuotaConfig(BaseModel):
    """Storage quotas (ADR-0179 second slice, experimental).

    ``0`` means unlimited. Warn-then-reject enforcement at the capsule-ingest
    routes lives in ``novafabric.server.quotas``; it activates only when
    ``rate_limits.enabled`` is true and at least one limit is non-zero.
    """

    max_capsules_soft: int = Field(default=0, ge=0)
    max_capsules_hard: int = Field(default=0, ge=0)
    max_bytes_soft: int = Field(default=0, ge=0)
    max_bytes_hard: int = Field(default=0, ge=0)

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

    # ADR-0184 (experimental): explicit opt-out from local-token auth.
    # True restores the pre-0184 anonymous-admin behaviour when OIDC is off.
    insecure_no_auth: bool = False
    # ADR-0184: second confirmation required to combine insecure_no_auth with
    # a non-loopback bind host (see check_insecure_bind).
    i_know_this_is_public: bool = False

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
        if val := os.environ.get("NOVAFABRIC_SCIM_TOKEN"):
            self.scim_token = val
        if val := os.environ.get("NOVAFABRIC_SERVER_INSECURE_NO_AUTH"):
            self.insecure_no_auth = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_I_KNOW_THIS_IS_PUBLIC"):
            self.i_know_this_is_public = val.lower() in ("1", "true", "yes", "on")
        if val := os.environ.get("NOVAFABRIC_SERVER_TOKEN"):
            self.local_token = val
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
