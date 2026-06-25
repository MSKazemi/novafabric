"""Pydantic model for EventEnvelope v1 — canonical wire format for NovaFabric evidence events."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_PAYLOAD_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_AGENT_ID_RE = re.compile(r"^[^\s\x00-\x1F]+$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_ALL_ZEROS_TRACE = "0" * 32
_ALL_ZEROS_SPAN = "0" * 16


class EventType(str, Enum):
    """Canonical event_type values for EventEnvelope v1."""

    run_start = "run.start"
    run_end = "run.end"
    model_call = "model_call"
    tool_call = "tool_call"
    span = "span"
    capsule_finalize = "capsule.finalize"


class EventEnvelope(BaseModel):
    """EventEnvelope v1 — canonical evidence event for NovaFabric agents and collectors.

    Spec: design/architecture/event-envelope-v1.md
    Schema: schemas/event-envelope-v1/envelope-v1.json
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )

    envelope_version: str
    schema_version: str
    event_id: str
    global_run_id: str
    run_id: str
    parent_run_id: str | None
    event_type: EventType
    trace_id: str
    span_id: str
    agent_id: str
    cluster_id: str | None
    tenant_id: str | None
    started_at: str
    emitter_node_id: str | None = None
    payload: dict[str, Any] | None = None
    payload_hash: str | None = None
    nova_batch_signature: str | None = Field(None, alias="nova.batch.signature")
    nova_batch_signing_key_id: str | None = Field(None, alias="nova.batch.signing_key_id")

    @field_validator("envelope_version")
    @classmethod
    def _check_envelope_version(cls, v: str) -> str:
        if v != "1":
            raise ValueError(f"envelope_version must be '1', got {v!r}")
        return v

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if v != "event-envelope/1":
            raise ValueError(f"schema_version must be 'event-envelope/1', got {v!r}")
        return v

    @field_validator("event_id", "run_id")
    @classmethod
    def _check_ulid(cls, v: str) -> str:
        if not _ULID_RE.match(v):
            raise ValueError(f"must be a 26-char Crockford Base32 ULID, got {v!r}")
        return v

    @field_validator("global_run_id")
    @classmethod
    def _check_run_id(cls, v: str) -> str:
        if not (_ULID_RE.match(v) or _UUID_V7_RE.match(v)):
            raise ValueError(
                f"global_run_id must be a 26-char ULID or 36-char UUID v7, got {v!r}"
            )
        return v

    @field_validator("parent_run_id")
    @classmethod
    def _check_parent_run_id(cls, v: str | None) -> str | None:
        if v is not None and not _ULID_RE.match(v):
            raise ValueError(
                f"parent_run_id must be a 26-char ULID or null, got {v!r}"
            )
        return v

    @field_validator("trace_id")
    @classmethod
    def _check_trace_id(cls, v: str) -> str:
        if not _TRACE_ID_RE.match(v):
            raise ValueError(f"trace_id must be 32 lowercase hex chars, got {v!r}")
        if v == _ALL_ZEROS_TRACE:
            raise ValueError("trace_id must not be all-zeros (W3C Trace Context)")
        return v

    @field_validator("span_id")
    @classmethod
    def _check_span_id(cls, v: str) -> str:
        if not _SPAN_ID_RE.match(v):
            raise ValueError(f"span_id must be 16 lowercase hex chars, got {v!r}")
        if v == _ALL_ZEROS_SPAN:
            raise ValueError("span_id must not be all-zeros (W3C Trace Context)")
        return v

    @field_validator("agent_id")
    @classmethod
    def _check_agent_id(cls, v: str) -> str:
        if not v or not _AGENT_ID_RE.match(v):
            raise ValueError(
                f"agent_id must be non-empty with no whitespace or control chars, got {v!r}"
            )
        return v

    @field_validator("cluster_id", "tenant_id")
    @classmethod
    def _check_nullable_nonempty(cls, v: str | None) -> str | None:
        if v is not None and len(v) == 0:
            raise ValueError("must be non-empty string or null")
        return v

    @field_validator("payload_hash")
    @classmethod
    def _check_payload_hash(cls, v: str | None) -> str | None:
        if v is not None and not _PAYLOAD_HASH_RE.match(v):
            raise ValueError(
                f"payload_hash must match 'sha256:<64 hex chars>', got {v!r}"
            )
        return v

    @field_validator("nova_batch_signature")
    @classmethod
    def _check_batch_signature(cls, v: str | None) -> str | None:
        if v is not None and len(v) != 88:
            raise ValueError(
                f"nova.batch.signature must be 88 base64 chars, got length {len(v)}"
            )
        return v

    @field_validator("nova_batch_signing_key_id")
    @classmethod
    def _check_signing_key_id(cls, v: str | None) -> str | None:
        if v is not None and not _UUID_RE.match(v):
            raise ValueError(
                f"nova.batch.signing_key_id must be a UUID, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _check_batch_signature_pair(self) -> "EventEnvelope":
        sig = self.nova_batch_signature
        kid = self.nova_batch_signing_key_id
        if (sig is None) != (kid is None):
            raise ValueError(
                "nova.batch.signature and nova.batch.signing_key_id"
                " must both be set or both be null"
            )
        return self

    def to_wire(self) -> dict[str, Any]:
        """Serialize to wire-format dict (uses dot-notation aliases for batch fields)."""
        data = self.model_dump(by_alias=True, exclude_none=False)
        return data
