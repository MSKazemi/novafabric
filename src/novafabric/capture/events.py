"""Pydantic models for the three new capsule event JSONL streams.

These models are written to:
- ``file_events.jsonl``    — file I/O operations performed by the agent
- ``network_events.jsonl`` — external network egress captured by wire-level hooks
- ``human_approvals.jsonl`` — structured human approval records from maker-checker

Design principle: all models are additive and optional-first so that they can
be extended without breaking existing capsules (ADR-0039 / additive schema rule).
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class FileEvent(BaseModel):
    """One file I/O operation performed by the agent during a run."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capsule_id: str
    timestamp_utc: str  # ISO 8601
    operation: Literal["read", "write", "append", "delete", "mkdir", "listdir", "stat"]
    path: str  # absolute path (PII: may be redacted)
    size_bytes: int | None = None
    success: bool = True
    error: str | None = None
    agent_id: str | None = None


class NetworkEvent(BaseModel):
    """One external network egress event captured from wire-level hooks."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capsule_id: str
    timestamp_utc: str
    method: str  # GET, POST, etc.
    url: str  # redacted if NOVA_REDACT_URLS=true
    host: str
    port: int | None = None
    status_code: int | None = None
    response_size_bytes: int | None = None
    duration_ms: float | None = None
    library: str  # "requests", "httpx", "aiohttp", "urllib3"
    is_ai_api: bool = False  # True if host matches known AI API endpoints


class HumanApprovalEvent(BaseModel):
    """A structured human approval record from the maker-checker workflow."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capsule_id: str
    timestamp_utc: str
    approver_id: str  # identity of the human approver
    action: Literal["proposed", "approved", "rejected", "bypassed"]
    target_run_id: str  # run being approved
    rationale: str | None = None
    policy_version: str | None = None
    seal_bundle_path: str | None = None  # path to NovaSeal bundle if signed


# ---------------------------------------------------------------------------
# Extended span taxonomy (gap-011, ADR-0082)
#
# Eight new ``CapsuleEventType`` members are backed by the six event models
# below. Each is additive and optional-first, following the same construction
# pattern as the models above. Per ADR-0021 and src-210, identifiers / digests /
# scores / counts are captured by default (bounded, non-sensitive), while raw
# *content* payloads are opt-in (default ``None``) so per-event size stays
# bounded and no sensitive material is logged unless the operator enables it.
# ---------------------------------------------------------------------------


class StateTransitionEvent(BaseModel):
    """Working-memory state before/after a single agent step (src-109).

    Digests are always present (content-addressed, bounded); the raw
    ``state_before``/``state_after`` payloads are opt-in (ADR-0021).
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capsule_id: str
    timestamp_utc: str  # ISO 8601
    step_index: int
    state_digest_before: str  # e.g. "sha256:…"
    state_digest_after: str
    agent_id: str | None = None
    # Opt-in content (ADR-0021): populated only when content capture is enabled.
    state_before: dict[str, Any] | None = None
    state_after: dict[str, Any] | None = None


class MemoryOperationEvent(BaseModel):
    """A memory read/write/update/delete with relevance/freshness (src-109)."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capsule_id: str
    timestamp_utc: str
    operation: Literal["read", "write", "update", "delete"]
    memory_key: str
    relevance_score: float | None = None
    freshness_seconds: float | None = None
    agent_id: str | None = None
    # Opt-in content (ADR-0021).
    value: Any | None = None


class GuardrailEvent(BaseModel):
    """A guardrail check that fired during a run (src-203, OpenInference)."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capsule_id: str
    timestamp_utc: str
    guardrail_name: str
    outcome: Literal["passed", "blocked", "modified", "error"]
    category: str | None = None
    score: float | None = None
    agent_id: str | None = None
    # Opt-in content (ADR-0021).
    details: dict[str, Any] | None = None


class EvaluatorEvent(BaseModel):
    """An in-trace evaluator/scorer result (src-203, OpenInference)."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capsule_id: str
    timestamp_utc: str
    evaluator_name: str
    score: float | None = None
    label: str | None = None
    passed: bool | None = None
    dataset_id: str | None = None
    agent_id: str | None = None
    # Opt-in content (ADR-0021).
    rationale: str | None = None


class RerankedDocument(BaseModel):
    """One document in a reranker result (bounded; content opt-in)."""

    document_id: str
    score: float | None = None
    rank: int | None = None
    # Opt-in content (ADR-0021).
    content: str | None = None


class RerankerEvent(BaseModel):
    """A reranker that reordered retrieved documents (src-203)."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capsule_id: str
    timestamp_utc: str
    reranker_model: str
    input_count: int | None = None
    output_count: int | None = None
    documents: list[RerankedDocument] = Field(default_factory=list)
    agent_id: str | None = None


class RetrievedDocument(BaseModel):
    """One document returned by a vector-DB query (bounded; content opt-in)."""

    document_id: str
    score: float | None = None
    # Opt-in content (ADR-0021).
    content: str | None = None


class VectorRetrievalEvent(BaseModel):
    """A vector-DB retrieval span (src-113, OpenLLMetry).

    Covers Pinecone/Qdrant/Chroma/Weaviate and similar stores. Document
    identifiers and scores are bounded and captured by default; document
    ``content`` is opt-in (ADR-0021).
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    capsule_id: str
    timestamp_utc: str
    vector_store: str  # "pinecone" | "qdrant" | "chroma" | "weaviate" | …
    operation: Literal["query", "upsert", "delete"] = "query"
    collection: str | None = None
    top_k: int | None = None
    returned_count: int | None = None
    duration_ms: float | None = None
    status: Literal["ok", "error"] = "ok"
    error: str | None = None
    documents: list[RetrievedDocument] = Field(default_factory=list)
    agent_id: str | None = None
