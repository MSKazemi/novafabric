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

"""ADR-0165 P1 — preservation anchor + fixity log (NF-331, NF-335).

Tests are organised by the ADR's invariants, because those are what a reviewer
needs to be convinced of: I-1 append-only / never break the original, I-2
references-and-digests-only, I-3 fail-open, I-4 record-only (bit rot is
surfaced, never repaired).
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.preservation import (
    FACET_NAME,
    MAX_REF_LENGTH,
    Fixity,
    FixityCheck,
    FixityLogRewriteError,
    InvalidDigestError,
    PayloadCaptureError,
    PreservationError,
    PreservationFacet,
    append_fixity_check,
    append_provenance_event,
    attach_facet,
    build_anchor,
    check_fixity,
    detected_bit_rot,
    digest_artifact,
    facet_from_capsule,
    fixity_status,
    provenance_event,
    scan_for_payloads,
    verify_anchor_binding,
    verify_append_only,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "preservation"
PRE_ANCHOR_CAPSULE = FIXTURES / "valid-pre-anchor-capsule.json"
VALID_ANCHOR = FIXTURES / "valid-anchor.json"

#: The artifact the golden anchor fixture preserves. Its digest is what the
#: fixture's `original_root`, `fixity.digest` and `bound_root` all carry, so
#: the fixture is verifiable rather than merely well-shaped.
SEALED_ARTIFACT = "sealed-capsule-2026"
ROOT = digest_artifact(SEALED_ARTIFACT)
AGENT = digest_artifact("node-7")


def _anchor(**kw: Any) -> PreservationFacet:
    base: dict[str, Any] = {
        "preservation_id": "presv-1",
        "original_root": ROOT,
        "fixity_digest": ROOT,
    }
    base.update(kw)
    return build_anchor(
        base.pop("preservation_id"), base.pop("original_root"), **base
    )


def _check(matches: bool, at: str = "2027-01-01T00:00:00Z") -> FixityCheck:
    return FixityCheck(
        checked_at=at,
        digest=ROOT if matches else digest_artifact("rotted"),
        matches_original=matches,
    )


# ── Golden fixtures (the two the ADR names) ───────────────────────────────


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    """The real run-capsule schema, unmodified.

    Written against an in-memory copy with `preservation` injected, because
    the branch predated the commit that registered it in the ADR-0196 facets
    registry. The entry has since landed, so the injection is gone and these
    tests validate against the shipped schema exactly as an auditor would.
    """
    loaded: dict[str, Any] = json.loads(SCHEMA_PATH.read_text())
    return loaded


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(PRE_ANCHOR_CAPSULE.read_text())


def test_golden_pre_anchor_capsule_is_still_valid(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden fixture 1 (ADR-0165 P1): a capsule sealed before this layer existed.

    The whole additive-first claim reduces to this one assertion: nothing
    NF-331 adds may make an already-sealed 2026 capsule invalid.
    """
    assert "facets" not in capsule
    jsonschema.validate(capsule, schema)


def test_golden_anchor_fixture_loads_and_verifies(schema: dict[str, Any]) -> None:
    """Golden fixture 2 (ADR-0165 P1): a valid anchor.

    Verified, not merely parsed: the fixture's baseline digest is re-checked
    against the artifact it claims to preserve, so a fixture that drifted into
    being well-shaped nonsense fails here.
    """
    facet = PreservationFacet.model_validate(json.loads(VALID_ANCHOR.read_text()))
    assert facet.original_root == ROOT
    assert verify_anchor_binding(facet, SEALED_ARTIFACT) is True
    assert fixity_status(facet) == "intact"


def test_golden_anchor_attached_to_golden_capsule_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Both fixtures together, against the REAL schema.

    Five earlier facet slices shipped code that produced capsules the schema
    rejected, because their tests only ever handled plain dicts (ADR-0196 D4).
    This test uses the shipped builder and the real schema for that reason.
    """
    facet = PreservationFacet.model_validate(json.loads(VALID_ANCHOR.read_text()))
    jsonschema.validate(attach_facet(capsule, facet), schema)


def test_preservation_is_registered_in_the_shipped_schema() -> None:
    on_disk: dict[str, Any] = json.loads(SCHEMA_PATH.read_text())
    assert FACET_NAME in on_disk["properties"]["facets"]["properties"]


def test_builder_output_validates_against_the_real_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    out = attach_facet(
        capsule,
        _anchor(
            provenance_events=[provenance_event("sealed", "2026-07-13T09:00:00Z")],
            bound_root=ROOT,
        ),
    )
    jsonschema.validate(out, schema)


def test_facet_value_must_be_an_object_in_the_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Guards the injection above: the registry entry is a real constraint."""
    capsule["facets"] = {FACET_NAME: "not-an-object"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(capsule, schema)


# ── I-1: additive-first, the original is never touched ────────────────────


def test_capsule_with_no_preservation_material_is_untouched(
    capsule: dict[str, Any]
) -> None:
    """Fail-open (I-3) meeting additive-first (I-1): no material, no facet."""
    before = copy.deepcopy(capsule)
    assert attach_facet(capsule, None) == before
    assert capsule == before


def test_attach_returns_a_new_dict_and_does_not_mutate_the_input() -> None:
    original: dict[str, Any] = {"run_id": "r"}
    out = attach_facet(original, _anchor())
    assert original == {"run_id": "r"}
    assert out is not original


def test_attach_preserves_sibling_facets() -> None:
    out = attach_facet(
        {"run_id": "r", "facets": {"safety": {"a": 1}}}, _anchor()
    )
    assert out["facets"]["safety"] == {"a": 1}
    assert FACET_NAME in out["facets"]


def test_pre_anchor_capsule_bytes_are_unchanged_by_a_no_op_attach() -> None:
    """Byte-identical, not merely equal.

    A capsule is hashed into a seal; an attach that reordered or re-typed a key
    would change the sealed root of a run that gained no evidence.
    """
    raw = PRE_ANCHOR_CAPSULE.read_bytes()
    loaded = json.loads(raw)
    assert json.dumps(attach_facet(loaded, None)).encode() == json.dumps(
        json.loads(raw)
    ).encode()


def test_facet_round_trips_through_a_capsule() -> None:
    facet = _anchor(bound_root=ROOT)
    assert facet_from_capsule(attach_facet({"run_id": "r"}, facet)) == facet


def test_facet_from_a_capsule_without_one_is_none() -> None:
    assert facet_from_capsule({"run_id": "r"}) is None
    assert facet_from_capsule({"run_id": "r", "facets": {}}) is None
    assert facet_from_capsule({"run_id": "r", "facets": "junk"}) is None


def test_absent_optional_fields_are_absent_not_null() -> None:
    """`null` and "not recorded" must not collapse into each other.

    In a layer whose entire value is that absence of evidence is distinguishable
    from negative evidence, a serialised `agent_ref: null` reads to a 2040
    verifier as "we recorded that there was no agent".
    """
    block = attach_facet({"run_id": "r"}, _anchor())["facets"][FACET_NAME]
    assert "bound_root" not in block


def test_facet_carries_a_schema_version() -> None:
    assert attach_facet({"run_id": "r"}, _anchor())["facets"][FACET_NAME][
        "schema_version"
    ]


def test_builders_preserve_recorded_order_rather_than_sorting() -> None:
    """Order is the record.

    Sorting by timestamp would silently repair a log whose entries arrived out
    of order — hiding the very anomaly a preservation auditor is looking for.
    """
    facet = _anchor(
        provenance_events=[
            provenance_event("late", "2030-01-01T00:00:00Z"),
            provenance_event("early", "2026-01-01T00:00:00Z"),
        ]
    )
    assert [e.event for e in facet.provenance_events] == ["late", "early"]


# ── I-1: the fixity log is append-only ────────────────────────────────────


def test_appending_a_check_returns_a_new_facet_and_leaves_the_old_alone() -> None:
    before = _anchor()
    after = append_fixity_check(before, _check(True))
    assert before.fixity_log == []
    assert len(after.fixity_log) == 1


def test_a_later_pass_does_not_erase_an_earlier_fail() -> None:
    """The load-bearing bit-rot case.

    An archive that lost bits and was restored from a good copy is materially
    different from one that never rotted. If a clean check could displace the
    FAIL, the log would record the archive's current state instead of its
    history, and the difference would be unrecoverable.
    """
    facet = _anchor()
    facet = append_fixity_check(facet, _check(False, "2031-01-01T00:00:00Z"))
    facet = append_fixity_check(facet, _check(True, "2032-01-01T00:00:00Z"))

    assert [c.matches_original for c in facet.fixity_log] == [False, True]
    assert fixity_status(facet) == "bit_rot_detected"
    assert [c.checked_at for c in detected_bit_rot(facet)] == [
        "2031-01-01T00:00:00Z"
    ]


def test_verify_append_only_accepts_a_pure_append() -> None:
    before = append_fixity_check(_anchor(), _check(True))
    after = append_fixity_check(before, _check(False, "2031-01-01T00:00:00Z"))
    verify_append_only(before, after)  # does not raise


def test_verify_append_only_rejects_a_truncated_log() -> None:
    before = append_fixity_check(_anchor(), _check(False))
    with pytest.raises(FixityLogRewriteError, match="shrank"):
        verify_append_only(before, _anchor())


def test_verify_append_only_rejects_a_rewritten_prior_check() -> None:
    """The falsification the whole guard exists for: FAIL edited into PASS."""
    before = append_fixity_check(_anchor(), _check(False))
    forged = _anchor(fixity_log=[_check(True)])
    with pytest.raises(FixityLogRewriteError, match="entry 0 was rewritten"):
        verify_append_only(before, forged)


def test_verify_append_only_rejects_a_silently_edited_timestamp() -> None:
    """Prior checks are compared whole, not just on their verdict.

    Backdating a check is the same falsification and is harder to notice
    precisely because the verdict still reads the same.
    """
    before = append_fixity_check(_anchor(), _check(False, "2031-01-01T00:00:00Z"))
    after = _anchor(fixity_log=[_check(False, "2039-01-01T00:00:00Z")])
    with pytest.raises(FixityLogRewriteError, match="entry 0 was rewritten"):
        verify_append_only(before, after)


def test_verify_append_only_rejects_a_changed_original_root() -> None:
    before = _anchor()
    after = _anchor(original_root=digest_artifact("some-other-capsule"))
    with pytest.raises(FixityLogRewriteError, match="original_root changed"):
        verify_append_only(before, after)


def test_verify_append_only_rejects_a_rewritten_fixity_baseline() -> None:
    """Redefining the baseline is how bit rot gets "repaired" out of existence."""
    before = _anchor()
    after = _anchor(fixity_digest=digest_artifact("rotted"))
    with pytest.raises(FixityLogRewriteError, match="baseline was rewritten"):
        verify_append_only(before, after)


def test_provenance_events_append_without_touching_the_prior_list() -> None:
    before = _anchor(provenance_events=[provenance_event("sealed", "2026-01-01")])
    after = append_provenance_event(
        before, provenance_event("migrated", "2029-01-01")
    )
    assert [e.event for e in before.provenance_events] == ["sealed"]
    assert [e.event for e in after.provenance_events] == ["sealed", "migrated"]


# ── I-4: bit rot is surfaced, never repaired ──────────────────────────────


def test_check_fixity_on_intact_bytes_records_a_pass() -> None:
    facet = check_fixity(_anchor(), SEALED_ARTIFACT, checked_at="2027-01-01T00:00:00Z")
    assert facet.fixity_log[-1].matches_original is True
    assert fixity_status(facet) == "intact"


def test_check_fixity_on_rotted_bytes_records_a_fail_and_keeps_the_baseline() -> None:
    """The mismatch is recorded; the anchor is not quietly re-based.

    Updating `fixity.digest` to the newly observed value would convert detected
    corruption into an accepted new normal — the single most dangerous thing
    this module could do (ADR-0165 I-4).
    """
    before = _anchor()
    after = check_fixity(before, "corrupted bytes", checked_at="2031-01-01T00:00:00Z")

    recorded = after.fixity_log[-1]
    assert recorded.matches_original is False
    assert recorded.digest == digest_artifact("corrupted bytes")
    assert after.fixity.digest == before.fixity.digest == ROOT
    assert fixity_status(after) == "bit_rot_detected"
    verify_append_only(before, after)


def test_repeated_checks_accumulate_rather_than_replace() -> None:
    facet = _anchor()
    for year in (2027, 2028, 2029):
        facet = check_fixity(
            facet, SEALED_ARTIFACT, checked_at=f"{year}-01-01T00:00:00Z"
        )
    assert len(facet.fixity_log) == 3


def test_check_fixity_does_not_mutate_the_facet_it_was_given() -> None:
    facet = _anchor()
    check_fixity(facet, "corrupted", checked_at="2031-01-01T00:00:00Z")
    assert facet.fixity_log == []


def test_an_unrecomputable_algorithm_raises_rather_than_logging_a_failure() -> None:
    """A check that could not run did not find corruption.

    Logging `matches_original: false` here would fabricate a bit-rot finding
    out of a tooling limitation, which is exactly the kind of laundering I-4
    forbids.
    """
    facet = _anchor(fixity_alg="sha3-256")
    with pytest.raises(PreservationError, match="cannot re-compute fixity"):
        check_fixity(facet, SEALED_ARTIFACT, checked_at="2031-01-01T00:00:00Z")
    assert facet.fixity_log == []


def test_module_exposes_no_repair_surface() -> None:
    """Record-only is a property of the API, not just of the docs.

    If a repair/heal/restore entry point ever appears here this fails, which is
    the point: I-4 should be structurally hard to violate.
    """
    import novafabric.preservation as preservation

    forbidden = {"repair", "heal", "restore", "fix", "rebase", "rewrite"}
    assert forbidden.isdisjoint({n.lower() for n in preservation.__all__})


# ── I-4: absent is not false ──────────────────────────────────────────────


def test_an_unchecked_anchor_is_not_checked_not_intact() -> None:
    """No fixity record means nobody looked — never "the bits are fine".

    An empty log is precisely the situation periodic fixity exists to
    eliminate; reporting it as `intact` would turn an absence of evidence into
    a positive finding.
    """
    assert fixity_status(_anchor()) == "not_checked"
    assert detected_bit_rot(_anchor()) == []


def test_matches_original_has_no_default() -> None:
    """A check with no recorded verdict cannot be constructed at all."""
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
        FixityCheck(checked_at="2027-01-01T00:00:00Z", digest=ROOT)  # type: ignore[call-arg]


def test_verify_anchor_binding_is_false_for_an_uncheckable_algorithm() -> None:
    """False, not True: an unverifiable binding is what the check exists to find."""
    assert verify_anchor_binding(_anchor(fixity_alg="sha512"), SEALED_ARTIFACT) is False


def test_verify_anchor_binding_detects_a_changed_artifact() -> None:
    facet = _anchor()
    assert verify_anchor_binding(facet, SEALED_ARTIFACT) is True
    assert verify_anchor_binding(facet, SEALED_ARTIFACT + " ") is False


# ── I-2: references and digests only, never the archived bytes ────────────


def test_digest_is_plain_sha256_with_an_algorithm_prefix() -> None:
    """Deliberately NOT a Merkle leaf.

    The repo carries two incompatible Merkle constructions (RFC 6962 in
    evidence/merkle.py, pairwise-with-odd-padding in trust/novaseal/merkle.py).
    P1 fixity is a single digest over a single artifact and needs no tree, so
    this module uses stdlib sha256 and computes none — sidestepping the
    silent-wrong-root failure entirely. This test pins that choice: if someone
    later adds a domain-separation prefix or a tree here, it fails.
    """
    expected = hashlib.sha256(SEALED_ARTIFACT.encode()).hexdigest()
    assert digest_artifact(SEALED_ARTIFACT) == f"sha256:{expected}"


def test_digest_treats_str_and_bytes_identically() -> None:
    assert digest_artifact(SEALED_ARTIFACT) == digest_artifact(
        SEALED_ARTIFACT.encode()
    )


def test_preservation_imports_no_merkle_construction() -> None:
    """A structural guard against the two-construction trap.

    If a later slice needs a tree it must import one construction deliberately
    and say which in a comment — not inherit one by accident through this
    module.
    """
    source = (
        REPO_ROOT / "src" / "novafabric" / "preservation" / "anchor.py"
    ).read_text()
    imports = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "merkle" in line
    ]
    assert imports == [], (
        "preservation/anchor.py must not import a merkle module without a "
        f"deliberate choice of construction (see the module docstring): {imports}"
    )


def test_raw_bytes_are_rejected_where_a_reference_belongs() -> None:
    """Rejected, not hashed on the caller's behalf.

    Hashing here would make it effortless to hand this module the archived
    object and have it quietly do the right-looking thing.
    """
    with pytest.raises(PayloadCaptureError, match="not raw bytes"):
        Fixity(digest=b"\x00\x01raw archived bytes")  # type: ignore[arg-type]


def test_an_inlined_document_is_rejected_as_a_payload() -> None:
    with pytest.raises(PayloadCaptureError, match="reference limit"):
        provenance_event("sealed", "2026-01-01", agent_ref="x" * (MAX_REF_LENGTH + 1))


def test_a_malformed_digest_fails_loudly_at_construction() -> None:
    """Loudly now, rather than failing to match in 2040 during an audit."""
    for bad in ("sha256:ABCD", "deadbeef", "sha256:" + "f" * 63):
        with pytest.raises(InvalidDigestError):
            _anchor(fixity_digest=bad)


def test_a_locator_is_not_accepted_where_content_identity_is_required() -> None:
    """A URI names where something lives, not what it is.

    Over a 30-year window the thing it names being replaced underneath the
    capsule is not hypothetical, so a root must bind by digest.
    """
    with pytest.raises(InvalidDigestError, match="not a locator"):
        _anchor(original_root="https://archive.example.org/capsules/1")


def test_an_agent_may_be_named_by_locator_or_by_digest() -> None:
    assert provenance_event("sealed", "2026-01-01", agent_ref=AGENT).agent_ref == AGENT
    assert (
        provenance_event(
            "sealed", "2026-01-01", agent_ref="https://example.org/a"
        ).agent_ref
        == "https://example.org/a"
    )


def test_scan_for_payloads_accepts_references_and_rejects_bytes() -> None:
    scan_for_payloads([ROOT, "https://example.org/a"])
    with pytest.raises(PayloadCaptureError):
        scan_for_payloads([ROOT, b"weights"])


def test_the_archived_bytes_never_appear_in_the_serialised_facet() -> None:
    facet = check_fixity(
        _anchor(), SEALED_ARTIFACT, checked_at="2027-01-01T00:00:00Z", agent_ref=AGENT
    )
    dumped = facet.model_dump_json()
    assert SEALED_ARTIFACT not in dumped
    assert ROOT in dumped


def test_errors_do_not_subclass_value_error() -> None:
    """A caller catching preservation failures must not also swallow coercion bugs."""
    assert issubclass(PreservationError, Exception)
    assert not issubclass(PreservationError, ValueError)


# ── I-3: fail-open ────────────────────────────────────────────────────────


def test_an_empty_event_name_is_rejected_rather_than_recorded_blank() -> None:
    with pytest.raises(Exception, match="event must be non-empty"):
        provenance_event("   ", "2026-01-01")


def test_extra_fields_survive_so_later_slices_need_no_schema_break() -> None:
    """P2-P5 append `format_migration_chain`, `ltv_renewal_chain`, and the rest.

    `extra="allow"` is what lets them land additively; if it were ever
    tightened, every later slice would become a schema break.
    """
    facet = PreservationFacet.model_validate(
        {
            "preservation_id": "p",
            "original_root": ROOT,
            "fixity": {"alg": "sha256", "digest": ROOT},
            "format_migration_chain": [{"from_version": "run-capsule@0.2.0"}],
        }
    )
    assert facet.model_dump()["format_migration_chain"]
