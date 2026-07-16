"""Shared fixtures for the offline metrics query DSL tests (ADR-0129)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

_SHA = "sha256:" + "a" * 64


def make_model_call(
    *,
    model: str = "claude-3-7-sonnet",
    cost: float | None = 0.01,
    prompt_tokens: int | None = 100,
    completion_tokens: int | None = 50,
    duration_ms: int | None = 800,
    log_level: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "duration_ms": duration_ms,
        "gen_ai.usage.input_tokens": prompt_tokens,
        "gen_ai.usage.output_tokens": completion_tokens,
    }
    if cost is not None:
        record["nova.cost"] = {"currency": "USD", "amount": cost}
    if log_level is not None:
        record["log_level"] = log_level
    return record


def make_score(name: str, value: float | bool | str, value_type: str = "numeric") -> dict[str, Any]:
    # ULIDs are 26 chars of Crockford base32.
    return {
        "score_id": "01HZZZZZZZZZZZZZZZZZZZZZZZ",
        "subject": _SHA,
        "subject_kind": "capsule",
        "name": name,
        "value": value,
        "value_type": value_type,
        "source": "code",
        "evaluator_id": "test-evaluator",
        "eval_card_digest": _SHA,
    }


@pytest.fixture
def model_call() -> Callable[..., dict[str, Any]]:
    """Factory fixture for model-call records."""
    return make_model_call


@pytest.fixture
def score() -> Callable[..., dict[str, Any]]:
    """Factory fixture for scores.jsonl records."""
    return make_score


@pytest.fixture
def make_capsule(tmp_path: Path) -> Callable[..., Path]:
    """Write a minimal on-disk Run Capsule and return its directory."""

    def _make(
        run_id: str,
        *,
        created_at: str = "2026-07-10T12:00:00Z",
        status: str = "success",
        metadata: dict[str, str] | None = None,
        manifest_extra: dict[str, Any] | None = None,
        model_calls: list[dict[str, Any]] | None = None,
        scores: list[dict[str, Any]] | None = None,
        manifest_format: str = "yaml",
    ) -> Path:
        capsule = tmp_path / run_id
        capsule.mkdir()
        manifest: dict[str, Any] = {
            "schema_version": "0.1.0",
            "run_id": run_id,
            "created_at": created_at,
            "status": status,
        }
        if metadata:
            manifest["metadata"] = metadata
        if manifest_extra:
            manifest.update(manifest_extra)
        if manifest_format == "yaml":
            (capsule / "capsule.yaml").write_text(yaml.dump(manifest))
        else:
            (capsule / "capsule.json").write_text(json.dumps(manifest))
        if model_calls is not None:
            (capsule / "model-calls.jsonl").write_text(
                "\n".join(json.dumps(c) for c in model_calls) + "\n" if model_calls else ""
            )
        if scores is not None:
            (capsule / "scores.jsonl").write_text(
                "\n".join(json.dumps(s) for s in scores) + "\n" if scores else ""
            )
        return capsule

    return _make


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    """The directory make_capsule writes into (may be empty)."""
    return tmp_path
