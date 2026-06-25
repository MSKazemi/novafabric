from __future__ import annotations

import getpass

import typer
from rich.console import Console

from novafabric.registry.service import (
    AssetNotFoundError,
    InvalidLifecycleTransitionError,
    PromotionBlockedError,
    SoDViolationError,
    approve_promotion,
    promote_asset,
    propose_promotion,
)
from novafabric.spec.models import AssetStatus
from novafabric.trust.keyring import (
    canonical_payload,
    ensure_keypair,
    sign_payload,
)

console = Console()

promote_app = typer.Typer(
    help="Promote assets (direct single-actor or maker-checker SoD).",
    no_args_is_help=True,
)


def _parse_ref(asset_ref: str) -> tuple[str, str]:
    if "@" not in asset_ref:
        console.print("[red]Must specify version: name@version[/red]")
        raise typer.Exit(code=1)
    name, version = asset_ref.rsplit("@", 1)
    return name, version


@promote_app.command("direct")
def promote_direct_cmd(
    asset_ref: str = typer.Argument(..., help="name@version"),
    to: AssetStatus = typer.Option(..., "--to", help="Target status"),
    force: bool = typer.Option(
        False, "--force", help="Bypass eval gate (requires confirmation)"
    ),
    significance_gate: bool = typer.Option(
        False,
        "--significance-gate",
        help=(
            "Opt-in: block promotion on a STATISTICALLY SIGNIFICANT eval regression "
            "(Wald SPRT over the recent pass/fail sequence, ADR-0080) instead of a "
            "single passing eval. Noise / inconclusive evidence does not block."
        ),
    ),
) -> None:
    """Promote an asset directly (single-actor, no maker-checker).

    Moves the asset through the lifecycle: draft → development → staging → production.
    Use `nova promote propose` + `nova promote approve` for maker-checker SoD.

    Scope: single asset.

    \b
    Examples:
      nova promote direct my-agent@v1.1 --to staging
      nova promote direct my-agent@v1.1 --to production
      nova promote direct my-agent@v1.1 --to staging --significance-gate
    """
    name, version = _parse_ref(asset_ref)

    if force:
        confirmed = typer.prompt(f"Type the asset name '{name}' to confirm forced promotion")
        if confirmed != name:
            console.print("[red]Confirmation failed. Aborting.[/red]")
            raise typer.Exit(code=1)

    try:
        result = promote_asset(
            name,
            version,
            to,
            actor="cli-user",
            force=force,
            significance_gate=significance_gate,
        )
    except (AssetNotFoundError, InvalidLifecycleTransitionError, PromotionBlockedError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    tag = " [yellow](forced)[/yellow]" if result["forced_promotion"] else ""
    console.print(f"[green]Promoted[/green] {name}@{version} → {result['status']}{tag}")


@promote_app.command("propose")
def promote_propose_cmd(
    asset_ref: str = typer.Argument(..., help="name@version"),
    to: AssetStatus = typer.Option(..., "--to", help="Target status"),
    identity: str = typer.Option(
        default_factory=getpass.getuser,
        help="Proposer identity (defaults to OS username)",
    ),
) -> None:
    """Propose a promotion for maker-checker review (maker step).

    Creates a signed proposal that must be approved by a separate key-holder
    using `nova promote approve` before the promotion takes effect.

    Scope: single asset.

    \b
    Examples:
      nova promote propose my-agent@v1.1 --to production
    """
    name, version = _parse_ref(asset_ref)

    private_key, key_fp = ensure_keypair(identity)
    proposal_id_placeholder = f"pending|{name}@{version}:{to.value}"
    # Sign before proposal_id is known; service will assign the UUID.
    # We sign over a deterministic pre-image so the sig is verifiable post-creation.
    payload = canonical_payload(proposal_id_placeholder, name, version, to.value)
    sig = sign_payload(private_key, payload)

    try:
        result = propose_promotion(
            name=name,
            version=version,
            to_status=to,
            proposer=identity,
            proposer_key_fp=key_fp,
            proposer_sig=sig,
        )
    except (AssetNotFoundError, InvalidLifecycleTransitionError, SoDViolationError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Proposal created[/green] {result['proposal_id']}\n"
        f"  {name}@{version} → {result['to_status']}\n"
        f"  proposer: {identity} (key fp: {key_fp})\n"
        f"  Run [bold]nova promote approve {asset_ref}[/bold] as a different identity to complete."
    )


@promote_app.command("approve")
def promote_approve_cmd(
    asset_ref: str = typer.Argument(..., help="name@version"),
    identity: str = typer.Option(
        default_factory=getpass.getuser,
        help="Approver identity (defaults to OS username)",
    ),
) -> None:
    """Approve a pending promotion proposal (checker step).

    Verifies and countersigns a proposal created with `nova promote propose`.

    Scope: single asset.

    \b
    Examples:
      nova promote approve my-agent@v1.1
    """
    name, version = _parse_ref(asset_ref)

    private_key, key_fp = ensure_keypair(identity)
    payload = canonical_payload(f"pending|{name}@{version}:approve", name, version, "approve")
    sig = sign_payload(private_key, payload)

    try:
        result = approve_promotion(
            name=name,
            version=version,
            approver=identity,
            approver_key_fp=key_fp,
            approver_sig=sig,
        )
    except (AssetNotFoundError, SoDViolationError, PromotionBlockedError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Promotion approved and executed[/green] "
        f"{name}@{version} → {result['status']}\n"
        f"  proposal: {result['proposal_id']}\n"
        f"  approver: {identity} (key fp: {key_fp})"
    )
