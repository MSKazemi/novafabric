"""CLI commands for the capture-level setting (cap-004, ADR-0066). Experimental.

The capture level is read from the ``NOVA_CAPTURE_LEVEL`` environment variable
and nowhere else: there is no policy store, so ``set`` validates a value and
prints the variable to export — it persists nothing.

What the level changes today: only ``forensic``/``air_gapped`` versus the rest.
At ``forensic``/``air_gapped`` the façade, framework-adapter and tool-hook paths
keep payload fields (tool arguments/results, state dicts, document text);
otherwise they keep digests. ``minimal`` and ``standard`` behave identically,
the per-field allowlists in ``policies/capture_level.py`` are not applied to any
capture path, ``air_gapped`` blocks no network call, and wire-level model-call
capture (``model-calls.jsonl``) stores full prompts and responses at every level.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    help=(
        "Show or validate the capture level (experimental). The level comes only from "
        "the NOVA_CAPTURE_LEVEL environment variable; nothing is stored."
    ),
    no_args_is_help=True,
)


@app.command("get")
def capture_level_get_cmd() -> None:
    """Show the capture level this process resolves (experimental).

    Reads NOVA_CAPTURE_LEVEL; unset or unknown values resolve to standard.
    Levels: minimal, standard, forensic, air_gapped. Only forensic and
    air_gapped change capture today: they keep payloads (tool arguments and
    results, state, documents) on the adapter, tool-hook and record-facade
    paths, which otherwise keep digests. minimal and standard behave the same,
    and model-call capture stores full prompts and responses at every level.

    Scope: environment (NOVA_CAPTURE_LEVEL).

    \b
    Examples:
      nova policy capture-level get
      NOVA_CAPTURE_LEVEL=forensic nova policy capture-level get
    """
    from novafabric.policies.capture_level import CaptureLevelPolicy

    policy = CaptureLevelPolicy.from_env()
    typer.echo(f"Current capture level: {policy.level.value}")


@app.command("set")
def capture_level_set_cmd(
    level: str = typer.Option(
        ...,
        "--level",
        help="Capture level to validate: minimal, standard, forensic, air_gapped",
    ),
) -> None:
    """Validate a capture level and print the variable to export (experimental).

    Persists nothing: there is no policy store. Export NOVA_CAPTURE_LEVEL in
    the environment of the process you capture (restart nova serve). An
    unknown level exits 1.

    Scope: none (prints an instruction).

    \b
    Examples:
      nova policy capture-level set --level forensic
    """
    from novafabric.policies.capture_level import CaptureLevel

    try:
        CaptureLevel(level)
    except ValueError:
        valid = [cl.value for cl in CaptureLevel]
        typer.echo(f"Error: unknown level {level!r}. Must be one of: {valid}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Valid level {level!r}. Nothing was saved; to use it, export it:")
    typer.echo(f"  export NOVA_CAPTURE_LEVEL={level}")
    typer.echo("then re-run `nova capture` (or restart `nova serve`).")
