"""Shared fixtures for the saved-views tests (ADR-0130)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml


def make_model_call(*, model: str = "m1", cost: float | None = 0.01) -> dict[str, Any]:
    record: dict[str, Any] = {
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "duration_ms": 800,
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 50,
    }
    if cost is not None:
        record["nova.cost"] = {"currency": "USD", "amount": cost}
    return record


@pytest.fixture
def model_call() -> Callable[..., dict[str, Any]]:
    return make_model_call


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    path = tmp_path / "capsules"
    path.mkdir()
    return path


@pytest.fixture
def views_dir(tmp_path: Path) -> Path:
    """A views directory path (not pre-created — creation is lazy)."""
    return tmp_path / "views"


@pytest.fixture
def make_capsule(capsule_dir: Path) -> Callable[..., Path]:
    """Write a minimal on-disk Run Capsule and return its directory."""

    def _make(
        run_id: str,
        *,
        created_at: str = "2026-07-10T12:00:00Z",
        status: str = "success",
        metadata: dict[str, str] | None = None,
        model_calls: list[dict[str, Any]] | None = None,
    ) -> Path:
        capsule = capsule_dir / run_id
        capsule.mkdir()
        manifest: dict[str, Any] = {
            "schema_version": "0.1.0",
            "run_id": run_id,
            "created_at": created_at,
            "status": status,
        }
        if metadata:
            manifest["metadata"] = metadata
        (capsule / "capsule.yaml").write_text(yaml.dump(manifest))
        if model_calls is not None:
            (capsule / "model-calls.jsonl").write_text(
                "\n".join(json.dumps(c) for c in model_calls) + "\n" if model_calls else ""
            )
        return capsule

    return _make
