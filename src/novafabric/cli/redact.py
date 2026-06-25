from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console

from novafabric.capture.secrets import (
    SecretScannerV0,
    recompute_chain_hash,
)

console = Console()


def _get_pepper() -> bytes:
    """Read NOVA_PII_PEPPER env var.  Raises if not set."""
    raw = os.environ.get("NOVA_PII_PEPPER", "")
    if not raw:
        console.print(
            "[red]✗[/red] NOVA_PII_PEPPER env var is not set.  "
            "Set it to the same secret value used during capture."
        )
        raise typer.Exit(code=1)
    return raw.encode("utf-8")


def _hmac_subject(pepper: bytes, subject_id: str) -> str:
    mac = hmac.new(pepper, subject_id.encode("utf-8"), hashlib.sha256)
    return "sha256:" + mac.hexdigest()


def subject_proof_cmd(
    subject_id: str = typer.Argument(
        ...,
        help="Data-subject identifier to look up in the redaction index.",
    ),
    db_path: Path | None = typer.Option(
        None,
        "--db",
        help=(
            "Path to the redaction_subject_idx SQLite database.  "
            "Defaults to $NOVAFABRIC_HOME/compliance/redaction_subject_idx.db"
        ),
    ),
    key: Path | None = typer.Option(
        None,
        "--key",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to an ed25519 private key for signing the proof report.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Path to write the redaction_proof_report.json.  Defaults to stdout.",
    ),
) -> None:
    """Look up PII redaction proof for a data subject (GDPR Art.17 erasure evidence).

    Returns cryptographic proof that a subject's PII was redacted.
    Requires NOVA_PII_PEPPER env var (same value used during capture).

    Scope: single capsule.

    \b
    Examples:
      NOVA_PII_PEPPER=secret nova subject-proof path/to/my-capsule/ --subject user@example.com
    """
    try:
        from novafabric.compliance.pii.index import RedactionSubjectIndex
    except ImportError as exc:
        console.print(f"[red]✗[/red] compliance module not available: {exc}")
        raise typer.Exit(code=1) from exc

    pepper = _get_pepper()
    subject_hmac = _hmac_subject(pepper, subject_id)

    if db_path is None:
        home = Path(os.environ.get("NOVAFABRIC_HOME", Path.home() / ".novafabric"))
        db_path = home / "compliance" / "redaction_subject_idx.db"

    report: dict[str, Any]
    if not db_path.exists():
        console.print(
            f"[yellow]⚠[/yellow] Redaction index not found at {db_path}. "
            "No redaction records exist for this subject."
        )
        report = {
            "subject_id_hmac": subject_hmac,
            "records": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "legal_hold_mode": True,
        }
    else:
        idx = RedactionSubjectIndex(db_path=db_path)
        with idx:
            records = idx.lookup_subject(subject_hmac)

        report = {
            "subject_id_hmac": subject_hmac,
            "records": [
                {
                    "capsule_id": r.capsule_id,
                    "field_path": r.field_path,
                    "legal_basis": r.legal_basis,
                    "redacted_at_utc": r.redacted_at_utc,
                }
                for r in records
            ],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "legal_hold_mode": True,
        }

    # Optionally sign the report.
    if key is not None:
        try:
            import base64

            from novafabric.evidence.signing import LocalSigner
            signer = LocalSigner(key)
            canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
            sig = signer.sign(canonical.encode("utf-8"))
            report["signature"] = {
                "keyid": signer.keyid,
                "sig": base64.b64encode(sig).decode("ascii"),
            }
        except Exception as exc:
            console.print(f"[yellow]⚠[/yellow] signing failed: {exc}")

    report_json = json.dumps(report, indent=2)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report_json, encoding="utf-8")
        console.print(
            f"[green]✓[/green] redaction proof report written: {output} "
            f"({len(report['records'])} records for subject)"
        )
        # Attempt NovaSeal document sealing (G-CROSS-004 / FR-03)
        _try_seal_proof_report(output, report_json.encode("utf-8"))
    else:
        console.print(report_json)


def _try_seal_proof_report(report_path: Path, proof_bytes: bytes) -> None:
    """Sign the proof report with NovaSeal if configured (G-CROSS-004 / FR-03).

    Writes ``<report_path>.seal.json`` alongside the report.
    Silently skips if NovaSeal is not configured.
    """
    try:
        from novafabric.trust.novaseal.config import SealConfigError, load_signing_profile
        profile = load_signing_profile()
    except SealConfigError as exc:
        console.print(f"[yellow]⚠[/yellow] NovaSeal config error — proof report not sealed: {exc}")
        return
    except Exception as exc:
        console.print(f"[yellow]⚠[/yellow] NovaSeal unavailable — proof report not sealed: {exc}")
        return

    if profile is None or profile.key_path is None or profile.cert_path is None:
        # NovaSeal not configured; skip silently (not an error for typical usage)
        return

    try:
        from novafabric.trust.novaseal.envelope import SigningIntent, create_envelope
        dsse_bytes = create_envelope(
            proof_bytes,
            profile.key_path,
            profile.cert_path,
            intent=SigningIntent.VERIFIED,
        )
        seal_path = report_path.with_suffix(".seal.json")
        seal_path.write_bytes(dsse_bytes)
        console.print(f"[green]✓[/green] Proof report sealed: {seal_path}")
    except Exception as exc:
        console.print(
            f"[yellow]⚠[/yellow] NovaSeal signing failed — proof report not sealed: {exc}"
        )


def _read_proof(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def _write_proof(path: Path, proof: dict[str, Any]) -> None:
    path.write_text(json.dumps(proof, indent=2))


def _parse_overrides(items: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in items:
        if ":" not in raw:
            raise typer.BadParameter(
                f"--strategy-override must be 'rule_id:strategy', got {raw!r}"
            )
        rule_id, strategy = raw.split(":", 1)
        parsed[rule_id.strip()] = strategy.strip()
    return parsed


def _run_id_for_capsule(capsule_dir: Path) -> str:
    manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str):
        raise typer.BadParameter(
            f"capsule.yaml in {capsule_dir} is missing a string run_id"
        )
    return run_id


def redact_cmd(
    capsule_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to a Run Capsule directory.",
    ),
    strategy_override: list[str] = typer.Option(
        [],
        "--strategy-override",
        help=(
            "Override redaction strategy for a rule. Format: 'rule_id:strategy' where "
            "strategy is one of {mask,hash,drop}. Repeatable."
        ),
    ),
    mark_unsafe_skip: list[str] = typer.Option(
        [],
        "--mark-unsafe-skip",
        help=(
            "Mark a finding_id as user-acknowledged false positive. "
            "Requires --rationale. Repeatable."
        ),
    ),
    rationale: str | None = typer.Option(
        None,
        "--rationale",
        help="Rationale recorded with --mark-unsafe-skip entries. Required when marking.",
    ),
    clear_unsafe_skips: bool = typer.Option(
        False,
        "--clear-unsafe-skips",
        help="Drop existing unsafe_skips entries before writing the proof.",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Interactive triage of findings (requires a TTY). Stub in v0.4.",
    ),
) -> None:
    """Re-scan a capsule and redact any detected PII.

    Updates redaction-proof.json with a new signed redaction record.
    Set NOVA_PII_PEPPER to the same value used during the original capture.

    Scope: single capsule.

    \b
    Examples:
      NOVA_PII_PEPPER=secret nova redact path/to/my-capsule/
    """
    proof_path = capsule_dir / "redaction-proof.json"

    if review and not sys.stdin.isatty():
        console.print(
            "[red]✗[/red] --review requires an interactive TTY; "
            "rerun in a terminal or use --strategy-override / --mark-unsafe-skip."
        )
        raise typer.Exit(code=2)

    if mark_unsafe_skip and not rationale:
        console.print("[red]✗[/red] --mark-unsafe-skip requires --rationale")
        raise typer.Exit(code=1)

    overrides = _parse_overrides(list(strategy_override))
    metadata_only = (
        not overrides
        and not review
        and (clear_unsafe_skips or mark_unsafe_skip)
    )

    if metadata_only:
        if not proof_path.exists():
            console.print(
                f"[red]✗[/red] missing redaction-proof.json in {capsule_dir}"
            )
            raise typer.Exit(code=1)
        proof = _read_proof(proof_path)
        if clear_unsafe_skips:
            proof["unsafe_skips"] = []
        if mark_unsafe_skip:
            assert rationale is not None  # guarded above
            existing_ids = {f["finding_id"] for f in proof["findings"]}
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for fid in mark_unsafe_skip:
                if fid not in existing_ids:
                    console.print(
                        f"[red]✗[/red] finding_id {fid} not present in current proof"
                    )
                    raise typer.Exit(code=1)
                rule_id = next(
                    f["rule_id"] for f in proof["findings"] if f["finding_id"] == fid
                )
                proof.setdefault("unsafe_skips", []).append({
                    "finding_id": fid,
                    "rule_id": rule_id,
                    "rationale": rationale,
                    "decided_by": "cli",
                    "decided_at": now,
                })
        proof = recompute_chain_hash(proof)
        _write_proof(proof_path, proof)
        console.print(
            f"[green]✓[/green] proof updated: {proof_path.name} "
            f"(unsafe_skips={len(proof.get('unsafe_skips', []))})"
        )
        return

    # Re-scan path: run scanner with optional overrides; carry over unsafe_skips.
    run_id = _run_id_for_capsule(capsule_dir)
    prior_unsafe_skips: list[dict[str, Any]] = []
    if proof_path.exists() and not clear_unsafe_skips:
        prior = _read_proof(proof_path)
        prior_unsafe_skips = list(prior.get("unsafe_skips", []))

    scanner = SecretScannerV0(
        capsule_dir=capsule_dir,
        run_id=run_id,
        strategy_overrides=overrides or None,
    )
    proof = scanner.scan_and_redact()

    if prior_unsafe_skips:
        proof["unsafe_skips"] = prior_unsafe_skips

    if mark_unsafe_skip:
        assert rationale is not None
        existing_ids = {f["finding_id"] for f in proof["findings"]}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for fid in mark_unsafe_skip:
            if fid not in existing_ids:
                console.print(
                    f"[red]✗[/red] finding_id {fid} not present in re-scan proof; "
                    "run `nova scan-secrets` to inspect new finding_ids."
                )
                raise typer.Exit(code=1)
            rule_id = next(
                f["rule_id"] for f in proof["findings"] if f["finding_id"] == fid
            )
            proof.setdefault("unsafe_skips", []).append({
                "finding_id": fid,
                "rule_id": rule_id,
                "rationale": rationale,
                "decided_by": "cli",
                "decided_at": now,
            })

    proof = recompute_chain_hash(proof)
    _write_proof(proof_path, proof)
    total = proof["findings_count"]["total"]
    skip_count = len(proof.get("unsafe_skips", []))
    console.print(
        f"[green]✓[/green] re-redacted {capsule_dir.name}: "
        f"{total} findings, {skip_count} unsafe_skips"
    )
