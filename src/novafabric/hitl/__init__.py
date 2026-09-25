"""Human-agent accountability evidence (ADR-0150, experimental).

Record-only: NovaFabric records the human<->agent interaction. It does not
adjudicate a dispute, grant or deny any right, or assert that oversight was
adequate or lawful.

P1 (NF-181) is conversation-thread provenance. P2 adds the turn-anchored
decision-context receipt (NF-182), human-override provenance (NF-187) and the
agent's surfaced rationale (NF-188), stored as lists inside
``facets.conversation``.
"""

from novafabric.hitl._records import (
    AccountabilityRecordError,
    RecordContentError,
    RecordDefect,
    RecordOutcome,
)
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
from novafabric.hitl.decision_context import (
    ContextVerification,
    DecisionContextReceipt,
    ReceiptVerdict,
    ShownItem,
    build_decision_context,
    compute_context_root,
    receipts_for_turn,
    record_decision_context,
    shown_item,
    verify_decision_contexts,
)
from novafabric.hitl.override import (
    NoOpOverrideError,
    OverrideRecord,
    load_overrides,
    record_override,
)
from novafabric.hitl.rationale import (
    RationaleRecord,
    load_rationales,
    record_rationale,
)

__all__ = [
    "AccountabilityRecordError",
    "ContextVerification",
    "ConversationError",
    "ConversationFacet",
    "DecisionContextReceipt",
    "DuplicateTurnError",
    "IdentityRefError",
    "NoOpOverrideError",
    "OverrideRecord",
    "RationaleRecord",
    "ReceiptVerdict",
    "RecordContentError",
    "RecordDefect",
    "RecordOutcome",
    "ShownItem",
    "Turn",
    "TurnContentError",
    "TurnTimeError",
    "attach_facet",
    "broken_parent_refs",
    "build_decision_context",
    "build_facet",
    "compute_context_root",
    "dangling_turn_refs",
    "digest_turn",
    "facet_from_capsule",
    "load_overrides",
    "load_rationales",
    "receipts_for_turn",
    "record_decision_context",
    "record_override",
    "record_rationale",
    "resolve_turn",
    "shown_item",
    "turn",
    "verify_decision_contexts",
    "verify_turn_binding",
]
