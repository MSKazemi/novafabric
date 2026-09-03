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

"""NF-162 — watermark-presence claims, and the pattern-only boundary (ADR-0148 D1)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from novafabric.compliance.export.art50_marking import attach_synthid_presence
from novafabric.trust.provenance.watermark import (
    CLAIM_ONLY_NOTE,
    FORBIDDEN_DETECTOR_MODULES,
    SOFT_BINDING_LABEL,
    attach_facet,
    build_facet,
    claims_from_manifest,
    declared_claim,
    facet_from_capsule,
    loaded_forbidden_detectors,
    unclaimed,
)
from trust._provenance_fixtures import IMAGE_BYTES, IMAGE_HASH, a_capsule, a_manifest


def with_soft_binding(
    present: Any = True, *, source: str | None = None, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    doc = a_manifest()
    body: dict[str, Any] = dict(data or {})
    if present is not None or data is None:
        body["present"] = present
    if source is not None:
        body["source"] = source
    doc["manifests"]["urn:manifest:1"]["assertions"].append(
        {"label": SOFT_BINDING_LABEL, "data": body}
    )
    return doc


# --- the pattern-only boundary ----------------------------------------------


def test_no_proprietary_detector_is_importable_from_this_package() -> None:
    """AC5 — the boundary is enforced by a test, not by intent.

    Importing the package in a *fresh* interpreter and asserting no forbidden module
    landed in ``sys.modules`` is what makes this real: an in-process check could pass
    only because some earlier test had not imported the offender yet.
    """
    code = (
        "import sys, json;"
        "import novafabric.trust.provenance.watermark as w;"
        "print(json.dumps(w.loaded_forbidden_detectors()))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip().endswith("[]"), result.stdout


def test_forbidden_list_names_the_known_proprietary_detectors() -> None:
    assert "synthid" in FORBIDDEN_DETECTOR_MODULES
    assert "luna" in FORBIDDEN_DETECTOR_MODULES


def test_loaded_forbidden_detectors_actually_detects_one() -> None:
    """Red-green for the guard itself: a guard that can never fire proves nothing."""
    import types

    assert loaded_forbidden_detectors() == []
    sys.modules["synthid"] = types.ModuleType("synthid")
    sys.modules["synthid.detect"] = types.ModuleType("synthid.detect")
    try:
        assert loaded_forbidden_detectors() == ["synthid", "synthid.detect"]
    finally:
        del sys.modules["synthid"], sys.modules["synthid.detect"]
    assert loaded_forbidden_detectors() == []


# --- three values, not two ---------------------------------------------------


def test_soft_binding_present_true_is_recorded() -> None:
    claims = claims_from_manifest(IMAGE_HASH, with_soft_binding(True))
    assert len(claims) == 1
    assert claims[0].method == "c2pa_soft_binding"
    assert claims[0].present is True
    assert claims[0].note == CLAIM_ONLY_NOTE


def test_soft_binding_present_false_is_a_claim_of_absence() -> None:
    """``False`` means somebody looked and said no — distinct from nobody looking."""
    claims = claims_from_manifest(IMAGE_HASH, with_soft_binding(False))
    assert len(claims) == 1
    assert claims[0].present is False


def test_a_soft_binding_with_no_verdict_is_unknown_not_false() -> None:
    """AC4 — a binding existing is not itself a presence claim."""
    doc = with_soft_binding(None, data={"alg": "com.example.softbind", "blocks": 4})
    claims = claims_from_manifest(IMAGE_HASH, doc)
    assert len(claims) == 1
    assert claims[0].present is None


def test_a_manifest_with_no_claim_yields_no_entry_at_all() -> None:
    """Absence of a claim is not a claim of absence — so there is nothing to record."""
    assert claims_from_manifest(IMAGE_HASH, a_manifest()) == []


def test_non_boolean_present_is_not_coerced_into_a_verdict() -> None:
    """A manifest saying ``present: "yes"`` has not made a machine-readable claim."""
    for junk in ["yes", 1, 0, "true", {}]:
        claims = claims_from_manifest(IMAGE_HASH, with_soft_binding(junk))
        assert len(claims) == 1
        assert claims[0].present is None, f"{junk!r} must not become a verdict"


def test_claims_from_a_non_manifest_or_bad_hash_are_empty() -> None:
    assert claims_from_manifest(IMAGE_HASH, {"nope": 1}) == []
    assert claims_from_manifest("not-a-hash", with_soft_binding(True)) == []
    assert claims_from_manifest(IMAGE_HASH, None) == []


def test_soft_binding_source_is_carried_when_the_manifest_names_one() -> None:
    claims = claims_from_manifest(
        IMAGE_HASH, with_soft_binding(True, source="acme-detector-v2")
    )
    assert claims[0].source_of_claim == "acme-detector-v2"


# --- the NF-094 carrier ------------------------------------------------------


def test_synthid_assertion_is_read_through_the_nf094_module() -> None:
    """One reader, not two that can drift: the NF-094 assertion is read by NF-094's
    own verifier, so a change to that format cannot leave this module reading a
    format that no longer exists."""
    doc = attach_synthid_presence(
        a_manifest(), present=True, detector="ext-detector-1", verified_at="2026-09-03T00:00:00Z"
    )
    claims = claims_from_manifest(IMAGE_HASH, doc)
    assert len(claims) == 1
    assert claims[0].method == "third_party_claim"
    assert claims[0].present is True
    assert claims[0].source_of_claim == "ext-detector-1"


def test_a_negative_synthid_claim_is_recorded_as_false_not_dropped() -> None:
    doc = attach_synthid_presence(
        a_manifest(), present=False, detector="ext-detector-1", verified_at="2026-09-03T00:00:00Z"
    )
    claims = claims_from_manifest(IMAGE_HASH, doc)
    assert len(claims) == 1
    assert claims[0].present is False


def test_both_carriers_on_one_manifest_produce_both_claims() -> None:
    """Two independent claims about the same bytes are two records, never merged into
    one verdict — merging would hide a disagreement between sources."""
    doc = attach_synthid_presence(
        with_soft_binding(False),
        present=True,
        detector="ext-detector-1",
        verified_at="2026-09-03T00:00:00Z",
    )
    claims = claims_from_manifest(IMAGE_HASH, doc)
    assert {c.method for c in claims} == {"c2pa_soft_binding", "third_party_claim"}
    assert {c.present for c in claims} == {False, True}


# --- declared claims ---------------------------------------------------------


def test_declared_claim_records_a_producers_own_statement() -> None:
    entry = declared_claim(IMAGE_HASH, present=True, source_of_claim="producer-declaration")
    assert entry is not None
    assert entry.method == "declared"
    assert entry.present is True


def test_declared_claim_accepts_an_inconclusive_statement() -> None:
    entry = declared_claim(IMAGE_HASH, present=None, source_of_claim="producer-declaration")
    assert entry is not None
    assert entry.present is None


def test_declared_claim_rejects_an_unusable_hash() -> None:
    assert declared_claim("nope", present=True, source_of_claim="x") is None


# --- facet -------------------------------------------------------------------


def test_build_facet_collects_manifest_claims_and_counts_scanned(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=with_soft_binding(True))
    facet = build_facet(capsule)
    assert facet is not None
    assert len(facet.entries) == 1
    assert facet.media_parts_scanned == 1


def test_build_facet_returns_none_when_no_claim_exists(tmp_path: Path) -> None:
    """AC7 — an empty facet would read as 'we checked the content for watermarks',
    which this module by construction never does."""
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    assert build_facet(capsule) is None


def test_build_facet_on_a_capsule_with_no_media_is_none(tmp_path: Path) -> None:
    capsule = tmp_path / "empty"
    capsule.mkdir()
    assert build_facet(capsule) is None


def test_declared_claims_are_carried_into_the_facet(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES)
    entry = declared_claim(IMAGE_HASH, present=None, source_of_claim="operator")
    assert entry is not None
    facet = build_facet(capsule, declared=[entry])
    assert facet is not None
    assert len(facet.entries) == 1
    assert unclaimed(facet) == facet.entries


def test_facet_round_trips_through_a_capsule(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=with_soft_binding(True))
    facet = build_facet(capsule)
    assert facet is not None
    manifest: dict[str, Any] = {"run_id": "run_1"}
    attach_facet(manifest, facet)
    read_back = facet_from_capsule(manifest)
    assert read_back is not None
    assert read_back.entries[0].present is True


def test_unknown_does_not_serialise_as_a_verdict(tmp_path: Path) -> None:
    """The serialised form must keep ``null`` — a JSON consumer reading a missing key
    as ``false`` is exactly the collapse this field exists to prevent."""
    entry = declared_claim(IMAGE_HASH, present=None, source_of_claim="operator")
    assert entry is not None
    from novafabric.trust.provenance.watermark import WatermarkPresenceFacet

    body = WatermarkPresenceFacet(entries=[entry]).model_dump(mode="json")
    assert "present" in body["entries"][0]
    assert body["entries"][0]["present"] is None


def test_facet_from_capsule_is_none_for_capsules_without_one() -> None:
    assert facet_from_capsule({}) is None
    assert facet_from_capsule({"facets": "nope"}) is None
    assert facet_from_capsule({"facets": {"watermark_presence": {"entries": 3}}}) is None
