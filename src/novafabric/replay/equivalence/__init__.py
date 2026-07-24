"""Behavioral-equivalence replay (ADR-0144, experimental).

P1 (NF-128, NF-122): versioned trajectory canonicalization and
set/ordered/edit trajectory matching with divergent-step detection.

Goal-completion equivalence (D1/NF-121) and the composite drift score
(D3/NF-125) are **planned**, not implemented.
"""

from novafabric.replay.equivalence.canonicalize import (
    ALL_RULES,
    DEFAULT_RULES,
    RULE_COLLAPSE_IDEMPOTENT_RETRIES,
    RULE_NORMALIZE_ARGUMENTS,
    RULE_REORDER_COMMUTABLE,
    RULES_VERSION,
    CanonicalizationResult,
    ToolCall,
    UnknownRuleError,
    canonicalize,
)
from novafabric.replay.equivalence.trajectory import (
    DivergenceKind,
    DivergentStep,
    MatchMode,
    TrajectoryComparison,
    compare,
)

__all__ = [
    "ALL_RULES",
    "DEFAULT_RULES",
    "RULES_VERSION",
    "RULE_COLLAPSE_IDEMPOTENT_RETRIES",
    "RULE_NORMALIZE_ARGUMENTS",
    "RULE_REORDER_COMMUTABLE",
    "CanonicalizationResult",
    "DivergenceKind",
    "DivergentStep",
    "MatchMode",
    "ToolCall",
    "TrajectoryComparison",
    "UnknownRuleError",
    "canonicalize",
    "compare",
]
