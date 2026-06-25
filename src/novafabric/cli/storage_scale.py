"""CLI commands for scale-architecture storage operations (cap-003, cap-009, ADR-0066)."""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Inspect and validate WORM storage objects.",
    no_args_is_help=True,
)


@app.command("inspect")
def storage_inspect_cmd(
    run_id: str = typer.Option(..., "--run-id", help="Run identifier to inspect"),
) -> None:
    """Inspect a stored object and show its metadata.

    Scope: single storage object.

    \b
    Examples:
      nova storage inspect --run-id <run-id>
    """
    typer.echo(f"Storage objects for run_id={run_id}:")
    typer.echo(f"  audit: <store>/{run_id}_audit.json")
    typer.echo(
        f"  pii:   <store>/{run_id}_pii.json"
        "  (active by default — OQ-01 resolved ADR-0069;"
        " set NOVA_CAP003_ENABLED=false to disable)"
    )


@app.command("validate")
def storage_validate_cmd(
    endpoint: str = typer.Option(
        None,
        "--endpoint",
        envvar="NOVA_S3_ENDPOINT_URL",
        help="S3 endpoint URL",
    ),
    bucket: str = typer.Option(
        "nova-capsules",
        "--bucket",
        envvar="NOVA_S3_BUCKET",
        help="S3 bucket name",
    ),
) -> None:
    """Validate that S3 Object Lock is correctly configured for WORM compliance.

    Scope: storage backend.

    \b
    Examples:
      nova storage validate
      nova storage validate --endpoint http://minio:9000 --bucket my-capsules
    """
    from novafabric.storage.nova_object_store import (
        NovaObjectStore,
        ObjectLockNotSupportedError,
    )

    store = NovaObjectStore(endpoint_url=endpoint, bucket=bucket)
    try:
        info = store.validate()
        typer.echo(f"S3 backend validated: {info}")
    except ObjectLockNotSupportedError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"Error connecting to S3: {exc}", err=True)
        raise typer.Exit(code=1)
