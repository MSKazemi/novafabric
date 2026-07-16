"""Shared capsule-factory fixtures for the agent execution-graph tests (ADR-0124)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import pytest

CAPSULE_RUN_ID = "01TESTRUN00000000000000000"


def model_call(
    call_id: str,
    parent_span_id: str | None,
    *,
    model: str = "claude-sonnet-4-7",
    started_at: str | None = "2026-05-07T10:23:00.100Z",
    duration_ms: int | None = 900,
    status: str | None = "success",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "model_call_id": call_id,
        "parent_span_id": parent_span_id,
        "gen_ai.request.model": model,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "status": status,
        **extra,
    }


def tool_call(
    call_id: str,
    parent_span_id: str | None,
    agent_call_id: str | None,
    *,
    tool_name: str = "web_search",
    started_at: str | None = "2026-05-07T10:23:00.234Z",
    duration_ms: int | None = 178,
    status: str | None = "success",
    mutation_class: str | None = "read-only",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "tool_call_id": call_id,
        "parent_span_id": parent_span_id,
        "agent_call_id": agent_call_id,
        "tool_name": tool_name,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "status": status,
        "mutation_class": mutation_class,
        **extra,
    }


def span(
    span_id: str,
    parent_span_id: str | None,
    *,
    name: str = "agent.turn",
    started_at: str | None = "2026-05-07T10:23:00.050Z",
    duration_ms: int | None = 1100,
    status: str | None = "ok",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "status": status,
        **extra,
    }


class CapsuleFactory(Protocol):
    def __call__(
        self,
        *,
        model_calls: list[dict[str, Any]] | None = ...,
        tool_calls: list[dict[str, Any]] | None = ...,
        spans: list[dict[str, Any]] | None = ...,
        manifest: dict[str, Any] | None = ...,
        raw_lines: dict[str, list[str]] | None = ...,
        name: str = ...,
    ) -> Path: ...


@pytest.fixture()
def make_capsule(tmp_path: Path) -> CapsuleFactory:
    """Write a minimal on-disk Run Capsule directory for reconstruction."""

    def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
        path.write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records),
            encoding="utf-8",
        )

    def _make(
        *,
        model_calls: list[dict[str, Any]] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        spans: list[dict[str, Any]] | None = None,
        manifest: dict[str, Any] | None = None,
        raw_lines: dict[str, list[str]] | None = None,
        name: str = "capsule",
    ) -> Path:
        capsule_dir = tmp_path / name
        capsule_dir.mkdir()
        if manifest is None:
            manifest = {"run_id": CAPSULE_RUN_ID}
        (capsule_dir / "capsule.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        _write_jsonl(capsule_dir / "model-calls.jsonl", model_calls or [])
        _write_jsonl(capsule_dir / "tool-calls.jsonl", tool_calls or [])
        _write_jsonl(capsule_dir / "trace.jsonl", spans or [])
        for filename, lines in (raw_lines or {}).items():
            (capsule_dir / filename).write_text(
                "".join(line + "\n" for line in lines), encoding="utf-8"
            )
        return capsule_dir

    return _make
