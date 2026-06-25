"""Slice A2 — capture sacct ConsumedEnergyRaw into receipts (ADR-0093 §A2).

The live ``sacct`` call needs a real Slurm cluster (n1); this is the pure,
testable step: given a captured ``sacct -P --format=ConsumedEnergyRaw,...``
string, parse it and append a measured ``sacct_energy`` receipt to the
capsule's ``energy-receipts.jsonl``. No subprocess, no Slurm.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from novafabric.energy import Confidence, MeasurementSource
from novafabric.energy._attribution import load_receipts
from novafabric.runners import RunnerJobSpec, SlurmRunner
from novafabric.runners._slurm_energy import (
    SACCT_ENERGY_FORMAT,
    _elapsed_to_seconds,
    _parse_elapsed_seconds,
    capture_slurm_energy,
)

_SACCT_OUTPUT = (
    "ConsumedEnergyRaw|Elapsed|NNodes\n"
    "|00:00:30|1\n"  # parent step row, no energy
    "123456|00:00:30|1\n"  # batch step row carries the joules
)


def test_capture_slurm_energy_appends_measured_receipt(tmp_path):
    receipt = capture_slurm_energy(
        capsule_dir=tmp_path,
        job_id="4242",
        sacct_output=_SACCT_OUTPUT,
        run_id="run-abc",
        node_id="n1",
    )

    assert receipt is not None
    assert receipt.measurement_source is MeasurementSource.SACCT_ENERGY
    assert receipt.confidence is Confidence.MEASURED
    assert receipt.measured_joules == 123456.0
    assert receipt.action_ref.id == "4242"
    assert receipt.run_id == "run-abc"
    assert receipt.consistency_errors() == []

    loaded = load_receipts(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].receipt_id == receipt.receipt_id
    assert loaded[0].measurement_source is MeasurementSource.SACCT_ENERGY


def test_capture_slurm_energy_appends_without_clobbering_existing(tmp_path):
    (tmp_path / "energy-receipts.jsonl").write_text("")  # empty existing file
    first = capture_slurm_energy(
        capsule_dir=tmp_path,
        job_id="1",
        sacct_output=_SACCT_OUTPUT,
        run_id="r",
        node_id="n1",
    )
    second = capture_slurm_energy(
        capsule_dir=tmp_path,
        job_id="2",
        sacct_output=_SACCT_OUTPUT,
        run_id="r",
        node_id="n1",
    )
    assert first is not None and second is not None
    loaded = load_receipts(tmp_path)
    assert len(loaded) == 2
    assert {r.action_ref.id for r in loaded} == {"1", "2"}


def test_capture_slurm_energy_returns_none_when_no_energy_column(tmp_path):
    # energy accounting not configured → no ConsumedEnergyRaw value
    out = "Elapsed|NNodes\n00:00:30|1\n"
    receipt = capture_slurm_energy(
        capsule_dir=tmp_path,
        job_id="9",
        sacct_output=out,
        run_id="r",
        node_id="n1",
    )
    assert receipt is None
    # no receipts file should be written when there is nothing to record
    assert not (tmp_path / "energy-receipts.jsonl").exists()


def test_capture_slurm_energy_parses_elapsed_into_wall_clock(tmp_path):
    receipt = capture_slurm_energy(
        capsule_dir=tmp_path,
        job_id="7",
        sacct_output=_SACCT_OUTPUT,
        run_id="r",
        node_id="n1",
    )
    assert receipt is not None
    # 00:00:30 → 30 seconds
    assert receipt.wall_clock_s == 30.0


def test_sacct_energy_format_constant_requests_energy_columns():
    assert "ConsumedEnergyRaw" in SACCT_ENERGY_FORMAT
    assert "Elapsed" in SACCT_ENERGY_FORMAT
    assert "NNodes" in SACCT_ENERGY_FORMAT


def test_elapsed_to_seconds_handles_days_and_hms():
    assert _elapsed_to_seconds("00:00:30") == 30.0
    assert _elapsed_to_seconds("01:02:03") == 3723.0
    assert _elapsed_to_seconds("1-00:00:00") == 86400.0
    assert _elapsed_to_seconds("02:30") == 150.0  # MM:SS form


def test_elapsed_to_seconds_rejects_garbage():
    assert _elapsed_to_seconds("not-a-time") is None
    assert _elapsed_to_seconds("xx-00:00:00") is None  # bad day part
    assert _elapsed_to_seconds("aa:bb:cc") is None  # non-int HMS fields


def test_parse_elapsed_seconds_absent_column_and_short_output():
    assert _parse_elapsed_seconds("only-one-line") is None
    assert _parse_elapsed_seconds("ConsumedEnergyRaw|NNodes\n1|1\n") is None
    # Elapsed column present but every row empty
    assert _parse_elapsed_seconds("Elapsed|NNodes\n|1\n") is None


def _job_spec(tmp_path: Path) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="run-xyz",
        command=["python", "-c", "pass"],
        capsule_dir=capsule_dir,
        env={"NOVAFABRIC_NODE_ID": "n1-cpu"},
    )


def test_runner_capture_energy_writes_receipt_with_mocked_sacct(tmp_path):
    runner = SlurmRunner()
    spec = _job_spec(tmp_path)
    sacct = subprocess.CompletedProcess([], 0, _SACCT_OUTPUT.encode(), b"")

    with patch("subprocess.run", return_value=sacct):
        wrote = runner._capture_energy("555", spec)

    assert wrote is True
    loaded = load_receipts(spec.capsule_dir)
    assert len(loaded) == 1
    assert loaded[0].measurement_source is MeasurementSource.SACCT_ENERGY
    assert loaded[0].confidence is Confidence.MEASURED
    assert loaded[0].action_ref.id == "555"
    assert loaded[0].run_id == "run-xyz"
    assert loaded[0].hardware.node_id == "n1-cpu"


def test_runner_capture_energy_fail_open_on_sacct_error(tmp_path):
    runner = SlurmRunner()
    spec = _job_spec(tmp_path)

    with patch("subprocess.run", side_effect=OSError("sacct missing")):
        wrote = runner._capture_energy("1", spec)

    assert wrote is False
    assert not (spec.capsule_dir / "energy-receipts.jsonl").exists()


def test_runner_capture_energy_no_receipt_when_nonzero_returncode(tmp_path):
    runner = SlurmRunner()
    spec = _job_spec(tmp_path)
    sacct = subprocess.CompletedProcess([], 1, b"", b"sacct: error")

    with patch("subprocess.run", return_value=sacct):
        wrote = runner._capture_energy("1", spec)

    assert wrote is False
