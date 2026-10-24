from __future__ import annotations

import getpass
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console

from novafabric._paths import promote_signers_path
from novafabric.registry.service import (
    AssetNotFoundError,
    InvalidLifecycleTransitionError,
    PromotionBlockedError,
    SelfApprovalError,
    SoDViolationError,
    approve_promotion,
    asset_spec_sha256,
    get_open_proposal,
    new_proposal_id,
    promote_asset_direct,
    propose_promotion,
)
from novafabric.registry.signers import (
    SignerVerificationError,
    approval_payload,
    proposal_payload,
)
from novafabric.spec.models import AssetStatus
from novafabric.trust.keyring import (
    KeyNotFoundError,
    ensure_keypair,
    key_path_for,
    public_key_b64,
    sign_payload,
)

console = Console()

promote_app = typer.Typer(
    help="Promote assets (direct single-actor or maker-checker SoD).",
    no_args_is_help=True,
)

#: Exit codes for maker-checker failures, consistent with `nova seal verify` (ADR-0059):
#: 3 = proposal-side signature/trust failure, 4 = approval-side, 6 = self-approval.
EXIT_PROPOSAL_SIGNATURE = 3
EXIT_APPROVAL_SIGNATURE = 4
EXIT_SELF_APPROVAL = 6

_IDENTITY_HELP = (
    "Signer identity. REQUIRED: it must name a keyring key created with "
    "`nova promote keygen` and bound to this identity in the signer trust list "
    "(ADR-0312). There is no default."
)


def _load_signing_key(identity: str) -> tuple[Any, str]:
    """Load an existing keyring key; never create one on the propose/approve path."""
    try:
        return ensure_keypair(identity, create=False)
    except KeyNotFoundError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(code=1) from None


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
    A protected stage (production) is refused unless the `promote_direct` policy action
    explicitly allows it (ADR-0312); --force does not bypass that. Use
    `nova promote propose` + `nova promote approve` for maker-checker SoD.

    Scope: single asset.

    \b
    Examples:
      nova promote direct my-agent@v1.1 --to staging
      nova promote direct my-agent@v1.1 --to staging --significance-gate
    """
    name, version = _parse_ref(asset_ref)

    if force:
        confirmed = typer.prompt(f"Type the asset name '{name}' to confirm forced promotion")
        if confirmed != name:
            console.print("[red]Confirmation failed. Aborting.[/red]")
            raise typer.Exit(code=1)

    try:
        result = promote_asset_direct(
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
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
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


@promote_app.command("keygen")
def promote_keygen_cmd(
    identity: str = typer.Option(
        ..., "--identity", help="Identity to create an Ed25519 keyring key for."
    ),
) -> None:
    """Create a maker-checker signing key and print its signer trust-list entry.

    The only command that creates a keyring key for `propose` / `approve` (ADR-0312).
    Idempotent: an existing key is never overwritten. The key is not trusted until an
    operator adds the printed entry to the signer trust list (keep only the roles the
    person actually holds).

    Scope: local keyring.

    \b
    Examples:
      nova promote keygen --identity alice
    """
    path = key_path_for(identity)
    existed = path.exists()
    private_key, key_fp = ensure_keypair(identity, create=True)
    state = "already exists (unchanged)" if existed else "created"
    console.print(f"Key for {identity!r} {state}: {path} (fingerprint {key_fp})", soft_wrap=True)
    console.print(
        "To bind it, an operator adds this entry under `signers:` in "
        f"{promote_signers_path()}:",
        soft_wrap=True,
    )
    # Plain stdout: the stanza must be copy-pasteable YAML, free of Rich markup/wrapping.
    typer.echo(
        f"  - identity: {json.dumps(identity)}\n"
        f"    public_key: {public_key_b64(private_key.public_key())}\n"
        f"    fingerprint: {key_fp}\n"
        "    roles: [proposer, approver]"
    )


@promote_app.command("propose")
def promote_propose_cmd(
    asset_ref: str = typer.Argument(..., help="name@version"),
    to: AssetStatus = typer.Option(..., "--to", help="Target status"),
    identity: str = typer.Option(..., "--identity", help=_IDENTITY_HELP),
) -> None:
    """Propose a promotion for maker-checker review (maker step).

    Signs the proposal (proposal id, asset, target stage) with the keyring key for
    --identity. The signature is verified against the signer trust list before the
    proposal is stored; it must then be approved by a different bound identity with
    `nova promote approve`. Exit 3: the signature or its trust-list binding failed.

    Scope: single asset.

    \b
    Examples:
      nova promote propose my-agent@v1.1 --to production --identity alice
    """
    name, version = _parse_ref(asset_ref)

    private_key, key_fp = _load_signing_key(identity)
    proposal_id = new_proposal_id()
    try:
        spec_sha256 = asset_spec_sha256(name, version)
    except AssetNotFoundError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(code=1) from None
    payload = proposal_payload(
        proposal_id=proposal_id, name=name, version=version, to_status=to.value,
        spec_sha256=spec_sha256,
    )
    sig = sign_payload(private_key, payload)

    try:
        result = propose_promotion(
            name=name,
            version=version,
            to_status=to,
            proposer=identity,
            proposer_key_fp=key_fp,
            proposer_sig=sig,
            proposal_id=proposal_id,
        )
    except SignerVerificationError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(code=EXIT_PROPOSAL_SIGNATURE) from None
    except (AssetNotFoundError, InvalidLifecycleTransitionError, SoDViolationError) as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(code=1)

    superseded = (
        f"\n  superseded unverified proposal: {result['superseded']}"
        if result.get("superseded")
        else ""
    )
    console.print(
        f"[green]Proposal created[/green] {result['proposal_id']}\n"
        f"  {name}@{version} → {result['to_status']}\n"
        f"  proposer: {result['proposer']} (key fp: {key_fp}, signature verified)"
        f"{superseded}\n"
        f"  Run [bold]nova promote approve {asset_ref} --identity APPROVER[/bold] "
        "as a different bound identity to complete."
    )


@promote_app.command("approve")
def promote_approve_cmd(
    asset_ref: str = typer.Argument(..., help="name@version"),
    identity: str = typer.Option(..., "--identity", help=_IDENTITY_HELP),
) -> None:
    """Approve a pending promotion proposal (checker step).

    Countersigns the open proposal (bound to its id, target stage and the maker's
    signature) with the keyring key for --identity. Both signatures are verified
    against the signer trust list and the two keys must be bound to different
    identities. Exit 3: the proposal is unverified or its signature fails. Exit 4:
    the approval signature or its binding fails. Exit 6: self-approval.

    Scope: single asset.

    \b
    Examples:
      nova promote approve my-agent@v1.1 --identity bob
    """
    name, version = _parse_ref(asset_ref)

    private_key, key_fp = _load_signing_key(identity)
    proposal = get_open_proposal(name, version)
    if proposal is None:
        console.print(f"[red]No open promotion proposal found for '{name}@{version}'.[/red]")
        raise typer.Exit(code=1)
    try:
        # The checker signs the content as it is now (rlr-04).
        spec_sha256 = asset_spec_sha256(name, version)
    except AssetNotFoundError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(code=1) from None
    payload = approval_payload(
        proposal_id=str(proposal["proposal_id"]),
        name=name,
        version=version,
        to_status=str(proposal["to_status"]),
        proposer_sig=str(proposal["proposer_sig"]),
        spec_sha256=spec_sha256,
    )
    sig = sign_payload(private_key, payload)

    try:
        result = approve_promotion(
            name=name,
            version=version,
            approver=identity,
            approver_key_fp=key_fp,
            approver_sig=sig,
        )
    except SignerVerificationError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        code = EXIT_PROPOSAL_SIGNATURE if exc.party == "proposer" else EXIT_APPROVAL_SIGNATURE
        raise typer.Exit(code=code) from None
    except SelfApprovalError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(code=EXIT_SELF_APPROVAL) from None
    except (AssetNotFoundError, SoDViolationError, PromotionBlockedError) as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        raise typer.Exit(code=1)

    console.print(
        f"[green]Promotion approved and executed[/green] "
        f"{name}@{version} → {result['status']}\n"
        f"  proposal: {result['proposal_id']}\n"
        f"  approver: {result['approver']} (key fp: {key_fp}, signatures verified)"
    )
