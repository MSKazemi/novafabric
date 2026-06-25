"""``nova evidence`` — completeness, criterion bindings, re-performance.

Experimental (ADR-0087, gap-008).  All three verbs work fully offline; the
signed variants use the same local Ed25519 key as ``nova export-evidence``.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric._paths import default_capsule_dir
from novafabric.audit import AUDIT_LOG_PATH
from novafabric.evidence.admissibility import (
    ChainOfCustody,
    Custodian,
    SelfAuthentication,
    admissibility_block,
    compute_admissibility_status,
)
from novafabric.evidence.binding import attest_bindings, build_bindings
from novafabric.evidence.completeness import (
    attest_completeness,
    compute_completeness,
)
from novafabric.evidence.reperformance import (
    build_reperformance_attestation,
    sign_reperformance,
)
from novafabric.evidence.signing import LocalSigner

console = Console()

evidence_app = typer.Typer(
    help=(
        "Audit-grade evidence operations: completeness assertion, "
        "criterion-evidence bindings, re-performance attestation "
        "(experimental, ADR-0087)."
    ),
    no_args_is_help=True,
)


class AttestReplayMode(str, Enum):
    mocked = "mocked"
    forensic = "forensic"
    semantic = "semantic"
    exact = "exact"


_KeyOpt = Annotated[
    Optional[Path],
    typer.Option(
        "--key",
        help="PEM-encoded ed25519 private key; when given, output is DSSE-signed",
    ),
]
_OutOpt = Annotated[
    Optional[Path],
    typer.Option("--output", "-o", help="Output file (default: stdout / cwd)"),
]


def _resolve_capsule(ref: str) -> Path:
    """Resolve a run_id or a capsule path to a valid capsule directory."""
    candidate = Path(ref)
    if candidate.is_dir() and (candidate / "capsule.yaml").exists():
        return candidate
    by_id = default_capsule_dir() / ref
    if by_id.is_dir() and (by_id / "capsule.yaml").exists():
        return by_id
    console.print(f"[red]Not a valid capsule (path or run_id): {ref}[/red]")
    raise typer.Exit(code=1)


def _load_signer(key: Path) -> LocalSigner:
    try:
        return LocalSigner(key)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Failed to load signing key:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@evidence_app.command("completeness")
def evidence_completeness_cmd(
    capsule: Annotated[str, typer.Argument(help="Run ID or capsule directory")],
    key: _KeyOpt = None,
    output: _OutOpt = None,
) -> None:
    """Compute (and optionally attest) the capsule completeness assertion.

    Declares per-stream event counts, drop counters, capture level, time
    window, and active hooks — so "no events" is distinguishable from
    "not captured".

    Scope: single capsule.

    \b
    Examples:
      nova evidence completeness 01HX...
      nova evidence completeness path/to/capsule --key ed25519.pem -o completeness.intoto.json
    """
    capsule_dir = _resolve_capsule(capsule)
    assertion = compute_completeness(capsule_dir, run_id=capsule_dir.name)
    if key is None:
        payload = assertion.model_dump_json(indent=2)
        if output:
            output.write_text(payload + "\n")
            console.print(f"Wrote unsigned assertion to {output}")
        else:
            typer.echo(payload)
        return
    envelope = attest_completeness(capsule_dir, assertion, _load_signer(key))
    path = output or Path("completeness.intoto.json")
    path.write_text(json.dumps(envelope, indent=2) + "\n")
    console.print(f"[green]✓[/green] Signed completeness attestation: {path}")


@evidence_app.command("bind")
def evidence_bind_cmd(
    capsule: Annotated[str, typer.Argument(help="Run ID or capsule directory")],
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help="Audit profile id (e.g. eu-ai-act-high-risk, nist-ai-rmf) or path",
        ),
    ],
    key: _KeyOpt = None,
    output: _OutOpt = None,
) -> None:
    """Generate criterion→evidence bindings from an audit profile.

    Each binding maps a control ID to capsule files (path + sha256) that
    evidence it; status mirrors ControlCoverage semantics.

    Scope: single capsule.

    \b
    Examples:
      nova evidence bind 01HX... --profile eu-ai-act-high-risk
      nova evidence bind 01HX... --profile gdpr --key ed25519.pem
    """
    capsule_dir = _resolve_capsule(capsule)
    try:
        bindings = build_bindings(capsule_dir, profile)
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[red]Unknown audit profile:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    doc = {"bindings": [b.model_dump() for b in bindings]}
    if key is None:
        path = output or Path("criterion-bindings.json")
        path.write_text(json.dumps(doc, indent=2) + "\n")
        console.print(
            f"Wrote {len(bindings)} binding(s) to {path} "
            f"({sum(1 for b in bindings if b.binding_status == 'satisfied')} satisfied)"
        )
        return
    envelope = attest_bindings(
        capsule_dir, capsule_dir.name, bindings, _load_signer(key)
    )
    path = output or Path("criterion-bindings.intoto.json")
    path.write_text(json.dumps(envelope, indent=2) + "\n")
    console.print(f"[green]✓[/green] Signed criterion bindings: {path}")


@evidence_app.command("attest-replay")
def evidence_attest_replay_cmd(
    capsule: Annotated[str, typer.Argument(help="Run ID or capsule directory")],
    key: Annotated[
        Path,
        typer.Option(
            "--key",
            help="PEM-encoded ed25519 private key (attestations are always signed)",
        ),
    ],
    mode: Annotated[
        AttestReplayMode, typer.Option("--mode", help="Replay mode for re-performance")
    ] = AttestReplayMode.mocked,
    output: _OutOpt = None,
    output_dir: Annotated[
        Optional[Path],
        typer.Option("--output-dir", help="Base dir for replay output"),
    ] = None,
) -> None:
    """Replay the run and emit a signed re-performance attestation.

    Records that a replay at time T reproduced outcome digest D under mode M
    with verdict V — audit-grade re-performance evidence.

    Scope: single capsule; runs the replay engine under its safety defaults.

    \b
    Examples:
      nova evidence attest-replay 01HX... --key ed25519.pem
      nova evidence attest-replay 01HX... --key ed25519.pem --mode exact
    """
    from novafabric.replay._engine import ReplayEngine
    from novafabric.replay._flags import ReplayFlags

    capsule_dir = _resolve_capsule(capsule)
    signer = _load_signer(key)
    base = output_dir or (Path.cwd() / ".novafabric" / "replays")
    flags = ReplayFlags(mode=mode.value, output_dir=base)
    result = ReplayEngine(capsule_dir=capsule_dir, flags=flags, base_dir=base).run()
    attestation = build_reperformance_attestation(capsule_dir, result)
    envelope = sign_reperformance(attestation, signer)
    path = output or Path(f"reperformance-{capsule_dir.name}.intoto.json")
    path.write_text(json.dumps(envelope, indent=2) + "\n")
    console.print(
        f"[green]✓[/green] Re-performance attestation: {path} "
        f"(mode={attestation.replay_mode}, match={attestation.match})"
    )
    if attestation.match == "mismatch":
        raise typer.Exit(code=2)


@evidence_app.command("bind-custody")
def evidence_bind_custody_cmd(
    capsule: Annotated[str, typer.Argument(help="Run ID or capsule directory")],
    custodian_id: Annotated[
        Optional[str],
        typer.Option(
            "--custodian",
            help="Custodian identity (e.g. email). Omit to mark operator_declared (I3).",
        ),
    ] = None,
    custodian_role: Annotated[
        Optional[str], typer.Option("--role", help="Custodian role")
    ] = None,
    provenance: Annotated[
        str,
        typer.Option(
            "--provenance",
            help="novaseal-identity | oidc | operator_declared",
        ),
    ] = "operator_declared",
    timestamp_ok: Annotated[
        bool,
        typer.Option(
            "--timestamp-ok/--no-timestamp-ok",
            help="Assert an RFC 3161 timestamp verifies for this capsule (FRE-902(14) point 4).",
        ),
    ] = False,
    key: _KeyOpt = None,
    output: _OutOpt = None,
) -> None:
    """Build the chain-of-custody + FRE-902(14) self-authentication block (ADR-0095).

    Witnessed-or-declared (I3): fields NovaFabric cannot witness are emitted null
    with provenance=operator_declared, never fabricated. With ``--key`` the capsule
    hash is signed and the signature verified, so ``signature_verifies`` is real.

    Scope: single capsule.

    \b
    Examples:
      nova evidence bind-custody 01HX... --custodian alice@corp --provenance oidc --key ed25519.pem
      nova evidence bind-custody path/to/capsule -o custody.json
    """
    capsule_dir = _resolve_capsule(capsule)
    custodian = None
    if custodian_id:
        custodian = Custodian(
            identity=custodian_id,
            role=custodian_role,
            provenance=provenance,  # type: ignore[arg-type]
        )
    signer = _load_signer(key) if key else None
    block = admissibility_block(
        capsule_dir,
        audit_log_path=AUDIT_LOG_PATH,
        run_id=capsule_dir.name,
        custodian=custodian,
        signer=signer,
        timestamp_ok=timestamp_ok,
    )
    payload = json.dumps(block, indent=2)
    if output:
        output.write_text(payload + "\n")
        console.print(
            f"[green]✓[/green] custody block "
            f"({block['admissibility_status']}) → {output}"
        )
    else:
        typer.echo(payload)


@evidence_app.command("check-admissibility")
def evidence_check_admissibility_cmd(
    block_file: Annotated[
        Path,
        typer.Argument(help="Admissibility/custody JSON (from bind-custody)"),
    ],
    timestamp_ok: Annotated[
        bool,
        typer.Option(
            "--timestamp-ok/--no-timestamp-ok",
            help="The checker has independently verified an RFC 3161 timestamp.",
        ),
    ] = False,
) -> None:
    """Re-run the five-point FRE-902(14) gate on a custody block (ADR-0095).

    Recomputes the status honestly from the block's own custody + self-authentication
    evidence (it does not trust the stored ``admissibility_status``). Exits non-zero
    unless the result is ``self-authenticating``.

    Scope: one custody block.

    \b
    Examples:
      nova evidence check-admissibility custody.json --timestamp-ok
    """
    try:
        data = json.loads(block_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]Cannot read custody block:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    coc_raw = data.get("chain_of_custody")
    sa_raw = data.get("self_authentication")
    coc = ChainOfCustody.model_validate(coc_raw) if coc_raw else None
    sa = SelfAuthentication.model_validate(sa_raw) if sa_raw else None
    status = compute_admissibility_status(coc, sa, timestamp_ok=timestamp_ok)
    console.print(f"admissibility_status: [bold]{status}[/bold]")
    if status != "self-authenticating":
        raise typer.Exit(code=3)
