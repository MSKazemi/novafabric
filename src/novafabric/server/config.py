"""Server configuration model for `nova server start`.

ADR-0029: YAML config at ~/.config/novafabric/server.yaml or --config path.
Environment-variable overrides follow the NOVAFABRIC_SERVER_* convention.
Secrets (postgres_dsn, offline_key_path) are NEVER in the YAML body —
they must be supplied via env vars only.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class OidcConfig(BaseModel):
    issuer_url: str = ""
    audience: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.issuer_url and self.audience)


class ServerConfig(BaseModel):
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=7433, ge=1, le=65535)
    # "sqlite" or "postgres"
    backend: str = Field(default="sqlite")
    # SQLite db path — None means default (~/.novafabric/registry.db)
    db_path: str | None = None
    # OIDC — issuer_url="" means auth disabled
    oidc: OidcConfig = Field(default_factory=OidcConfig)

    # Secrets — only from env vars, never from YAML
    postgres_dsn: str | None = Field(default=None, exclude=True)
    offline_key_path: str | None = Field(default=None, exclude=True)

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
        return self


_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "novafabric" / "server.yaml"


def load_config(config_path: Path | None = None) -> ServerConfig:
    """Load ServerConfig from a YAML file (or the default path) plus env overrides."""
    path = config_path or _DEFAULT_CONFIG_PATH
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        # Strip secret keys that must not come from YAML
        for secret_key in ("postgres_dsn", "offline_key_path"):
            raw.pop(secret_key, None)
        return ServerConfig.model_validate(raw)
    return ServerConfig()
