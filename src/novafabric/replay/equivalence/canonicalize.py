# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Trajectory canonicalization — ADR-0144 D2/P1 (NF-128).

Normalizes a tool-call trajectory *before* any comparison, so that
differences which are not behavioral differences (argument formatting, an
idempotent retry, two independent calls in either order) do not read as
drift.

**Canonicalization is auditable.** Every rule is named, individually
switchable, and versioned; :class:`CanonicalizationResult` records which
rules were applied and what each one changed. A normalization step that
cannot be inspected is indistinguishable from hiding a real divergence, and
this module exists to make the normalization checkable rather than trusted.

**Nothing is assumed commutable.** Reordering is applied only to tool names
the caller explicitly declares independent. Inferring commutativity — for
example, treating two reads as freely orderable — would silently normalize
away genuine ordering bugs, which is the one failure this comparison exists
to catch.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Bumped when a rule's behavior changes, so a recorded result can be
#: re-derived. Recorded in CanonicalizationResult.rules_version.
RULES_VERSION = "1"

RULE_NORMALIZE_ARGUMENTS = "normalize_arguments"
RULE_COLLAPSE_IDEMPOTENT_RETRIES = "collapse_idempotent_retries"
RULE_REORDER_COMMUTABLE = "reorder_commutable"

#: Applied by default. Reordering is NOT here: it needs a declared
#: commutable set, and defaulting it on with an empty set would make the
#: rule look active in the audit record while doing nothing.
DEFAULT_RULES: frozenset[str] = frozenset(
    {RULE_NORMALIZE_ARGUMENTS, RULE_COLLAPSE_IDEMPOTENT_RETRIES}
)

ALL_RULES: frozenset[str] = DEFAULT_RULES | {RULE_REORDER_COMMUTABLE}


class UnknownRuleError(ValueError):
    """Raised when a caller requests a canonicalization rule that does not exist.

    Silently ignoring an unknown rule name would let a typo disable a
    normalization the caller believed was active, and the audit record would
    corroborate the mistake.
    """


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation in a trajectory."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    #: Optional; used only for reporting divergent steps back to the caller.
    index: int | None = None

    def key(self) -> str:
        """A stable comparison key: name plus canonically-encoded arguments."""
        return f"{self.name}({_encode(self.arguments)})"


def _encode(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class CanonicalizationResult:
    """A canonicalized trajectory plus the audit trail of how it got there."""

    calls: list[ToolCall]
    rules_version: str
    rules_applied: list[str]
    #: rule name → human-readable note about what it changed. A rule that was
    #: enabled but changed nothing is absent, so the record shows effect
    #: rather than intent.
    changes: dict[str, str] = field(default_factory=dict)


def _normalize_arguments(calls: Sequence[ToolCall]) -> list[ToolCall]:
    """Normalize argument *formatting* only — never argument values.

    Keys are ordered and scalars are left exactly as given. Coercing types
    here (``"1"`` → ``1``) would make a trajectory that passed a string where
    an int was expected compare equal to a correct one.
    """
    return [
        ToolCall(
            name=c.name,
            arguments=json.loads(_encode(c.arguments)),
            index=c.index,
        )
        for c in calls
    ]


def _collapse_idempotent_retries(
    calls: Sequence[ToolCall], idempotent: frozenset[str]
) -> list[ToolCall]:
    """Collapse *consecutive* identical calls to a declared-idempotent tool.

    Only consecutive and only identical: a repeat separated by other work is
    a second round trip, not a retry, and a repeat with different arguments
    is a different call. Only tools the caller declares idempotent are
    eligible — collapsing a non-idempotent repeat would erase a duplicated
    side effect, which is a real bug worth failing on.
    """
    out: list[ToolCall] = []
    for call in calls:
        if (
            out
            and call.name in idempotent
            and out[-1].name == call.name
            and out[-1].arguments == call.arguments
        ):
            continue
        out.append(call)
    return out


def _reorder_commutable(
    calls: Sequence[ToolCall], commutable: frozenset[str]
) -> list[ToolCall]:
    """Sort maximal runs of declared-commutable calls into a canonical order.

    A run ends at the first call outside the commutable set, so a
    non-commutable call acts as a barrier and never has work reordered
    across it.
    """
    out: list[ToolCall] = []
    run: list[ToolCall] = []
    for call in calls:
        if call.name in commutable:
            run.append(call)
            continue
        if run:
            out.extend(sorted(run, key=lambda c: c.key()))
            run = []
        out.append(call)
    if run:
        out.extend(sorted(run, key=lambda c: c.key()))
    return out


def canonicalize(
    calls: Iterable[ToolCall],
    *,
    rules: Iterable[str] | None = None,
    commutable: Iterable[str] = (),
    idempotent: Iterable[str] = (),
) -> CanonicalizationResult:
    """Canonicalize a trajectory and record how.

    Args:
        calls: the trajectory, in observed order.
        rules: rule names to apply; defaults to :data:`DEFAULT_RULES`.
        commutable: tool names whose relative order carries no meaning.
            Required for :data:`RULE_REORDER_COMMUTABLE` to do anything.
        idempotent: tool names for which a consecutive identical repeat is a
            retry rather than a second effect.

    Raises:
        UnknownRuleError: if ``rules`` names a rule that does not exist.
    """
    requested = frozenset(DEFAULT_RULES if rules is None else rules)
    unknown = requested - ALL_RULES
    if unknown:
        raise UnknownRuleError(
            f"unknown canonicalization rule(s): {sorted(unknown)}; "
            f"known rules are {sorted(ALL_RULES)}"
        )

    commutable_set = frozenset(commutable)
    idempotent_set = frozenset(idempotent)
    current = [
        ToolCall(name=c.name, arguments=dict(c.arguments), index=i)
        for i, c in enumerate(calls)
    ]
    changes: dict[str, str] = {}

    # Order matters: normalize formatting first so that retry collapsing and
    # reordering compare already-normalized arguments. Running them the other
    # way round would miss a retry that differed only in key order.
    if RULE_NORMALIZE_ARGUMENTS in requested:
        after = _normalize_arguments(current)
        if [c.key() for c in after] != [c.key() for c in current]:
            changes[RULE_NORMALIZE_ARGUMENTS] = "argument encoding normalized"
        current = after

    if RULE_COLLAPSE_IDEMPOTENT_RETRIES in requested:
        after = _collapse_idempotent_retries(current, idempotent_set)
        if len(after) != len(current):
            changes[RULE_COLLAPSE_IDEMPOTENT_RETRIES] = (
                f"collapsed {len(current) - len(after)} consecutive retry/retries"
            )
        current = after

    if RULE_REORDER_COMMUTABLE in requested:
        after = _reorder_commutable(current, commutable_set)
        if [c.key() for c in after] != [c.key() for c in current]:
            changes[RULE_REORDER_COMMUTABLE] = "commutable runs sorted"
        current = after

    return CanonicalizationResult(
        calls=current,
        rules_version=RULES_VERSION,
        rules_applied=sorted(requested),
        changes=changes,
    )
