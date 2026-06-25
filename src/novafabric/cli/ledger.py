"""``nova ledger`` — Adversary-Anchored Accountability Ledger (ADR-0094 A).

Three verbs:

- ``anchor`` — build per-stream sidecar chains for every ``.jsonl`` evidence
  stream, build + DSSE-sign a multi-stream checkpoint, and write a local
  finalize anchor (offline-safe).
- ``verify`` — verify the ledger and exit with the ADR-0094 taxonomy
  (0 ok / 3 tamper / 4 reorder / 5 truncation / 6 bad sig / 7 rollback /
  8 merkle / 9 anchor / 10 no checkpoint).
- ``status`` — show the per-stream chain heads.

Experimental, opt-in, additive: the ``.jsonl`` streams are never modified. This
module exposes ``ledger_app``; it is wired into the top-level CLI elsewhere.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric.evidence.signing import LocalSigner, verify_with_pem
from novafabric.trust.ledger._chain import (
    jsonl_path_for,
    verify_chain,
    write_chain,
)
from novafabric.trust.ledger._checkpoint import build_checkpoint, sign_checkpoint
from novafabric.trust.ledger._verify import exit_code_for, verify_ledger

console = Console()

ledger_app = typer.Typer(
    help=(
        "Adversary-Anchored Accountability Ledger: per-stream sidecar chains + "
        "a forward-secure, externally anchored checkpoint (experimental, ADR-0094)."
    ),
    no_args_is_help=True,
)


def _resolve_capsule(ref: str) -> Path:
    candidate = Path(ref)
    if candidate.is_dir() and (candidate / "capsule.yaml").exists():
        return candidate
    console.print(f"[red]Not a valid capsule directory:[/red] {ref}")
    raise typer.Exit(code=1)


def _discover_jsonl_streams(capsule_dir: Path) -> list[str]:
    return sorted(p.name[: -len(".jsonl")] for p in capsule_dir.glob("*.jsonl"))


@ledger_app.command("anchor")
def ledger_anchor_cmd(
    capsule: Annotated[str, typer.Argument(help="Capsule directory")],
    key: Annotated[
        Path,
        typer.Option("--key", help="PEM ed25519 private key (DSSE-signs the checkpoint)"),
    ],
    node_id: Annotated[
        str, typer.Option("--node-id", help="Signing node identity")
    ] = "local",
    epoch: Annotated[
        int, typer.Option("--epoch", help="Ratchet epoch of the signing key")
    ] = 0,
    agent_id: Annotated[
        Optional[str], typer.Option("--agent-id", help="Agent identity to bind")
    ] = None,
    principal: Annotated[
        Optional[str], typer.Option("--principal", help="Principal (OIDC/RBAC subject)")
    ] = None,
) -> None:
    """Build sidecar chains + sign a checkpoint for every ``.jsonl`` stream.

    Writes ``<capsule>/.ledger/<stream>.chain.json`` per stream and a signed
    ``<capsule>/.ledger/checkpoint.json`` with a local finalize anchor. The
    ``.jsonl`` evidence streams are never modified.

    Scope: single capsule (local finalize, offline-safe).

    \b
    Examples:
      nova ledger anchor ./run --key ed25519.pem --node-id n1 --epoch 0
      nova ledger anchor ./run --key k.pem --agent-id a --principal alice@x
    """
    capsule_dir = _resolve_capsule(capsule)
    streams = _discover_jsonl_streams(capsule_dir)
    if not streams:
        console.print("[red]No .jsonl evidence streams to anchor.[/red]")
        raise typer.Exit(code=1)

    try:
        signer = LocalSigner(key)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Failed to load signing key:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    for stream in streams:
        write_chain(capsule_dir, stream)

    record = build_checkpoint(
        capsule_dir,
        node_id=node_id,
        epoch=epoch,
        epoch_pubkey=signer.public_pem.decode(),
        agent_id=agent_id,
        principal=principal,
    )
    sign_checkpoint(record, signer, capsule_dir=capsule_dir, persist=False)

    # Local finalize anchor: bind the signature digest (offline-safe; no TSA).
    sig_value = str((record.signature or {}).get("value", ""))
    record.anchor = {
        "method": "none",
        "anchored_value": "sha256:" + hashlib.sha256(sig_value.encode()).hexdigest(),
        "anchored_at": datetime.now(timezone.utc).isoformat(),
    }
    from novafabric.trust.ledger._checkpoint import checkpoint_path

    path = checkpoint_path(capsule_dir)
    path.write_text(json.dumps(record.to_record(), indent=2) + "\n")

    console.print(
        f"[green]✓[/green] Anchored {len(streams)} stream(s); "
        f"checkpoint {record.checkpoint_id} → {path}"
    )


@ledger_app.command("verify")
def ledger_verify_cmd(
    capsule: Annotated[str, typer.Argument(help="Capsule directory")],
    pubkey: Annotated[
        Optional[Path],
        typer.Option("--pubkey", help="PEM ed25519 public key to verify the checkpoint"),
    ] = None,
    observed_epoch: Annotated[
        Optional[int],
        typer.Option("--observed-epoch", help="Reject a checkpoint epoch below this"),
    ] = None,
) -> None:
    """Verify the ledger; exit with the ADR-0094 taxonomy.

    Without ``--pubkey`` the check is structural (chains + epoch + anchor); the
    signature is then not verified.

    \b
    Exit codes: 0 ok | 3 tamper | 4 reorder | 5 truncation | 6 bad sig |
                7 rollback | 8 merkle | 9 anchor | 10 no checkpoint

    \b
    Examples:
      nova ledger verify ./run --pubkey ed25519.pub.pem
      nova ledger verify ./run --pubkey k.pub --observed-epoch 5
    """
    capsule_dir = _resolve_capsule(capsule)

    verify_fn = None
    if pubkey is not None:
        try:
            pub_pem = pubkey.read_bytes()
        except OSError as exc:
            console.print(f"[red]Failed to read public key:[/red] {exc}")
            raise typer.Exit(code=1) from exc

        def verify_fn(pae: bytes, sig: bytes) -> bool:
            return verify_with_pem(pub_pem, pae, sig)

    verdict = verify_ledger(
        capsule_dir, None, verify_fn, observed_epoch=observed_epoch
    )
    code = exit_code_for(verdict)
    if verdict.ok:
        console.print("[green]✓ ledger OK[/green]")
    else:
        console.print(f"[red]✗ {verdict.exit.name}[/red] (exit {code})")
        for reason in verdict.reasons:
            console.print(f"  - {reason}")
    raise typer.Exit(code=code)


@ledger_app.command("status")
def ledger_status_cmd(
    capsule: Annotated[str, typer.Argument(help="Capsule directory")],
    fmt: Annotated[
        str, typer.Option("--format", help="Output format: table | json")
    ] = "table",
) -> None:
    """Show per-stream sidecar-chain heads (chain health).

    \b
    Examples:
      nova ledger status ./run
      nova ledger status ./run --format json
    """
    capsule_dir = _resolve_capsule(capsule)
    streams = _discover_jsonl_streams(capsule_dir)
    rows: list[dict[str, object]] = []
    for stream in streams:
        chain = capsule_dir / ".ledger" / f"{stream}.chain.json"
        if not chain.exists():
            continue
        verdict = verify_chain(capsule_dir, stream)
        rows.append(
            {
                "stream": stream,
                "seq_high": verdict.seq_high,
                "record_count": verdict.record_count,
                "head_link_hash": "sha256:" + verdict.head_link_hash
                if verdict.head_link_hash
                else "",
                "status": verdict.tamper_class.value,
                "jsonl_present": jsonl_path_for(capsule_dir, stream).exists(),
            }
        )

    if not rows:
        console.print("[red]No sidecar chains found (run `nova ledger anchor`).[/red]")
        raise typer.Exit(code=1)

    if fmt == "json":
        typer.echo(json.dumps({"streams": rows}, indent=2))
        return

    for row in rows:
        console.print(
            f"{row['stream']:<18} seq_high={row['seq_high']:<4} "
            f"records={row['record_count']:<4} status={row['status']}"
        )
