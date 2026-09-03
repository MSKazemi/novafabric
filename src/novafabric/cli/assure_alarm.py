"""nova assure-alarm — the standing production regression alarm (ADR-0147 D4, NF-156).

Runs the shipped Wilson + Wald-SPRT primitive over a window of run outcomes and
emits `no-regression` / `regression` / `inconclusive`, firing **only** on a
statistically significant regression. A single-run dip does not fire — that is why
this reuses the SPRT instead of thresholding a delta.

⚠ **Polarity.** Outcomes are `1 = healthy` by default. If your window is drift flags
(`1 = drifted`), pass `--drift-flags`; feeding them raw inverts the alarm silently,
so it would fire on improvement and stay quiet on a real regression.

⚠ **This is not the promote gate.** ADR-0147 D4 keeps ADR-0080 unchanged, so this
never uses the gate's exit-code contract. It exits `1` when the alarm fires, because
an alarm exists to be noticed, and `0` otherwise — including on `inconclusive`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.assure._honesty import HONESTY_LINE
from novafabric.assure.alarm import AlarmError, AlarmVerdict, evaluate

app = typer.Typer(
    name="assure-alarm",
    help="Standing production regression alarm (experimental, ADR-0147 NF-156).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


@app.command("check")
def check(
    window: Annotated[
        Path,
        typer.Option(
            "--window",
            help='JSON: {"metric": "...", "baseline": [1,0,...], "window": [1,0,...]}',
        ),
    ],
    drift_flags: Annotated[
        bool,
        typer.Option(
            "--drift-flags",
            help="Outcomes are 1 = drifted rather than 1 = healthy; both are inverted.",
        ),
    ] = False,
    p0: Annotated[
        float | None, typer.Option("--p0", help="SPRT null pass-rate.")
    ] = None,
    p1: Annotated[
        float | None, typer.Option("--p1", help="SPRT alternative pass-rate.")
    ] = None,
) -> None:
    """Evaluate the standing regression alarm over a window."""
    if not window.is_file():
        err_console.print(f"[red]Window document not found:[/red] {window}")
        raise typer.Exit(2)
    try:
        doc: Any = json.loads(window.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]Could not read the window:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Window document must be a JSON object.[/red]")
        raise typer.Exit(2)

    baseline, window_outcomes = doc.get("baseline"), doc.get("window")
    if not isinstance(baseline, list) or not isinstance(window_outcomes, list):
        err_console.print(
            "[red]Window document needs 'baseline' and 'window' arrays.[/red]"
        )
        raise typer.Exit(2)

    extra: dict[str, Any] = {}
    if p0 is not None:
        extra["p0"] = p0
    if p1 is not None:
        extra["p1"] = p1

    try:
        alarm = evaluate(
            baseline,
            window_outcomes,
            metric=str(doc.get("metric", "task_pass")),
            outcomes_are_drift_flags=drift_flags,
            **extra,
        )
    except AlarmError as exc:
        err_console.print(f"[red]Cannot evaluate the alarm:[/red] {exc}")
        raise typer.Exit(2) from exc

    console.print_json(json.dumps(alarm.model_dump(mode="json")))
    if alarm.verdict is AlarmVerdict.inconclusive:
        console.print(
            "[yellow]inconclusive[/yellow] — not enough evidence yet; "
            "this is not a regression."
        )
    console.print(f"[dim]{HONESTY_LINE}[/dim]")

    if alarm.fired:
        err_console.print(
            f"[red]ALARM:[/red] statistically significant regression in "
            f"{alarm.metric} ({alarm.window_successes}/{alarm.window_n} vs "
            f"baseline {alarm.baseline_successes}/{alarm.baseline_n})"
        )
        raise typer.Exit(1)
