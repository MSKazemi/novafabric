"""CLI commands for lineage store migration and deployment profile generation."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()

lineage_store_app = typer.Typer(
    name="lineage-store",
    help=(
        "Lineage store migration (into a SQLite lineage store) and deployment "
        "profiles (KuzuDB, JanusGraph)."
    ),
    no_args_is_help=True,
)

migrate_lineage_app = typer.Typer(
    name="migrate-lineage-store",
    help="Migrate lineage edges between backends.",
    no_args_is_help=True,
)


@lineage_store_app.command("migrate")
def migrate_cmd(
    parquet: Annotated[
        Path | None,
        typer.Argument(
            help="Source Parquet file containing lineage edges (deprecated; use --from-ocs)"
        ),
    ] = None,
    db: Annotated[
        Path,
        typer.Option("--db", help="Target SQLite database path"),
    ] = Path("lineage.db"),
    commit: Annotated[
        bool,
        typer.Option("--commit/--dry-run", help="Commit to target store (default: dry run)"),
    ] = False,
    no_validate: Annotated[
        bool,
        typer.Option("--no-validate", help="Skip post-load divergence check"),
    ] = False,
    from_ocs: Annotated[
        bool,
        typer.Option(
            "--from-ocs",
            help=(
                "Not implemented: refuses with exit 1. NovaFabric has no on-disk "
                "ObjectCapsuleStore reader for this command (ADR-0022 path planned)."
            ),
        ),
    ] = False,
    ocs_tenant: Annotated[
        str,
        typer.Option("--ocs-tenant", help="OCS tenant identifier (used with --from-ocs)"),
    ] = "",
    ocs_run_ids: Annotated[
        str,
        typer.Option(
            "--ocs-run-ids",
            help="Comma-separated list of run_ids to migrate (used with --from-ocs)",
        ),
    ] = "",
    ocs_data_dir: Annotated[
        Path,
        typer.Option(
            "--ocs-data-dir",
            help="Not read: --from-ocs is not implemented (kept for compatibility).",
        ),
    ] = Path("."),
) -> None:
    """Migrate lineage edges from a Parquet file into a SQLite lineage store.

    Defaults to dry-run mode; pass --commit to write to the target store.
    Post-migration divergence check runs automatically unless --no-validate is set.

    --from-ocs is not implemented: NovaFabric has no reader for an on-disk
    ObjectCapsuleStore here, so it refuses (exit 1) and writes nothing rather
    than report an empty migration as a success.

    Scope: lineage store (SQLite target).

    \b
    Examples:
      # Dry-run first, then commit (Parquet source; deprecated path)
      nova lineage-store migrate edges.parquet --db lineage.db
      nova lineage-store migrate edges.parquet --db lineage.db --commit
    """
    from novafabric.lineage.migration.kit import MigrationDivergenceError

    if from_ocs:
        # H5: this built an empty in-memory store, never read --ocs-data-dir, and
        # printed "ok loaded=0 … committed=yes" with exit 0. There is no on-disk
        # ObjectCapsuleStore backend to read (backend_router's "local" is in-memory
        # and test-only), so refuse before creating the target store.
        console.print(
            "[red]x[/red] not implemented: `--from-ocs` cannot read an "
            f"ObjectCapsuleStore. --ocs-data-dir ({ocs_data_dir}) was not read and "
            "nothing was migrated; no target store was created. NovaFabric has no "
            "on-disk ObjectCapsuleStore backend for this command (planned, ADR-0022). "
            "Re-index local capsules with `nova lineage import <capsule-dir>` instead."
        )
        raise typer.Exit(code=1)

    # Legacy Parquet-backed path (deprecated).
    from novafabric.lineage.backends.sqlite import SqliteLineageStore
    from novafabric.lineage.migration.kit import migrate

    if parquet is None:
        console.print("[red]x[/red] Provide a Parquet source file.")
        raise typer.Exit(code=1)

    if not parquet.exists():
        console.print(f"[red]x[/red] Parquet file not found: {parquet}")
        raise typer.Exit(code=1)

    target = SqliteLineageStore(db_path=db)

    console.print("[bold]Migration plan (Parquet — deprecated)[/bold]")
    console.print(f"  Source: {parquet}")
    console.print(f"  Target: {db}")
    console.print(f"  Mode:   {'commit' if commit else 'dry-run'}")
    console.print(f"  Validate: {not no_validate}")

    try:
        result = migrate(
            source_parquet=parquet,
            target=target,
            validate=not no_validate,
            commit=commit,
        )
    except MigrationDivergenceError as exc:
        console.print(f"[red]x[/red] Divergence detected: {exc}")
        raise typer.Exit(code=2) from exc

    committed = result["committed"]
    console.print(
        f"[green]ok[/green] loaded={result['loaded']} "
        f"diverged={result['diverged']} "
        f"committed={'yes' if committed else 'no (dry-run)'}"
    )


@lineage_store_app.command("profile")
def profile_cmd(
    target: Annotated[
        str,
        typer.Option("--target", help="Profile target: kuzudb-vertical | janusgraph-minimal"),
    ] = "kuzudb-vertical",
    node_size: Annotated[
        str,
        typer.Option("--node-size", help="Node size tag for kuzudb-vertical"),
    ] = "16g-ram-500g-nvme",
    replication_factor: Annotated[
        int,
        typer.Option("--rf", help="Cassandra replication factor for janusgraph-minimal"),
    ] = 3,
    image_tag: Annotated[
        str | None,
        typer.Option(
            "--image-tag",
            help=(
                "Docker image tag. For kuzudb-vertical this sets the single "
                "nova-lineage image tag. Deprecated for janusgraph-minimal: "
                "overrides all three of that profile's images "
                "(janusgraph/cassandra/novafabric) to this one value, rather "
                "than each image's own independently-pinned default — kept "
                "for backward compatibility only."
            ),
        ),
    ] = None,
) -> None:
    """Print a docker-compose deployment profile for the chosen lineage backend.

    Outputs a ready-to-use docker-compose YAML for either a KuzuDB vertical
    single-node deployment or a JanusGraph + Cassandra minimal cluster.

    Scope: deployment configuration (stdout only, no filesystem writes).

    \b
    Examples:
      # KuzuDB vertical single-node (default)
      nova lineage-store profile

      # KuzuDB with a specific node size tag
      nova lineage-store profile --target kuzudb-vertical --node-size 32g-ram-1t-nvme

      # JanusGraph minimal cluster with replication factor 1 (dev)
      nova lineage-store profile --target janusgraph-minimal --rf 1
    """
    if target == "kuzudb-vertical":
        from novafabric.lineage.profiles.kuzudb_vertical import generate_kuzudb_vertical_profile

        kuzudb_kwargs: dict[str, str] = {"node_size": node_size}
        if image_tag is not None:
            kuzudb_kwargs["image_tag"] = image_tag
        typer.echo(generate_kuzudb_vertical_profile(**kuzudb_kwargs))
    elif target == "janusgraph-minimal":
        from novafabric.lineage.profiles.janusgraph_minimal import (
            generate_janusgraph_minimal_profile,
        )

        typer.echo(
            generate_janusgraph_minimal_profile(
                replication_factor=replication_factor, image_tag=image_tag
            )
        )
    else:
        console.print(
            f"[red]x[/red] Unknown target '{target}'. "
            "Choose: kuzudb-vertical | janusgraph-minimal"
        )
        raise typer.Exit(code=1)
