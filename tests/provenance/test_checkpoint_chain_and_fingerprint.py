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

"""ADR-0152 P2 — checkpoint chain + weight-fingerprint pin (NF-202, NF-206).

Organised around the three things a reviewer has to be convinced of:

1. **The hash construction is a chain, not a tree.** The repo carries two
   mutually incompatible Merkle modules; mixing them yields a wrong root that
   still looks valid. The bit-identity test below pins the construction to raw
   ``hashlib.sha256`` over a documented preimage so a tree cannot creep in later.
2. **A green chain walk is not proof nothing was dropped.** Tail truncation
   leaves every remaining link resolving. The separately-sealed head digest is
   the only detector, and there is an explicit test saying so.
3. **A fingerprint is recorded, never resolved.** Absence means unknown, not
   matched; a mismatch never re-pins.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.provenance import (
    MAX_CHAIN_HOPS,
    ChainTooLargeError,
    CheckpointHop,
    CyclicCheckpointChainError,
    DuplicateCheckpointError,
    ModelProvenanceError,
    ModelProvenanceFacet,
    UnresolvedParentError,
    WeightCaptureError,
    attach_facet,
    build_chain,
    build_facet,
    chain_head,
    check_fingerprint,
    digest_ref,
    facet_from_capsule,
    hop_preimage,
    pin_fingerprint,
    record_fingerprint_check,
    verify_chain,
    verify_chain_binding,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPSULE_SCHEMA = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE_CAPSULE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)

BASE = digest_ref("base-8b-weights-manifest")
SFT = digest_ref("sft-v3-weights-manifest")
RLHF = digest_ref("rlhf-v3-weights-manifest")
SIDE = digest_ref("dpo-v3-weights-manifest")
MERGED = digest_ref("merged-v3-weights-manifest")
MISSING = digest_ref("a-checkpoint-nobody-published")


def _hop(stage: str, cid: str, digest: str, parent: Any = None) -> CheckpointHop:
    return CheckpointHop(
        stage=stage,  # type: ignore[arg-type]
        checkpoint_id=cid,
        checkpoint_digest=digest,
        parent=parent,
    )


def _linear() -> list[CheckpointHop]:
    """The ADR's canonical ancestry: base → sft → rlhf."""
    return [
        _hop("base", "base-8b", BASE),
        _hop("sft", "sft-v3", SFT, BASE),
        _hop("rlhf", "rlhf-v3", RLHF, SFT),
    ]


def _diamond() -> list[CheckpointHop]:
    """base → {sft, dpo} → merged. Convergent, and emphatically not a cycle.

    This is the shape ADR-0152 D2's own `merged` stage requires: a merge has two
    ancestors. A verifier that flagged it as cyclic would reject the very
    ancestry the ADR names.
    """
    return [
        _hop("base", "base-8b", BASE),
        _hop("sft", "sft-v3", SFT, BASE),
        _hop("dpo", "dpo-v3", SIDE, BASE),
        _hop("merged", "merged-v3", MERGED, [SFT, SIDE]),
    ]


# ── Chain walk: valid ancestries (NF-202) ─────────────────────────────────


def test_linear_chain_walks_and_orders_parents_before_children() -> None:
    ordered = build_chain(_linear())
    digests = [h.checkpoint_digest for h in ordered]
    assert digests.index(BASE) < digests.index(SFT) < digests.index(RLHF)


def test_chain_order_is_independent_of_the_order_the_caller_supplied() -> None:
    """The facet is hashed into the seal; a stable order keeps the hash comparable."""
    forward = build_chain(_linear())
    shuffled = build_chain(list(reversed(_linear())))
    assert [h.checkpoint_id for h in forward] == [h.checkpoint_id for h in shuffled]


def test_valid_chain_reports_all_three_flags(  # NF-202 acceptance criterion
) -> None:
    flags = verify_chain(_linear())
    assert (flags.chain_walk_ok, flags.acyclic, flags.no_broken_parent) == (
        True,
        True,
        True,
    )


def test_verify_chain_leaves_unperformed_checks_absent_not_false() -> None:
    """This function checks the chain and nothing else (I-4)."""
    flags = verify_chain(_linear())
    assert flags.fingerprint_pinned is None
    assert flags.signature_ok is None
    assert flags.sealed_into_root is None


def test_empty_chain_is_not_reported_as_a_successful_walk() -> None:
    """"Vacuously fine" and "checked and fine" must not serialise identically."""
    flags = verify_chain([])
    assert flags.chain_walk_ok is None
    assert flags.acyclic is None


def test_a_diamond_is_valid_ancestry_and_is_not_flagged_as_a_cycle() -> None:
    ordered = build_chain(_diamond())
    digests = [h.checkpoint_digest for h in ordered]
    assert digests.index(MERGED) == len(digests) - 1
    assert digests.index(BASE) < digests.index(SFT) < digests.index(MERGED)
    assert digests.index(BASE) < digests.index(SIDE) < digests.index(MERGED)
    assert verify_chain(_diamond()).acyclic is True


def test_merge_hop_may_declare_several_parents() -> None:
    """ADR-0152 D2 writes `parent` as a scalar but lists a `merged` stage.

    Resolved toward the shape that can represent the stages the ADR itself names
    — see the `parent` field comment in the module.
    """
    hops = build_chain(_diamond())
    merged = next(h for h in hops if h.stage == "merged")
    assert set(merged.parent_digests) == {SFT, SIDE}


def test_scalar_parent_wire_shape_survives_a_round_trip() -> None:
    """The spec's single-string form stays valid input *and* output."""
    hop = _hop("sft", "sft-v3", SFT, BASE)
    assert hop.model_dump(exclude_none=True)["parent"] == BASE


# ── Chain walk: acyclicity (NF-202) ───────────────────────────────────────


def test_self_cycle_is_rejected() -> None:
    """A checkpoint that is its own parent is a cycle of length one."""
    with pytest.raises(CyclicCheckpointChainError) as exc:
        build_chain([_hop("sft", "sft-v3", SFT, SFT)])
    assert exc.value.cycle == [SFT, SFT]


def test_two_cycle_is_rejected_and_names_both_hops() -> None:
    with pytest.raises(CyclicCheckpointChainError) as exc:
        build_chain(
            [
                _hop("sft", "sft-v3", SFT, RLHF),
                _hop("rlhf", "rlhf-v3", RLHF, SFT),
            ]
        )
    assert set(exc.value.cycle) == {SFT, RLHF}


def test_longer_cycle_is_rejected_and_the_reported_path_closes() -> None:
    """"This chain has a cycle" is not actionable; the concrete path is."""
    with pytest.raises(CyclicCheckpointChainError) as exc:
        build_chain(
            [
                _hop("base", "base-8b", BASE, RLHF),
                _hop("sft", "sft-v3", SFT, BASE),
                _hop("rlhf", "rlhf-v3", RLHF, SFT),
            ]
        )
    cycle = exc.value.cycle
    assert cycle[0] == cycle[-1], "a reported cycle must return to its entry hop"
    assert set(cycle) == {BASE, SFT, RLHF}


def test_a_cycle_still_reports_that_every_parent_resolved() -> None:
    """That is precisely how the walk got far enough to find the cycle."""
    flags = verify_chain(
        [_hop("sft", "sft-v3", SFT, RLHF), _hop("rlhf", "rlhf-v3", RLHF, SFT)]
    )
    assert (flags.chain_walk_ok, flags.acyclic, flags.no_broken_parent) == (
        False,
        False,
        True,
    )


def test_a_cyclic_chain_never_reaches_a_sealed_facet() -> None:
    """Fail-open covers *missing* material, not *incoherent* material."""
    with pytest.raises(CyclicCheckpointChainError):
        build_facet("m", checkpoint_chain=[_hop("sft", "s", SFT, SFT)])


# ── Chain walk: parent resolution (NF-202) ────────────────────────────────


def test_unresolved_parent_is_raised_not_dropped() -> None:
    """Dropping the dangling edge would make an incomplete ancestry look complete."""
    with pytest.raises(UnresolvedParentError) as exc:
        build_chain([_hop("base", "base-8b", BASE), _hop("sft", "sft-v3", SFT, MISSING)])
    assert exc.value.checkpoint_id == "sft-v3"
    assert exc.value.parent == MISSING


def test_unresolved_parent_sets_no_broken_parent_false_and_leaves_acyclic_unknown() -> (
    None
):
    """The walk never established acyclicity, so it must not claim to have."""
    flags = verify_chain([_hop("sft", "sft-v3", SFT, MISSING)])
    assert flags.no_broken_parent is False
    assert flags.chain_walk_ok is False
    assert flags.acyclic is None


def test_duplicate_checkpoint_digest_is_ambiguous_and_rejected() -> None:
    with pytest.raises(DuplicateCheckpointError):
        build_chain([_hop("base", "a", BASE), _hop("sft", "b", BASE)])


def test_duplicate_digests_leave_both_structural_flags_unknown() -> None:
    flags = verify_chain([_hop("base", "a", BASE), _hop("sft", "b", BASE)])
    assert flags.chain_walk_ok is False
    assert flags.acyclic is None
    assert flags.no_broken_parent is None


def test_chain_length_is_bounded_for_offline_verifiers() -> None:
    """This runs inside verifiers walking capsules they did not produce."""
    hops = [
        _hop("base", f"c{i}", digest_ref(f"c{i}")) for i in range(MAX_CHAIN_HOPS + 1)
    ]
    with pytest.raises(ChainTooLargeError):
        build_chain(hops)


def test_a_deep_chain_does_not_exhaust_the_stack() -> None:
    """Kahn's algorithm is iterative; recursion here would be a DoS surface."""
    digests = [digest_ref(f"deep-{i}") for i in range(3000)]
    hops = [_hop("base", "c0", digests[0])] + [
        _hop("sft", f"c{i}", digests[i], digests[i - 1]) for i in range(1, 3000)
    ]
    assert len(build_chain(hops)) == 3000


def test_chain_errors_share_one_catchable_base() -> None:
    assert issubclass(CyclicCheckpointChainError, ModelProvenanceError)
    assert issubclass(UnresolvedParentError, ModelProvenanceError)
    assert not issubclass(ModelProvenanceError, ValueError)


# ── Hash construction: a chain, never a tree ──────────────────────────────


def test_chain_head_is_bit_identical_to_raw_sha256_over_the_documented_preimage() -> (
    None
):
    """The guard that stops a Merkle tree creeping in later.

    This repo has two mutually incompatible Merkle constructions
    (`evidence/merkle.py`, RFC 6962; `trust/novaseal/merkle.py`, pairwise with
    odd-duplicate padding). Mixing leaves from one with the other's combiner
    silently yields a wrong root that still looks like a root. P2 uses neither:
    ancestry is ordered, so a folded head digest already commits to the whole
    prefix. Recomputing the fold here by hand, with nothing but `hashlib`,
    freezes that choice — swap in either tree module and this fails.
    """
    hops = build_chain(_linear())

    expected: str | None = None
    for hop in hops:
        payload: dict[str, Any] = {
            "hop": json.loads(hop.model_dump_json(exclude_none=True))
        }
        if expected is not None:
            payload["prev_head"] = expected
        preimage = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        expected = f"sha256:{hashlib.sha256(preimage.encode()).hexdigest()}"

    assert chain_head(hops) == expected


def test_hop_preimage_is_canonical_and_omits_absent_fields() -> None:
    """An absent optional field must hash as absent, not as `null`.

    Otherwise the head would depend on which optional fields a given NovaFabric
    version happened to know about, and a facet round-tripped through JSON would
    stop verifying.
    """
    preimage = hop_preimage(_hop("base", "base-8b", BASE), None)
    assert "null" not in preimage
    assert "attestation_ref" not in preimage
    assert "prev_head" not in preimage
    assert preimage == json.dumps(
        json.loads(preimage), separators=(",", ":"), sort_keys=True
    )


def test_genesis_hop_omits_prev_head_rather_than_using_a_sentinel() -> None:
    """A fixed stand-in for "no predecessor" looks real to any tool lacking it."""
    assert "prev_head" not in hop_preimage(_hop("base", "b", BASE), None)
    assert "prev_head" in hop_preimage(_hop("sft", "s", SFT, BASE), chain_head(_linear()))


def test_empty_chain_has_no_head_rather_than_the_digest_of_nothing() -> None:
    assert chain_head([]) is None


def test_head_changes_when_any_hop_changes() -> None:
    tampered = build_chain(
        [
            _hop("base", "base-8b", BASE),
            _hop("continued_pretrain", "sft-v3", SFT, BASE),  # stage edited
            _hop("rlhf", "rlhf-v3", RLHF, SFT),
        ]
    )
    assert chain_head(tampered) != chain_head(build_chain(_linear()))


# ── Tail truncation: the chain alone cannot see it ────────────────────────


def test_truncating_the_tail_leaves_the_chain_walk_green() -> None:
    """The whole reason a head digest exists. Read this test before trusting a walk.

    Drop the last hop and every remaining parent link still resolves, so
    `verify_chain` reports a clean ancestry. Nobody may read that as evidence
    that nothing was removed.
    """
    truncated = _linear()[:-1]
    flags = verify_chain(truncated)
    assert (flags.chain_walk_ok, flags.acyclic, flags.no_broken_parent) == (
        True,
        True,
        True,
    )


def test_the_sealed_head_digest_is_what_detects_the_truncation() -> None:
    sealed = build_facet("m", checkpoint_chain=_linear())
    assert verify_chain_binding(sealed) is True

    # The custodian hands back a chain one hop shorter than the sealed one.
    assert verify_chain_binding(sealed, _linear()[:-1]) is False


def test_the_sealed_head_also_detects_a_dropped_middle_hop() -> None:
    sealed = build_facet("m", checkpoint_chain=_diamond())
    without_middle = [h for h in _diamond() if h.checkpoint_digest != SIDE]
    assert verify_chain_binding(sealed, without_middle) is False


def test_a_facet_with_no_head_does_not_pass_a_binding_check() -> None:
    """An unbound facet is the case a binding check exists to surface."""
    facet = ModelProvenanceFacet.model_validate(
        {
            "model_id": "m",
            "checkpoint_chain": [h.model_dump(exclude_none=True) for h in _linear()],
        }
    )
    assert facet.checkpoint_chain_head is None
    assert verify_chain_binding(facet) is False


def test_the_head_survives_a_capsule_round_trip() -> None:
    """`attach_facet` dumps with exclude_none — the same exclusion the head hashes."""
    facet = build_facet("m", checkpoint_chain=_linear())
    read_back = facet_from_capsule(attach_facet({"run_id": "r"}, facet))
    assert read_back is not None
    assert verify_chain_binding(read_back) is True


# ── Weight-fingerprint pin (NF-206) ───────────────────────────────────────


def test_pin_stores_a_digest_and_the_scheme_that_produced_it() -> None:
    pin = pin_fingerprint(RLHF, scheme="model_signing_manifest")
    assert pin.fingerprint_digest == RLHF
    assert pin.fingerprint_scheme == "model_signing_manifest"


def test_raw_weight_bytes_are_refused_not_silently_hashed() -> None:
    """Hashing weights here would make smuggling a checkpoint in effortless.

    The producer digests its own weights; NovaFabric never sees them (I-2, spec
    requirement 10: "capture of raw weights is prohibited").
    """
    with pytest.raises(WeightCaptureError, match="not raw bytes"):
        pin_fingerprint(b"\x80\x02\x95PYTORCH-WEIGHTS")  # type: ignore[arg-type]


def test_the_module_offers_no_weight_hashing_helper() -> None:
    """The absence is the design, not an oversight — see WeightCaptureError."""
    import novafabric.provenance as provenance

    assert "digest_weights" not in provenance.__all__


def test_a_pinned_facet_carries_no_weight_bytes() -> None:
    facet = build_facet("m", weight_fingerprint=pin_fingerprint(RLHF))
    assert "PYTORCH" not in facet.model_dump_json()
    assert facet.model_dump_json().count(RLHF) == 1


# ── fingerprint_mismatch surfacing (NF-206) ───────────────────────────────


def test_matching_fingerprint_is_reported_as_a_match() -> None:
    facet = build_facet("m", weight_fingerprint=pin_fingerprint(RLHF))
    check = check_fingerprint(facet, RLHF)
    assert check.status == "match"
    assert check.mismatch is False


def test_differing_fingerprint_is_surfaced_as_a_mismatch_with_both_digests() -> None:
    """ADR-0152 D2: replay surfaces `fingerprint_mismatch` [E1-005]."""
    facet = build_facet("m", weight_fingerprint=pin_fingerprint(RLHF))
    check = check_fingerprint(facet, SFT)
    assert check.status == "mismatch"
    assert check.mismatch is True
    assert (check.pinned_digest, check.observed_digest) == (RLHF, SFT)


def test_an_unpinned_facet_is_unknown_and_never_a_match() -> None:
    """Absence of a fingerprint means unpinned, never matched."""
    facet = build_facet("m", model_signing_ref=digest_ref("manifest"))
    check = check_fingerprint(facet, RLHF)
    assert check.status == "unpinned"
    assert check.mismatch is False
    assert check.pinned_digest is None


def test_a_replay_that_reports_nothing_is_unpinned_not_a_match() -> None:
    """A comparison with one side missing is not agreement."""
    facet = build_facet("m", weight_fingerprint=pin_fingerprint(RLHF))
    assert check_fingerprint(facet, None).status == "unpinned"


def test_a_malformed_observed_digest_is_unknown_rather_than_fatal() -> None:
    """A corrupt observation must not block the replay path (I-3)."""
    facet = build_facet("m", weight_fingerprint=pin_fingerprint(RLHF))
    assert check_fingerprint(facet, "not-a-digest").status == "unpinned"


def test_a_mismatch_is_recorded_and_never_silently_re_pins() -> None:
    """Re-pinning would erase the only evidence of the spoof NF-206 exists to catch."""
    facet = build_facet("m", weight_fingerprint=pin_fingerprint(RLHF))
    recorded = record_fingerprint_check(facet, check_fingerprint(facet, SFT))

    assert recorded.fingerprint_check is not None
    assert recorded.fingerprint_check.status == "mismatch"
    assert recorded.weight_fingerprint is not None
    assert recorded.weight_fingerprint.fingerprint_digest == RLHF, "the pin must stand"
    assert facet.fingerprint_check is None, "the input facet is not mutated"


def test_a_recorded_mismatch_survives_a_capsule_round_trip() -> None:
    facet = build_facet("m", weight_fingerprint=pin_fingerprint(RLHF))
    recorded = record_fingerprint_check(facet, check_fingerprint(facet, SFT))
    block = attach_facet({"run_id": "r"}, recorded)["facets"]["model_provenance"]
    assert block["fingerprint_check"]["status"] == "mismatch"


def test_an_unchecked_facet_has_no_fingerprint_check_key_at_all() -> None:
    """Absent means no replay reported back — which is not a pass (I-4)."""
    facet = build_facet("m", weight_fingerprint=pin_fingerprint(RLHF))
    block = attach_facet({"run_id": "r"}, facet)["facets"]["model_provenance"]
    assert "fingerprint_check" not in block


# ── I-1: additive over P1 ─────────────────────────────────────────────────


def test_a_p1_era_facet_still_parses_and_gains_no_chain_claims() -> None:
    """P2 fields are additive; a facet written before them reads unchanged."""
    facet = ModelProvenanceFacet.model_validate(
        {"model_id": "m", "model_signing_ref": digest_ref("manifest")}
    )
    assert facet.checkpoint_chain is None, "absent ancestry, not an empty one"
    assert facet.checkpoint_chain_head is None
    assert facet.weight_fingerprint is None


def test_a_facet_without_a_chain_writes_no_chain_key_at_all() -> None:
    """`[]` would assert "descends from nothing"; absence says "not recorded"."""
    block = attach_facet(
        {"run_id": "r"}, build_facet("m", weight_fingerprint=pin_fingerprint(RLHF))
    )["facets"]["model_provenance"]
    assert "checkpoint_chain" not in block
    assert "checkpoint_chain_head" not in block


def test_a_capsule_with_no_provenance_material_is_still_untouched() -> None:
    capsule: dict[str, Any] = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(capsule, build_facet("m")) == capsule


def test_a_chain_alone_counts_as_provenance_material() -> None:
    assert build_facet("m", checkpoint_chain=_linear()).has_material is True


def test_a_pin_alone_counts_as_provenance_material() -> None:
    assert build_facet("m", weight_fingerprint=pin_fingerprint(RLHF)).has_material is True


# ── Real-schema validation ────────────────────────────────────────────────


def test_a_p2_capsule_validates_against_the_real_run_capsule_schema() -> None:
    """The boundary that five earlier facet slices missed (ADR-0196 D4).

    Facet tests operate on plain dicts, so nothing validates a facet-bearing
    capsule against `schemas/run-capsule.schema.json` unless a test does it
    explicitly. `facets` is `additionalProperties: false`, so this is the check
    that a P2 capsule is actually loadable.
    """
    capsule = json.loads(BASELINE_CAPSULE.read_text(encoding="utf-8"))
    schema = json.loads(CAPSULE_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(capsule, schema)

    facet = build_facet(
        "acme/planner-8b-instruct@2026-06",
        checkpoint_chain=_diamond(),
        weight_fingerprint=pin_fingerprint(MERGED, bound_root=digest_ref("root")),
        verified=verify_chain(_diamond()),
    )
    out = attach_facet(capsule, record_fingerprint_check(
        facet, check_fingerprint(facet, MERGED)
    ))

    jsonschema.validate(out, schema)
    assert out["facets"]["model_provenance"]["checkpoint_chain_head"]
    assert out["facets"]["model_provenance"]["verified"]["acyclic"] is True
