from __future__ import annotations

import stat
from pathlib import Path
from typing import Annotated, Optional

import typer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from rich.console import Console

from novafabric._paths import nova_home

console = Console()

_SUBDIRS = ("capsules", "keys", "replays")


def init_cmd(
    home: Annotated[
        Optional[Path],
        typer.Option(
            "--home",
            help="Override NOVAFABRIC_HOME for this init.",
            show_default=False,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Regenerate keypair even if one already exists.",
        ),
    ] = False,
) -> None:
    """Set up a local NovaFabric installation.

    Creates the NOVAFABRIC_HOME directory tree (capsules/, keys/, replays/)
    and generates a local Ed25519 key pair (mode 600). Idempotent — safe
    to run multiple times.

    Scope: global (one-time setup).

    \b
    Examples:
      # Default setup (uses NOVAFABRIC_HOME or ~/.novafabric)
      nova init

      # Custom home directory
      nova init --home /data/novafabric

      # Re-generate keys even if they already exist
      nova init --force
    """
    root: Path = home or nova_home()

    for sub in _SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)

    key_path = root / "keys" / "signing_key.pem"
    pub_path = root / "keys" / "signing_key.pub.pem"
    already_existed = key_path.exists()

    if already_existed and not force:
        console.print(f"[green]Already initialized:[/green] {root}")
        console.print("[dim]Use --force to regenerate the signing keypair.[/dim]")
        return

    private_key = Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub_pem = private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    key_path.write_bytes(priv_pem)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    pub_path.write_bytes(pub_pem)

    action = "Reinitialized" if already_existed else "Initialized"
    console.print(f"[green]{action}:[/green] {root}")
    console.print(f"  capsules  → {root / 'capsules'}")
    console.print(f"  keys      → {key_path}")
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  nova capture python my_agent.py")
    console.print("  nova serve --experimental")
