from __future__ import annotations

import datetime
import os
import shutil
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
err_console = Console(stderr=True)

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
            help=(
                "Regenerate the keypair even if one already exists. The old pair is "
                "moved to keys/archive/<UTC>/, never deleted."
            ),
        ),
    ] = False,
) -> None:
    """Set up a local NovaFabric installation.

    Creates the NOVAFABRIC_HOME directory tree (capsules/, keys/, replays/)
    and generates a local Ed25519 key pair (mode 600) for signing Evidence
    Bundle exports. Idempotent — safe to run multiple times.

    It does not turn on capsule sealing: that is the explicit, optional
    `nova seal init` step, which it offers as a next step (ADR-0301).

    Scope: global (one-time setup).

    \b
    Examples:
      # Default setup (uses NOVAFABRIC_HOME or ~/.novafabric)
      nova init

      # Custom home directory
      nova init --home /data/novafabric

      # Re-generate keys (the old pair is archived under keys/archive/)
      nova init --force

      # Optional next step: seal new capsules with a local, self-asserted identity
      nova seal init
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
        _print_sealing_status(root)
        return

    archive_dir: Optional[Path] = None
    if already_existed:
        _refuse_if_sealing_uses(root, key_path)
        archive_dir = _archive_keypair(root, key_path, pub_path)

    private_key = Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub_pem = private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    # Created 0600 from the first byte — never briefly readable under the umask.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "wb") as handle:
        handle.write(priv_pem)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    pub_path.write_bytes(pub_pem)

    action = "Reinitialized" if already_existed else "Initialized"
    console.print(f"[green]{action}:[/green] {root}")
    console.print(f"  capsules  → {root / 'capsules'}")
    console.print(f"  keys      → {key_path}")
    if archive_dir is not None:
        console.print(
            f"[yellow]⚠ The previous keypair was archived to {archive_dir}.[/yellow] "
            "Evidence Bundles signed with it verify only with the archived public key."
        )
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  nova capture python my_agent.py")
    console.print("  nova serve --experimental")
    _print_sealing_status(root)


def _print_sealing_status(root: Path) -> None:
    """Offer `nova seal init` — never run it (ADR-0301: sealing is an explicit choice)."""
    if (root / "novaseal.yaml").exists():
        console.print(f"[dim]Capsule sealing: configured ({root / 'novaseal.yaml'}).[/dim]")
        return
    console.print(
        "  nova seal init                  # optional: seal new capsules with a local, "
        "self-asserted identity (offline)"
    )
    console.print(
        "[dim]Capsule sealing is off until you run `nova seal init` (or configure "
        "novaseal.yaml).[/dim]"
    )


def _refuse_if_sealing_uses(root: Path, key_path: Path) -> None:
    """Exit 1 when novaseal.yaml signs with this key: rotating it would orphan the cert."""
    config = root / "novaseal.yaml"
    if not config.exists():
        return
    try:
        import yaml

        raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unreadable config cannot reference the key
        return
    if not isinstance(raw, dict) or not raw.get("key_path"):
        return
    configured = Path(str(raw["key_path"])).expanduser()
    try:
        same = configured.resolve() == key_path.resolve()
    except OSError:
        same = False
    if same:
        err_console.print(
            f"[red]nova init --force:[/red] {config} signs capsules with {key_path}. "
            "Rotating it here would leave a certificate that no longer matches the key. "
            "Rotate the sealing key with `nova seal init --force`, or point key_path at "
            "another key first."
        )
        raise typer.Exit(code=1)


def _archive_keypair(root: Path, key_path: Path, pub_path: Path) -> Path:
    """Move the current pair to keys/archive/<UTC>/ (mode 700); return that directory."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = root / "keys" / "archive"
    target = archive_root / stamp
    suffix = 1
    while target.exists():
        suffix += 1
        target = archive_root / f"{stamp}-{suffix}"
    target.mkdir(parents=True)
    archive_root.chmod(stat.S_IRWXU)
    target.chmod(stat.S_IRWXU)
    for source in (key_path, pub_path):
        if source.exists():
            shutil.move(str(source), str(target / source.name))
    return target
