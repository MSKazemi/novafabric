from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric._paths import default_capsule_dir
from novafabric.capture.orchestrator import AssetStatusCheckError, CaptureOrchestrator
from novafabric.runners import (
    UnknownRunnerError,
    get_runner,
)


class RunnerName(str, Enum):
    local = "local"
    docker = "docker"
    kubernetes = "kubernetes"
    slurm = "slurm"
    lsf = "lsf"
    pbs = "pbs"

console = Console()


def _parse_runner_option(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise typer.BadParameter(
            f"--runner-option must be key=value, got: {raw!r}",
            param_hint="'--runner-option'",
        )
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise typer.BadParameter(
            f"--runner-option key cannot be empty: {raw!r}",
            param_hint="'--runner-option'",
        )
    return key, value


def _parse_status_list(raw: str | None) -> list[str] | None:
    """Parse a comma-separated status list into a list of stripped strings."""
    if not raw:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()]


def capture_cmd(
    ctx: typer.Context,
    command: Annotated[list[str], typer.Argument(help="Command and args to capture")],
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", "-o", help="Base directory for capsule runs"),
    ] = None,
    runner_name: Annotated[
        RunnerName,
        typer.Option(
            "--runner",
            help="Runner backend for executing the captured command.",
        ),
    ] = RunnerName.local,
    runner_options: Annotated[
        list[str] | None,
        typer.Option(
            "--runner-option",
            help=(
                "Runner-specific option as key=value. "
                "Example: --runner-option image=myorg/agent:latest"
            ),
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help="Wall-clock deadline in seconds for the captured command (default: 600).",
        ),
    ] = None,
    # C-1.5 — asset status enforcement flags
    asset_ref: Annotated[
        Optional[str],
        typer.Option(
            "--asset",
            help=(
                "Named asset to check before capturing (e.g. my-agent@v1). "
                "Used with --require-asset-status or --warn-if-asset-status."
            ),
        ),
    ] = None,
    require_asset_status: Annotated[
        Optional[str],
        typer.Option(
            "--require-asset-status",
            help=(
                "Comma-separated list of lifecycle statuses the asset must be in "
                "before capture starts. Exits with a non-zero code if not satisfied. "
                "Example: --require-asset-status staging,production"
            ),
        ),
    ] = None,
    warn_if_asset_status: Annotated[
        Optional[str],
        typer.Option(
            "--warn-if-asset-status",
            help=(
                "Comma-separated list of lifecycle statuses that trigger a warning "
                "(but do not block capture). "
                "Example: --warn-if-asset-status development"
            ),
        ),
    ] = None,
    require_registered: Annotated[
        bool,
        typer.Option(
            "--require-registered",
            help=(
                "Block capture if the named asset (--asset) is not in the registry. "
                "By default, an unregistered asset produces a warning only."
            ),
        ),
    ] = False,
    mark_provenance: Annotated[
        bool,
        typer.Option(
            "--mark-provenance",
            help=(
                "Write a C2PA synthetic-content provenance marker (c2pa-manifest.json) "
                "into the capsule when the run produces model output. Satisfies the "
                "EU AI Act Art.50 machine-readable AI-generated-content disclosure. "
                "The marker is sealed with the capsule when NovaSeal is configured."
            ),
        ),
    ] = False,
    fast_emit: Annotated[
        bool,
        typer.Option(
            "--fast-emit",
            help=(
                "Install capture hooks lazily in the workload subprocess: a "
                "target SDK is patched only if/when the workload imports it, so "
                "unused SDKs are never imported at startup just to be patched. "
                "Cuts per-run startup cost #2 (ADR-0092 slice B). Fidelity is "
                "unchanged. Runs in-process (not delegated to the daemon)."
            ),
        ),
    ] = False,
    emit_spool: Annotated[
        bool,
        typer.Option(
            "--emit-spool",
            help=(
                "Also write run-boundary EventEnvelope v1 records to the local "
                "event spool ($NOVAFABRIC_SPOOL_DIR, default $NOVAFABRIC_HOME/"
                "spool) so the resident drain can forward them to the collector "
                "tier (ADR-0092 slice C). Off by default; edge stays keyless "
                "(signing happens at the hub). Runs in-process (not delegated)."
            ),
        ),
    ] = False,
    daemon: Annotated[
        bool,
        typer.Option(
            "--daemon/--no-daemon",
            help=(
                "Delegate a plain capture to the warm capture daemon if one is "
                "running (eliminates per-run cold-start). Auto: falls back to "
                "in-process direct spawn when no daemon is reachable. Ignored when "
                "any of --runner/--runner-option/--timeout/--asset/--mark-provenance/"
                "--output-dir are set (those run in-process to honor the flags)."
            ),
        ),
    ] = True,
) -> None:
    """Wrap any shell command and record its execution as a replayable capsule.

    All LLM calls, tool use, stdin/stdout, and timing are recorded.
    The resulting capsule can be replayed, diffed, audited, or sealed.

    Scope: single run. Output written to NOVAFABRIC_HOME/capsules/.

    \b
    Examples:
      # Capture a script
      nova capture python agent.py

      # Gate on asset status before capturing
      nova capture --asset my-agent@v1 --require-asset-status staging,production -- python agent.py

      # Capture using Docker runner
      nova capture --runner docker --runner-option image=myorg/agent:latest python agent.py

      # Set a 5-minute wall-clock deadline
      nova capture --timeout 300 python agent.py

      # Mark AI-generated output for EU AI Act Art.50 (writes c2pa-manifest.json)
      nova capture --mark-provenance python agent.py
    """
    # Collect args: explicit Argument list + any extra args captured via context
    cmd = list(command) + list(ctx.args)
    if not cmd:
        raise typer.BadParameter("COMMAND is required", param_hint="'COMMAND'")

    # Warm-daemon delegation (ADR-0092): only for a *plain* invocation. The thin
    # client forwards argv/cwd/env only, so any flag the daemon worker does not
    # carry forces in-process execution to preserve behavior/fidelity.
    _is_plain = (
        runner_name is RunnerName.local
        and not runner_options
        and timeout is None
        and asset_ref is None
        and require_asset_status is None
        and warn_if_asset_status is None
        and not require_registered
        and not mark_provenance
        and not fast_emit
        and not emit_spool
        and output_dir is None
    )
    if daemon and _is_plain:
        from novafabric._paths import daemon_socket_path

        sock_path = daemon_socket_path()
        if sock_path.exists():
            import socket as _socket

            try:
                probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                probe.connect(str(sock_path))
                probe.close()
            except OSError:
                pass  # daemon not reachable → fall through to in-process spawn
            else:
                os.execvp("novacap", ["novacap", *cmd])  # never returns

    try:
        runner = get_runner(runner_name.value)
    except UnknownRunnerError as exc:
        raise typer.BadParameter(str(exc), param_hint="'--runner'") from exc

    options_dict: dict[str, object] = {}
    for raw in runner_options or []:
        key, value = _parse_runner_option(raw)
        options_dict[key] = value

    require_statuses = _parse_status_list(require_asset_status)
    warn_statuses = _parse_status_list(warn_if_asset_status)

    # Resolve registry DB path from env (same path used by the registry CLI).
    db_env = os.environ.get("NOVAFABRIC_DB_PATH")
    registry_db_path = Path(db_env) if db_env else None

    base_dir = output_dir or default_capsule_dir()
    orch = CaptureOrchestrator(
        base_dir=base_dir,
        runner=runner,
        mark_provenance=mark_provenance,
        fast_emit=fast_emit,
        emit_spool=emit_spool,
    )

    try:
        result = orch.run(
            command=cmd,
            runner_options=options_dict or None,
            timeout_s=timeout,
            asset_ref=asset_ref,
            require_asset_statuses=require_statuses,
            warn_if_asset_statuses=warn_statuses,
            require_registered=require_registered,
            registry_db_path=registry_db_path,
        )
    except AssetStatusCheckError as exc:
        console.print(f"[red]Asset status check failed:[/red] {exc}")
        raise typer.Exit(code=1)

    status_icon = "[green]✓[/green]" if result.exit_code == 0 else "[red]✗[/red]"
    console.print(
        f"{status_icon} Capsule written: {result.capsule_dir}  "
        f"(run_id={result.run_id})"
    )
    if result.exit_code == 0 and os.environ.get("NOVAFABRIC_SUGGEST", "1") != "0":
        _print_suggestion_hint(result.capsule_dir, registry_db_path)
    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


def _print_suggestion_hint(capsule_dir: Path, db_path: Path | None) -> None:
    """Print a brief hint if the capsule contains unregistered assets."""
    try:
        from novafabric.registry.suggestion_engine import SuggestionEngine

        engine = SuggestionEngine()
        suggestions = engine.analyze(capsule_dir, db_path=db_path)
        if not suggestions:
            return
        by_type: dict[str, int] = {}
        for s in suggestions:
            by_type[s.asset_type] = by_type.get(s.asset_type, 0) + 1
        summary = "  ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in sorted(by_type.items()))
        console.print(f"[dim]Unregistered assets detected: {summary}[/dim]")
        console.print(f"[dim]Run `nova suggest-register {capsule_dir.name}` to review.[/dim]")
        console.print("[dim](Suppress with NOVAFABRIC_SUGGEST=0)[/dim]")
    except Exception:
        pass  # suggestion hint is best-effort; never block capture output
