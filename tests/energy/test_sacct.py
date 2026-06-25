"""Slice A2 — Slurm sacct measured energy path (ADR-0093, the HPC moat).

Slurm's ``sacct ConsumedEnergyRaw`` gives a true per-job joule total (with
``AcctGatherEnergyType=acct_gather_energy/rapl|ipmi``). This is the strongest
receipt class — confidence='measured', source='sacct_energy'. The live sacct
call needs a Slurm cluster (n1); the parser + builder are tested here with
captured output.
"""

from __future__ import annotations

from novafabric.capsule.ulid_util import new_ulid
from novafabric.energy import (
    AttributionMethod,
    Confidence,
    CounterName,
    MeasurementScope,
    MeasurementSource,
)
from novafabric.energy._sacct import parse_sacct_energy, sacct_job_receipt

_SACCT_OUTPUT = "ConsumedEnergyRaw|Elapsed|NNodes\n1500000|00:02:30|1\n"


def test_parse_sacct_energy_returns_joules():
    assert parse_sacct_energy(_SACCT_OUTPUT) == 1_500_000.0


def test_parse_sacct_energy_none_when_absent():
    assert parse_sacct_energy("ConsumedEnergyRaw|Elapsed\n|00:00:01\n") is None
    assert parse_sacct_energy("") is None


def test_parse_sacct_energy_none_when_column_missing():
    # energy accounting not configured → no ConsumedEnergyRaw column at all
    assert parse_sacct_energy("Elapsed|NNodes\n00:00:05|1\n") is None


def test_parse_sacct_energy_skips_non_numeric():
    out = "ConsumedEnergyRaw|Elapsed\nNONE|00:00:05\n900000|00:00:06\n"
    assert parse_sacct_energy(out) == 900_000.0


def test_sacct_job_receipt_is_measured_with_counter():
    r = sacct_job_receipt(
        job_id="4242",
        run_id=new_ulid(),
        capsule_id="capsule:x",
        node_id="n1",
        consumed_joules=1_500_000.0,
        elapsed_s=150.0,
    )
    assert r.confidence is Confidence.MEASURED
    assert r.measurement_source is MeasurementSource.SACCT_ENERGY
    assert r.measurement_scope is MeasurementScope.PER_JOB
    assert r.attribution_method is AttributionMethod.DIRECT_COUNTER
    assert r.measured_joules == 1_500_000.0
    assert r.wall_clock_s == 150.0
    assert CounterName.SACCT_ENERGY in r.hardware.counters_available
    # the moat: this is a real measurement, so it passes the forgery guard
    assert r.consistency_errors() == []
