"""Slice A0 — EnergyReceipt model + honest-degradation builders (ADR-0093).

The core invariant under test: *measured-or-declared-unknown, never fabricated.*
A receipt may carry a measured number, an honestly-labelled declared estimate,
or nothing (null) — but it must never present an estimate as a measurement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from novafabric.capsule.ulid_util import new_ulid
from novafabric.energy import (
    ActionRef,
    Confidence,
    EnergyReceipt,
    MeasurementSource,
    declared_receipt,
    unavailable_receipt,
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "energy-receipt.schema.json"
)


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _action_ref() -> ActionRef:
    return ActionRef(kind="model_call", id=new_ulid())


# --- degrade-safe path: nothing measurable -----------------------------------


def test_unavailable_receipt_is_null_and_unknown(schema_validator):
    r = unavailable_receipt(
        action_ref=_action_ref(),
        run_id=new_ulid(),
        capsule_id="sha256:" + "a" * 64,
        node_id="n1",
        counters_available=(),
    )
    assert r.measured_joules is None
    assert r.confidence is Confidence.UNKNOWN
    assert r.measurement_source is MeasurementSource.UNAVAILABLE
    assert r.carbon_g_co2e is None
    # the receipt must be a valid instance of the published schema
    schema_validator.validate(r.to_record())


# --- declared estimate: labelled, never "measured" ---------------------------


def test_declared_receipt_is_declared_not_measured(schema_validator):
    r = declared_receipt(
        action_ref=_action_ref(),
        run_id=new_ulid(),
        capsule_id="sha256:" + "b" * 64,
        node_id="laptop",
        declared_joules=1234.5,
        basis="openai gpt-4 price-table energy estimate",
    )
    assert r.confidence is Confidence.DECLARED
    assert r.measurement_source is MeasurementSource.DECLARED_MODEL_TABLE
    assert r.measured_joules == 1234.5
    # a declared figure is NOT a measurement, so no counter is claimed
    assert r.hardware.counters_available == []
    schema_validator.validate(r.to_record())


# --- payload hash binds content, excludes the (mutable) seal block ------------


def test_payload_hash_excludes_seal_and_is_stable():
    r = unavailable_receipt(
        action_ref=ActionRef(kind="tool_call", id=new_ulid()),
        run_id=new_ulid(),
        capsule_id="sha256:" + "c" * 64,
        node_id="n1",
        counters_available=(),
    )
    h1 = r.compute_payload_hash()
    # mutating the seal block must NOT change the payload hash
    sealed = r.model_copy(update={"seal": {"signed": True, "merkle_leaf_hash": None}})
    assert sealed.compute_payload_hash() == h1
    assert h1.startswith("sha256:") and len(h1) == len("sha256:") + 64


# --- forgery guard: a "measured" receipt with no counter is inconsistent ------


def test_consistency_errors_flag_measured_without_counters():
    forged = EnergyReceipt(
        receipt_id=new_ulid(),
        action_ref=_action_ref(),
        run_id=new_ulid(),
        capsule_id="sha256:" + "d" * 64,
        measured_joules=999.0,
        measurement_source=MeasurementSource.NVML,
        measurement_scope="per_process",
        confidence=Confidence.MEASURED,
        attribution_method="direct_counter",
        carbon_g_co2e=None,
        grid_intensity_g_per_kwh=None,
        carbon_accounting_mode="unknown",
        hardware={"node_id": "n1", "counters_available": []},
        payload_hash="sha256:" + "0" * 64,
        generated_at="2026-06-19T00:00:00+00:00",
    )
    errors = forged.consistency_errors()
    assert any("counter" in e.lower() for e in errors)


def _measured(**overrides) -> EnergyReceipt:
    base = dict(
        receipt_id=new_ulid(),
        action_ref=_action_ref(),
        run_id=new_ulid(),
        capsule_id="sha256:" + "1" * 64,
        measured_joules=5.0,
        measurement_source=MeasurementSource.RAPL_PKG,
        measurement_scope="per_package",
        confidence=Confidence.APPORTIONED,
        attribution_method="time_share",
        carbon_g_co2e=None,
        grid_intensity_g_per_kwh=None,
        carbon_accounting_mode="unknown",
        hardware={"node_id": "n1", "counters_available": ["rapl_pkg"]},
        payload_hash="sha256:" + "0" * 64,
        generated_at="2026-06-19T00:00:00+00:00",
    )
    base.update(overrides)
    return EnergyReceipt(**base)


def test_consistency_errors_flag_declared_source_without_declared_confidence():
    r = _measured(
        measurement_source=MeasurementSource.DECLARED_TDP,
        confidence=Confidence.MEASURED,
        hardware={"node_id": "n1", "counters_available": ["rapl_pkg"]},
    )
    assert any("declared" in e.lower() for e in r.consistency_errors())


def test_consistency_errors_flag_unavailable_with_joules():
    r = _measured(
        measurement_source=MeasurementSource.UNAVAILABLE,
        confidence=Confidence.UNKNOWN,
        measured_joules=7.0,
    )
    assert any("unavailable" in e.lower() for e in r.consistency_errors())


def test_consistency_errors_flag_carbon_without_energy():
    r = _measured(
        confidence=Confidence.UNKNOWN,
        measured_joules=None,
        carbon_g_co2e=3.0,
    )
    msgs = " ".join(r.consistency_errors()).lower()
    assert "carbon" in msgs and "measured_joules" in msgs


def test_declared_receipt_rejects_non_declared_source():
    with pytest.raises(ValueError, match="declared source"):
        declared_receipt(
            action_ref=_action_ref(),
            run_id=new_ulid(),
            capsule_id="sha256:" + "2" * 64,
            node_id="n1",
            declared_joules=1.0,
            source=MeasurementSource.NVML,
        )


def test_consistency_errors_empty_for_honest_receipts():
    declared = declared_receipt(
        action_ref=_action_ref(),
        run_id=new_ulid(),
        capsule_id="sha256:" + "e" * 64,
        node_id="laptop",
        declared_joules=10.0,
        basis="table",
    )
    unavailable = unavailable_receipt(
        action_ref=_action_ref(),
        run_id=new_ulid(),
        capsule_id="sha256:" + "f" * 64,
        node_id="n1",
        counters_available=(),
    )
    assert declared.consistency_errors() == []
    assert unavailable.consistency_errors() == []
