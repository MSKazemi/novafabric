"""`nova energy` — Energy-Anchored Action Receipts CLI (ADR-0093).

Subcommands:

- ``probe``   — report which energy counters are readable on this host.
- ``attest``  — produce ``energy-receipts.jsonl`` for a captured capsule.
- ``verify``  — re-check receipt integrity + conservation; forgery-guard exit codes.
- ``report``  — tabulate the receipts in a capsule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from novafabric.energy._attribution import attest_capsule, load_receipts, write_receipts
from novafabric.energy._conservation import compute_conservation
from novafabric.energy._readers import probe_counters
from novafabric.energy._seal import receipt_integrity_errors

app = typer.Typer(
    help="Energy-Anchored Action Receipts (ADR-0093). Measured-or-declared-unknown, "
    "never fabricated.",
    no_args_is_help=True,
)

# verify exit codes (mirror the seal verifier's structured taxonomy)
_EXIT_OK = 0
_EXIT_INTEGRITY = 3
_EXIT_DIVERGED = 4

_RaplBase = Annotated[
    Path | None, typer.Option(help="Override the RAPL sysfs base (testing/non-Linux).")
]


@app.command()
def probe(rapl_base: _RaplBase = None) -> None:
    """Report which energy counters this host can actually read."""
    base = rapl_base or Path("/sys/class/powercap")
    counters = probe_counters(rapl_base=base)
    if counters:
        names = ", ".join(c.value for c in counters)
        typer.echo(f"readable energy counters: {names}")
    else:
        typer.echo(
            "readable energy counters: none — per-action energy is unavailable on "
            "this host (receipts will be honestly marked 'unavailable')."
        )


@app.command()
def attest(
    capsule_dir: Annotated[Path, typer.Argument(help="Run Capsule directory.")],
    node_id: Annotated[str | None, typer.Option(help="Override the node id.")] = None,
    rapl_base: _RaplBase = None,
) -> None:
    """Write energy receipts for every action in the capsule."""
    base = rapl_base or Path("/sys/class/powercap")
    receipts = attest_capsule(capsule_dir, rapl_base=base, node_id=node_id)
    path = write_receipts(capsule_dir, receipts)
    typer.echo(f"wrote {len(receipts)} energy receipt(s) to {path}")


@app.command()
def verify(
    capsule_dir: Annotated[Path, typer.Argument(help="Run Capsule directory.")],
) -> None:
    """Verify receipt integrity + energy conservation (forgery-guard exit codes)."""
    from pydantic import ValidationError  # noqa: PLC0415

    try:
        receipts = load_receipts(capsule_dir)
    except ValidationError as exc:
        # A receipt that no longer parses (e.g. an out-of-enum measurement_source
        # written by tampering) is an integrity failure, not a crash.
        typer.echo(f"FAIL: receipt does not conform to the EnergyReceipt schema: {exc}")
        raise typer.Exit(_EXIT_INTEGRITY) from exc
    if not receipts:
        typer.echo("no energy-receipts.jsonl found")
        raise typer.Exit(_EXIT_OK)

    failed = 0
    for receipt in receipts:
        errors = receipt_integrity_errors(receipt)
        if errors:
            failed += 1
            for err in errors:
                typer.echo(f"  [{receipt.receipt_id}] {err}")
    if failed:
        typer.echo(f"FAIL: {failed}/{len(receipts)} receipt(s) failed integrity")
        raise typer.Exit(_EXIT_INTEGRITY)

    run_id = receipts[0].run_id
    conservation = compute_conservation(receipts, run_id=run_id)
    typer.echo(
        f"OK: {len(receipts)} receipt(s) consistent; "
        f"conservation status={conservation.status}"
    )
    if conservation.status == "diverged":
        raise typer.Exit(_EXIT_DIVERGED)


@app.command()
def report(
    capsule_dir: Annotated[Path, typer.Argument(help="Run Capsule directory.")],
    output_format: Annotated[
        str, typer.Option("--format", help="table | json")
    ] = "table",
) -> None:
    """Tabulate the energy receipts in a capsule."""
    receipts = load_receipts(capsule_dir)
    if output_format == "json":
        typer.echo(json.dumps([r.to_record() for r in receipts], indent=2))
        return
    typer.echo(f"{'action':<14}{'source':<22}{'confidence':<12}{'joules':>12}")
    for r in receipts:
        joules = "-" if r.measured_joules is None else f"{r.measured_joules:.3f}"
        typer.echo(
            f"{r.action_ref.kind.value:<14}{r.measurement_source.value:<22}"
            f"{r.confidence.value:<12}{joules:>12}"
        )
    typer.echo(f"total: {len(receipts)} receipt(s)")


__all__ = ["app"]
