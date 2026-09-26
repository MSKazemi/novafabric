"""Human-agent accountability evidence (ADR-0150, experimental).

Record-only: NovaFabric records the human<->agent interaction. It does not
adjudicate a dispute, grant or deny any right, or assert that oversight was
adequate or lawful.

P1 (NF-181) is conversation-thread provenance. P2 adds the turn-anchored
decision-context receipt (NF-182), human-override provenance (NF-187) and the
agent's surfaced rationale (NF-188), stored as lists inside
``facets.conversation``. P3 adds the ISO/IEC TS 27560-shaped consent receipt
(NF-183), the signed accountability handoff (NF-189) and the acted-on-behalf
binding to an NF-084 delegation hop, by reference only (NF-186).
"""

from novafabric.hitl._records import (
    AccountabilityRecordError,
    RecordContentError,
    RecordDefect,
    RecordOutcome,
)
from novafabric.hitl.acted_as import (
    ActedAsView,
    ActedOnBehalfRecord,
    HopResolution,
    bindings_for_turn,
    load_acted_on_behalf,
    record_acted_on_behalf,
    resolve_hop,
)
from novafabric.hitl.consent import (
    ConsentReceipt,
    ConsentReceiptError,
    ConsentVerdict,
    ConsentVerification,
    build_consent_receipt,
    compute_receipt_digest,
    load_consents,
    record_consent,
    verify_consents,
    withdraw_consent,
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
from novafabric.hitl.handoff import (
    HandoffRecord,
    HandoffSignatureError,
    SelfHandoffError,
    SignatureCheck,
    check_handoff_signature,
    load_handoffs,
    record_handoff,
    sign_handoff,
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
    "ActedAsView",
    "ActedOnBehalfRecord",
    "ConsentReceipt",
    "ConsentReceiptError",
    "ConsentVerdict",
    "ConsentVerification",
    "ContextVerification",
    "ConversationError",
    "ConversationFacet",
    "DecisionContextReceipt",
    "DuplicateTurnError",
    "HandoffRecord",
    "HandoffSignatureError",
    "HopResolution",
    "IdentityRefError",
    "NoOpOverrideError",
    "OverrideRecord",
    "RationaleRecord",
    "ReceiptVerdict",
    "RecordContentError",
    "RecordDefect",
    "RecordOutcome",
    "SelfHandoffError",
    "ShownItem",
    "SignatureCheck",
    "Turn",
    "TurnContentError",
    "TurnTimeError",
    "attach_facet",
    "bindings_for_turn",
    "broken_parent_refs",
    "build_consent_receipt",
    "build_decision_context",
    "build_facet",
    "check_handoff_signature",
    "compute_context_root",
    "compute_receipt_digest",
    "dangling_turn_refs",
    "digest_turn",
    "facet_from_capsule",
    "load_acted_on_behalf",
    "load_consents",
    "load_handoffs",
    "load_overrides",
    "load_rationales",
    "receipts_for_turn",
    "record_acted_on_behalf",
    "record_consent",
    "record_decision_context",
    "record_handoff",
    "record_override",
    "record_rationale",
    "resolve_hop",
    "resolve_turn",
    "shown_item",
    "sign_handoff",
    "turn",
    "verify_consents",
    "verify_decision_contexts",
    "verify_turn_binding",
    "withdraw_consent",
]
