"""EventEnvelope v1 — canonical wire format for NovaFabric evidence events.

Spec: the private design/architecture/event-envelope-v1.md
Schema: schemas/event-envelope-v1/envelope-v1.json (SHA-256 pinned per §7)

Public API:
    EventEnvelope — Pydantic model for the envelope wire format
    EventType     — Enum of valid event_type values
    validate_event — JSON Schema validation against the canonical schema file
    EventEnvelopeValidationError — raised on schema validation failure
    to_cloudevents / from_cloudevents — CloudEvents v1.0 structured-mode interop
        (ADR-0081, gap-009)
"""

from novafabric.envelope.cloudevents import from_cloudevents, to_cloudevents
from novafabric.envelope.models import EventEnvelope, EventType
from novafabric.envelope.validator import EventEnvelopeValidationError, validate_event

__all__ = [
    "EventEnvelope",
    "EventType",
    "EventEnvelopeValidationError",
    "from_cloudevents",
    "to_cloudevents",
    "validate_event",
]
