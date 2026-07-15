from __future__ import annotations

import getpass
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

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


class _KeyringSigner:
    """Adapt a keyring Ed25519 key to the DSSE signer protocol (`sign` + `keyid`)."""

    def __init__(self, private_key: Any, key_fp: str) -> None:
        self._key = private_key
        self.keyid = key_fp

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)  # type: ignore[no-any-return]


def _emit_slsa_provenance(
    *,
    result: dict[str, Any],
    name: str,
    version: str,
    identity: str,
    gated: bool,
    out: Optional[Path],
    ml_profile: bool = False,
) -> Path:
    """Emit a DSSE-signed SLSA v1 provenance for a successful promotion (NF-031/NF-057)."""
    from novafabric.envelopes.slsa import (  # noqa: PLC0415
        ml_promotion_provenance,
        promotion_provenance,
    )
    from novafabric.evidence.intoto import dsse_sign  # noqa: PLC0415

    # Content-address the asset by the sha256 of its registered spec (authoritative content).
    spec_json = result.get("spec_json") or ""
    asset_sha256 = hashlib.sha256(spec_json.encode("utf-8")).hexdigest()
    gate = "significance-gate/v1" if gated else None
    if ml_profile:
        # SLSA-for-ML (NF-057): bind the promoted model to the digest of the gating
        # eval verdict (content-addressed over the recorded decision context).
        verdict_ctx = json.dumps(
            {"asset": f"{name}@{version}", "asset_sha256": asset_sha256,
             "decision": "promoted", "gate": gate},
            sort_keys=True,
        )
        eval_verdict_sha256 = hashlib.sha256(verdict_ctx.encode("utf-8")).hexdigest()
        statement = ml_promotion_provenance(
            asset_ref=f"{name}@{version}",
            asset_sha256=asset_sha256,
            eval_verdict_sha256=eval_verdict_sha256,
            decision="promoted",
            gate=gate,
            invocation_id=result.get("id"),
        )
    else:
        statement = promotion_provenance(
            asset_ref=f"{name}@{version}",
            asset_sha256=asset_sha256,
            decision="promoted",
            gate=gate,
            invocation_id=result.get("id"),
        )
    private_key, key_fp = ensure_keypair(identity)
    envelope = dsse_sign(statement, _KeyringSigner(private_key, key_fp))
    out_path = out or Path(f"{name}-{version}.slsa.json")
    out_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return out_path


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
    scores_file: Optional[Path] = typer.Option(
        None,
        "--scores-file",
        help=(
            "With --significance-gate: source the pass/fail sequence from this "
            "evidence-grade scores.jsonl instead of the eval_results table (NF-007)."
        ),
    ),
    metric: str = typer.Option(
        "task_pass", "--metric", help="Boolean metric name to read from --scores-file."
    ),
    slsa_provenance: bool = typer.Option(
        False,
        "--slsa-provenance",
        help=(
            "On successful promotion, also emit a DSSE-signed SLSA v1 provenance "
            "(slsa.dev/provenance/v1, NF-031/ADR-0096) recording the promotion decision "
            "and gate. Written to <name>-<version>.slsa.json (or --slsa-out). Opt-in; "
            "promotion behavior is otherwise unchanged."
        ),
    ),
    slsa_ml_profile: bool = typer.Option(
        False,
        "--slsa-ml-profile",
        help=(
            "With --slsa-provenance, emit the SLSA-for-ML profile (NF-057, ADR-0105): "
            "buildType promote-ml/v1 plus an eval-verdict digest byproduct binding the "
            "promoted model to the gating eval verdict. Use for model assets."
        ),
    ),
    slsa_out: Optional[Path] = typer.Option(
        None, "--slsa-out", help="Output path for the --slsa-provenance envelope."
    ),
    identity: str = typer.Option(
        default_factory=getpass.getuser,
        help="Signing identity (keyring key) for --slsa-provenance.",
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
            sig_scores_path=scores_file,
            sig_scores_metric=metric,
        )
    except (AssetNotFoundError, InvalidLifecycleTransitionError, PromotionBlockedError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    tag = " [yellow](forced)[/yellow]" if result["forced_promotion"] else ""
    console.print(f"[green]Promoted[/green] {name}@{version} → {result['status']}{tag}")

    if slsa_provenance:
        prov_path = _emit_slsa_provenance(
            result=result,
            name=name,
            version=version,
            identity=identity,
            gated=significance_gate,
            out=slsa_out,
            ml_profile=slsa_ml_profile,
        )
        profile = "SLSA-for-ML " if slsa_ml_profile else ""
        console.print(f"[green]✓[/green] {profile}SLSA provenance written: {prov_path}")


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
