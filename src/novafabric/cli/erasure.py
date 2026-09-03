"""``nova erasure`` — NOT IMPLEMENTED; see ``nova pii erase`` (cap-003, ADR-0066).

Both subcommands here were stubs that printed a success message and did nothing.
``request`` said "GDPR erasure request queued" — writing no queue row and
destroying no key, exiting 0 even for a run id that did not exist — and
``status`` returned "pending" for any id, including one never created.

On a GDPR Art.17 surface that is the most damaging possible failure: the operator
records an erasure as started, and nothing started. They now fail loudly and name
the working paths rather than reporting a success that did not happen.

Working today: ``nova pii erase <subject_id>`` (DEK crypto-shredding, ADR-0069)
and the ``/v0/erasure`` server route, which uses the persisted queue in
``pii/erasure_queue.py`` behind an explicit confirmation and a fail-closed
cap-003 gate.
"""

from __future__ import annotations

import typer
from rich.console import Console

err_console = Console(stderr=True)

app = typer.Typer(
    help="NOT IMPLEMENTED — use `nova pii erase` for GDPR Art.17 erasure.",
    no_args_is_help=True,
)


@app.command("request")
def erasure_request_cmd(
    run_id: str = typer.Option(
        ..., "--run-id", help="Run ID whose PII object should be erased"
    ),
) -> None:
    """NOT IMPLEMENTED — use ``nova pii erase <subject_id>``.

    This command previously printed "GDPR erasure request queued" and **did
    nothing**: it wrote no queue row, destroyed no key, and exited 0 — including
    for a run id that did not exist. On a GDPR Art.17 surface a false success is
    the most damaging possible behaviour, because the operator records an
    erasure that never started.

    The real implementations are ``nova pii erase`` (crypto-shredding, ADR-0069)
    and the ``/v0/erasure`` server route, which uses the persisted queue in
    ``pii/erasure_queue.py`` behind an explicit confirmation and a fail-closed
    cap-003 gate. Wiring this command to that queue is a change to what a
    compliance command does and is left as an owner decision (ADR-0210).
    """
    err_console.print(
        "[red]nova erasure request is not implemented.[/red] It never queued "
        "anything; it only printed a message.\n"
        "  Use [bold]nova pii erase <subject_id>[/bold] for GDPR Art.17 "
        "crypto-shredding (ADR-0069),\n"
        "  or POST /v0/erasure on the server, which uses the persisted queue.\n"
        f"  (nothing was done for run_id={run_id})"
    )
    raise typer.Exit(2)


@app.command("status")
def erasure_status_cmd(
    request_id: str = typer.Option(
        ..., "--request-id", help="Erasure request identifier"
    ),
) -> None:
    """NOT IMPLEMENTED — this reported ``status=pending`` for any id.

    It consulted nothing: the same "pending" was returned for a request id that
    had never been created. A status surface that cannot distinguish "pending"
    from "never existed" is worse than none on a compliance path.
    """
    err_console.print(
        "[red]nova erasure status is not implemented.[/red] It reported "
        "'pending' for every id, including ids that were never created.\n"
        "  Erasure receipts written by [bold]nova pii erase[/bold] are the "
        "record of what actually happened.\n"
        f"  (no status was looked up for request_id={request_id})"
    )
    raise typer.Exit(2)
