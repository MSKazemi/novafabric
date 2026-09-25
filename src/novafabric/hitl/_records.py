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

"""Shared plumbing for turn-anchored accountability records — ADR-0150 P2.

The decision-context receipt (NF-182), override (NF-187) and rationale (NF-188)
records share four concerns, kept here so each is written once:

- **Field validation** that encodes the D7 invariants: pseudonymous ``human:``
  refs for the deciding/overriding person, ``sha256:`` digests for anything
  content-shaped, short machine *codes* (never prose) for decisions and
  reasons, and a refusal of payload-shaped extension fields.
- **Storage location.** ADR-0150 D3 names separate top-level facets
  (``facets.decision_context`` …). Those keys are not in the closed
  ``run-capsule`` facet registry and this slice may not change that schema, so
  each record type is stored as a list *inside* ``facets.conversation`` (whose
  schema is an open object). The key names are the ADR's facet names, so a
  later promotion to top-level facets is a move, not a rename.
- **Fail-open recording** (D7/I-3): the ``record_*`` entry points never raise
  into the workload; a record that cannot be attached is reported in a
  :class:`RecordOutcome` and the capsule is returned untouched.
- **Fail-closed reading**: a stored record that does not validate is surfaced
  as a :class:`RecordDefect`, never silently skipped.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from novafabric.hitl.conversation import (
    FACET_NAME,
    ConversationError,
    ConversationFacet,
    IdentityRefError,
    _parse_at,
    _validate_identity_ref,
    facet_from_capsule,
    resolve_turn,
)

logger = logging.getLogger(__name__)

#: Upper bound on records of one kind per capsule. A conversation that produces
#: more human decisions than this is not a conversation; the cap keeps a
#: runaway capture loop from growing ``capsule.yaml`` without bound.
MAX_RECORDS_PER_KIND = 10_000

#: A ``turn_ref`` is an identifier; anything longer is inlined content.
MAX_TURN_REF_LENGTH = 256

#: Extension (``extra``) values are bounded scalars — the same limit the
#: conversation module applies to identity refs.
MAX_EXTRA_VALUE_LENGTH = 512

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")

#: A machine code: ``approve``, ``risk_within_tolerance``, ``policy:p-17``.
#: No whitespace, so a sentence of prose (the usual carrier of names and other
#: free-text PII) cannot pass as a code.
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}\Z")

#: Substrings that mark an extension key as payload-shaped. An extension field
#: called ``reason_text`` or ``prompt`` is exactly the raw-content capture D7
#: forbids, whatever its value happens to be on the day it is written.
_PAYLOAD_KEY_MARKERS = (
    "content",
    "text",
    "prompt",
    "response",
    "completion",
    "message",
    "body",
    "payload",
    "raw",
    "transcript",
    "utterance",
)


class AccountabilityRecordError(ConversationError):
    """Base for every turn-anchored accountability-record error."""


class RecordContentError(AccountabilityRecordError):
    """Raised when raw text or a payload-shaped field is offered to a record.

    The message never echoes the offending value: if it is the text the field
    exists to keep out, echoing it would write that text into a log.
    """


@dataclass(frozen=True)
class RecordOutcome:
    """Result of a fail-open ``record_*`` call.

    ``capsule`` is the input object itself when nothing was recorded, so a
    caller that ignores the outcome still holds a valid capsule.
    """

    capsule: dict[str, Any]
    recorded: bool
    reason: str | None = None


@dataclass(frozen=True)
class RecordDefect:
    """A stored record that failed validation when read back (fail-closed)."""

    index: int
    error: str


M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class LoadedRecords(Generic[M]):
    """Records of one kind read from ``facets.conversation``."""

    records: list[tuple[int, M]] = field(default_factory=list)
    defects: list[RecordDefect] = field(default_factory=list)


# ── Field validators ──────────────────────────────────────────────────────


def check_turn_ref(value: object) -> str:
    """Return ``value`` if it is a usable ``turn_ref``."""
    if not isinstance(value, str) or not value.strip():
        raise AccountabilityRecordError("turn_ref must be a non-empty string")
    if len(value) > MAX_TURN_REF_LENGTH or any(ch.isspace() for ch in value):
        raise RecordContentError(
            f"turn_ref must be a turn id (no whitespace, <= {MAX_TURN_REF_LENGTH} "
            "chars); this looks like inlined content"
        )
    return value


def check_digest(value: object, *, field_name: str) -> str:
    """Return ``value`` if it is a ``sha256:<64 lower-hex>`` digest."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise RecordContentError(
            f"{field_name} must be a sha256 digest, not raw bytes; hash the "
            "material yourself (ADR-0150 D7 — digest-only)"
        )
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise RecordContentError(
            f"{field_name} must be 'sha256:<64 lower-case hex>'; raw text never "
            "enters an accountability record (ADR-0150 D7)"
        )
    return value


def check_code(value: object, *, field_name: str) -> str:
    """Return ``value`` if it is a short machine code (no prose)."""
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise RecordContentError(
            f"{field_name} must be a short lower-case code matching "
            f"{_CODE_RE.pattern} (no whitespace, no prose)"
        )
    return value


def check_code_or_digest(value: object, *, field_name: str) -> str:
    """Return ``value`` if it is a machine code or a ``sha256:`` digest.

    Used for free-text-in-the-ADR fields such as ``reason``: a structured
    reason code is recorded as is, and a prose reason is recorded only as the
    digest of that prose, so the capsule binds to it without holding it.
    """
    if isinstance(value, str) and (_DIGEST_RE.fullmatch(value) or _CODE_RE.fullmatch(value)):
        return value
    raise RecordContentError(
        f"{field_name} must be a short lower-case code or a 'sha256:' digest of "
        "the prose reason; free text never enters an accountability record "
        "(ADR-0150 D7)"
    )


def check_human_ref(value: object, *, field_name: str) -> str:
    """Return ``value`` if it is a pseudonymous ``human:`` identity ref."""
    ref = _validate_identity_ref(value, field=field_name)
    if not ref.startswith("human:"):
        raise IdentityRefError(
            f"{field_name} must be a 'human:' identity ref — the person who "
            f"decided or overrode, not an agent or system component"
        )
    return ref


def check_timestamp(value: object, *, field_name: str) -> str:
    """Return ``value`` unchanged if it parses as an ISO-8601 timestamp."""
    if not isinstance(value, str):
        raise AccountabilityRecordError(f"{field_name} must be an ISO-8601 string")
    _parse_at(value, field=field_name)
    return value


def check_extras(data: Any, *, known: frozenset[str]) -> Any:
    """Refuse payload-shaped or non-scalar extension fields.

    Records keep ``extra="allow"`` so a later slice can extend them without a
    schema break (I-1), but an extension is a *bounded scalar* with a
    non-payload name. Anything else is how raw content would get past the
    typed fields.
    """
    if not isinstance(data, Mapping):
        return data
    for key, value in data.items():
        if key in known:
            continue
        lowered = str(key).lower()
        if any(marker in lowered for marker in _PAYLOAD_KEY_MARKERS):
            raise RecordContentError(
                f"extension field {key!r} is payload-shaped; accountability "
                "records hold digests and codes only (ADR-0150 D7)"
            )
        if value is None or isinstance(value, (bool, int, float)):
            continue
        if isinstance(value, str) and len(value) <= MAX_EXTRA_VALUE_LENGTH:
            continue
        raise RecordContentError(
            f"extension field {key!r} must be a bounded scalar "
            f"(<= {MAX_EXTRA_VALUE_LENGTH} chars); nested or long values look "
            "like inlined content"
        )
    return data


# ── Storage ───────────────────────────────────────────────────────────────


def conversation_block(capsule: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the raw ``facets.conversation`` mapping, or None."""
    facets = capsule.get("facets")
    if not isinstance(facets, Mapping):
        return None
    block = facets.get(FACET_NAME)
    return block if isinstance(block, Mapping) else None


def duplicate_turn_ids(facet: ConversationFacet) -> list[str]:
    """Return turn ids that occur more than once (sorted, each once).

    A duplicated id makes every ``turn_ref`` naming it ambiguous, so a
    verifier treats it as a defect rather than resolving to the first match.
    """
    seen: set[str] = set()
    dupes: set[str] = set()
    for item in facet.turns:
        if item.turn_id in seen:
            dupes.add(item.turn_id)
        seen.add(item.turn_id)
    return sorted(dupes)


def append_record(
    capsule: dict[str, Any],
    key: str,
    record: BaseModel,
    *,
    turn_ref: str,
    unique_per_turn: bool,
) -> RecordOutcome:
    """Append ``record`` under ``facets.conversation[key]``, additively.

    Returns a not-recorded outcome (never raises for these) when there is no
    conversation facet, the ``turn_ref`` does not resolve, the existing list is
    malformed, the per-kind cap is reached, or a unique-per-turn record already
    exists for the turn. Returns a new dict; the input is not mutated.
    """
    facet = facet_from_capsule(capsule)
    if facet is None:
        return RecordOutcome(capsule, False, "no conversation facet")
    if resolve_turn(facet, turn_ref) is None:
        return RecordOutcome(capsule, False, "turn_ref does not resolve")
    block = conversation_block(capsule) or {}
    existing = block.get(key, [])
    if not isinstance(existing, list):
        return RecordOutcome(capsule, False, f"existing {key} is not a list")
    if len(existing) >= MAX_RECORDS_PER_KIND:
        return RecordOutcome(capsule, False, f"{key} record cap reached")
    if unique_per_turn and any(
        isinstance(item, Mapping) and item.get("turn_ref") == turn_ref for item in existing
    ):
        return RecordOutcome(capsule, False, f"{key} already recorded for turn")
    new_block = dict(block)
    new_block[key] = [*existing, record.model_dump(exclude_none=True)]
    facets = dict(capsule.get("facets") or {})
    facets[FACET_NAME] = new_block
    out = dict(capsule)
    out["facets"] = facets
    return RecordOutcome(out, True, None)


def record_fail_open(
    capsule: dict[str, Any],
    key: str,
    model: type[BaseModel],
    record: BaseModel | Mapping[str, Any],
    *,
    unique_per_turn: bool,
) -> RecordOutcome:
    """Validate and attach one record without ever raising (D7 fail-open).

    Any failure — a malformed record, a malformed capsule — is logged by
    exception *type only* (a Pydantic message can echo the offending input,
    which may be the very content D7 keeps out) and returned as a
    not-recorded outcome with the capsule untouched.
    """
    try:
        validated = record if isinstance(record, model) else model.model_validate(record)
        turn_ref = str(getattr(validated, "turn_ref"))
        outcome = append_record(
            capsule, key, validated, turn_ref=turn_ref, unique_per_turn=unique_per_turn
        )
    except Exception as exc:  # noqa: BLE001 — fail-open is the contract (D7)
        logger.warning(
            "hitl: %s record not attached (%s); workload unaffected",
            key,
            type(exc).__name__,
        )
        return RecordOutcome(capsule, False, type(exc).__name__)
    if not outcome.recorded:
        logger.warning("hitl: %s record not attached (%s)", key, outcome.reason)
    return outcome


def load_records(capsule: Mapping[str, Any], key: str, model: type[M]) -> LoadedRecords[M]:
    """Read and validate every ``facets.conversation[key]`` entry.

    Fail-closed: an entry that does not validate becomes a
    :class:`RecordDefect` carrying the exception *type* (never the message,
    which may echo inlined content). A non-list value is one defect at
    index -1. Reading is bounded by :data:`MAX_RECORDS_PER_KIND`.
    """
    block = conversation_block(capsule)
    if block is None or key not in block:
        return LoadedRecords()
    raw = block[key]
    if not isinstance(raw, list):
        return LoadedRecords(defects=[RecordDefect(-1, f"{key} is not a list")])
    loaded: LoadedRecords[M] = LoadedRecords()
    if len(raw) > MAX_RECORDS_PER_KIND:
        loaded.defects.append(RecordDefect(-1, f"{key} exceeds {MAX_RECORDS_PER_KIND} records"))
        raw = raw[:MAX_RECORDS_PER_KIND]
    for index, item in enumerate(raw):
        try:
            loaded.records.append((index, model.model_validate(item)))
        except (ConversationError, ValidationError, TypeError) as exc:
            loaded.defects.append(RecordDefect(index, type(exc).__name__))
    return loaded


def dangling_refs(facet: ConversationFacet | None, refs: Sequence[str]) -> list[str]:
    """Return refs that do not resolve (every ref, when there is no facet)."""
    if facet is None:
        return list(refs)
    return [ref for ref in refs if resolve_turn(facet, ref) is None]
