"""nova verify — verify a capsule's NovaSeal signature, timestamp, and Merkle log."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def verify_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Path to the capsule directory to verify"),
    ],
    seal_config: Annotated[
        str | None,
        typer.Option(
            "--seal-config",
            envvar="NOVAFABRIC_SEAL_CONFIG",
            help="Path to novaseal.yaml config (default: ~/.novafabric/novaseal.yaml)",
        ),
    ] = None,
    check_redaction: Annotated[
        Optional[Path],
        typer.Option(
            "--check-redaction",
            help=(
                "Path to a redaction_proof_report.seal.json file. "
                "Verifies the NovaSeal DSSE envelope of the proof report "
                "independently of the capsule seal check."
            ),
            exists=False,
            dir_okay=False,
        ),
    ] = None,
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help=(
                "Verification backend: 'local' (ECDSA P-256 DSSE, default) or "
                "'sigstore' (Sigstore bundle verification). "
                r'Requires pip install "novafabric\[sigstore]" for the sigstore backend.'
            ),
        ),
    ] = "local",
    capsule_id: Annotated[
        str,
        typer.Option(
            "--capsule-id",
            help=(
                "Capsule ID for Sigstore bundle lookup "
                "(required when --backend sigstore is used without a .seal/ dir)."
            ),
        ),
    ] = "",
    home: Annotated[
        str,
        typer.Option(
            "--home",
            help="NovaFabric home dir for Sigstore bundle lookup (default: ~/.novafabric).",
        ),
    ] = "",
) -> None:
    """Verify a capsule's cryptographic seal, timestamp, and Merkle log inclusion.

    Checks three layers of integrity in the .seal/ directory (local backend):
      • ECDSA P-256 DSSE signature
      • RFC 3161 timestamp (structural hash)
      • Merkle log inclusion proof

    With ``--backend sigstore``, verifies the Sigstore bundle stored under
    ``<home>/sigstore/<capsule_id>.bundle.json``.

    Exits 0 if all checks pass, 1 otherwise.
    Requires NovaSeal config (novaseal.yaml or NOVAFABRIC_SEAL_CONFIG env var)
    for the local backend.

    Scope: single capsule.

    \b
    Examples:
      # Verify a capsule (local ECDSA backend)
      nova verify path/to/my-capsule/

      # Use an explicit seal config path
      nova verify --seal-config ~/configs/novaseal.yaml path/to/my-capsule/

      # Verify a redaction proof report seal independently
      nova verify --check-redaction path/to/report.seal.json path/to/my-capsule/

      # Verify a Sigstore bundle
      nova verify --backend sigstore --capsule-id <capsule-id> path/to/my-capsule/
    """
    # Handle --check-redaction standalone check
    if check_redaction is not None:
        _verify_redaction_seal(check_redaction)
        return

    # Handle Sigstore backend path
    if backend == "sigstore":
        _verify_sigstore(capsule_dir, capsule_id=capsule_id, home=home)
        return

    if backend not in ("local", "sigstore"):
        err_console.print(
            f"[red]Error:[/red] Unknown backend {backend!r}. "
            "Choose 'local' or 'sigstore'."
        )
        raise typer.Exit(code=1)

    if not capsule_dir.exists():
        console.print(f"[red]Error:[/red] capsule directory not found: {capsule_dir}")
        raise typer.Exit(code=1)

    seal_dir = capsule_dir / ".seal"
    if not seal_dir.exists():
        console.print(
            f"[yellow]No .seal/ directory found in {capsule_dir}[/yellow]\n"
            "This capsule was not sealed with NovaSeal. "
            "Run `nova capture` with a novaseal.yaml config to produce sealed capsules."
        )
        raise typer.Exit(code=1)

    # Load signing profile for Merkle DB location
    import os
    if seal_config:
        os.environ["NOVAFABRIC_SEAL_CONFIG"] = seal_config

    try:
        from novafabric.trust.novaseal.config import SealConfigError, load_signing_profile
        profile = load_signing_profile()
    except SealConfigError as exc:
        console.print(f"[red]NovaSeal config error:[/red] {exc}")
        raise typer.Exit(code=1)

    if profile is None:
        console.print(
            "[yellow]NovaSeal is not configured.[/yellow]\n"
            "Create ~/.novafabric/novaseal.yaml or set NOVAFABRIC_SEAL_CONFIG "
            "to enable cryptographic verification."
        )
        raise typer.Exit(code=1)

    from novafabric.trust.novaseal import KeyConfig, NovaSeal

    config = KeyConfig(
        profile=profile.profile,
        key_path=str(profile.key_path),
        cert_path=str(profile.cert_path),
    )
    seal = NovaSeal(
        config=config,
        tsa_url=profile.tsa_url,
        db_path=str(profile.merkle_db),
    )

    # Read capsule_id from log-entry.json
    import json
    log_file = seal_dir / "log-entry.json"
    capsule_id = ""
    if log_file.exists():
        try:
            entry = json.loads(log_file.read_bytes())
            capsule_id = entry.get("entry", {}).get("capsule_id", "")
        except Exception:
            pass

    result = seal.verify(capsule_id=capsule_id, seal_dir=str(seal_dir))

    # Print results
    console.print(f"\n[bold]NovaSeal verification:[/bold] {capsule_dir.name}")
    _print_check("Signature (DSSE ECDSA P-256)", result.signature_ok)
    if result.signing_intent is not None:
        console.print(f"    Intent: {result.signing_intent.value}")
    _print_check("Timestamp (RFC 3161)", result.timestamp_ok)
    _print_check("Merkle log inclusion", result.log_integrity_ok)

    if result.errors:
        console.print()
        for err in result.errors:
            console.print(f"  [red]✗[/red] {err}")

    console.print()
    console.print(str(result))

    if not result.valid:
        raise typer.Exit(code=1)


def _print_check(label: str, ok: bool) -> None:
    icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
    status = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
    console.print(f"  {icon} {label}: {status}")


def _verify_sigstore(
    capsule_dir: Path,
    *,
    capsule_id: str,
    home: str,
) -> None:
    """Verify a Sigstore bundle for *capsule_dir*.

    Looks up the bundle at ``<home>/sigstore/<capsule_id>.bundle.json``
    and verifies it against the capsule manifest.  If no bundle is found,
    falls back gracefully with an explanatory message and exits 1.
    """
    # Check sigstore package is installed
    try:
        import sigstore  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        err_console.print(
            "[red]Error:[/red] Sigstore backend requires: "
            r"pip install novafabric\[sigstore]"
        )
        raise typer.Exit(code=1)

    import hashlib

    from novafabric.trust.novaseal.sigstore_signer import (
        SigstoreBundleStore,
        SigstoreSigner,
    )

    home_path = Path(home) if home else Path.home() / ".novafabric"

    # Determine capsule_id: use provided or compute from manifest
    effective_capsule_id = capsule_id
    manifest_bytes: bytes | None = None

    if capsule_dir.exists():
        # Try to read manifest from capsule dir
        for candidate in ("manifest.json", "capsule.json"):
            manifest_file = capsule_dir / candidate
            if manifest_file.exists():
                manifest_bytes = manifest_file.read_bytes()
                if not effective_capsule_id:
                    effective_capsule_id = hashlib.sha256(manifest_bytes).hexdigest()
                break

    if not effective_capsule_id:
        err_console.print(
            "[red]Error:[/red] Cannot determine capsule ID. "
            "Provide --capsule-id or point to a capsule directory with manifest.json."
        )
        raise typer.Exit(code=1)

    bundle_dict = SigstoreBundleStore.load_bundle(effective_capsule_id, home_path)
    if bundle_dict is None:
        err_console.print(
            f"[red]Error:[/red] No Sigstore bundle found for capsule {effective_capsule_id!r}. "
            f"Run [bold]nova seal sign --backend sigstore[/bold] first."
        )
        raise typer.Exit(code=1)

    # Verify bundle; artifact_bytes defaults to manifest if available
    artifact_bytes = manifest_bytes or effective_capsule_id.encode("utf-8")
    signer = SigstoreSigner()
    result = signer.verify_bundle(bundle_dict, artifact_bytes)

    dir_name = capsule_dir.name if capsule_dir != Path("") else effective_capsule_id
    console.print(f"\n[bold]Sigstore verification:[/bold] {dir_name}")
    _print_check("Sigstore bundle signature + Rekor inclusion", result.valid)
    if result.identity:
        console.print(f"    Identity: {result.identity}")
    if result.rekor_log_index is not None:
        console.print(f"    Rekor log index: {result.rekor_log_index}")

    if result.error:
        console.print()
        console.print(f"  [red]✗[/red] {result.error}")

    console.print()
    console.print(str(result))

    if not result.valid:
        raise typer.Exit(code=1)


def _verify_redaction_seal(seal_path: Path) -> None:
    """Verify a redaction proof report DSSE seal (G-CROSS-004).

    Checks the DSSE envelope in *seal_path* and reports the result.
    Exits 0 on success, 1 on failure.
    """
    if not seal_path.exists():
        console.print(f"[red]Error:[/red] seal file not found: {seal_path}")
        raise typer.Exit(code=1)

    try:
        from novafabric.trust.novaseal.envelope import (
            extract_intent,
            verify_envelope,
        )
        seal_bytes = seal_path.read_bytes()
        verify_envelope(seal_bytes)
        intent = extract_intent(seal_bytes)
        console.print(f"\n[bold]Redaction proof seal:[/bold] {seal_path.name}")
        _print_check("Signature (DSSE ECDSA P-256)", True)
        if intent is not None:
            console.print(f"    Intent: {intent.value}")
        console.print("\n[green]Redaction proof seal is VALID[/green]")
    except Exception as exc:
        console.print(f"\n[bold]Redaction proof seal:[/bold] {seal_path.name}")
        _print_check("Signature (DSSE ECDSA P-256)", False)
        console.print(f"\n[red]✗[/red] {exc}")
        console.print("\n[red]Redaction proof seal is INVALID[/red]")
        raise typer.Exit(code=1)
