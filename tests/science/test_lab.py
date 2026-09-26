"""NF-322 lab_experiment + NF-329 instrument_provenance (ADR-0164 P3).

Covers build/verify, every fail-closed verify check, the golden fixtures, and
the I-2 boundary: no telemetry, raw data, or credential can enter either block.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from novafabric.science import (
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
from novafabric.science.lab import (
    INSTRUMENTS_KEY,
    LAB_BOUNDARY,
    LAB_KEY,
    MAX_INSTRUMENTS,
    DuplicateInstrumentError,
    InstrumentRecord,
    InstrumentTelemetryError,
    InvalidCalibrationTimestampError,
    InvalidLabRecordError,
    LabExperiment,
    LabProvenance,
    UnknownLabKindError,
    attach_lab,
    build_instrument_record,
    build_lab_experiment,
    check_no_payload,
    lab_from_capsule,
    lab_from_facet_block,
    verify_lab,
)
from novafabric.science.provenance import FACET_NAME, PayloadCaptureError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "science-provenance"
R1 = digest_node("R1")
D1 = digest_node("D1")
H1 = digest_node("H1")


def _instrument(**over: Any) -> InstrumentRecord:
    kw: dict[str, Any] = {
        "instrument_id": "hplc-7",
        "instrument_class": "hplc",
        "firmware_digest": digest_node("fw"),
        "calibration_ref": digest_node("cal"),
        "calibration_timestamp": "2026-06-30T09:00:00Z",
        "manufacturer_ref": "https://ror.org/000example1",
    }
    kw.update(over)
    return build_instrument_record(**kw)


def _experiment(instruments: list[InstrumentRecord], **over: Any) -> LabExperiment:
    kw: dict[str, Any] = {
        "lab_kind": "self_driving",
        "protocol_ref": digest_node("protocol"),
        "run_id": "sdl-job-1",
        "sim_to_real": "real",
        "outcome_digest": digest_node("outcome"),
        "instrument_refs": [i.record_digest for i in instruments],
        "started_at": "2026-07-15T09:30:00Z",
    }
    kw.update(over)
    return build_lab_experiment(**kw)


def _capsule_with_dag() -> dict[str, Any]:
    nodes = [
        ScienceNode(kind="hypothesis", node_id="H1", node_digest=H1),
        ScienceNode(kind="experiment_design", node_id="D1", node_digest=D1, parent=H1),
        ScienceNode(kind="experiment_run", node_id="R1", node_digest=R1, parent=D1),
    ]
    base = {"run_id": "r1", "created_at": "2026-07-15T10:00:00Z"}
    return attach_facet(base, build_facet(nodes))


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


# ── Build / verify: success ───────────────────────────────────────────────


def test_valid_lab_verifies() -> None:
    inst = [_instrument(), _instrument(instrument_id="xrd-2", firmware_digest=digest_node("x"))]
    lab = LabProvenance(experiment=_experiment(inst), instruments=tuple(inst))
    result = verify_lab(lab)
    assert result.ok
    assert result.experiment_time_source == "lab_experiment.started_at"
    assert result.lineage_node_resolves is None


def test_verification_is_record_only() -> None:
    inst = [_instrument()]
    result = verify_lab(LabProvenance(_experiment(inst), tuple(inst)))
    body = result.model_dump(mode="json")
    assert body["controls_lab"] is False
    assert body["reads_telemetry"] is False
    assert body["verdict"] is None


def test_digests_are_deterministic_and_content_bound() -> None:
    assert _instrument().record_digest == _instrument().record_digest
    assert (
        _instrument().record_digest != _instrument(firmware_digest=digest_node("fw2")).record_digest
    )
    inst = [_instrument()]
    assert _experiment(inst).experiment_digest == _experiment(inst).experiment_digest
    assert (
        _experiment(inst).experiment_digest != _experiment(inst, run_id="other").experiment_digest
    )


def test_boundary_line_names_the_non_goals() -> None:
    assert "never runs experiments" in LAB_BOUNDARY
    assert "never reads instrument telemetry" in LAB_BOUNDARY


# ── Verify: every fail-closed check ───────────────────────────────────────


def test_unresolved_instrument_ref_fails() -> None:
    inst = [_instrument()]
    ghost = digest_node("ghost instrument")
    exp = _experiment(inst, instrument_refs=[inst[0].record_digest, ghost])
    result = verify_lab(LabProvenance(exp, tuple(inst)))
    assert not result.instrument_refs_resolve and not result.ok
    assert result.unresolved_instrument_refs == [ghost]


def test_calibration_after_experiment_fails() -> None:
    inst = [_instrument(calibration_timestamp="2026-07-15T11:00:00+01:00")]  # 10:00Z
    result = verify_lab(LabProvenance(_experiment(inst), tuple(inst)))
    assert not result.calibration_not_after_experiment and not result.ok
    assert result.calibrated_after_experiment == ["hplc-7"]


def test_calibration_equal_to_experiment_time_is_not_after() -> None:
    inst = [_instrument(calibration_timestamp="2026-07-15T09:30:00Z")]
    assert verify_lab(LabProvenance(_experiment(inst), tuple(inst))).ok


def test_experiment_time_falls_back_to_capsule_created_at() -> None:
    inst = [_instrument()]
    exp = _experiment(inst, started_at=None)
    ok = verify_lab(LabProvenance(exp, tuple(inst)), capsule={"created_at": "2026-07-15T10:00:00Z"})
    assert ok.ok and ok.experiment_time_source == "capsule.created_at"
    # A YAML loader may hand back an aware datetime.
    aware = {"created_at": datetime(2026, 7, 15, tzinfo=UTC)}
    assert verify_lab(LabProvenance(exp, tuple(inst)), capsule=aware).ok
    late = verify_lab(
        LabProvenance(exp, tuple(inst)), capsule={"created_at": "2026-06-01T00:00:00Z"}
    )
    assert not late.ok and late.calibrated_after_experiment == ["hplc-7"]


@pytest.mark.parametrize("capsule", [None, {}, {"created_at": "yesterday"}, {"created_at": 5}])
def test_unknown_experiment_time_fails_closed(capsule: dict[str, Any] | None) -> None:
    inst = [_instrument()]
    exp = _experiment(inst, started_at=None)
    result = verify_lab(LabProvenance(exp, tuple(inst)), capsule=capsule)
    assert not result.calibration_not_after_experiment and not result.ok
    assert result.experiment_time is None


def test_no_instruments_needs_no_experiment_time() -> None:
    exp = _experiment([], started_at=None, lab_kind="simulation", sim_to_real="sim")
    assert verify_lab(LabProvenance(exp, ())).ok


def test_tampered_instrument_fails() -> None:
    inst = _instrument()
    forged = inst.model_copy(update={"firmware_digest": digest_node("swapped")})
    result = verify_lab(LabProvenance(_experiment([inst]), (forged,)))
    assert not result.instrument_digests_ok and result.tampered_instruments == ["hplc-7"]
    assert not result.ok


def test_tampered_experiment_fails() -> None:
    inst = [_instrument()]
    exp = _experiment(inst).model_copy(update={"outcome_digest": digest_node("better outcome")})
    result = verify_lab(LabProvenance(exp, tuple(inst)))
    assert not result.experiment_digest_ok and not result.ok


def test_simulation_declared_real_is_inconsistent() -> None:
    exp = _experiment([], lab_kind="simulation", sim_to_real="real")
    result = verify_lab(LabProvenance(exp, ()))
    assert not result.sim_to_real_consistent and not result.ok


@pytest.mark.parametrize("sim", ["sim", "hybrid"])
def test_simulation_sim_or_hybrid_is_consistent(sim: str) -> None:
    exp = _experiment([], lab_kind="simulation", sim_to_real=sim)
    assert verify_lab(LabProvenance(exp, ())).sim_to_real_consistent


def test_unreferenced_instrument_is_informational() -> None:
    used, spare = _instrument(), _instrument(instrument_id="spare")
    result = verify_lab(LabProvenance(_experiment([used]), (used, spare)))
    assert result.ok and result.unreferenced_instruments == ["spare"]


def test_instruments_without_experiment_fail() -> None:
    inst = _instrument()
    result = verify_lab(LabProvenance(None, (inst,)))
    assert not result.experiment_present and not result.ok
    assert result.unreferenced_instruments == ["hplc-7"]


def test_node_ref_resolves_to_experiment_node() -> None:
    capsule = _capsule_with_dag()
    inst = [_instrument()]
    for node in (R1, D1):
        exp = _experiment(inst, node_ref=node)
        assert verify_lab(LabProvenance(exp, tuple(inst)), capsule=capsule).lineage_node_resolves


@pytest.mark.parametrize(
    ("node_ref", "capsule"),
    [
        (H1, "dag"),  # resolves, but to a hypothesis — not an experiment
        (digest_node("nowhere"), "dag"),
        (R1, None),
        (R1, {"facets": {FACET_NAME: {"hypothesis_experiment_result": "not a list"}}}),
    ],
)
def test_node_ref_that_does_not_bind_fails(node_ref: str, capsule: Any) -> None:
    cap = _capsule_with_dag() if capsule == "dag" else capsule
    inst = [_instrument()]
    exp = _experiment(inst, node_ref=node_ref)
    result = verify_lab(LabProvenance(exp, tuple(inst)), capsule=cap)
    assert result.lineage_node_resolves is False and not result.ok


# ── Golden fixtures ───────────────────────────────────────────────────────


def test_golden_valid_fixture_verifies_and_round_trips() -> None:
    capsule = _load("valid-lab-experiment.json")
    lab = lab_from_capsule(capsule)
    assert lab is not None and lab.experiment is not None
    result = verify_lab(lab, capsule=capsule)
    assert result.ok and result.lineage_node_resolves is True
    rebuilt = attach_lab(dict(capsule), lab.experiment, list(lab.instruments))
    assert rebuilt == capsule
    # The P1 facet still parses: lab blocks ride its extra="allow".
    facet = facet_from_capsule(capsule)
    assert facet is not None and facet.has_material


@pytest.mark.parametrize(
    ("name", "flag"),
    [
        ("invalid-lab-unresolved-instrument.json", "instrument_refs_resolve"),
        ("invalid-lab-calibration-after-experiment.json", "calibration_not_after_experiment"),
        ("invalid-lab-tampered-instrument.json", "instrument_digests_ok"),
    ],
)
def test_golden_invalid_fixtures_fail_verification(name: str, flag: str) -> None:
    capsule = _load(name)
    lab = lab_from_capsule(capsule)
    assert lab is not None
    result = verify_lab(lab, capsule=capsule)
    assert not result.ok and getattr(result, flag) is False


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("invalid-lab-unknown-kind.json", UnknownLabKindError),
        ("invalid-lab-telemetry.json", InstrumentTelemetryError),
        ("invalid-lab-malformed-digest.json", InvalidLabRecordError),
    ],
)
def test_golden_invalid_fixtures_are_rejected_on_read(name: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        lab_from_capsule(_load(name))


# ── Field validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["wet_lab", "SIMULATION", "", 3])
def test_unknown_lab_kind_rejected(kind: object) -> None:
    with pytest.raises(UnknownLabKindError):
        _experiment([], lab_kind=kind)


def test_unknown_sim_to_real_rejected() -> None:
    with pytest.raises(UnknownLabKindError):
        _experiment([], sim_to_real="mostly-real")


@pytest.mark.parametrize(
    "bad",
    [
        "sha256:" + "a" * 64 + "\n",  # a `$`-anchored regex would accept this
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "md5:" + "a" * 32,
        42,
    ],
)
def test_malformed_digest_rejected(bad: object) -> None:
    with pytest.raises(InvalidLabRecordError):
        _experiment([], outcome_digest=bad)


def test_bytes_or_oversize_in_digest_is_payload_capture() -> None:
    with pytest.raises(PayloadCaptureError):
        _instrument(calibration_ref=b"calibration record bytes")
    with pytest.raises(PayloadCaptureError):
        _instrument(firmware_digest="x" * 5000)


@pytest.mark.parametrize("bad", ["", "   ", "line\nbreak", "nul\x00", 7])
def test_bad_identifier_rejected(bad: object) -> None:
    with pytest.raises(InvalidLabRecordError):
        _instrument(instrument_id=bad)


def test_oversize_or_bytes_identifier_is_payload_capture() -> None:
    with pytest.raises(PayloadCaptureError):
        _instrument(manufacturer_ref="m" * 300)
    with pytest.raises(PayloadCaptureError):
        _experiment([], run_id=b"job")


@pytest.mark.parametrize(
    "bad", ["2026-07-15T09:30:00", "not a time", "", "2" * 80, 1_700_000_000, None]
)
def test_bad_calibration_timestamp_rejected(bad: object) -> None:
    with pytest.raises(InvalidCalibrationTimestampError):
        _instrument(calibration_timestamp=bad)


def test_naive_datetime_rejected_and_aware_datetime_normalised() -> None:
    with pytest.raises(InvalidCalibrationTimestampError):
        _instrument(calibration_timestamp=datetime(2026, 6, 30))  # noqa: DTZ001
    rec = _instrument(calibration_timestamp=datetime(2026, 6, 30, 9, tzinfo=UTC))
    assert rec.calibration_timestamp == "2026-06-30T09:00:00+00:00"


def test_duplicate_instrument_refs_rejected() -> None:
    inst = _instrument()
    with pytest.raises(DuplicateInstrumentError):
        _experiment([inst, inst])


def test_instrument_refs_must_be_list() -> None:
    with pytest.raises(InvalidLabRecordError):
        _experiment([], instrument_refs="sha256:" + "a" * 64)


def test_instrument_refs_none_normalises_to_empty() -> None:
    body = _experiment([]).model_dump(mode="json")
    body["instrument_refs"] = None
    assert LabExperiment.model_validate(body).instrument_refs == []


# ── I-2 boundary: no telemetry, no raw data, no credentials ───────────────


@pytest.mark.parametrize(
    "key",
    [
        "telemetry",
        "Raw-Data",
        "RAW_DATA",
        "rawData",
        "raw data",
        "instrument_readings",
        "sensor_data",
        "time_series",
        "Waveform",
        "spectra",
        "payload",
        "ｔｅｌｅｍｅｔｒｙ",  # full-width "telemetry"
        "api_key",
        "Password",
        "auth-token",
        "private_key_digest",  # credential markers are never exempt, even as a digest
    ],
)
def test_payload_or_credential_key_rejected_in_instrument(key: str) -> None:
    body = _instrument().model_dump(mode="json")
    body[key] = digest_node("x") if key.endswith("digest") else [0.1, 0.2]
    with pytest.raises(InstrumentTelemetryError) as exc:
        InstrumentRecord.model_validate(body)
    assert "0.1" not in str(exc.value)  # the rule is named, the value is not


def test_payload_key_rejected_in_lab_experiment_nested_extra() -> None:
    body = _experiment([]).model_dump(mode="json")
    body["meta"] = {"notes": {"raw_bytes": "AAAA"}}
    with pytest.raises(InstrumentTelemetryError) as exc:
        LabExperiment.model_validate(body)
    assert exc.value.path == "lab_experiment.meta.notes.raw_bytes"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("telemetry_digest", digest_node("telemetry stream")),
        ("sample_count", 12),
        ("reading_count", 0),
    ],
)
def test_references_and_counts_under_marker_names_are_allowed(key: str, value: object) -> None:
    body = _instrument().model_dump(mode="json")
    body[key] = value
    InstrumentRecord.model_validate(body)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("telemetry_digest", "not a digest"),
        ("sample_count", -1),
        ("sample_count", True),
        ("sample_count", "12"),
    ],
)
def test_marker_names_with_non_reference_values_rejected(key: str, value: object) -> None:
    body = _instrument().model_dump(mode="json")
    body[key] = value
    with pytest.raises(InstrumentTelemetryError):
        InstrumentRecord.model_validate(body)


@pytest.mark.parametrize(
    "value",
    [
        "-----BEGIN RSA PRIVATE KEY-----",
        "Authorization: Bearer abcdef",
        "password=hunter2",
        "data:application/octet-stream;base64,AAAA",
        "sk-ant-" + "a" * 40,
        "x" * 300,
    ],
)
def test_credential_or_payload_value_rejected(value: str) -> None:
    body = _experiment([]).model_dump(mode="json")
    body["note"] = value
    with pytest.raises(InstrumentTelemetryError):
        LabExperiment.model_validate(body)


class _ArrayLike:
    shape = (3,)
    dtype = "float32"


@pytest.mark.parametrize("value", [b"\x00\x01", bytearray(b"x"), _ArrayLike(), object()])
def test_binary_or_array_value_rejected(value: object) -> None:
    with pytest.raises(InstrumentTelemetryError):
        check_no_payload({"note": value})


def test_payload_walk_is_bounded() -> None:
    deep: dict[str, Any] = {}
    cur = deep
    for _ in range(20):
        cur["n"] = {}
        cur = cur["n"]
    with pytest.raises(InstrumentTelemetryError, match="nesting"):
        check_no_payload(deep)
    with pytest.raises(InstrumentTelemetryError, match="cap"):
        check_no_payload({"refs": list(range(MAX_INSTRUMENTS + 1))})
    wide = {f"k{i}": list(range(200)) for i in range(30)}
    with pytest.raises(InstrumentTelemetryError, match="values"):
        check_no_payload(wide)
    with pytest.raises(InstrumentTelemetryError, match="non-string key"):
        check_no_payload({1: "x"})
    with pytest.raises(InstrumentTelemetryError, match="over-long key"):
        check_no_payload({"k" * 300: "x"})


def test_written_self_driving_facet_holds_only_refs_and_digests() -> None:
    """Spec §6 secret boundary: only protocol/outcome/instrument digests are present."""
    capsule = _load("valid-lab-experiment.json")
    science = capsule["facets"][FACET_NAME]
    blob = json.dumps({LAB_KEY: science[LAB_KEY], INSTRUMENTS_KEY: science[INSTRUMENTS_KEY]})
    for marker in ("telemetry", "raw", "payload", "reading", "waveform", "password", "token"):
        assert marker not in blob.lower()
    check_no_payload(science[LAB_KEY])
    for record in science[INSTRUMENTS_KEY]:
        check_no_payload(record)


# ── Attach / read ─────────────────────────────────────────────────────────


def test_attach_nothing_leaves_capsule_untouched() -> None:
    capsule = {"run_id": "r"}
    assert attach_lab(capsule, None, []) is capsule
    assert lab_from_capsule(capsule) is None


def test_attach_is_additive_and_preserves_dag_and_receipt() -> None:
    capsule = _capsule_with_dag()
    capsule = attach_receipt(capsule, build_receipt(code_digest=digest_node("code")))
    before = copy.deepcopy(capsule)
    inst = [_instrument()]
    out = attach_lab(capsule, _experiment(inst), inst)
    assert capsule == before  # input not mutated
    science = out["facets"][FACET_NAME]
    assert (
        science["hypothesis_experiment_result"]
        == before["facets"][FACET_NAME]["hypothesis_experiment_result"]
    )
    receipt = receipt_from_capsule(out)
    assert receipt is not None and verify_receipt(receipt).ok
    lab = lab_from_capsule(out)
    assert lab is not None and verify_lab(lab, capsule=out).ok


def test_attach_instruments_only_and_order_is_deterministic() -> None:
    a, b = _instrument(instrument_id="a"), _instrument(instrument_id="b")
    one = attach_lab({}, None, [a, b])
    two = attach_lab({}, None, [b, a])
    assert one == two and LAB_KEY not in one["facets"][FACET_NAME]


def test_attach_rejects_duplicate_or_too_many_instruments() -> None:
    inst = _instrument()
    with pytest.raises(DuplicateInstrumentError):
        attach_lab({}, None, [inst, inst])
    with pytest.raises(InvalidLabRecordError):
        attach_lab({}, None, [inst] * (MAX_INSTRUMENTS + 1))


@pytest.mark.parametrize(
    "capsule",
    [
        {},
        {"facets": "x"},
        {"facets": {}},
        {"facets": {FACET_NAME: "x"}},
        {"facets": {FACET_NAME: {}}},
    ],
)
def test_absent_lab_reads_as_none(capsule: dict[str, Any]) -> None:
    assert lab_from_capsule(capsule) is None


@pytest.mark.parametrize(
    "science",
    [
        {LAB_KEY: "not a mapping"},
        {INSTRUMENTS_KEY: {"not": "a list"}},
        {INSTRUMENTS_KEY: ["not a mapping"]},
        {INSTRUMENTS_KEY: [{}] * (MAX_INSTRUMENTS + 1)},
    ],
)
def test_malformed_blocks_raise(science: dict[str, Any]) -> None:
    with pytest.raises(InvalidLabRecordError):
        lab_from_facet_block(science)


def test_duplicate_instrument_records_on_read_raise() -> None:
    row = _instrument().model_dump(mode="json")
    with pytest.raises(DuplicateInstrumentError):
        lab_from_facet_block({INSTRUMENTS_KEY: [row, row]})


def test_non_science_capsule_fixture_unchanged() -> None:
    capsule = _load("valid-non-science-capsule.json")
    assert lab_from_capsule(capsule) is None
    assert attach_lab(capsule, None) == capsule
