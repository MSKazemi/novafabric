"""Human-agent accountability evidence (ADR-0150, experimental).

Record-only: NovaFabric records the human<->agent interaction. It does not
adjudicate a dispute, grant or deny any right, or assert that oversight was
adequate or lawful.

P1 (NF-181) is conversation-thread provenance only.
"""

from novafabric.hitl.conversation import (
    ConversationError,
    ConversationFacet,
    DuplicateTurnError,
    IdentityRefError,
    Turn,
    TurnContentError,
    TurnTimeError,
    attach_facet,
    broken_parent_refs,
    build_facet,
    dangling_turn_refs,
    digest_turn,
    facet_from_capsule,
    resolve_turn,
    turn,
    verify_turn_binding,
)

__all__ = [
    "ConversationError",
    "ConversationFacet",
    "DuplicateTurnError",
    "IdentityRefError",
    "Turn",
    "TurnContentError",
    "TurnTimeError",
    "attach_facet",
    "broken_parent_refs",
    "build_facet",
    "dangling_turn_refs",
    "digest_turn",
    "facet_from_capsule",
    "resolve_turn",
    "turn",
    "verify_turn_binding",
]
