"""Typed models for the NovaFabric Python client (ADR-0202 D4).

Response models are Pydantic v2 with ``extra="allow"`` so additive server
fields never break an older client. Field sets mirror ``api/openapi.yaml``
and the real route handlers (``server/routes/{capsules,assets}.py``).
``ApiResult``/``ResponseMeta`` are frozen dataclasses per the companion spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


@dataclass(frozen=True)
class ResponseMeta:
    """Per-response metadata surfaced alongside every decoded body."""

    status: int
    deprecation: str | None = None
    sunset: str | None = None
    quota_warning: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class ApiResult(Generic[T]):
    """Decoded body plus response metadata."""

    data: T
    meta: ResponseMeta


class _Model(BaseModel):
    """Base for wire models: additive server fields never raise."""

    model_config = ConfigDict(extra="allow")


class ServerHealth(_Model):
    """``GET /health`` body (root-mounted, unauthenticated)."""

    ok: bool | None = None
    service: str | None = None
    version: str | None = None
    backend: str | None = None


class CapsuleSummary(_Model):
    """One row of ``GET /v0/capsules`` (and the upload 201 body)."""

    run_id: str | None = None
    status: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    duration_ms: float | None = None
    command: str | list[str] | None = None
    exit_code: int | None = None


class CapsuleDetail(CapsuleSummary):
    """``GET /v0/capsules/{run_id}`` body."""

    schema_version: str | None = None
    novafabric_version: str | None = None
    model_call_count: int | None = None
    tool_call_count: int | None = None
    mutating_tool_count: int | None = None
    capture_mode: str | None = None


class AssetSummary(_Model):
    """One row of ``GET /v0/assets``."""

    id: str | None = None
    name: str | None = None
    version: str | None = None
    asset_type: str | None = None
    status: str | None = None
    created_at: str | None = None
    promoted_at: str | None = None
    git_commit_sha: str | None = None


class AssetDetail(AssetSummary):
    """``GET /v0/assets/{id}`` body."""

    spec_json: str | None = None
    promoted_by: str | None = None
    forced_promotion: bool | None = None


class Page(_Model, Generic[T]):
    """One page of a cursor-paginated listing: ``{items, next_cursor, total}``.

    ``next_cursor`` is an **opaque** string — the client never decodes it.
    """

    items: list[T]
    next_cursor: str | None = None
    total: int | None = None


class ScoreSubmission(_Model):
    """``POST /v0/capsules/{run_id}/scores`` request body (ADR-0119).

    Mirrors the server's ``ScoreSubmissionRequest``. ``None`` fields are
    omitted on the wire (the server rejects unknown keys, so extras here are
    a caller decision).
    """

    name: str
    value: bool | float | str
    value_type: str
    source: str
    evaluator_id: str
    subject: str
    subject_kind: str = "span"
    eval_card_digest: str
    score_id: str | None = None
    supersedes: str | None = None
    run_id: str | None = None
    significance: dict[str, Any] | None = None
    created_at: str | None = None


class ScoreSubmissionResult(_Model):
    """``201 Created`` body of a score submission (a ``200`` replay has none)."""

    score: dict[str, Any]
    idempotent_replay: bool | None = None
    config_bound: bool | None = None
    submission: dict[str, Any] | None = None
