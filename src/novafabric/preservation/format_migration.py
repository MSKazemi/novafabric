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

"""Format-migration provenance chain — ADR-0165 D2 P2 (NF-332).

A capsule sealed in 2026 as ``run-capsule@0.2.0`` may have to be read in 2040
by software that only understands ``run-capsule@0.4.0``. Somebody will migrate
it. This module records *that* the migration happened — which format it went
from and to, when, with which migrator (by reference), and the digest of the
artifact before and after — as an ordered, append-only list of hops stored in
``facets.preservation.format_migration_chain``. Each hop names its ``parent``
(the prior hop's ``post_digest``; ``null`` for the first hop), so an offline
verifier can walk from the newest representation back to ``original_root``,
the sealed object the chain preserves.

The spec (§3 req. 7) makes four demands of a verifier, and each maps to one
field of :class:`FormatMigrationVerification`:

- walk the chain offline and **detect a broken/missing parent** →
  ``chain_walk_ok``;
- **confirm the chain terminates at ``original_root``** →
  ``reaches_original_root`` (I-1: the original is always reachable);
- the chain is **acyclic** → ``acyclic``;
- the chain is **monotonic** → ``monotonic`` (each hop moves the format
  version strictly forward, hops are contiguous, time never runs backwards).

Record-only (I-4): NovaFabric does **not** run the migrator, does not rewrite
a stored capsule, and does not judge whether a migration was faithful or
authorized. The migrator is recorded by ``tool_ref`` (a digest or URI — never
the migrator bytes, I-2); the migrated artifact is recorded by
``post_digest`` (never the bytes). The original sealed capsule and its seal are
never touched, and a verifier refuses any chain whose earlier hops were
rewritten or dropped.

A verification verdict is **returned, never persisted into the facet**. A
cached ``verified`` block inside the object it describes goes stale the moment
the chain grows, and a stale "ok" in an evidence record is worse than none.
The sealed whole-chain re-verification receipt is NF-339 (P5, future design).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from novafabric.preservation.anchor import (
    PreservationError,
    PreservationFacet,
    _validate_digest,
    _validate_ref,
    append_provenance_event,
    provenance_event,
)

__all__ = [
    "CHAIN_FIELD",
    "MIGRATION_EVENT",
    "BrokenMigrationChainError",
    "ChainFinding",
    "ChainFindingCode",
    "FormatMigrationHop",
    "FormatMigrationRewriteError",
    "FormatMigrationVerification",
    "InvalidFormatVersionError",
    "append_format_migration",
    "chain_from_facet",
    "parse_format_version",
    "plan_next_hop",
    "verify_format_migration_chain",
    "verify_migration_append_only",
]

#: Key of the chain inside ``facets.preservation``. The anchor model is
#: ``extra="allow"``, so the chain lands without a schema change (I-1).
CHAIN_FIELD = "format_migration_chain"

#: PREMIS 3 eventType for a format migration. Appended to the anchor's
#: ``provenance_events`` alongside each hop so the PREMIS event history and the
#: migration chain tell the same story.
MIGRATION_EVENT = "migration"

#: ``<format-family>@<dotted-numeric-version>`` — e.g. ``run-capsule@0.3.0`` or
#: ``evidence-bundle@2``. Numeric-only on purpose: monotonicity has to be
#: decidable offline in 2045 without knowing what a pre-release tag meant.
_FORMAT_VERSION_RE = re.compile(r"^([a-z][a-z0-9-]{0,63})@(\d{1,9}(?:\.\d{1,9}){0,5})$")

#: A URI whose authority carries ``user[:password]@`` embeds a credential; a
#: reference must never do that (I-2, ADR-0009).
_URI_USERINFO_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://[^/?#]*@", re.IGNORECASE)

#: Upper bound on the backward walk; far beyond any real chain and it keeps a
#: hostile archive from turning verification into an unbounded loop.
MAX_CHAIN_LENGTH = 10_000

ChainFindingCode = Literal[
    "first_hop_has_parent",
    "missing_parent",
    "parent_not_previous_hop",
    "pre_digest_mismatch",
    "does_not_reach_original_root",
    "cycle",
    "family_mismatch",
    "version_not_increasing",
    "version_discontinuity",
    "time_not_monotonic",
    "chain_too_long",
]

#: Which verdict each finding code falsifies. A code missing here would be a
#: finding that makes no verdict false — so every code is mapped.
_LINK_CODES: frozenset[str] = frozenset(
    {
        "first_hop_has_parent",
        "missing_parent",
        "parent_not_previous_hop",
        "pre_digest_mismatch",
        "chain_too_long",
    }
)
_ROOT_CODES: frozenset[str] = frozenset({"does_not_reach_original_root"})
_CYCLE_CODES: frozenset[str] = frozenset({"cycle"})
_MONOTONIC_CODES: frozenset[str] = frozenset(
    {
        "family_mismatch",
        "version_not_increasing",
        "version_discontinuity",
        "time_not_monotonic",
    }
)


# ── Errors ────────────────────────────────────────────────────────────────


class InvalidFormatVersionError(PreservationError):
    """Raised when a format version is not ``<family>@<dotted-numeric>``."""


class BrokenMigrationChainError(PreservationError):
    """Raised when a chain fails verification where a valid one is required.

    Carries the :class:`FormatMigrationVerification` so a caller can render
    every finding, not just the first.
    """

    def __init__(self, message: str, verification: FormatMigrationVerification) -> None:
        super().__init__(message)
        self.verification = verification


class FormatMigrationRewriteError(PreservationError):
    """Raised when a newer chain did not merely append to an older one (I-1).

    A migration chain is evidence of a history. A later version of it that
    dropped, reordered, or edited an earlier hop has rewritten that history,
    and the original is no longer provably reachable along the recorded path.
    """


# ── Version parsing ───────────────────────────────────────────────────────


def parse_format_version(value: str) -> tuple[str, tuple[int, ...]]:
    """Split ``run-capsule@0.3.0`` into ``("run-capsule", (0, 3, 0))``.

    Raises:
        InvalidFormatVersionError: if ``value`` is not the canonical shape.
    """
    match = _FORMAT_VERSION_RE.match(value) if isinstance(value, str) else None
    if match is None:
        raise InvalidFormatVersionError(
            f"format version must look like 'run-capsule@0.3.0' "
            f"(<family>@<dotted numeric version>), got {value!r}"
        )
    family, version = match.groups()
    return family, tuple(int(part) for part in version.split("."))


def _version_key(parts: tuple[int, ...], width: int) -> tuple[int, ...]:
    """Zero-pad so ``1`` and ``1.0.0`` compare equal, as a reader expects."""
    return parts + (0,) * (width - len(parts))


def _parse_timestamp(value: str) -> datetime:
    """Parse an RFC 3339 timestamp; reject naive times.

    A naive timestamp cannot be ordered against one recorded in another zone,
    and ordering is exactly what the monotonic check needs.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"migrated_at must be an RFC 3339 timestamp, got {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"migrated_at must carry a timezone (e.g. 'Z'), got {value!r}")
    return parsed


# ── Hop model ─────────────────────────────────────────────────────────────


class FormatMigrationHop(BaseModel):
    """One format-migration hop (NF-332, spec §3 req. 7).

    Shape is validated at construction (digests, references, version syntax,
    timestamp); *chain* properties — links, cycles, monotonicity — are judged
    by :func:`verify_format_migration_chain`, because a verifier reading an
    archived chain has to report every fault, not stop at the first.

    ``parent`` is required-but-nullable: the first hop must say ``null``
    explicitly, so "this is the first hop" and "the parent field was lost" are
    distinguishable.
    """

    model_config = ConfigDict(extra="allow")

    from_version: str
    to_version: str
    migrated_at: str
    #: Digest or URI of the migrator — never the migrator bytes (I-2).
    tool_ref: str
    #: Digest of the artifact before this hop (``original_root`` for hop 0).
    pre_digest: str
    #: Digest of the artifact this hop produced.
    post_digest: str
    #: The prior hop's ``post_digest``; ``None`` for the first hop.
    parent: str | None

    @field_validator("from_version", "to_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        try:
            parse_format_version(v)
        except InvalidFormatVersionError as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("migrated_at")
    @classmethod
    def _check_migrated_at(cls, v: str) -> str:
        _parse_timestamp(v)
        return v

    @field_validator("tool_ref", mode="before")
    @classmethod
    def _check_tool_ref(cls, v: object) -> str:
        ref = _validate_ref(v, field="tool_ref")
        if _URI_USERINFO_RE.match(ref):
            raise ValueError(
                "tool_ref URI embeds credentials (user[:password]@host); a "
                "reference must never carry a secret (ADR-0165 I-2, ADR-0009)"
            )
        return ref

    @field_validator("pre_digest", "post_digest", mode="before")
    @classmethod
    def _check_digest(cls, v: object) -> str:
        return _validate_digest(v, field="digest")

    @field_validator("parent", mode="before")
    @classmethod
    def _check_parent(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="parent")


# ── Verification result ───────────────────────────────────────────────────


class ChainFinding(BaseModel):
    """One fault found while walking the chain."""

    model_config = ConfigDict(frozen=True)

    hop_index: int
    code: ChainFindingCode
    message: str


class FormatMigrationVerification(BaseModel):
    """Outcome of an offline walk of a format-migration chain.

    Every boolean is derived from ``findings``, so the verdicts and the
    reasons can never disagree. An empty chain is ``ok``: nothing was
    migrated, and the original *is* the current representation.
    """

    model_config = ConfigDict(frozen=True)

    original_root: str
    hop_count: int
    chain_walk_ok: bool
    reaches_original_root: bool
    acyclic: bool
    monotonic: bool
    findings: list[ChainFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only when every spec §3 req. 7 property holds."""
        return self.chain_walk_ok and self.reaches_original_root and self.acyclic and self.monotonic


# ── Reading the chain out of a facet ──────────────────────────────────────


def chain_from_facet(facet: PreservationFacet) -> list[FormatMigrationHop]:
    """Return the facet's migration chain, in recorded order.

    An absent chain is an empty list (I-3 — no migration yet is the normal
    case). The order is never sorted: the recorded order *is* the evidence,
    and sorting would repair exactly the faults the verifier must report.

    Raises:
        PreservationError: if the stored chain is not a list of well-formed
            hops. A malformed hop is not something a walk can reason about.
    """
    raw = (facet.model_extra or {}).get(CHAIN_FIELD)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PreservationError(f"{CHAIN_FIELD} must be a list of hops, got {type(raw).__name__}")
    hops: list[FormatMigrationHop] = []
    for index, item in enumerate(raw):
        try:
            hops.append(
                item
                if isinstance(item, FormatMigrationHop)
                else FormatMigrationHop.model_validate(item)
            )
        except ValidationError as exc:
            raise PreservationError(
                f"{CHAIN_FIELD}[{index}] is malformed: {exc.errors(include_url=False)[0]['msg']}"
            ) from exc
    return hops


# ── Verification ──────────────────────────────────────────────────────────


def _link_findings(hops: Sequence[FormatMigrationHop], original_root: str) -> list[ChainFinding]:
    """Parent-link and pre-digest findings (``chain_walk_ok``)."""
    findings: list[ChainFinding] = []
    #: ``post_digest`` of every hop before ``i`` — grown once per hop, never
    #: rebuilt, so the pass stays linear up to ``MAX_CHAIN_LENGTH``.
    earlier: set[str] = set()
    for i, hop in enumerate(hops):
        if i == 0:
            if hop.parent is not None:
                findings.append(
                    ChainFinding(
                        hop_index=0,
                        code="first_hop_has_parent",
                        message=(
                            "the first hop must have parent null; a non-null "
                            "parent points at a hop this chain does not contain"
                        ),
                    )
                )
            expected_pre = original_root
        else:
            previous = hops[i - 1].post_digest
            if hop.parent is None:
                findings.append(
                    ChainFinding(
                        hop_index=i,
                        code="missing_parent",
                        message=(
                            "hop has parent null but is not the first hop; the "
                            "link back to the prior representation is missing"
                        ),
                    )
                )
            elif hop.parent != previous and hop.parent in earlier:
                findings.append(
                    ChainFinding(
                        hop_index=i,
                        code="parent_not_previous_hop",
                        message=(
                            "parent resolves to an earlier hop, not the "
                            "immediately preceding one; the chain forks or was "
                            "reordered"
                        ),
                    )
                )
            elif hop.parent != previous:
                findings.append(
                    ChainFinding(
                        hop_index=i,
                        code="missing_parent",
                        message=(
                            f"parent {hop.parent} resolves to no earlier "
                            "post_digest; the chain is broken here"
                        ),
                    )
                )
            expected_pre = previous
        if hop.pre_digest != expected_pre:
            findings.append(
                ChainFinding(
                    hop_index=i,
                    code="pre_digest_mismatch",
                    message=(
                        f"pre_digest {hop.pre_digest} is not the digest of the "
                        f"representation this hop migrated from ({expected_pre})"
                    ),
                )
            )
        earlier.add(hop.post_digest)
    return findings


def _reaches_root(hops: Sequence[FormatMigrationHop], original_root: str) -> bool:
    """Walk backward from the newest hop by ``parent`` to ``original_root``.

    Independent of :func:`_link_findings` on purpose: a chain can be locally
    inconsistent yet still resolve (or locally tidy yet end somewhere else),
    and the spec names reachability as its own property.
    """
    if not hops:
        return True
    by_post: dict[str, int] = {}
    for index, hop in enumerate(hops):
        by_post.setdefault(hop.post_digest, index)
    current = len(hops) - 1
    visited: set[int] = set()
    # Each step moves strictly backwards (parent_index < current), so the walk
    # ends within len(hops) steps; the visited set is belt-and-braces.
    while True:
        if current in visited:  # pragma: no cover - unreachable, see above
            return False  # a parent loop never reaches any root
        visited.add(current)
        hop = hops[current]
        if hop.parent is None:
            return hop.pre_digest == original_root
        parent_index = by_post.get(hop.parent)
        if parent_index is None or parent_index >= current:
            return False  # unresolvable, or resolves forward in time
        current = parent_index


def _cycle_findings(hops: Sequence[FormatMigrationHop], original_root: str) -> list[ChainFinding]:
    """A hop whose output is a representation already seen is a cycle."""
    findings: list[ChainFinding] = []
    seen = {original_root}
    for i, hop in enumerate(hops):
        if hop.post_digest in seen or hop.post_digest == hop.pre_digest:
            findings.append(
                ChainFinding(
                    hop_index=i,
                    code="cycle",
                    message=(
                        f"post_digest {hop.post_digest} was already a "
                        "representation earlier in the chain (or the hop's own "
                        "input); the chain loops back on itself"
                    ),
                )
            )
        seen.add(hop.post_digest)
    return findings


def _monotonic_findings(hops: Sequence[FormatMigrationHop]) -> list[ChainFinding]:
    """Version strictly forward per hop, contiguous across hops, time forward."""
    findings: list[ChainFinding] = []
    for i, hop in enumerate(hops):
        fam_from, ver_from = parse_format_version(hop.from_version)
        fam_to, ver_to = parse_format_version(hop.to_version)
        if fam_from != fam_to:
            findings.append(
                ChainFinding(
                    hop_index=i,
                    code="family_mismatch",
                    message=(
                        f"{hop.from_version} → {hop.to_version} changes format "
                        "family; versions across families are not comparable, "
                        "so monotonicity cannot be established"
                    ),
                )
            )
        else:
            width = max(len(ver_from), len(ver_to))
            if _version_key(ver_to, width) <= _version_key(ver_from, width):
                findings.append(
                    ChainFinding(
                        hop_index=i,
                        code="version_not_increasing",
                        message=(
                            f"{hop.from_version} → {hop.to_version} does not "
                            "move the format version forward"
                        ),
                    )
                )
        if i > 0:
            previous = hops[i - 1]
            if hop.from_version != previous.to_version:
                findings.append(
                    ChainFinding(
                        hop_index=i,
                        code="version_discontinuity",
                        message=(
                            f"hop starts at {hop.from_version} but the previous "
                            f"hop ended at {previous.to_version}"
                        ),
                    )
                )
            if _parse_timestamp(hop.migrated_at) < _parse_timestamp(previous.migrated_at):
                findings.append(
                    ChainFinding(
                        hop_index=i,
                        code="time_not_monotonic",
                        message=(
                            f"migrated_at {hop.migrated_at} is earlier than the "
                            f"previous hop's {previous.migrated_at}"
                        ),
                    )
                )
    return findings


def verify_format_migration_chain(
    hops: Sequence[FormatMigrationHop], original_root: str
) -> FormatMigrationVerification:
    """Walk a format-migration chain offline and report every fault.

    Never raises on a chain fault — a verifier that stopped at the first one
    would hide the rest. Pure: no IO, no network, no clock.

    Args:
        hops: The chain, in recorded order.
        original_root: The anchor's ``original_root`` the chain must reach.
    """
    root = _validate_digest(original_root, field="original_root")
    if len(hops) > MAX_CHAIN_LENGTH:
        finding = ChainFinding(
            hop_index=MAX_CHAIN_LENGTH,
            code="chain_too_long",
            message=f"chain exceeds {MAX_CHAIN_LENGTH} hops; refusing to walk it",
        )
        return FormatMigrationVerification(
            original_root=root,
            hop_count=len(hops),
            chain_walk_ok=False,
            reaches_original_root=False,
            acyclic=False,
            monotonic=False,
            findings=[finding],
        )
    findings = [
        *_link_findings(hops, root),
        *_cycle_findings(hops, root),
        *_monotonic_findings(hops),
    ]
    reaches = _reaches_root(hops, root)
    if not reaches:
        findings.append(
            ChainFinding(
                hop_index=len(hops) - 1,
                code="does_not_reach_original_root",
                message=(
                    "walking parent links back from the newest hop does not "
                    f"terminate at original_root {root} (ADR-0165 I-1)"
                ),
            )
        )
    codes = {f.code for f in findings}
    return FormatMigrationVerification(
        original_root=root,
        hop_count=len(hops),
        chain_walk_ok=not codes & _LINK_CODES,
        reaches_original_root=reaches and not codes & _ROOT_CODES,
        acyclic=not codes & _CYCLE_CODES,
        monotonic=not codes & _MONOTONIC_CODES,
        findings=findings,
    )


# ── Appending ─────────────────────────────────────────────────────────────


def plan_next_hop(
    facet: PreservationFacet,
    *,
    to_version: str,
    tool_ref: str,
    post_digest: str,
    migrated_at: str,
    from_version: str | None = None,
) -> FormatMigrationHop:
    """Derive the next hop's links from the chain so a caller cannot mis-link.

    ``parent`` and ``pre_digest`` are always computed, never supplied: the
    prior hop's ``post_digest``, or ``null`` / ``original_root`` for the first
    hop. ``from_version`` defaults to the prior hop's ``to_version``; it is
    required for the first hop, where only the caller knows the original
    format.

    Raises:
        InvalidFormatVersionError: first hop with no ``from_version``, or a
            ``from_version`` that contradicts the prior hop's ``to_version``.
        PreservationError: the stored chain is malformed.
    """
    hops = chain_from_facet(facet)
    last = hops[-1] if hops else None
    if last is None:
        if from_version is None:
            raise InvalidFormatVersionError(
                "the first migration hop needs from_version — the format the "
                "original sealed capsule was written in"
            )
        resolved_from = from_version
    else:
        if from_version is not None and from_version != last.to_version:
            raise InvalidFormatVersionError(
                f"from_version {from_version!r} contradicts the chain, whose "
                f"last hop ended at {last.to_version!r}"
            )
        resolved_from = last.to_version
    return FormatMigrationHop(
        from_version=resolved_from,
        to_version=to_version,
        migrated_at=migrated_at,
        tool_ref=tool_ref,
        pre_digest=last.post_digest if last else facet.original_root,
        post_digest=post_digest,
        parent=last.post_digest if last else None,
    )


def append_format_migration(
    facet: PreservationFacet,
    hop: FormatMigrationHop,
    *,
    record_event: bool = True,
) -> PreservationFacet:
    """Append one hop to the chain, returning a **new** facet (I-1).

    Refuses to extend a chain that is already broken — appending to it would
    lend a faulty history a fresh, valid-looking tip — and refuses a hop that
    would break a sound chain. Nothing else on the anchor changes:
    ``original_root``, ``fixity``, and the fixity log are carried over as-is.

    With ``record_event`` (default), a PREMIS ``migration`` event with the
    hop's time and migrator reference is appended to ``provenance_events`` so
    the two histories agree.

    Raises:
        BrokenMigrationChainError: the existing or resulting chain fails
            verification.
    """
    hops = chain_from_facet(facet)
    before = verify_format_migration_chain(hops, facet.original_root)
    if not before.ok:
        raise BrokenMigrationChainError(
            "refusing to append to a format-migration chain that already fails "
            "verification; repair the record's provenance first",
            before,
        )
    after = verify_format_migration_chain([*hops, hop], facet.original_root)
    if not after.ok:
        raise BrokenMigrationChainError(
            "refusing to append a hop that would break the format-migration "
            "chain: " + "; ".join(f.code for f in after.findings),
            after,
        )
    serialized: list[dict[str, Any]] = [h.model_dump(mode="json") for h in (*hops, hop)]
    out = facet.model_copy(update={CHAIN_FIELD: serialized})
    if record_event:
        out = append_provenance_event(
            out,
            provenance_event(MIGRATION_EVENT, hop.migrated_at, agent_ref=hop.tool_ref),
        )
    return out


def verify_migration_append_only(before: PreservationFacet, after: PreservationFacet) -> None:
    """Assert ``after``'s chain only appended to ``before``'s (I-1).

    Raises:
        FormatMigrationRewriteError: ``original_root`` changed, the chain
            shrank, or any earlier hop was edited or reordered.
    """
    if before.original_root != after.original_root:
        raise FormatMigrationRewriteError(
            "original_root changed between chain versions "
            f"({before.original_root} → {after.original_root}); the chain now "
            "preserves a different object (ADR-0165 I-1)"
        )
    old = chain_from_facet(before)
    new = chain_from_facet(after)
    if len(new) < len(old):
        raise FormatMigrationRewriteError(
            f"format-migration chain shrank from {len(old)} to {len(new)} hops; "
            "a prior migration was dropped (ADR-0165 I-1)"
        )
    for index, (a, b) in enumerate(zip(old, new)):
        if a.model_dump(mode="json") != b.model_dump(mode="json"):
            raise FormatMigrationRewriteError(
                f"format-migration hop {index} was rewritten; recorded hops are "
                "history and are never edited (ADR-0165 I-1)"
            )
