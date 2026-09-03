"""CLI commands for scale-architecture storage operations (cap-003, cap-009, ADR-0066)."""

from __future__ import annotations

import os

import typer

app = typer.Typer(
    help="Inspect and validate WORM storage objects.",
    no_args_is_help=True,
)


def _s3_configured() -> bool:
    """True when the S3 backend is configured, i.e. the S3 key layout applies."""
    return bool(os.getenv("NOVA_S3_ENDPOINT_URL") or os.getenv("NOVA_S3_BUCKET"))


@app.command("inspect")
def storage_inspect_cmd(
    run_id: str = typer.Option(..., "--run-id", help="Run identifier to inspect"),
    output_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of text"
    ),
) -> None:
    """Show where a run's dual-object split is stored, under the layout in effect.

    This computes object names from the run id and the configured backend. It does
    **not** contact the store, so it reports a location, never an existence claim:
    an unknown run id yields keys just as readily as a real one.

    Scope: single run, name computation only.

    \b
    Examples:
      nova storage inspect --run-id <run-id>
      NOVA_S3_BUCKET=nova-capsules nova storage inspect --run-id <run-id> --json
    """
    import json

    from novafabric.storage.dual_object_store import (
        local_audit_filename,
        local_pii_filename,
        s3_audit_key,
        s3_pii_key,
    )

    cap003 = os.getenv("NOVA_CAP003_ENABLED", "false").lower() == "true"
    s3 = _s3_configured()

    audit = s3_audit_key(run_id) if s3 else local_audit_filename(run_id)
    pii = (s3_pii_key(run_id) if s3 else local_pii_filename(run_id)) if cap003 else None

    payload = {
        "run_id": run_id,
        "layout": "s3" if s3 else "local",
        "audit_object_key": audit,
        "pii_object_key": pii,
        "cap003_enabled": cap003,
        "existence_checked": False,
    }

    if output_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    where = (
        f"bucket {os.getenv('NOVA_S3_BUCKET', '<NOVA_S3_BUCKET>')}"
        if s3
        else "the local output directory passed to the writer"
    )
    typer.echo(f"Dual-object split for run_id={run_id}  (layout: {payload['layout']}, in {where})")
    typer.echo(f"  audit: {audit}")
    if pii is None:
        typer.echo("  pii:   (none — cap-003 disabled; set NOVA_CAP003_ENABLED=true to write it)")
    else:
        typer.echo(f"  pii:   {pii}")
    typer.echo("  note:  computed from the run id; the store was not contacted.")


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
