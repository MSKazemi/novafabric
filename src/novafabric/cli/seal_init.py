"""``nova seal init`` — create a local, self-asserted sealing identity (ADR-0301).

Sealing stays opt-in: this command is the explicit step that turns it on. ``nova init``
only *offers* it as a next step; nothing configures sealing silently.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric._paths import nova_home

console = Console()
err_console = Console(stderr=True)


def seal_init_cmd(
    home: Annotated[
        Optional[Path],
        typer.Option(
            "--home",
            help="Override NOVAFABRIC_HOME for this command.",
            show_default=False,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Rotate: archive the current signing key, certificate and novaseal.yaml "
                "to keys/novaseal/archive/<UTC>/, then issue a new key and certificate "
                "from the same local CA. Never replaces a novaseal.yaml it did not write."
            ),
        ),
    ] = False,
    new_ca: Annotated[
        bool,
        typer.Option(
            "--new-ca",
            help=(
                "With --force: also archive and regenerate the local seal CA. Verifiers "
                "who pinned the old CA will not validate new capsules."
            ),
        ),
    ] = False,
) -> None:
    """Set up local capsule sealing with a self-asserted identity (experimental).

    Creates a dedicated ECDSA P-256 signing key, a local seal CA and a certificate
    for the key under $NOVAFABRIC_HOME/keys/novaseal/ (private keys mode 600), and
    writes $NOVAFABRIC_HOME/novaseal.yaml (profile: local). After this, `nova capture`
    seals new Run Capsules. Fully offline: no timestamp authority is configured and
    nothing is contacted.

    The identity is SELF-ASSERTED: a seal proves that the holder of this key signed,
    not who that holder is. `nova verify` says so. Pin continuity across rotations
    with `nova verify --ca-bundle <home>/keys/novaseal/ca.crt.pem`.

    Idempotent: when sealing is already configured it changes nothing. A
    novaseal.yaml that `nova seal init` did not write (KMS or operator CA profile)
    is never replaced.

    Scope: global (one-time setup).

    \b
    Examples:
      # Turn on local sealing
      nova seal init

      # Rotate the signing key (same local CA; old files archived)
      nova seal init --force

      # Rotate the key and the local CA (breaks CA-pinned continuity)
      nova seal init --force --new-ca

      # Custom home directory
      nova seal init --home /data/novafabric
    """
    from novafabric.trust.novaseal.local_identity import (  # noqa: PLC0415
        LocalIdentityError,
        init_local_identity,
    )

    root: Path = home or nova_home()
    try:
        result = init_local_identity(root, force=force, new_ca=new_ca)
    except LocalIdentityError as exc:
        err_console.print(f"[red]nova seal init:[/red] {exc}")
        raise typer.Exit(code=1) from None

    paths = result.paths
    if result.status == "operator-managed":
        console.print(
            f"[green]Sealing already configured[/green] by an operator-managed "
            f"{paths.config} (profile: {result.profile or 'unknown'}). Nothing changed."
        )
        console.print(
            "[dim]nova seal init never replaces a novaseal.yaml it did not write.[/dim]"
        )
        _warn_env_override(paths.config)
        return

    if result.status == "already-configured":
        console.print(f"[green]Sealing already configured:[/green] {paths.config}")
    elif result.status == "rotated":
        console.print(f"[green]Rotated the local sealing key:[/green] {paths.config}")
    else:
        console.print(f"[green]Local sealing identity created:[/green] {paths.config}")

    console.print(f"  signing key  → {paths.signing_key}  (mode 600)")
    console.print(f"  certificate  → {paths.signing_cert}")
    console.print(f"  local CA     → {paths.ca_cert}")
    if result.new_keyid:
        console.print(f"  keyid        → {result.new_keyid}")
    if result.leaf_not_after is not None:
        console.print(
            f"  valid until  → {result.leaf_not_after:%Y-%m-%d} "
            "(rotate before then: nova seal init --force)"
        )
    if result.signing_key_reused:
        console.print("  [dim]reused the existing signing key in keys/novaseal/[/dim]")

    if result.archive_dir is not None:
        console.print(
            f"[yellow]⚠ Previous material archived (not deleted):[/yellow] {result.archive_dir}"
        )
    if result.status == "rotated":
        if new_ca:
            console.print(
                "[yellow]⚠ The local seal CA was replaced.[/yellow] Verifiers who pinned the "
                "old ca.crt.pem will not validate capsules sealed from now on; give them "
                f"{paths.ca_cert}."
            )
        else:
            console.print(
                "  Same local CA: verifiers who pinned ca.crt.pem keep validating old and "
                "new capsules."
            )
        if result.rotation_logged:
            console.print(
                f"  key_rotation recorded in the Merkle log: {result.old_keyid} → "
                f"{result.new_keyid}"
            )
        else:
            console.print(
                "[yellow]⚠ The rotation could not be recorded in the Merkle log:[/yellow] "
                f"{result.rotation_log_error}"
            )

    console.print()
    console.print(
        "[bold]Trust level:[/bold] [yellow]self-asserted[/yellow] — a seal proves the "
        "holder of this key signed; it does not identify a person, organisation or machine."
    )
    console.print(
        "[dim]No RFC 3161 timestamp is configured, so seals carry no trusted time "
        "(add tsa_url to novaseal.yaml to opt in).[/dim]"
    )
    _warn_env_override(paths.config)
    if result.status != "already-configured":
        console.print()
        console.print("[bold]Next steps:[/bold]")
        console.print("  nova capture python my_agent.py      # new capsules are sealed")
        console.print("  nova verify <capsule-dir>            # reports identity: self-asserted")
        console.print(f"  nova verify --ca-bundle {paths.ca_cert} <capsule-dir>")


def _warn_env_override(written: Path) -> None:
    """Warn when NOVAFABRIC_SEAL_CONFIG makes capture read a different file."""
    env = os.environ.get("NOVAFABRIC_SEAL_CONFIG")
    if env and Path(env).expanduser().resolve() != written.resolve():
        console.print(
            f"[yellow]⚠ NOVAFABRIC_SEAL_CONFIG={env} is set:[/yellow] capture and verify "
            f"read that file, not {written}."
        )
