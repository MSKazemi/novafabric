"""URL registry — wire-level capture's classification map (RFC-0001 Option C).

Wire-level hooks (httpx, requests, and the future aiohttp/urllib3/boto3
hooks added in C-3) consume this registry to decide which outbound HTTP
URLs to capture and how to classify them. The registry is data, not code;
users extend it without editing the package.

Lookup precedence:

1. ``$NOVAFABRIC_URL_REGISTRY`` — absolute path; explicit override.
2. ``~/.novafabric/url_registry.yaml`` — per-user override.
3. ``url_registry.yaml`` vendored next to this module — default.

An override file **replaces** the vendored default. Merge semantics are
deferred to v0.6 (would need an ADR).

Dynamic Ollama detection: ``OLLAMA_BASE_URL`` (langchain_ollama) and
``OLLAMA_HOST`` (ollama SDK) are read at *call time* (not hook-install time)
so that workloads that load these variables via ``python-dotenv`` are captured
even when the env var is not yet set when the hook installs via sitecustomize.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

_VENDORED_DEFAULT_PATH = Path(__file__).with_name("url_registry.yaml")


class UrlRegistryEntry(BaseModel):
    """One row in the URL registry."""

    match: str = Field(min_length=1, description="Substring matched against the full URL.")
    gen_ai_system: str = Field(
        min_length=1, description="Value for the record's gen_ai.system field."
    )
    transport: Literal["http", "grpc", "ws"] = "http"
    notes: str | None = None


class UrlRegistry(BaseModel):
    """Loaded URL registry. Iterable order = match priority (first wins)."""

    schema_version: str = "0.1.0"
    patterns: list[UrlRegistryEntry]

    def lookup(self, url: str) -> UrlRegistryEntry | None:
        for entry in self.patterns:
            if entry.match in url:
                return entry
        return None

    def is_known(self, url: str) -> bool:
        return self.lookup(url) is not None or self._env_ollama_match(url)

    def gen_ai_system(self, url: str) -> str:
        entry = self.lookup(url)
        if entry:
            return entry.gen_ai_system
        if self._env_ollama_match(url):
            return "ollama"
        return "unknown"

    @staticmethod
    def _env_ollama_match(url: str) -> bool:
        """Check OLLAMA_BASE_URL / OLLAMA_HOST at call time for non-default ports.

        langchain_ollama reads ``OLLAMA_BASE_URL``; the raw ollama SDK reads
        ``OLLAMA_HOST``.  Both are loaded by the workload via python-dotenv
        *after* sitecustomize installs the hook, so this check must run at
        HTTP-call time, not at hook-install time.
        """
        for env_var in ("OLLAMA_BASE_URL", "OLLAMA_HOST"):
            raw = os.environ.get(env_var, "").strip()
            if not raw:
                continue
            try:
                parsed = urlparse(raw if "://" in raw else f"http://{raw}")
                host = parsed.hostname or ""
                port = parsed.port
                fragment = f"{host}:{port}" if port else host
                if fragment and fragment in url:
                    return True
            except Exception:
                pass
        return False


class UrlRegistryError(RuntimeError):
    """Raised when an explicitly chosen registry file is malformed."""


def load_url_registry(*, _override_path: Path | str | None = None) -> UrlRegistry:
    """Load the active URL registry, applying the documented precedence.

    Parameters
    ----------
    _override_path:
        Test-only injection point. Production callers pass nothing; the
        function reads ``$NOVAFABRIC_URL_REGISTRY`` and ``~/.novafabric/``.
    """
    chosen = _resolve_path(_override_path)
    explicit = chosen is not None and chosen != _VENDORED_DEFAULT_PATH
    path = chosen or _VENDORED_DEFAULT_PATH
    try:
        return _load_from(path)
    except (yaml.YAMLError, ValidationError, OSError) as exc:
        if explicit:
            raise UrlRegistryError(
                f"URL registry at {path} is malformed: {exc}"
            ) from exc
        logger.warning(
            "novafabric: URL registry %s could not be loaded (%s); falling back to empty registry",
            path, exc,
        )
        return UrlRegistry(patterns=[])


def _resolve_path(_override_path: Path | str | None) -> Path | None:
    if _override_path is not None:
        return Path(_override_path)
    env_value = os.environ.get("NOVAFABRIC_URL_REGISTRY")
    if env_value:
        return Path(env_value)
    user_path = Path.home() / ".novafabric" / "url_registry.yaml"
    if user_path.is_file():
        return user_path
    return _VENDORED_DEFAULT_PATH


def _load_from(path: Path) -> UrlRegistry:
    raw: Any = yaml.safe_load(path.read_text())
    if raw is None:
        return UrlRegistry(patterns=[])
    return UrlRegistry.model_validate(raw)
