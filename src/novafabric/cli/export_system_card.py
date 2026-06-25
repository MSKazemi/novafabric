from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric._paths import nova_home
from novafabric.compliance.system_card import SystemCardGenerator
from novafabric.evidence.signing import LocalSigner, generate_keypair

console = Console()


def _resolve_signer(key: Path | None) -> LocalSigner:
    """Load *key* if given, else create/load a local keypair under $NOVAFABRIC_HOME/keys."""
    if key is not None:
        return LocalSigner(key)
    keys_dir = nova_home() / "keys"
    priv_path = keys_dir / "ed25519.pem"
    if not priv_path.exists():
        generate_keypair(keys_dir)
    return LocalSigner(priv_path)


def export_system_card_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Path to a Run Capsule directory.",
        ),
    ],
    output: Annotated[
        Optional[Path],
        typer.Option(
            "--output",
            "-o",
            help="Path to write the sealed system-card DSSE envelope (JSON). "
            "Default: <capsule_dir>/system-card.intoto.json.",
        ),
    ] = None,
    key: Annotated[
        Optional[Path],
        typer.Option(
            "--key",
            exists=True,
            dir_okay=False,
            readable=True,
            help="PEM-encoded ed25519 private key to seal with. "
            "If omitted, a local key under $NOVAFABRIC_HOME/keys is created/used.",
        ),
    ] = None,
) -> None:
    """Generate and SEAL an auto-generated system/audit card (ADR-0085).

    Assembles a card from capsule + eval + lineage facts and seals it with the
    existing DSSE/Ed25519 path. The card is generated, never hand-written, so it
    cannot drift from the capsule it describes. The sealed output verifies with
    the same verifier used for Evidence Bundles.

    Scope: single capsule.

    \b
    Examples:
      nova export-system-card path/to/my-capsule/
      nova export-system-card path/to/my-capsule/ -o card.intoto.json --key ed25519.pem
    """
    if not (capsule_dir / "capsule.yaml").exists():
        console.print(f"[red]✗[/red] Not a valid capsule directory: {capsule_dir}")
        raise typer.Exit(code=1)

    out_path = output or (capsule_dir / "system-card.intoto.json")

    try:
        signer = _resolve_signer(key)
    except Exception as exc:
        console.print(f"[red]✗[/red] failed to load signing key: {exc}")
        raise typer.Exit(code=1) from exc

    gen = SystemCardGenerator()
    try:
        card = gen.build_card(capsule_dir)
        envelope = gen.seal_card(card, signer)
    except Exception as exc:
        console.print(f"[red]✗[/red] system-card generation failed: {exc}")
        raise typer.Exit(code=1) from exc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(envelope, indent=2, sort_keys=True))
    console.print(
        f"[green]✓[/green] sealed system-card written: {out_path} "
        f"(run_id={card['run_id']}, keyid={signer.keyid})"
    )
