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
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.assure._honesty import HONESTY_LINE

app = typer.Typer(
    name="drift",
    help="Offline drift detection over sealed capsules (experimental, ADR-0147).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


def _honesty(json_out: bool) -> None:
    """Emit the ADR-0147 I-4 assurance-honesty line.

    On the ``--json`` path it goes to **stderr**: the line is a disclosure about
    the output, not part of it, and printing it to stdout would make
    ``nova drift detect --json | jq`` fail.
    """
    (err_console if json_out else console).print(f"[dim]{HONESTY_LINE}[/dim]")


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

    if isinstance(record, OutputDriftRecord):
        label, stat = record.metric, record.statistic
    else:
        label, stat = record.dimension, record.distance

    if record.drifted:
        # ADR-0192 wired source. Placed before the --json branch so the alert
        # does not depend on which output format the caller happened to pick.
        from novafabric.events.sources import (  # noqa: PLC0415
            emit_drift_detected_alert,
        )

        emit_drift_detected_alert(
            kind=record.kind,
            label=label,
            value=record.value,
            threshold=record.threshold,
        )

    if json_out:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
        _honesty(json_out=True)
        raise typer.Exit(0)

    flag = "[red]DRIFTED[/red]" if record.drifted else "[green]stable[/green]"
    console.print(
        f"Drift ({record.kind}) — {label}: {flag}\n"
        f"  {stat} = {record.value:.4f}   threshold = {record.threshold}"
    )
    _honesty(json_out=False)

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
        _honesty(json_out=True)
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

    _honesty(json_out=False)
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
        _honesty(json_out=True)
        raise typer.Exit(0)

    console.print(
        f"Drift root-cause — confidence ({hypothesis.confidence}), "
        f"correlation_only={hypothesis.correlation_only}"
    )
    for change in hypothesis.changes:
        console.print(
            f"  {change.kind}: {change.removed or '—'} -> {change.added or '—'}"
        )

    _honesty(json_out=False)
    raise typer.Exit(0)


@app.command("fingerprint")
def fingerprint(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {run:{run_id,calls:[{name,arguments}],scores?}, "
            "baseline?:{…same shape…}, threshold?, commutable?, idempotent?}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the fingerprint (or comparison) as JSON."),
    ] = False,
) -> None:
    """Fingerprint a run's behaviour, and measure it against a baseline fingerprint.

    The signature is deterministic and offline-reproducible over the C3-canonicalized
    trajectory, the tool mix and the score profile — so a collapsed retry or a reordered pair of
    declared-commutable calls does not read as a change, while a different trajectory does.

    A shift is an **observation, not a verdict**: the command exits ``0`` whether or not the
    distance crosses the threshold (``2`` only on bad input). With no ``baseline`` the run is
    fingerprinted and nothing is compared.

    \b
    Examples:
      nova drift fingerprint run.json
      nova drift fingerprint run.json --json
    """
    from novafabric.drift.fingerprint import (  # noqa: PLC0415
        BehavioralFingerprint,
        FingerprintError,
        compare_fingerprints,
        fingerprint_run,
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

    run = doc.get("run")
    if not isinstance(run, dict):
        err_console.print("[red]`run` must be an object.[/red]")
        raise typer.Exit(2)
    baseline = doc.get("baseline")
    if baseline is not None and not isinstance(baseline, dict):
        err_console.print("[red]`baseline` must be an object when present.[/red]")
        raise typer.Exit(2)

    commutable = [str(t) for t in doc.get("commutable", [])]
    idempotent = [str(t) for t in doc.get("idempotent", [])]

    def _build(block: dict[str, Any], what: str) -> BehavioralFingerprint:
        calls = block.get("calls", [])
        if not isinstance(calls, list):
            raise ValueError(f"`{what}.calls` must be an array")
        scores = block.get("scores")
        if scores is not None and not isinstance(scores, list):
            raise ValueError(f"`{what}.scores` must be an array")
        return fingerprint_run(
            str(block.get("run_id", what)),
            calls,
            scores=[float(s) for s in scores] if scores else None,
            commutable=commutable,
            idempotent=idempotent,
        )

    try:
        record = _build(run, "run")
        base = _build(baseline, "baseline") if baseline is not None else None
        comparison = (
            compare_fingerprints(record, base, threshold=float(doc["threshold"]))
            if base is not None
            else None
        )
    except (FingerprintError, ValueError, TypeError, KeyError) as exc:
        err_console.print(f"[red]Invalid fingerprint input:[/red] {exc}")
        raise typer.Exit(2) from exc

    payload = (comparison or record).model_dump(mode="json")
    if json_out:
        print(json.dumps(payload, indent=2))
        _honesty(json_out=True)
        raise typer.Exit(0)

    console.print(
        f"Behavioral fingerprint — {record.run_id}\n"
        f"  signature = {record.signature}\n"
        f"  basis     = {', '.join(record.basis)}"
    )
    if comparison is not None:
        if comparison.distance is None:
            console.print(
                f"  vs baseline {comparison.baseline_signature}: "
                f"[yellow]unknown[/yellow] — {comparison.note}"
            )
        else:
            flag = (
                "[red]SHIFTED[/red]" if comparison.shifted else "[green]stable[/green]"
            )
            console.print(
                f"  vs baseline {comparison.baseline_signature}: {flag}\n"
                f"  distance = {comparison.distance:.4f}   "
                f"threshold = {comparison.threshold}"
            )
            for component in comparison.components:
                console.print(
                    f"    {component.component}: {component.distance:.4f}"
                )

    _honesty(json_out=False)
    raise typer.Exit(0)


def _window(spec: str, what: str) -> tuple[str | None, str | None]:
    """Split a ``SINCE..UNTIL`` window spec. Either side may be empty."""
    if ".." not in spec:
        raise ValueError(
            f"--{what} must be SINCE..UNTIL (e.g. '7d..' or "
            "'2026-07-01T00:00:00Z..2026-07-08T00:00:00Z')"
        )
    since, until = spec.split("..", 1)
    return (since or None, until or None)


@app.command("collect")
def collect(
    capsules: Annotated[
        Path,
        typer.Option(
            "--capsules",
            help="Directory of sealed Run Capsules. Unused by --emit root-cause, which reads "
            "the lineage store.",
        ),
    ] = Path("."),
    window: Annotated[
        str,
        typer.Option(
            "--window",
            help="SINCE..UNTIL for the window examined. Ignored by --emit fingerprint, "
            "which is keyed by run id.",
        ),
    ] = "..",
    emit: Annotated[
        str,
        typer.Option(
            "--emit", help="runs | detect | silent-failure | fingerprint | root-cause."
        ),
    ] = "runs",
    baseline: Annotated[
        str | None,
        typer.Option("--baseline", help="SINCE..UNTIL for the baseline (--emit detect)."),
    ] = None,
    dimension: Annotated[
        str | None,
        typer.Option("--dimension", help="What to sample: cost, total-tokens, …, score:<name>."),
    ] = None,
    statistic: Annotated[
        str | None,
        typer.Option("--statistic", help="psi | ks — the two-sample statistic (--emit detect)."),
    ] = None,
    threshold: Annotated[
        float | None,
        typer.Option("--threshold", help="The caller's threshold; never defaulted."),
    ] = None,
    quality_metric: Annotated[
        str | None,
        typer.Option(
            "--quality-metric",
            help="Score name to read (--emit silent-failure; opt-in for --emit fingerprint).",
        ),
    ] = None,
    run: Annotated[
        str | None,
        typer.Option("--run", help="Run id to fingerprint (--emit fingerprint)."),
    ] = None,
    baseline_run: Annotated[
        str | None,
        typer.Option("--baseline-run", help="Run id to compare against (--emit fingerprint)."),
    ] = None,
    commutable: Annotated[
        list[str] | None,
        typer.Option("--commutable", help="Tool name whose order carries no meaning; repeatable."),
    ] = None,
    idempotent: Annotated[
        list[str] | None,
        typer.Option("--idempotent", help="Tool name whose consecutive repeat is a retry."),
    ] = None,
    kinds: Annotated[
        list[str] | None,
        typer.Option("--kind", help="Provenance kind to compare; repeatable (--emit root-cause)."),
    ] = None,
    depth: Annotated[
        int,
        typer.Option("--depth", help="Lineage walk depth (--emit root-cause)."),
    ] = 5,
    lineage_db: Annotated[
        Path | None,
        typer.Option("--lineage-db", help="Lineage store path; the default store when omitted."),
    ] = None,
    baseline_id: Annotated[
        str | None,
        typer.Option("--baseline-id", help="Optional pinned-baseline id to record."),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Full authoritative scan, ignoring the ADR-0225 cache."),
    ] = False,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the document as JSON — the form the detectors read."),
    ] = False,
) -> None:
    """Read sealed capsules into the document a drift detector consumes.

    Until this existed every ``nova drift`` subcommand took a hand-written document. It reads
    through the same ADR-0129 scanner ``nova query`` uses, so both agree about what a capsule is
    and what a window means (``since`` inclusive, ``until`` exclusive).

    It **collects, it does not judge** — ``--threshold`` and ``--statistic`` are your policy and
    are never defaulted, and no ``drifted`` flag is computed here.

    \b
    Examples:
      nova drift collect --capsules ./capsules --window 7d.. --json
      nova drift collect --capsules ./capsules --window 7d.. --emit silent-failure \\
        --quality-metric pass-rate --threshold 0.8 --json > runs.json
      nova drift collect --capsules ./capsules --emit detect --dimension cost \\
        --baseline 30d..2026-07-05T00:00:00Z --window 2026-07-05T00:00:00Z.. \\
        --statistic psi --threshold 0.2 --json > drift.json
      nova drift collect --capsules ./capsules --emit fingerprint \\
        --run run-42 --baseline-run golden-1 --threshold 0.2 --json > fp.json
      nova drift collect --emit root-cause --run run-42 --baseline-run golden-1 --json > rc.json
    """
    from novafabric.drift.collect import (  # noqa: PLC0415
        CollectError,
        collect_runs,
        detect_document,
        fingerprint_document,
        runs_document,
        silent_failure_document,
    )

    if emit not in {"runs", "detect", "silent-failure", "fingerprint", "root-cause"}:
        err_console.print(
            f"[red]Unknown --emit {emit!r}:[/red] "
            "runs | detect | silent-failure | fingerprint | root-cause."
        )
        raise typer.Exit(2)
    if emit != "root-cause" and not capsules.is_dir():
        # root-cause reads the lineage store, not the capsule tree, so it must not fail on a
        # capsule directory it never opens.
        err_console.print(f"[red]Capsule directory not found:[/red] {capsules}")
        raise typer.Exit(2)

    if emit == "root-cause":
        # Keyed by two run ids against the lineage graph, so neither the capsule window nor the
        # capsule directory is consulted.
        from novafabric.drift.collect import root_cause_document  # noqa: PLC0415
        from novafabric.lineage._store import LineageStore  # noqa: PLC0415

        try:
            if run is None or baseline_run is None:
                raise ValueError(
                    "--emit root-cause needs --run <drifted_run_id> and "
                    "--baseline-run <baseline_run_id>"
                )
            payload = root_cause_document(
                LineageStore(lineage_db),
                baseline_run=baseline_run,
                drifted_run=run,
                kinds=kinds or None,
                depth=depth,
            )
        except (CollectError, ValueError) as exc:
            err_console.print(f"[red]Could not collect:[/red] {exc}")
            raise typer.Exit(2) from exc
        if json_out:
            print(json.dumps(payload, indent=2))
            _honesty(json_out=True)
            raise typer.Exit(0)
        meta = payload["collected"]
        console.print(
            f"Collected provenance at depth {meta['depth']} — "
            f"baseline {meta['baseline_run']}: {meta['baseline_ancestors']} ancestor(s), "
            f"drifted {meta['drifted_run']}: {meta['drifted_ancestors']} ancestor(s)"
        )
        console.print("  Re-run with [bold]--json[/bold] for the document the detector reads.")
        _honesty(json_out=False)
        raise typer.Exit(0)

    if emit == "fingerprint":
        try:
            if run is None:
                raise ValueError("--emit fingerprint needs --run <run_id>")
            payload = fingerprint_document(
                capsules,
                run_id=run,
                baseline_run_id=baseline_run,
                threshold=threshold,
                quality_metric=quality_metric,
                commutable=commutable or (),
                idempotent=idempotent or (),
            )
        except (CollectError, ValueError) as exc:
            err_console.print(f"[red]Could not collect:[/red] {exc}")
            raise typer.Exit(2) from exc
        if json_out:
            print(json.dumps(payload, indent=2))
            _honesty(json_out=True)
            raise typer.Exit(0)
        target = payload["run"]
        console.print(
            f"Collected the trajectory of {target['run_id']} from {capsules} — "
            f"{len(target['calls'])} tool call(s)"
        )
        console.print("  Re-run with [bold]--json[/bold] for the document the detector reads.")
        _honesty(json_out=False)
        raise typer.Exit(0)

    try:
        window_since, window_until = _window(window, "window")
        runs = collect_runs(
            capsules, since=window_since, until=window_until, use_cache=not no_cache
        )

        if emit == "runs":
            payload = runs_document(
                runs, window={"since": window_since, "until": window_until}
            )
        elif emit == "silent-failure":
            if quality_metric is None or threshold is None:
                raise ValueError(
                    "--emit silent-failure needs --quality-metric and --threshold; the "
                    "threshold is your policy and is never defaulted"
                )
            payload = silent_failure_document(
                runs=runs, quality_metric=quality_metric, threshold=threshold
            )
        else:
            if baseline is None or dimension is None or statistic is None or threshold is None:
                raise ValueError(
                    "--emit detect needs --baseline, --dimension, --statistic and --threshold"
                )
            base_since, base_until = _window(baseline, "baseline")
            baseline_runs = collect_runs(
                capsules, since=base_since, until=base_until, use_cache=not no_cache
            )
            payload = detect_document(
                baseline=baseline_runs,
                window=runs,
                dimension=dimension,
                statistic=statistic,
                threshold=threshold,
                baseline_id=baseline_id,
            )
    except (CollectError, ValueError) as exc:
        err_console.print(f"[red]Could not collect:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(payload, indent=2))
        _honesty(json_out=True)
        raise typer.Exit(0)

    console.print(
        f"Collected {len(runs)} run(s) from {capsules} — window {window}, emitting {emit!r}"
    )
    collected = payload.get("collected")
    if isinstance(collected, dict):
        for key in ("baseline", "window"):
            block = collected.get(key)
            if isinstance(block, dict):
                console.print(
                    f"  {key}: {block['contributing']} contributing, "
                    f"{block['missing']} missing of {block['runs']} run(s)"
                )
        if "contributing" in collected:
            console.print(
                f"  {collected['contributing']} contributing, "
                f"{collected['missing']} missing of {collected['runs']} run(s)"
            )
    console.print("  Re-run with [bold]--json[/bold] for the document the detector reads.")

    _honesty(json_out=False)
    raise typer.Exit(0)
