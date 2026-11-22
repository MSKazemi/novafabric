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

Dynamic AWS Bedrock detection, the same way: ``AWS_ENDPOINT_URL_BEDROCK_RUNTIME``
and ``AWS_ENDPOINT_URL_BEDROCK`` -- botocore's service-specific endpoint variables
-- are read at call time, and a request to exactly that scheme-less ``host:port``
is classified ``aws.bedrock`` (a VPC or local Bedrock-compatible endpoint). The
generic ``AWS_ENDPOINT_URL`` is not used: it applies to every AWS service.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from novafabric import _paths as _nf_paths

logger = logging.getLogger(__name__)

_VENDORED_DEFAULT_PATH = Path(__file__).with_name("url_registry.yaml")


class UrlRegistryEntry(BaseModel):
    """One row in the URL registry."""

    match: str = Field(
        min_length=1,
        description="Substring matched against the full URL, or a regex when match_kind=regex.",
    )
    #: ``substring`` (default, every pre-existing entry) matches anywhere in the URL, including
    #: its query string and path. ``regex`` is ``re.match`` (anchored at the start, case-
    #: insensitive) and is how an entry pins scheme and host, so a look-alike URL that merely
    #: *contains* the host cannot be classified.
    match_kind: Literal["substring", "regex"] = "substring"
    gen_ai_system: str = Field(
        min_length=1, description="Value for the record's gen_ai.system field."
    )
    transport: Literal["http", "grpc", "ws"] = "http"
    notes: str | None = None

    @model_validator(mode="after")
    def _regex_compiles(self) -> UrlRegistryEntry:
        if self.match_kind == "regex":
            try:
                re.compile(self.match)
            except re.error as exc:
                raise ValueError(f"match is not a valid regex: {exc}") from None
        return self

    def matches(self, url: str) -> bool:
        if self.match_kind == "regex":
            return re.match(self.match, url, re.IGNORECASE) is not None
        return self.match in url


class UrlRegistry(BaseModel):
    """Loaded URL registry. Iterable order = match priority (first wins)."""

    schema_version: str = "0.1.0"
    patterns: list[UrlRegistryEntry]

    def lookup(self, url: str) -> UrlRegistryEntry | None:
        for entry in self.patterns:
            if entry.matches(url):
                return entry
        return None

    def is_known(self, url: str) -> bool:
        return (
            self.lookup(url) is not None
            or self._env_ollama_match(url)
            or _env_bedrock_match(url)
        )

    def gen_ai_system(self, url: str) -> str:
        entry = self.lookup(url)
        if entry:
            return entry.gen_ai_system
        if self._env_ollama_match(url):
            return "ollama"
        if _env_bedrock_match(url):
            return "aws.bedrock"
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


#: botocore's service-specific endpoint variables for the Bedrock runtime and
#: control plane, read at call time.
BEDROCK_ENDPOINT_ENV_VARS: tuple[str, ...] = (
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
    "AWS_ENDPOINT_URL_BEDROCK",
)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _netloc(url: str) -> tuple[str, int | None] | None:
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = (parsed.hostname or "").lower()
        port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme)
    except ValueError:
        return None
    return (host, port) if host else None


def _env_bedrock_match(url: str) -> bool:
    """The request goes to the host:port an AWS Bedrock endpoint variable names."""
    target = None
    for env_var in BEDROCK_ENDPOINT_ENV_VARS:
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            continue
        if target is None:
            target = _netloc(url)
            if target is None:
                return False
        if _netloc(raw) == target:
            return True
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
    user_path = _nf_paths.nova_home() / "url_registry.yaml"
    if user_path.is_file():
        return user_path
    return _VENDORED_DEFAULT_PATH


def _load_from(path: Path) -> UrlRegistry:
    raw: Any = yaml.safe_load(path.read_text())
    if raw is None:
        return UrlRegistry(patterns=[])
    return UrlRegistry.model_validate(raw)
