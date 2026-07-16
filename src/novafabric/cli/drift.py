"""nova drift — offline drift & silent-failure detection over sealed capsules (ADR-0147).

Read-only. ``nova drift detect`` (D2) computes a two-sample drift record (NF-151 output-drift or
NF-152 behavioral-drift) from supplied baseline/window samples — **no model re-invocation, zero
token cost**. ``nova drift silent-failure`` (D6 / NF-158) flags runs that reported terminal success
but whose quality signal fell below a threshold. ``nova drift root-cause`` (D5 / NF-157) links an
observed drift to the model/prompt/tool/dataset that changed between a baseline and a drifted run —
a **correlation, not a cause**. All three **detect and evidence**: ``drifted`` / ``silent_failure``
/ the root-cause hypothesis are observations, never a promote/pass gate, so each command exits ``0``
whether or not anything is flagged (``2`` only on bad input).

This first slice takes the samples/runs directly in the document; the collector that reads them from
sealed capsules over a ``--baseline``/``--window`` range is a documented follow-on.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(
    name="drift",
    help="Offline drift detection over sealed capsules (experimental, ADR-0147).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


@app.command("detect")
def detect(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {kind:output|behavioral, ...samples..., threshold}. "
            "output: {metric, statistic, baseline[], window[], window_meta, baseline_id?}. "
            "behavioral: {dimension, distance, baseline, window}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the drift record as JSON."),
    ] = False,
) -> None:
    """Compute an offline two-sample drift record from supplied samples.

    \b
    Examples:
      nova drift detect drift.json
      nova drift detect drift.json --json
    """
    from novafabric.drift.detectors import (
        BehavioralDriftRecord,
        OutputDriftRecord,
        build_behavioral_drift,
        build_output_drift,
    )

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Document must be a JSON object.[/red]")
        raise typer.Exit(2)

    kind = doc.get("kind", "output")
    record: OutputDriftRecord | BehavioralDriftRecord
    try:
        if kind == "behavioral":
            record = build_behavioral_drift(
                dimension=str(doc["dimension"]),
                distance=str(doc.get("distance", "psi")),
                baseline=doc["baseline"],
                window=doc["window"],
                threshold=float(doc["threshold"]),
            )
        elif kind == "output":
            record = build_output_drift(
                metric=str(doc["metric"]),
                statistic=str(doc.get("statistic", "psi")),
                baseline=doc["baseline"],
                window=doc["window"],
                threshold=float(doc["threshold"]),
                window_meta=doc.get("window_meta", {}),
                baseline_id=doc.get("baseline_id"),
            )
        else:
            err_console.print(
                f"[red]Unknown drift kind:[/red] {kind!r} (expected output|behavioral)"
            )
            raise typer.Exit(2)
    except typer.Exit:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        err_console.print(f"[red]Invalid drift input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    flag = "[red]DRIFTED[/red]" if record.drifted else "[green]stable[/green]"
    if isinstance(record, OutputDriftRecord):
        label, stat = record.metric, record.statistic
    else:
        label, stat = record.dimension, record.distance
    console.print(
        f"Drift ({record.kind}) — {label}: {flag}\n"
        f"  {stat} = {record.value:.4f}   threshold = {record.threshold}"
    )

    raise typer.Exit(0)


@app.command("silent-failure")
def silent_failure(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {runs:[{run_id,status,quality_signal}], threshold, success_statuses?}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the silent-failure report as JSON."),
    ] = False,
) -> None:
    """Flag runs that reported success but whose quality signal fell below a threshold.

    A silent failure is surfaced for review — it is a detector observation, not a determination
    that the run failed — so the command exits ``0`` whether or not any run is flagged (``2`` only
    on bad input).

    \b
    Examples:
      nova drift silent-failure runs.json
      nova drift silent-failure runs.json --json
    """
    from novafabric.drift.silent_failure import detect_silent_failures

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Document must be a JSON object.[/red]")
        raise typer.Exit(2)

    runs = doc.get("runs")
    if not isinstance(runs, list):
        err_console.print("[red]`runs` must be an array.[/red]")
        raise typer.Exit(2)
    success_statuses = doc.get("success_statuses", ["success"])
    if not isinstance(success_statuses, list):
        err_console.print("[red]`success_statuses` must be an array.[/red]")
        raise typer.Exit(2)

    try:
        report = detect_silent_failures(
            runs,
            threshold=float(doc["threshold"]),
            success_statuses=[str(s) for s in success_statuses],
        )
    except (ValueError, TypeError, KeyError) as exc:
        err_console.print(f"[red]Invalid silent-failure input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print(
        f"Silent-failure scan — {report.silent_failures} flagged of "
        f"{report.total_reported_success} reported-success run(s), threshold {report.threshold}"
    )
    for rec in report.records:
        if rec.silent_failure:
            console.print(
                f"  [red]silent-failure[/red] {rec.run_id} "
                f"(status={rec.status}, quality={rec.quality_signal})"
            )

    raise typer.Exit(0)


@app.command("root-cause")
def root_cause(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {baseline:[{kind,ref}], drifted:[{kind,ref}], kinds?}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the root-cause hypothesis as JSON."),
    ] = False,
) -> None:
    """Link an observed drift to the input(s) that changed between a baseline and a drifted run.

    Diffs the two runs' lineage provenance ancestors to the model/prompt/tool/dataset that changed.
    The result is a **correlation, not a cause** — a hypothesis to investigate — so the command
    exits ``0`` whether or not anything changed (``2`` only on bad input).

    \b
    Examples:
      nova drift root-cause rc.json
      nova drift root-cause rc.json --json
    """
    from novafabric.drift.root_cause import find_root_cause

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Document must be a JSON object.[/red]")
        raise typer.Exit(2)

    baseline = doc.get("baseline")
    drifted = doc.get("drifted")
    if not isinstance(baseline, list) or not isinstance(drifted, list):
        err_console.print("[red]`baseline` and `drifted` must be arrays.[/red]")
        raise typer.Exit(2)
    kinds = doc.get("kinds")
    kwargs = {}
    if kinds is not None:
        if not isinstance(kinds, list):
            err_console.print("[red]`kinds` must be an array.[/red]")
            raise typer.Exit(2)
        kwargs["kinds"] = [str(k) for k in kinds]

    try:
        hypothesis = find_root_cause(baseline, drifted, **kwargs)
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid root-cause input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(hypothesis.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print(
        f"Drift root-cause — confidence ({hypothesis.confidence}), "
        f"correlation_only={hypothesis.correlation_only}"
    )
    for change in hypothesis.changes:
        console.print(
            f"  {change.kind}: {change.removed or '—'} -> {change.added or '—'}"
        )

    raise typer.Exit(0)
