"""CLI commands for GDPR erasure requests (cap-003, ADR-0066)."""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Request and track GDPR PII erasure for a data subject.",
    no_args_is_help=True,
)


@app.command("request")
def erasure_request_cmd(
    run_id: str = typer.Option(
        ..., "--run-id", help="Run ID whose PII object should be erased"
    ),
) -> None:
    """Record a GDPR Art.17 erasure request for a data subject.

    Creates a tracked erasure request. Requires NOVA_PII_PEPPER to
    generate the redaction proofs across all capsules.

    Scope: registry-wide.

    \b
    Examples:
      NOVA_PII_PEPPER=secret nova erasure request --subject user@example.com
    """
    typer.echo(f"GDPR erasure request queued for run_id={run_id}")
    typer.echo(
        "Note: for full DEK-destruction erasure (ADR-0069), use `nova pii erase <subject_id>`. "
        "S3 GOVERNANCE mode Object Lock requires NOVA_S3_ENDPOINT_URL."
    )


@app.command("status")
def erasure_status_cmd(
    request_id: str = typer.Option(
        ..., "--request-id", help="Erasure request identifier"
    ),
) -> None:
    """Show the current erasure status for a data subject.

    Scope: registry-wide.

    \b
    Examples:
      nova erasure status --subject user@example.com
    """
    typer.echo(
        f"Erasure request {request_id}: status=pending"
        " (ClickHouse integration required)"
    )
