"""NF-323 reproducibility receipt (ADR-0164 P2) — record-only, never fabricated.

Acceptance criteria (NF-321-330 spec §3 item 7, §6 "Success (NF-323)"):

- every present component seals under one ``bound_root``; any change re-roots;
- absent components are *named* in ``receipt_incomplete``, never invented;
- verification is pure hashing: no re-execution, ``reproducible_in_fact`` null;
- payloads (bytes / over-long strings) never reach a digest field (I-2);
- attach is additive and a receipt with no material writes nothing (I-1, I-3).
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novafabric.science import (
    InvalidSeedError,
    ReproducibilityReceipt,
    ReproducibilityReceiptError,
    ScienceNode,
    attach_facet,
    attach_receipt,
    build_facet,
    build_receipt,
    digest_node,
    facet_from_capsule,
    receipt_from_capsule,
    verify_receipt,
)
from novafabric.science.provenance import (
    FACET_NAME,
    InvalidNodeDigestError,
    PayloadCaptureError,
)
from novafabric.science.reproducibility import (
    MAX_SEEDS,
    RECEIPT_KEY,
    REQUIRED_COMPONENTS,
    compute_bound_root,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "science-provenance"

ENV = digest_node("uv.lock v1")
DATA = digest_node("dataset v1")
CODE = digest_node("analysis.py v1")
WORKFLOW = digest_node("workflow.cwl v1")
ROOT = digest_node("sealed capsule root")


def _full(**kw: Any) -> ReproducibilityReceipt:
    args: dict[str, Any] = {
        "environment_digest": ENV,
        "seeds": [1337, 42],
        "data_digest": DATA,
        "code_digest": CODE,
        "determinism_class": "statistical",
    }
    args.update(kw)
    return build_receipt(**args)


# ── Success ────────────────────────────────────────────────────────────────


def test_full_receipt_is_complete_and_verifies() -> None:
    receipt = _full(workflow_digest=WORKFLOW)
    assert receipt.receipt_incomplete == []
    result = verify_receipt(receipt)
    assert result.ok and result.sealed_into_root and result.complete
    assert result.determinism_class == "statistical"


def test_verification_never_claims_reexecution_or_reproducibility() -> None:
    body = verify_receipt(_full()).model_dump(mode="json")
    assert body["re_executed"] is False
    assert body["reproducible_in_fact"] is None


def test_verification_does_not_spawn_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record-only: verifying must never run anything."""

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("verify_receipt tried to execute a process")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert verify_receipt(_full()).ok


def test_root_is_deterministic() -> None:
    assert _full().bound_root == _full().bound_root


@pytest.mark.parametrize(
    "change",
    [
        {"environment_digest": digest_node("other env")},
        {"seeds": [1337, 43]},
        {"seeds": [42, 1337]},  # order matters: seeds are an ordered list
        {"data_digest": digest_node("other data")},
        {"code_digest": digest_node("other code")},
        {"workflow_digest": WORKFLOW},
        {"determinism_class": "bitwise"},
        {"capsule_root": ROOT},
    ],
)
def test_any_component_change_reroots(change: dict[str, Any]) -> None:
    assert _full(**change).bound_root != _full().bound_root


def test_scalar_seed_normalises_to_list() -> None:
    assert build_receipt(seed=7).seeds == [7]


def test_capsule_root_is_bound() -> None:
    receipt = _full(capsule_root=ROOT)
    assert receipt.capsule_root == ROOT
    assert verify_receipt(receipt).ok


# ── Never fabricated: receipt_incomplete ──────────────────────────────────


def test_empty_receipt_names_every_required_component() -> None:
    receipt = build_receipt()
    assert receipt.receipt_incomplete == list(REQUIRED_COMPONENTS)
    assert receipt.environment_digest is None and receipt.seeds == []
    assert receipt.determinism_class == "undeclared"
    assert verify_receipt(receipt).ok
    assert not verify_receipt(receipt).complete


def test_partial_receipt_names_only_missing() -> None:
    receipt = build_receipt(environment_digest=ENV, code_digest=CODE)
    assert receipt.receipt_incomplete == ["seeds", "data_digest"]


def test_workflow_digest_is_optional_not_incomplete() -> None:
    assert "workflow_digest" not in _full().receipt_incomplete


# ── Failure: tampering / dishonest incompleteness ─────────────────────────


def test_tampered_component_fails_verification() -> None:
    body = _full().model_dump(mode="json")
    body["code_digest"] = digest_node("swapped code")
    result = verify_receipt(ReproducibilityReceipt.model_validate(body))
    assert not result.sealed_into_root and not result.ok
    assert result.declared_root != result.expected_root


def test_hidden_incompleteness_fails_verification() -> None:
    body = build_receipt(environment_digest=ENV).model_dump(mode="json")
    body["receipt_incomplete"] = []
    result = verify_receipt(ReproducibilityReceipt.model_validate(body))
    assert result.sealed_into_root
    assert not result.incomplete_declared_correctly and not result.ok


# ── Golden fixtures (valid + invalid) ─────────────────────────────────────


def test_golden_valid_receipt_round_trips_and_verifies() -> None:
    blob = json.loads((FIXTURES / "valid-receipt.json").read_text())
    receipt = ReproducibilityReceipt.model_validate(blob)
    assert verify_receipt(receipt).ok
    assert receipt.model_dump(mode="json", exclude_none=True) == blob


@pytest.mark.parametrize(
    "name", ["invalid-receipt-tampered.json", "invalid-receipt-hidden-incomplete.json"]
)
def test_golden_invalid_receipts_fail_verification(name: str) -> None:
    blob = json.loads((FIXTURES / name).read_text())
    assert not verify_receipt(ReproducibilityReceipt.model_validate(blob)).ok


# ── Input validation / I-2 ────────────────────────────────────────────────


def test_bytes_in_digest_field_is_payload_capture() -> None:
    with pytest.raises(PayloadCaptureError):
        build_receipt(data_digest=b"raw dataset bytes")  # type: ignore[arg-type]


def test_long_string_in_digest_field_is_payload_capture() -> None:
    with pytest.raises(PayloadCaptureError):
        build_receipt(code_digest="x" * 5000)


def test_malformed_digest_rejected() -> None:
    with pytest.raises(InvalidNodeDigestError):
        build_receipt(environment_digest="sha256:ABC")


@pytest.mark.parametrize("bad", [True, 1.5, "7", 2**64, -(2**63) - 1])
def test_bad_seed_rejected(bad: object) -> None:
    with pytest.raises(InvalidSeedError):
        build_receipt(seeds=[bad])  # type: ignore[list-item]


def test_too_many_seeds_rejected() -> None:
    with pytest.raises(InvalidSeedError):
        build_receipt(seeds=list(range(MAX_SEEDS + 1)))


def test_seed_and_seeds_are_exclusive() -> None:
    with pytest.raises(InvalidSeedError):
        build_receipt(seed=1, seeds=[2])


def test_unknown_determinism_class_rejected() -> None:
    with pytest.raises(ValidationError):
        build_receipt(determinism_class="mostly")  # type: ignore[arg-type]


def test_unknown_incomplete_component_rejected() -> None:
    body = build_receipt().model_dump(mode="json")
    body["receipt_incomplete"] = ["vibes"]
    with pytest.raises(ReproducibilityReceiptError):
        ReproducibilityReceipt.model_validate(body)


def test_incomplete_must_be_list() -> None:
    body = build_receipt().model_dump(mode="json")
    body["receipt_incomplete"] = "seeds"
    with pytest.raises(ReproducibilityReceiptError):
        ReproducibilityReceipt.model_validate(body)


def test_incomplete_none_normalises_to_empty() -> None:
    body = _full().model_dump(mode="json")
    body["receipt_incomplete"] = None
    assert ReproducibilityReceipt.model_validate(body).receipt_incomplete == []


def test_seeds_none_normalises_to_empty() -> None:
    body = build_receipt().model_dump(mode="json")
    body["seeds"] = None
    assert ReproducibilityReceipt.model_validate(body).seeds == []


def test_compute_bound_root_ignores_absent_rather_than_hashing_null() -> None:
    kwargs: dict[str, Any] = {
        "environment_digest": None,
        "seeds": [],
        "data_digest": None,
        "code_digest": None,
        "workflow_digest": None,
        "determinism_class": "undeclared",
        "capsule_root": None,
    }
    assert compute_bound_root(**kwargs) == build_receipt().bound_root


# ── Capsule attach / read (I-1, I-3) ──────────────────────────────────────


def test_attach_without_material_leaves_capsule_untouched() -> None:
    capsule = {"run_id": "r1"}
    assert attach_receipt(capsule, build_receipt()) is capsule


def test_attach_is_additive_and_preserves_dag() -> None:
    node = ScienceNode(kind="hypothesis", node_id="H1", node_digest=digest_node("H1"))
    capsule = attach_facet({"run_id": "r1", "facets": {"other": {"x": 1}}}, build_facet([node]))
    before = copy.deepcopy(capsule)
    out = attach_receipt(capsule, _full())
    assert capsule == before  # input not mutated
    assert out["facets"]["other"] == {"x": 1}
    science = out["facets"][FACET_NAME]
    assert science["hypothesis_experiment_result"][0]["node_id"] == "H1"
    assert RECEIPT_KEY in science
    # The P1 facet model still parses with the receipt riding extra="allow".
    facet = facet_from_capsule(out)
    assert facet is not None and facet.has_material


def test_attach_with_only_declared_class_is_material() -> None:
    out = attach_receipt({}, build_receipt(determinism_class="bitwise"))
    assert out["facets"][FACET_NAME][RECEIPT_KEY]["determinism_class"] == "bitwise"


def test_attach_with_only_workflow_is_material() -> None:
    out = attach_receipt({}, build_receipt(workflow_digest=WORKFLOW))
    assert RECEIPT_KEY in out["facets"][FACET_NAME]


def test_read_back_round_trips() -> None:
    receipt = _full()
    assert receipt_from_capsule(attach_receipt({}, receipt)) == receipt


@pytest.mark.parametrize(
    "capsule",
    [
        {},
        {"facets": "nope"},
        {"facets": {}},
        {"facets": {FACET_NAME: "nope"}},
        {"facets": {FACET_NAME: {}}},
        {"facets": {FACET_NAME: {RECEIPT_KEY: "nope"}}},
    ],
)
def test_absent_receipt_reads_as_none(capsule: dict[str, Any]) -> None:
    assert receipt_from_capsule(capsule) is None


def test_non_science_capsule_fixture_unchanged() -> None:
    blob = json.loads((FIXTURES / "valid-non-science-capsule.json").read_text())
    assert receipt_from_capsule(blob) is None
    assert attach_receipt(blob, build_receipt()) == blob
