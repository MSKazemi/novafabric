"""Multi-agent A2A message & handoff evidence (ADR-0142, experimental).

Record-only: NovaFabric records the wire between agents. It does not
orchestrate, route, or adjudicate them.
"""

from novafabric.a2a.messages import (
    A2A_VERSION,
    A2AEvidenceError,
    A2AMessageEnvelope,
    A2AMessagesFacet,
    CardBindingError,
    DuplicateMessageIdError,
    EmptyMessagePartsError,
    MessagePart,
    attach_facet,
    build_facet,
    card_fingerprint,
    card_status,
    content_hash_for_parts,
    envelope_from_parts,
    verify_card_binding,
    verify_content_binding,
)

__all__ = [
    "A2A_VERSION",
    "A2AEvidenceError",
    "A2AMessageEnvelope",
    "A2AMessagesFacet",
    "CardBindingError",
    "DuplicateMessageIdError",
    "EmptyMessagePartsError",
    "MessagePart",
    "attach_facet",
    "build_facet",
    "card_fingerprint",
    "card_status",
    "content_hash_for_parts",
    "envelope_from_parts",
    "verify_card_binding",
    "verify_content_binding",
]
