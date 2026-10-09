from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
from novafabric.replay._engine import ReplayEngine
from novafabric.replay._errors import ReplayOverrideUnenforceableError
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._intervention import InterventionError
from novafabric.replay.environment_gate import (
    EXIT_ENVIRONMENT_MISMATCH,
    ReplayEnvironmentMismatchError,
    validate_environment_value,
)


class ReplayMode(str, Enum):
    mocked = "mocked"
    forensic = "forensic"
    semantic = "semantic"
    exact = "exact"
    intervention = "intervention"

console = Console()


def replay_cmd(
    capsule: Annotated[
        Path,
        typer.Argument(
            help="Run capsule directory, or a bare run id (printed by `nova capture`, or "
            "written by a framework adapter)."
        ),
    ],
    mode: Annotated[
        ReplayMode,
        typer.Option("--mode", help="Replay mode.")
    ] = ReplayMode.mocked,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would execute without running")
    ] = False,
    allow_readonly: Annotated[
        bool,
        typer.Option(
            "--allow-readonly",
            help=(
                "Safety ladder (ADR-0012): permit read-only tools. In mocked mode, "
                "under --permissive an unmatched record.tool call of that class runs "
                "live, and a replay.yaml `allow: true` override for such a tool is "
                "honoured (ADR-0306, experimental). MCP calls always count as "
                "unknown mutation."
            ),
        ),
    ] = False,
    allow_mutating: Annotated[
        bool,
        typer.Option(
            "--allow-mutating",
            help=(
                "Safety ladder (ADR-0012): permit writes/deletes (implies "
                "--allow-readonly); the replay must also pass the policy engine's "
                "replay_mutating check. In mocked mode, under --permissive an "
                "unmatched record.tool call of that class runs live, and a "
                "replay.yaml `allow: true` override for such a tool is honoured "
                "(ADR-0306, experimental)."
            ),
        ),
    ] = False,
    allow_external_side_effects: Annotated[
        bool,
        typer.Option(
            "--allow-external-side-effects",
            help=(
                "Safety ladder (ADR-0012): permit external side effects (implies "
                "--allow-mutating). In mocked mode, under --permissive an unmatched "
                "record.tool call of that class runs live, and a replay.yaml "
                "`allow: true` override for such a tool is honoured (ADR-0306, "
                "experimental)."
            ),
        ),
    ] = False,
    allow_unknown_mutation: Annotated[
        bool,
        typer.Option(
            "--allow-unknown-mutation",
            help=(
                "Safety ladder (ADR-0012): permit tools of unknown mutation class "
                "(implies every lower rung). Every MCP call counts as unknown: in "
                "mocked mode this is the flag that lets an unmatched MCP call run "
                "live under --permissive, and that a replay.yaml `allow: true` "
                "override on an MCP tool needs before it re-executes (ADR-0306, "
                "experimental)."
            ),
        ),
    ] = False,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Base dir for replay output")
    ] = None,
    intervention_file: Annotated[
        Path | None,
        typer.Option(
            "--intervention-file",
            help=(
                "InterventionSpec YAML for --mode intervention "
                "(experimental, ADR-0086)"
            ),
        ),
    ] = None,
    environment: Annotated[
        str | None,
        typer.Option(
            "--environment",
            help=(
                "Experimental (ADR-0126): only replay a capsule that recorded this "
                "deployment_environment (e.g. staging). Exit 2, before anything "
                "runs, if it recorded another value or none."
            ),
        ),
    ] = None,
    permissive: Annotated[
        bool,
        typer.Option(
            "--permissive",
            help=(
                "mocked mode only (ADR-0300): do NOT fail on divergence. A model "
                "call with no recorded response gets an empty reply, unsupported "
                "model surfaces run LIVE, an unmatched intercepted tool call runs "
                "live only if an --allow-* flag permits its mutation class (MCP: "
                "--allow-unknown-mutation; record.tool: its declared class), never "
                "if replay.yaml says `allow: false`, and unconsumed recordings are "
                "only reported. Default is fail-closed."
            ),
        ),
    ] = False,
) -> None:
    """Re-run a captured run against recorded or mocked LLM responses.

    Five modes control how outbound calls are handled:
      mocked       — re-runs the command (Python workloads). Serves recorded
                     responses for OpenAI chat.completions and responses, and
                     Anthropic messages calls (sync or async, streamed or not),
                     and recorded results for MCP ClientSession.call_tool
                     and for functions declared with
                     novafabric.capture.record.tool (experimental).
                     Fail-closed: an extra, unmatched or unsupported call, or an
                     unconsumed recording, fails the replay (--permissive to
                     only report). replay.yaml tool_overrides are enforced
                     on both tool surfaces: `allow: false` is never run live,
                     `allow: true` re-executes only with the --allow-* flag
                     for its class (experimental). Other tools (HTTP,
                     shell, files, framework-native, undeclared functions)
                     are NOT intercepted: they run live; outbound
                     connections are reported, not blocked
      forensic     — read-only: inspects the capsule, runs nothing
      semantic     — does not re-run: scores how similar the recorded LLM
                     responses are to each other (0.0-1.0)
      exact        — does not re-run: reports whether a byte-exact re-run is
                     possible (deterministic env.lock, seeds, no schema drift)
      intervention — experimental: substitute one captured event per an
                     InterventionSpec, re-execute downstream under mocked
                     semantics, emit a diffable counterfactual capsule (a
                     substituted tool result is not delivered: tools run live)

    Capsules written by a framework adapter or novafabric.sdk.agent
    (capture_mode: sdk-decorator) or imported from OpenTelemetry spans record
    no command to re-run: mocked refuses them up front (error.type
    CapsuleNotReplayable, exit 1, and the same under --dry-run); intervention
    emits the counterfactual streams without re-running; exact reports them
    not eligible; forensic and semantic work as usual.

    \b
    Exit codes:
      0  replay succeeded, or a --dry-run the real run would not refuse
      1  replay failed or was aborted (incl. CapsuleNotReplayable, a
         divergence under the fail-closed default, a launch error/timeout)
      2  --environment did not match the capsule's recorded environment
      3  mocked: refused to start (also under --dry-run) because a replay.yaml
         `allow: false` override names a tool recorded on a transport replay
         cannot intercept (error.type ToolOverrideUnenforceable); --permissive
         starts anyway and reports override_unenforceable
      N  mocked/intervention: the replayed command's own non-zero exit code

    Scope: single capsule.

    \b
    Examples:
      # By run id — the id `nova capture` printed
      nova replay 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # Or by path
      nova replay path/to/my-capsule/

      # Forensic mode — observe without writing
      nova replay --mode forensic 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # Dry-run: show what would execute
      nova replay --dry-run path/to/my-capsule/

      # Mocked, but only report divergences instead of failing
      nova replay --permissive 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # CI gate: only replay capsules recorded in staging (exit 2 otherwise)
      nova replay --environment staging --dry-run 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # Counterfactual: what if the model had answered differently?
      nova replay --mode intervention --intervention-file spec.yaml path/to/my-capsule/
    """
    if environment is not None:
        try:
            validate_environment_value(environment)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="'--environment'") from exc
    try:
        capsule = resolve_capsule_ref(capsule)
    except CapsuleRefError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if mode is ReplayMode.intervention and intervention_file is None:
        console.print(
            "[red]--mode intervention requires --intervention-file[/red]"
        )
        raise typer.Exit(code=1)
    if intervention_file is not None and mode is not ReplayMode.intervention:
        console.print(
            "[red]--intervention-file only applies to --mode intervention[/red]"
        )
        raise typer.Exit(code=1)
    if permissive and mode is not ReplayMode.mocked:
        console.print("[red]--permissive only applies to --mode mocked[/red]")
        raise typer.Exit(code=1)

    flags = ReplayFlags(
        mode=mode.value,
        dry_run=dry_run,
        allow_readonly=allow_readonly,
        allow_mutating=allow_mutating,
        allow_external_side_effects=allow_external_side_effects,
        allow_unknown_mutation=allow_unknown_mutation,
        output_dir=output_dir,
        intervention_file=intervention_file,
        required_environment=environment,
        permissive=permissive,
    )

    base = output_dir or (Path.cwd() / ".novafabric" / "replays")
    engine = ReplayEngine(capsule_dir=capsule, flags=flags, base_dir=base)
    try:
        result = engine.run()
    except ReplayEnvironmentMismatchError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_ENVIRONMENT_MISMATCH) from exc
    except InterventionError as exc:
        console.print(f"[red]✗ Intervention error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if result.status == "dry_run":
        dry_report_path = base / result.replay_id / "dry_run_report.txt"
        if dry_report_path.exists():
            console.print(dry_report_path.read_text(), end="")
        if result.error is not None:
            # The real run would refuse this capsule: fail the dry run too.
            raise typer.Exit(code=_refusal_exit_code(result.error))
        return

    status_icon = "[green]✓[/green]" if result.status == "success" else "[red]✗[/red]"
    result_path = base / result.replay_id
    console.print(
        f"{status_icon} Replay written: {result_path}  "
        f"(replay_id={result.replay_id}  mode={result.mode})"
    )

    if result.model_calls_available is not None:
        # ADR-0300: what was actually served, so "✓" never reads as more.
        errors = int((result.replay_contract or {}).get("model_errors_replayed") or 0)
        console.print(
            f"  model calls: {result.model_calls_mocked} of "
            f"{result.model_calls_available} served from the capsule"
            + (f" ({errors} raised as the recorded error{'s' if errors != 1 else ''})"
               if errors else "")
            + f", {result.model_calls_unmatched or 0} unmatched"
        )
        not_intercepted = (result.tool_calls_recorded or 0) - (
            result.tool_calls_available or 0
        )
        console.print(
            f"  tool calls (MCP call_tool, record.tool): {result.tool_calls_mocked} of "
            f"{result.tool_calls_available or 0} served, "
            f"{result.tool_calls_live or 0} live, "
            f"{result.tool_calls_unmatched or 0} unmatched; "
            f"{not_intercepted} recorded on surfaces replay does not intercept"
        )
        contract = result.replay_contract or {}
        if contract.get("network_observed"):
            # ADR-0304: observed, never blocked -- say whether anything went live.
            live = int(contract.get("network_connections_live") or 0)
            where = ", ".join(contract.get("network_destinations") or [])
            console.print(
                f"  network: {live}{'+' if contract.get('network_connections_capped') else ''}"
                f" live connection{'s' if live != 1 else ''} from the replayed process"
                + (f" ({where})" if where else "")
                + " — observed, not blocked"
            )
    if result.status == "aborted" and result.error:
        console.print(
            f"  {result.error.get('type', 'error')}: {result.error.get('message', '')}",
            style="red",
            markup=False,
        )
    if result.divergence_reason:
        colour = "red" if result.status == "failure" else "yellow"
        console.print(f"  [{colour}]divergence: {result.divergence_reason}[/{colour}]")

    if result.env_warnings:
        for w in result.env_warnings:
            console.print(f"  [yellow]⚠ {w.get('message', '')}[/yellow]")

    if result.intervention is not None and not result.intervention.get(
        "substitution_delivered_to_workload", True
    ):
        console.print(
            "  [yellow]⚠ substitution not delivered to the re-executed workload: "
            f"{result.intervention.get('substitution_note', '')}[/yellow]",
            highlight=False,
        )

    if result.status not in ("success", "dry_run"):
        if result.status == "aborted" and result.error:
            raise typer.Exit(code=_refusal_exit_code(result.error))
        raise typer.Exit(code=result.exit_code or 1)


def _refusal_exit_code(error: dict[str, Any]) -> int:
    """Exit status of a replay refused before anything ran."""
    if error.get("type") == ReplayOverrideUnenforceableError.error_type:
        return ReplayOverrideUnenforceableError.exit_code
    return 1
