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

"""Watermark-presence *claims* (ADR-0148 D1 / NF-162) — pattern-only, verify-never-detect.

A ``watermark_presence`` entry records that **somebody claimed** a watermark is or is
not present in a piece of content, who claimed it, and by what mechanism. It is not a
detection result, and this module contains no detector.

**The pattern-only boundary, and why it is a hard rule rather than a preference.**
Every production watermark scheme for generated media is proprietary — SynthID, Luna
and their successors ship as closed detectors under licences ADR-0024 default-denies.
NovaFabric therefore never imports, ships, vendors, or depends at runtime on one.
:data:`FORBIDDEN_DETECTOR_MODULES` names the ones known today and
``tests/trust/test_watermark_presence.py`` asserts none of them is reachable from an
import of this package, so the boundary is enforced by a test rather than by intent.

**Three values, not two.** ``present`` is ``True``, ``False``, or ``None``:

- ``True`` — a claim was made that a watermark is present.
- ``False`` — a claim was made that it is **absent**. Somebody looked.
- ``None`` — **no claim exists.** Nobody looked, or nothing said.

Folding ``None`` into ``False`` is the defect this field exists to prevent: "no
watermark claim was recorded" and "a detector reported no watermark" are opposite
strengths of evidence, and an unmarked-content finding built from the first would be an
assertion NovaFabric has no basis for (ADR-0148 I-4).

**Where a claim comes from.** Three mechanisms, all of them recorded rather than run:

- ``c2pa_soft_binding`` — the C2PA manifest itself carries a soft-binding assertion.
- ``declared`` — the run declared it (a producer's own statement).
- ``third_party_claim`` — an external detector's result, handed to NovaFabric as data.
  Reading one of these back is verification *of the claim*, never detection.

The NF-094 SynthID-presence assertion (``compliance/export/art50_marking.py``) is one
such carrier, and :func:`claims_from_manifest` reads it through that module's own
:func:`~novafabric.compliance.export.art50_marking.verify_synthid_assertion` rather
than re-parsing the manifest — one reader, not two that can drift apart.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novafabric.compliance.export.art50_marking import (
    SYNTHID_PRESENCE_LABEL,
    verify_synthid_assertion,
)
from novafabric.trust.provenance.c2pa_bind import normalise_content_hash

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "watermark_presence"

#: C2PA assertion label for a soft binding — the spec's own carrier for a watermark or
#: fingerprint reference. NovaFabric reads it; it never produces one.
SOFT_BINDING_LABEL = "c2pa.soft-binding"

#: Import paths this module and its package must never reach. Asserted by a test, not
#: merely documented: a proprietary detector arriving as a transitive dependency would
#: otherwise be invisible until it was already shipped (ADR-0024, ADR-0148 D1).
FORBIDDEN_DETECTOR_MODULES: tuple[str, ...] = (
    "synthid",
    "google_synthid",
    "luna",
    "luna_watermark",
    "imatag",
    "steg_watermark",
)

#: Carried on every entry so a reader of the sealed capsule cannot mistake a recorded
#: claim for a detection NovaFabric performed.
CLAIM_ONLY_NOTE = (
    "Records a CLAIM of watermark presence; NovaFabric runs no proprietary detector."
)

WatermarkMethod = Literal["c2pa_soft_binding", "declared", "third_party_claim"]


class WatermarkPresenceEntry(BaseModel):
    """One recorded watermark-presence claim about one media blob."""

    model_config = ConfigDict(extra="forbid")

    bound_content_hash: str
    method: WatermarkMethod
    present: bool | None = Field(
        default=None,
        description=(
            "``True``/``False`` when a claim was made either way; ``None`` when no "
            "claim exists. ``None`` is never equivalent to ``False``."
        ),
    )
    source_of_claim: str = Field(
        description="Who or what made the claim, e.g. ``c2pa_manifest`` or a detector name."
    )
    note: str = CLAIM_ONLY_NOTE


class WatermarkPresenceFacet(BaseModel):
    """``facets.watermark_presence`` — additive, optional, absent when empty."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    entries: list[WatermarkPresenceEntry] = Field(default_factory=list)
    media_parts_scanned: int = Field(
        default=0,
        description=(
            "How many MediaParts were looked at, so an empty entry list cannot be read "
            "as 'no content carried a watermark'."
        ),
    )


def loaded_forbidden_detectors() -> list[str]:
    """Names from :data:`FORBIDDEN_DETECTOR_MODULES` currently in ``sys.modules``.

    The runtime half of the pattern-only boundary. Returns the offenders rather than a
    bool so a failure names what to remove.
    """
    hits: list[str] = []
    for name in sys.modules:
        root = name.split(".", 1)[0].lower()
        if root in FORBIDDEN_DETECTOR_MODULES:
            hits.append(name)
    return sorted(hits)


def _active_manifest_entry(document: Mapping[str, Any]) -> Mapping[str, Any] | None:
    active_id = document.get("active_manifest")
    manifests = document.get("manifests")
    if not isinstance(active_id, str) or not isinstance(manifests, Mapping):
        return None
    entry = manifests.get(active_id)
    return entry if isinstance(entry, Mapping) else None


def claims_from_manifest(
    content_hash: str, document: Any
) -> list[WatermarkPresenceEntry]:
    """Read every watermark-presence claim a manifest carries about *content_hash*.

    Two carriers are understood: the C2PA ``c2pa.soft-binding`` assertion, and the
    NF-094 SynthID-presence assertion — the latter through
    :func:`~novafabric.compliance.export.art50_marking.verify_synthid_assertion`, so
    the two modules cannot drift into disagreeing about the same bytes.

    Returns ``[]`` when the manifest carries no claim. It does **not** return an entry
    with ``present=False``: absence of a claim is not a claim of absence.
    """
    bound = normalise_content_hash(content_hash)
    if bound is None or not isinstance(document, Mapping):
        return []
    entry = _active_manifest_entry(document)
    if entry is None:
        return []

    claims: list[WatermarkPresenceEntry] = []
    assertions = entry.get("assertions")
    if isinstance(assertions, list):
        for assertion in assertions:
            if not isinstance(assertion, Mapping):
                continue
            if assertion.get("label") != SOFT_BINDING_LABEL:
                continue
            data = assertion.get("data")
            present: bool | None = None
            source = "c2pa_manifest"
            if isinstance(data, Mapping):
                raw_present = data.get("present")
                if isinstance(raw_present, bool):
                    present = raw_present
                elif data.get("alg") or data.get("blocks"):
                    # A soft binding exists but states no verdict. The binding's
                    # presence is not itself a presence claim.
                    present = None
                claimed_source = data.get("source")
                if isinstance(claimed_source, str) and claimed_source:
                    source = claimed_source
            claims.append(
                WatermarkPresenceEntry(
                    bound_content_hash=bound,
                    method="c2pa_soft_binding",
                    present=present,
                    source_of_claim=source,
                )
            )

    synthid = verify_synthid_assertion(document)
    if synthid is not None:
        claims.append(
            WatermarkPresenceEntry(
                bound_content_hash=bound,
                method="third_party_claim",
                present=synthid.present,
                source_of_claim=synthid.detector or SYNTHID_PRESENCE_LABEL,
            )
        )
    return claims


def declared_claim(
    content_hash: str, *, present: bool | None, source_of_claim: str
) -> WatermarkPresenceEntry | None:
    """Record a declared claim (a producer's own statement).

    ``present=None`` is accepted and recorded: "we were told nothing conclusive" is a
    fact worth sealing, and it is the only value that cannot be mistaken for a verdict.
    """
    bound = normalise_content_hash(content_hash)
    if bound is None:
        return None
    return WatermarkPresenceEntry(
        bound_content_hash=bound,
        method="declared",
        present=present,
        source_of_claim=source_of_claim,
    )


def build_facet(
    capsule_dir: Path,
    *,
    manifests: Mapping[str, Any] | None = None,
    declared: Iterable[WatermarkPresenceEntry] = (),
) -> WatermarkPresenceFacet | None:
    """Collect every claim discoverable for *capsule_dir*'s media.

    Returns ``None`` when there is no claim to record — an empty facet would read as
    "we checked the content for watermarks", which this module by construction never
    does.
    """
    from novafabric.capture.media import iter_media_parts
    from novafabric.trust.provenance.c2pa_bind import discover_sidecar_manifests

    discovered = (
        dict(manifests) if manifests is not None else discover_sidecar_manifests(capsule_dir)
    )
    entries: list[WatermarkPresenceEntry] = list(declared)
    scanned = 0
    seen_hashes: set[str] = set()
    for _call_id, media in iter_media_parts(capsule_dir):
        scanned += 1
        content_hash = normalise_content_hash(media.get("content_hash"))
        if content_hash is None or content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)
        document = discovered.get(content_hash)
        if document is None:
            continue
        entries.extend(claims_from_manifest(content_hash, document))

    if not entries:
        return None
    return WatermarkPresenceFacet(entries=entries, media_parts_scanned=scanned)


def attach_facet(
    capsule: dict[str, Any], facet: WatermarkPresenceFacet
) -> dict[str, Any]:
    """Attach *facet* under ``facets.watermark_presence``."""
    facets = capsule.setdefault("facets", {})
    if not isinstance(facets, dict):  # pragma: no cover - defensive
        raise TypeError("capsule 'facets' must be a mapping")
    facets[FACET_NAME] = facet.model_dump()
    return capsule


def facet_from_capsule(capsule: Mapping[str, Any]) -> WatermarkPresenceFacet | None:
    """Read the facet back, or ``None`` when the capsule carries none."""
    facets = capsule.get("facets")
    if not isinstance(facets, Mapping):
        return None
    body = facets.get(FACET_NAME)
    if not isinstance(body, Mapping):
        return None
    try:
        return WatermarkPresenceFacet.model_validate(dict(body))
    except ValueError:
        return None


def unclaimed(facet: WatermarkPresenceFacet) -> list[WatermarkPresenceEntry]:
    """Entries that carry no verdict — recorded, but conclusive about nothing."""
    return [e for e in facet.entries if e.present is None]
