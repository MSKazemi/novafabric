"""Slurm sacct measured-energy path (ADR-0093 §A2) — the HPC moat.

``sacct -j <jobid> --format=ConsumedEnergyRaw,Elapsed,NNodes -P`` yields a true
per-job joule total when the cluster runs
``AcctGatherEnergyType=acct_gather_energy/rapl`` (or ``/ipmi``). That total is an
*independent measurement* (the strongest receipt class, and the node total the
conservation check reconciles against). The live ``sacct`` call belongs to the
Slurm runner and needs a real cluster (n1); this module is the pure parser +
receipt builder, validated against captured output.
"""

from __future__ import annotations

from datetime import datetime, timezone

from novafabric.energy._receipt import (
    ActionKind,
    ActionRef,
    AttributionMethod,
    CarbonAccountingMode,
    Confidence,
    CounterName,
    EnergyReceipt,
    Hardware,
    MeasurementScope,
    MeasurementSource,
)


def parse_sacct_energy(output: str) -> float | None:
    """Parse joules from ``sacct -P --format=ConsumedEnergyRaw,...`` output.

    Returns the first non-empty ``ConsumedEnergyRaw`` value (joules), or ``None``
    when the column is absent/empty (energy accounting not configured).
    """
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header = lines[0].split("|")
    try:
        col = header.index("ConsumedEnergyRaw")
    except ValueError:
        return None
    for row in lines[1:]:
        fields = row.split("|")
        if col < len(fields) and fields[col].strip():
            try:
                return float(fields[col].strip())
            except ValueError:
                continue
    return None


def sacct_job_receipt(
    *,
    job_id: str,
    run_id: str,
    capsule_id: str,
    node_id: str,
    consumed_joules: float,
    elapsed_s: float | None = None,
) -> EnergyReceipt:
    """Build the strongest receipt class: a measured per-job sacct energy total."""
    receipt = EnergyReceipt(
        action_ref=ActionRef(kind=ActionKind.JOB_STEP, id=job_id),
        run_id=run_id,
        capsule_id=capsule_id,
        measured_joules=consumed_joules,
        measurement_source=MeasurementSource.SACCT_ENERGY,
        measurement_scope=MeasurementScope.PER_JOB,
        wall_clock_s=elapsed_s,
        confidence=Confidence.MEASURED,
        attribution_method=AttributionMethod.DIRECT_COUNTER,
        carbon_g_co2e=None,
        grid_intensity_g_per_kwh=None,
        carbon_accounting_mode=CarbonAccountingMode.UNKNOWN,
        hardware=Hardware(
            node_id=node_id,
            counters_available=[CounterName.SACCT_ENERGY],
        ),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    return receipt.with_payload_hash()


__all__ = ["parse_sacct_energy", "sacct_job_receipt"]
