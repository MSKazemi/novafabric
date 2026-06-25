"""Tests for the externalized URL registry (RFC-0001 Option C, v0.5.x C-1).

The registry replaces the historical hardcoded ``_AI_URL_PATTERNS`` list in
the httpx hook. Wire-level hooks consume it; users override it via
``$NOVAFABRIC_URL_REGISTRY`` or ``~/.novafabric/url_registry.yaml``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.capture.hooks._url_registry import (
    UrlRegistry,
    UrlRegistryError,
    load_url_registry,
)


def test_vendored_default_loads_cleanly() -> None:
    registry = load_url_registry()
    assert isinstance(registry, UrlRegistry)
    assert len(registry.patterns) >= 1


def test_vendored_default_contains_historical_providers() -> None:
    registry = load_url_registry()
    expected = {
        "openai", "anthropic", "cohere", "together", "mistral", "replicate",
    }
    actual = {entry.gen_ai_system for entry in registry.patterns}
    missing = expected - actual
    assert not missing, f"missing historical providers in default registry: {missing}"


def test_lookup_known_url_returns_entry() -> None:
    registry = load_url_registry()
    entry = registry.lookup("https://api.openai.com/v1/chat/completions")
    assert entry is not None
    assert entry.gen_ai_system == "openai"


def test_lookup_unknown_url_returns_none() -> None:
    registry = load_url_registry()
    assert registry.lookup("https://example.com/api") is None
    assert registry.is_known("https://example.com/api") is False
    assert registry.gen_ai_system("https://example.com/api") == "unknown"


def test_bedrock_runtime_url_classified_as_aws_bedrock() -> None:
    """C-3.3a: Bedrock runtime per-region URLs must be in the vendored
    registry so the urllib3 hook captures boto3 calls without override."""
    registry = load_url_registry()
    url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/x/invoke"
    assert registry.is_known(url)
    assert registry.gen_ai_system(url) == "aws.bedrock"

    # Other regions match too (the registry uses hostname prefix).
    assert registry.is_known(
        "https://bedrock-runtime.eu-central-1.amazonaws.com/model/x/invoke"
    )
    assert registry.is_known(
        "https://bedrock-runtime.ap-southeast-2.amazonaws.com/model/x/invoke"
    )


def test_ollama_default_port_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama on localhost:11434 (default port) must be captured out of the box."""
    monkeypatch.delenv("NOVAFABRIC_URL_REGISTRY", raising=False)
    registry = load_url_registry()
    for url in [
        "http://localhost:11434/v1/chat/completions",
        "http://127.0.0.1:11434/v1/chat/completions",
        "http://localhost:11434/api/chat",
    ]:
        assert registry.is_known(url), f"Ollama URL not in registry: {url}"
        assert registry.gen_ai_system(url) == "ollama", url


def test_explicit_path_override_takes_precedence(tmp_path: Path) -> None:
    override = tmp_path / "override.yaml"
    override.write_text(
        """
schema_version: "0.1.0"
patterns:
  - match: "internal.example.com/llm"
    gen_ai_system: "internal-prod"
    transport: "http"
"""
    )
    registry = load_url_registry(_override_path=override)
    assert registry.gen_ai_system("https://internal.example.com/llm/chat") == "internal-prod"
    assert registry.lookup("https://api.openai.com/v1/chat") is None


def test_env_var_override_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "from_env.yaml"
    override.write_text(
        """
schema_version: "0.1.0"
patterns:
  - match: "from-env.example.com"
    gen_ai_system: "env-only"
"""
    )
    monkeypatch.setenv("NOVAFABRIC_URL_REGISTRY", str(override))
    registry = load_url_registry()
    assert registry.gen_ai_system("https://from-env.example.com/x") == "env-only"


def test_malformed_explicit_override_raises_named_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: valid: registry: data: at: all\n")
    with pytest.raises(UrlRegistryError):
        load_url_registry(_override_path=bad)


def test_missing_env_override_falls_back_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An env var pointing at a non-existent file should not crash; we resolve
    to that path, the loader fails to read it, and (because it is explicit)
    raises UrlRegistryError. This documents the explicit-vs-implicit
    distinction: env-var = explicit = errors are loud."""
    monkeypatch.setenv("NOVAFABRIC_URL_REGISTRY", "/nonexistent/path/registry.yaml")
    with pytest.raises(UrlRegistryError):
        load_url_registry()


def test_first_match_wins() -> None:
    registry = UrlRegistry(
        schema_version="0.1.0",
        patterns=[
            {"match": "api.openai.com", "gen_ai_system": "openai-first"},  # type: ignore[list-item]
            {"match": "api.openai.com", "gen_ai_system": "openai-second"},  # type: ignore[list-item]
        ],
    )
    assert registry.gen_ai_system("https://api.openai.com/v1/chat") == "openai-first"
