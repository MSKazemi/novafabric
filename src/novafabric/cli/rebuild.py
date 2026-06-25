# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""nova rebuild-metadata-db — recover metadata DB from chain log (FR-04).

Uses checkpoint-based replay: reads the latest .checkpoint.ndjson per
(tenant, run_id) and fetches only delta commits since the last checkpoint.
Completes in minutes from the checkpoint, not hours from ListObjectsV2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

app = typer.Typer(name="rebuild-metadata-db", add_completion=False)
console = Console()


@app.callback(invoke_without_command=True)
def rebuild_metadata_db(
    prefix: Annotated[
        str,
        typer.Option(
            help="Tenant or tenant/run_id prefix to scan (empty = all tenants).",
        ),
    ] = "",
    target_db: Annotated[
        str,
        typer.Option(
            help="Path to output SQLite database.  Defaults to nova-metadata-rebuild.db.",
        ),
    ] = "nova-metadata-rebuild.db",
    backend: Annotated[
        str,
        typer.Option(
            help="Storage backend: local | s3 | minio | ceph_rgw | azure_blob.",
            envvar="NOVA_OCS_BACKEND",
        ),
    ] = "local",
    data_dir: Annotated[
        Optional[str],
        typer.Option(help="Local backend: path to the object store root directory."),
    ] = None,
) -> None:
    """Rebuild the metadata database from the manifest chain log.

    Disaster-recovery path: replays the checkpoint and delta logs to
    reconstruct the metadata DB in minutes without touching capsule files.

    Scope: global (affects the whole metadata store).

    \b
    Examples:
      nova rebuild-metadata-db

      # Override data directory
      nova rebuild-metadata-db --data-dir /data/nova/
    """
    from novafabric.object_capsule_store.backend_router import make_adapter
    from novafabric.object_capsule_store.rebuild import rebuild_metadata_db as _rebuild

    kwargs = {}
    if data_dir is not None:
        kwargs["data_dir"] = data_dir
    try:
        adapter = make_adapter(backend=backend, **kwargs)
    except Exception as exc:
        console.print(f"[red]Error initialising backend {backend!r}: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    target_path = Path(target_db)
    prefix_display = prefix or "(all)"
    console.print(
        f"[bold]Rebuilding metadata DB[/bold] prefix={prefix_display!r} → {target_path}"
    )

    report = _rebuild(adapter=adapter, prefix=prefix, target_db=str(target_path))

    console.print(f"  capsules found : {report.capsules_found:,}")
    console.print(f"  elapsed        : {report.time_to_replay_seconds:.2f}s")
    if report.integrity_warnings:
        console.print(
            f"  [yellow]warnings ({len(report.integrity_warnings)})[/yellow]:"
        )
        for w in report.integrity_warnings[:20]:
            console.print(f"    [yellow]• {w}[/yellow]")
        if len(report.integrity_warnings) > 20:
            console.print(f"    … and {len(report.integrity_warnings) - 20} more")
    else:
        console.print("  [green]no integrity warnings[/green]")
    console.print(f"[green]Done.[/green] Database written to {target_path}")
