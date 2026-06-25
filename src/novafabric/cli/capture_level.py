"""CLI commands for capture-level policy management (cap-004, ADR-0066)."""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Get or set the capture-level policy for new runs.",
    no_args_is_help=True,
)


@app.command("get")
def capture_level_get_cmd() -> None:
    """Show the current capture-level policy.

    Capture levels: full, metadata-only, events-only, off.

    Scope: policy store.

    \b
    Examples:
      nova policy capture-level get
    """
    from novafabric.policies.capture_level import CaptureLevelPolicy

    policy = CaptureLevelPolicy.from_env()
    typer.echo(f"Current capture level: {policy.level.value}")


@app.command("set")
def capture_level_set_cmd(
    level: str = typer.Option(
        ...,
        "--level",
        help="Capture level: minimal, standard, forensic, air_gapped",
    ),
) -> None:
    """Set the capture-level policy for new runs.

    Scope: policy store.

    \b
    Examples:
      nova policy capture-level set full
      nova policy capture-level set metadata-only
    """
    from novafabric.policies.capture_level import CaptureLevel

    try:
        CaptureLevel(level)
    except ValueError:
        valid = [cl.value for cl in CaptureLevel]
        typer.echo(f"Error: unknown level {level!r}. Must be one of: {valid}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Set NOVA_CAPTURE_LEVEL={level} (restart required)")
