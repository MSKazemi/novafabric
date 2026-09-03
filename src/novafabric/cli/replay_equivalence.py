"""nova replay-equivalence — the C3 behavioral-equivalence verdict (ADR-0144).

Takes a baseline trajectory and a replay trajectory, canonicalizes both, and emits
one verdict: equivalent or not, the distance, the tolerance it was judged against,
and the steps that diverged.

**This command computes nothing itself.** It calls
``replay.equivalence.canonicalize`` and ``replay.equivalence.compare``. ADR-0147 D3
is explicit that the canary-replay scheduler and the model-update impact report
must *consume* C3 and never re-implement it — one verdict engine, many consumers —
and that only holds if the engine has exactly one implementation. This is its CLI
surface.

⚠ **This is not `nova replay --check-equivalence`.** ADR-0147 writes the surface
that way; `nova replay` is an existing command that *performs* a replay, and fusing
the verdict into it requires integrating with the replay engine's output. That
remains future work. What ships here is the verdict surface a scheduler calls.

Exit ``1`` on a non-equivalent verdict: unlike `nova drift`, whose detectors are
observations and always exit 0, non-equivalence is the condition a canary alarms on.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.replay.determinism import Eligibility, assess
from novafabric.replay.equivalence import (
    ALL_RULES,
    RULES_VERSION,
    MatchMode,
    ToolCall,
    UnknownRuleError,
    canonicalize,
    compare,
)

app = typer.Typer(
    name="replay-equivalence",
    help="Behavioral-equivalence verdict over two trajectories (experimental, ADR-0144).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


def _load_trajectory(path: Path, what: str) -> list[ToolCall]:
    """Read a trajectory: a JSON array of ``{name, arguments}`` objects."""
    if not path.is_file():
        err_console.print(f"[red]{what} not found:[/red] {path}")
        raise typer.Exit(2)
    try:
        doc: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]Could not read {what.lower()}:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, list):
        err_console.print(
            f"[red]{what} must be a JSON array of tool calls.[/red]"
        )
        raise typer.Exit(2)

    calls: list[ToolCall] = []
    for index, entry in enumerate(doc):
        if not isinstance(entry, dict) or "name" not in entry:
            err_console.print(
                f"[red]{what}[{index}] must be an object with a 'name'.[/red]"
            )
            raise typer.Exit(2)
        arguments = entry.get("arguments", {})
        if not isinstance(arguments, dict):
            err_console.print(
                f"[red]{what}[{index}].arguments must be an object.[/red]"
            )
            raise typer.Exit(2)
        calls.append(ToolCall(name=str(entry["name"]), arguments=arguments, index=index))
    return calls


@app.command("check")
def check(
    baseline: Annotated[
        Path, typer.Option("--baseline", help="Baseline trajectory JSON array.")
    ],
    replay: Annotated[
        Path, typer.Option("--replay", help="Replayed trajectory JSON array.")
    ],
    mode: Annotated[
        str,
        typer.Option("--mode", help="Correspondence required: set | ordered | edit."),
    ] = "ordered",
    tolerance: Annotated[
        float,
        typer.Option(
            "--tolerance",
            help="Maximum distance still counted as equivalent (default 0.0 = exact).",
        ),
    ] = 0.0,
    rule: Annotated[
        list[str] | None,
        typer.Option(
            "--rule",
            help="Canonicalization rule (repeatable). Known: " + ", ".join(ALL_RULES),
        ),
    ] = None,
    commutable: Annotated[
        list[str] | None,
        typer.Option("--commutable", help="Tool name whose order does not matter (repeatable)."),
    ] = None,
    idempotent: Annotated[
        list[str] | None,
        typer.Option("--idempotent", help="Tool name whose retries collapse (repeatable)."),
    ] = None,
) -> None:
    """Emit the behavioral-equivalence verdict for two trajectories."""
    try:
        match_mode = MatchMode(mode)
    except ValueError:
        err_console.print(
            f"[red]Unknown mode:[/red] {mode!r} "
            f"(expected {', '.join(m.value for m in MatchMode)})"
        )
        raise typer.Exit(2) from None
    if tolerance < 0:
        err_console.print("[red]--tolerance cannot be negative.[/red]")
        raise typer.Exit(2)

    base_calls = _load_trajectory(baseline, "Baseline")
    replay_calls = _load_trajectory(replay, "Replay")

    try:
        base_canon = canonicalize(
            base_calls, rules=rule, commutable=commutable or (),
            idempotent=idempotent or (),
        )
        replay_canon = canonicalize(
            replay_calls, rules=rule, commutable=commutable or (),
            idempotent=idempotent or (),
        )
    except UnknownRuleError as exc:
        err_console.print(f"[red]Unknown canonicalization rule:[/red] {exc}")
        raise typer.Exit(2) from exc

    verdict = compare(
        base_canon.calls, replay_canon.calls, mode=match_mode, tolerance=tolerance
    )

    payload = {
        "equivalent": verdict.equivalent,
        "mode": verdict.mode.value,
        "distance": verdict.distance,
        "tolerance": verdict.tolerance,
        # Recorded so the verdict stays interpretable: a verdict whose
        # canonicalization is unknown cannot be re-derived later.
        "rules_version": RULES_VERSION,
        "rules_applied": list(base_canon.rules_applied),
        "divergent_steps": [asdict(step) for step in verdict.divergent_steps],
    }
    console.print_json(json.dumps(payload, default=str))

    if not verdict.equivalent:
        err_console.print(
            f"[red]not equivalent:[/red] distance {verdict.distance} "
            f"exceeds tolerance {verdict.tolerance}"
        )
        raise typer.Exit(1)


@app.command("regime")
def regime(
    capsule: Annotated[
        Path,
        typer.Option("--capsule", help="Capsule directory holding model-calls.jsonl."),
    ],
) -> None:
    """Report whether a run was in a regime where a replay could match.

    An equivalence verdict means something different depending on the regime the
    original run executed in: divergence from a ``temperature=1.2`` run tells you
    almost nothing, while the same divergence from a fully pinned run is a finding.

    Exits ``0`` for an eligible regime and ``1`` otherwise — including
    ``unknown``, because a run that recorded nothing has not demonstrated that a
    replay was expected to match. That is a statement about the evidence, not an
    accusation about the run.
    """
    calls_file = capsule / "model-calls.jsonl"
    if not capsule.is_dir():
        err_console.print(f"[red]Capsule directory not found:[/red] {capsule}")
        raise typer.Exit(2)
    if not calls_file.is_file():
        err_console.print(f"[red]No model-calls.jsonl in:[/red] {capsule}")
        raise typer.Exit(2)

    calls: list[dict[str, Any]] = []
    for lineno, line in enumerate(
        calls_file.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            err_console.print(
                f"[red]model-calls.jsonl line {lineno} is not valid JSON:[/red] {exc}"
            )
            raise typer.Exit(2) from exc
        if not isinstance(record, dict):
            err_console.print(
                f"[red]model-calls.jsonl line {lineno} is not an object.[/red]"
            )
            raise typer.Exit(2)
        calls.append(record)

    result = assess(calls)
    console.print_json(json.dumps(result.model_dump(mode="json")))
    for reason in result.reasons:
        console.print(f"[dim]{reason}[/dim]")

    if result.eligibility is not Eligibility.eligible:
        raise typer.Exit(1)
