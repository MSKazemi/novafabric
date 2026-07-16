"""LifecycleEvent record — the one `events.jsonl` line / webhook body (ADR-0137 D1/D2).

Mirrors ``schemas/lifecycle-event.schema.json`` (schema_version 0.1.0). The
record is append-only: events are facts, never mutated or deleted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from novafabric.capture._ulid import new_ulid

SCHEMA_VERSION: Final = "0.1.0"


class EventType(str, Enum):
    """Additive, versioned lifecycle-event taxonomy (ADR-0137 D2).

    Receivers MUST ignore unknown types, never reject them.
    """

    CAPSULE_CREATED = "capsule.created"
    CAPSULE_VALIDATED = "capsule.validated"
    PROMOTION_PROPOSED = "promotion.proposed"
    PROMOTION_APPROVED = "promotion.approved"
    PROMOTION_REJECTED = "promotion.rejected"
    POLICY_FAILED = "policy.failed"
    RETENTION_APPLIED = "retention.applied"
    PROMOTION_BYPASS_CREATED = "promotion.bypass.created"
    PROMOTION_BYPASS_APPROVED = "promotion.bypass.approved"
    PROMOTION_BYPASS_EXPIRED = "promotion.bypass.expired"


class SubjectKind(str, Enum):
    CAPSULE = "capsule"
    ASSET = "asset"
    PROMOTION = "promotion"
    POLICY = "policy"
    RETENTION = "retention"


class Subject(BaseModel):
    """The thing the event is about — refs and digests only, never content."""

    model_config = ConfigDict(extra="forbid")

    kind: SubjectKind
    ref: str = Field(min_length=1)
    digest: str | None = None


class Signature(BaseModel):
    """Optional detached signature over the canonical body (ADR-0137 D5)."""

    model_config = ConfigDict(extra="forbid")

    alg: Literal["hmac-sha256", "novaseal"]
    keyid: str
    value: str


def _now_rfc3339() -> str:
    """UTC, microsecond precision, RFC 3339 with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class LifecycleEvent(BaseModel):
    """One structured, non-sensitive lifecycle event record.

    ``payload`` and ``subject`` carry only refs, digests, enums, timestamps,
    and counts — never prompt/response text, secrets, tokens, or env values
    (payload hygiene is enforced at emission time; see ``emitter``).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    event_id: str = Field(default_factory=new_ulid)
    type: EventType
    subject: Subject
    occurred_at: str = Field(default_factory=_now_rfc3339)
    payload: dict[str, Any] = Field(default_factory=dict)
    signature: Signature | None = None
    source: str | None = None
    nova_version: str | None = Field(default=None, serialization_alias="nova.version")
    extensions: dict[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        """Serialize to the wire/log record shape (schema field names).

        Unset optional top-level fields are omitted (the schema is closed but
        all optional fields may be absent); ``subject.digest`` stays present
        as ``null`` — the schema requires the key.
        """
        record = self.model_dump(mode="json", by_alias=True)
        for key in ("signature", "source", "nova.version", "extensions"):
            if record.get(key) is None:
                record.pop(key, None)
        return record
