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
"""nova worm-test CLI (FR-10).

Usage:
    nova-worm-test --backend minio --endpoint http://localhost:9000 \\
        --bucket my-worm-bucket --framework sec-17a-4 --output report.json
"""

from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from nova_worm_conformance.frameworks import VALID_BACKENDS, VALID_FRAMEWORKS

app = typer.Typer(
    name="nova-worm-test",
    help="WORM behavioral test suite for NovaFabric-compatible object storage backends.",
)
console = Console()


@app.command()
def run_suite(
    backend: Annotated[
        str, typer.Option(help=f"Backend type. One of: {', '.join(VALID_BACKENDS)}")
    ] = "minio",
    endpoint: Annotated[str, typer.Option(help="Backend endpoint URL")] = "http://localhost:9000",
    bucket: Annotated[str, typer.Option(help="Target bucket name")] = "nova-worm-test",
    framework: Annotated[
        str, typer.Option(help=f"Regulatory framework. One of: {', '.join(VALID_FRAMEWORKS)}")
    ] = "sec-17a-4",
    output: Annotated[Optional[str], typer.Option(help="Output JSON report path")] = None,
    sign: Annotated[
        bool, typer.Option("--sign/--no-sign", help="Sign report with NovaSeal (FR-13)")
    ] = False,
    signing_key: Annotated[
        Optional[str],
        typer.Option(help="PEM ECDSA P-256 private key for --sign (NovaSeal LocalSigningBackend)"),
    ] = None,
    signing_cert: Annotated[
        Optional[str],
        typer.Option(help="PEM X.509 certificate matching --signing-key, for signer identity"),
    ] = None,
    access_key: Annotated[
        str, typer.Option(help="Storage access key / client ID", envvar="WORM_ACCESS_KEY")
    ] = "minioadmin",
    secret_key: Annotated[
        str, typer.Option(help="Storage secret key", envvar="WORM_SECRET_KEY")
    ] = "minioadmin",
) -> None:
    """Run the WORM conformance suite against a backend."""
    if framework not in VALID_FRAMEWORKS:
        console.print(f"[red]Unknown framework: {framework!r}[/red]")
        console.print(f"Valid: {', '.join(VALID_FRAMEWORKS)}")
        raise typer.Exit(1)

    if backend not in VALID_BACKENDS + ["s3"]:
        console.print(f"[red]Unknown backend: {backend!r}[/red]")
        raise typer.Exit(1)

    console.print("[bold]WORM Conformance Suite[/bold]")
    console.print(f"  Backend:   {backend}")
    console.print(f"  Endpoint:  {endpoint}")
    console.print(f"  Bucket:    {bucket}")
    console.print(f"  Framework: {framework}")
    console.print()

    # Build the S3-compatible client
    try:
        client = _build_client(backend, endpoint, access_key, secret_key)
    except ImportError as e:
        console.print(f"[red]Missing dependency: {e}[/red]")
        console.print("Install with: pip install nova-worm-conformance boto3")
        raise typer.Exit(2)

    from nova_worm_conformance.suite import WormConformanceSuite
    suite = WormConformanceSuite(
        client=client,
        backend=backend,
        bucket=bucket,
        framework=framework,
        endpoint=endpoint,
    )

    with console.status("Running tests..."):
        report = suite.run()

    # Optionally sign the report — honestly. A real signature requires the
    # NovaSeal backend plus a key/cert; otherwise the report is left unsigned
    # (with an integrity digest), never stamped with a hash pretending to be one.
    if sign:
        from pathlib import Path

        from nova_worm_conformance.signing import apply_signing

        apply_signing(
            report,
            key_path=Path(signing_key) if signing_key else None,
            cert_path=Path(signing_cert) if signing_cert else None,
        )
        if report.signing_status == "signed":
            console.print(f"[green]Report signed[/green] ({report.signing_method})")
        else:
            console.print(f"[yellow]Report NOT signed[/yellow]: {report.signing_detail}")
            console.print(f"  content_sha256 = {report.content_sha256}")

    # Display results table
    table = Table(title="WORM Conformance Results")
    table.add_column("Test", style="cyan")
    table.add_column("Result", style="bold")
    table.add_column("Error")

    for record in report.records:
        status = "[green]PASS[/green]" if record.passed else "[red]FAIL[/red]"
        table.add_row(record.test_name, status, record.error or "")

    console.print(table)
    console.print(
        f"\n[bold]{'PASSED' if report.all_passed else 'FAILED'}[/bold]: "
        f"{report.passed}/{report.total} tests passed"
    )

    if output:
        report.write(output)
        console.print(f"Report written to {output}")

    raise typer.Exit(0 if report.all_passed else 1)


def _build_client(backend: str, endpoint: str, access_key: str, secret_key: str) -> object:
    import boto3  # type: ignore[import-untyped]

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


if __name__ == "__main__":
    app()
