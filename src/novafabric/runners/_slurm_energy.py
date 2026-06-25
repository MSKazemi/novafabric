"""Capture Slurm ``sacct`` measured energy into receipts (ADR-0093 §A2).

The SLURM runner already shells out to ``sacct``; on job completion it can also
request ``ConsumedEnergyRaw,Elapsed,NNodes`` and record a *measured* per-job
energy receipt — the strongest receipt class (an independent counter total the
conservation check reconciles against). The live ``sacct`` call needs a real
cluster (n1); this module is the **pure, testable step**: given a captured
``sacct -P`` string, it parses the joules + elapsed and appends an
``sacct_energy``/``measured`` receipt to ``energy-receipts.jsonl``.

Keeping the parse/receipt step a pure function (driven by a captured string in
unit tests) means the live poll-loop wiring is the only piece that needs a real
Slurm cluster to validate.
"""

from __future__ import annotations

from pathlib import Path

from novafabric.energy._attribution import load_receipts, write_receipts
from novafabric.energy._receipt import EnergyReceipt
from novafabric.energy._sacct import parse_sacct_energy, sacct_job_receipt

#: the sacct --format the runner should request to get a measured energy total
SACCT_ENERGY_FORMAT = "JobID,State,ConsumedEnergyRaw,Elapsed,NNodes"


def _parse_elapsed_seconds(output: str) -> float | None:
    """Parse the first non-empty ``Elapsed`` value (``[DD-]HH:MM:SS``) → seconds."""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header = lines[0].split("|")
    try:
        col = header.index("Elapsed")
    except ValueError:
        return None
    for row in lines[1:]:
        fields = row.split("|")
        if col < len(fields) and fields[col].strip():
            return _elapsed_to_seconds(fields[col].strip())
    return None


def _elapsed_to_seconds(value: str) -> float | None:
    """Convert a Slurm ``Elapsed`` field (``[DD-]HH:MM:SS``) to seconds."""
    days = 0
    if "-" in value:
        day_part, _, value = value.partition("-")
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = value.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for num in nums:
        seconds = seconds * 60 + num
    return float(days * 86400 + seconds)


def capture_slurm_energy(
    *,
    capsule_dir: Path,
    job_id: str,
    sacct_output: str,
    run_id: str,
    node_id: str,
) -> EnergyReceipt | None:
    """Parse a captured ``sacct`` energy string and append a measured receipt.

    Returns the appended :class:`EnergyReceipt`, or ``None`` when the captured
    output carries no ``ConsumedEnergyRaw`` value (energy accounting not
    configured) — in which case nothing is written.
    """
    consumed = parse_sacct_energy(sacct_output)
    if consumed is None:
        return None

    elapsed_s = _parse_elapsed_seconds(sacct_output)
    receipt = sacct_job_receipt(
        job_id=job_id,
        run_id=run_id,
        capsule_id=f"capsule:{capsule_dir.name}",
        node_id=node_id,
        consumed_joules=consumed,
        elapsed_s=elapsed_s,
    )

    existing = load_receipts(capsule_dir)
    write_receipts(capsule_dir, [*existing, receipt])
    return receipt


__all__ = ["SACCT_ENERGY_FORMAT", "capture_slurm_energy"]
