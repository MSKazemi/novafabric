# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""CLI commands for the GDPR Art.17 per-subject DEK lifecycle (cap-001, ADR-0069).

Status — experimental (ADR-0314). ``nova pii erase`` destroys a data subject's
AES-256-GCM DEK. PII captured with ``nova capture --pii-subject <id>`` is encrypted
under that DEK, so after erasure it is unreadable and renders as ``[erased]``. PII
in capsules captured without --pii-subject is not encrypted; erasure does not touch
it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape

from novafabric import _paths as _nf_paths
from novafabric.pii.dek import (
    ErasureDeferredReceipt,
    ErasureHeldReceipt,
    ErasureReceipt,
    open_dek_store,
)
from novafabric.pii.status import PIIStatusError, build_pii_status

console = Console()

#: ``nova pii erase`` exit code when a legal hold kept the DEK (GDPR Art.17(3)(e)):
#: the request was valid but nothing was erased. Distinct from 1 (failure) and from
#: typer's 2 (usage).
EXIT_HELD = 3

app = typer.Typer(
    help=(
        "GDPR Art.17 crypto-shredding — experimental: erasure makes PII unreadable "
        "only in capsules captured with --pii-subject (ADR-0314)."
    ),
    no_args_is_help=True,
)


def _find_capsule_ids_for_subject(
    subject_id: str,
    capsule_dir: Path,
) -> list[str]:
    """Scan *capsule_dir* for redaction_manifest.json files that mention *subject_id*.

    Returns a list of capsule IDs (directory names) whose manifests reference
    the given subject.  The subject_id match is done by scanning for the raw
    string in the manifest JSON (the actual stored value is an HMAC, so we look
    for the directory name as capsule_id which is always stored).

    In practice, because subject_ids are never stored in plaintext (only their
    HMAC appears in redaction manifests), we scan by capsule directory name and
    return all capsule IDs found under the directory.  Operators should supply the
    complete list via ``--capsule-ids`` when available.
    """
    capsule_ids: list[str] = []
    if not capsule_dir.is_dir():
        return capsule_ids

    for manifest_path in capsule_dir.rglob("redaction_manifest.json"):
        try:
            content = manifest_path.read_text(encoding="utf-8")
            data = json.loads(content)
            capsule_id = data.get("capsule_id", "")
            if capsule_id:
                capsule_ids.append(capsule_id)
        except (OSError, json.JSONDecodeError):
            continue

    return capsule_ids


@app.command("erase")
def erase_cmd(
    subject_id: str = typer.Argument(
        ...,
        help="Data subject identifier whose DEK should be destroyed.",
    ),
    capsule_dir: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--capsule-dir",
        exists=False,
        file_okay=False,
        dir_okay=True,
        help=(
            "Directory to scan for redaction_manifest.json files associated "
            "with the data subject. Defaults to where `nova capture` writes: "
            "$NOVAFABRIC_CAPSULE_DIR, else $NOVAFABRIC_HOME/capsules/ "
            "(~/.novafabric/capsules/ when NOVAFABRIC_HOME is unset)."
        ),
    ),
    retention_months: int = typer.Option(
        6,
        "--retention-months",
        min=0,
        help=(
            "Minimum Art.17(3)(b) retention window in months "
            "(NOVA_AI_ACT_RETENTION_MONTHS). Provider mode should use 120."
        ),
    ),
    output: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--output",
        "-o",
        help=(
            "Path to write the ErasureReceipt or ErasureDeferredReceipt JSON. "
            "Defaults to stdout."
        ),
    ),
) -> None:
    """Destroy the AES-256-GCM DEK for a data subject (experimental, ADR-0314).

    Overwrites then deletes the per-subject Data Encryption Key from dek.db.
    PII captured with `nova capture --pii-subject <id>` is encrypted under this
    DEK and becomes unreadable (readers render the placeholder 'erased'); PII in
    capsules captured without --pii-subject is not encrypted and stays readable.
    WORM storage and NovaSeal manifest chains are not modified.

    If the DEK was created within the Art.17(3)(b) retention window
    (--retention-months), an ErasureDeferredReceipt is written instead (exit 0).

    Legal holds win (GDPR Art.17(3)(e)): while any legal hold is active
    (`nova hold create` or POST /api/holds, any registry), the DEK is kept and an
    ErasureHeldReceipt names the holds and the held capsules; exit 3. One DEK
    protects all of a subject's capsules and holds are registry-global, so
    nothing is erased while a hold is active. There is no override. Every
    outcome is appended to the audit log as an erasure.request event
    (subject hash only).

    Exit codes: 0 = erased or deferred; 1 = failure (no DEK, store error);
    3 = held by a legal hold (nothing erased).

    Scope: single data subject.

    \b
    Examples:
      nova pii erase user@example.com
      nova pii erase user@example.com --output receipt.json
      nova pii erase user@example.com --retention-months 120 --output receipt.json
      NOVAFABRIC_HOME=/srv/nova nova pii erase user@example.com
    """
    # Resolve capsule directory: flag > NOVAFABRIC_CAPSULE_DIR > <home>/capsules.
    nova_home = _nf_paths.nova_home()
    if capsule_dir is None:
        capsule_dir = _nf_paths.capsule_dir_for_home(nova_home)

    # Find capsule IDs associated with this subject.
    capsule_ids = _find_capsule_ids_for_subject(subject_id, capsule_dir)

    # Open DEK store and perform erasure.
    try:
        dek_store = open_dek_store(nova_home)
    except Exception as exc:
        console.print(f"[red]✗[/red] Failed to open DEK store: {exc}")
        raise typer.Exit(code=1) from exc

    from novafabric.retention.holds import capsule_base_holds_root

    try:
        result = dek_store.erase_subject(
            subject_id=subject_id,
            capsule_ids=capsule_ids,
            retention_months=retention_months,
            hold_roots=(capsule_base_holds_root(capsule_dir),),
        )
    except KeyError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]✗[/red] Erasure failed: {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        dek_store.close()

    receipt_json = result.model_dump_json(indent=2)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(receipt_json, encoding="utf-8")
    _audit_erasure(subject_id, result)

    if isinstance(result, ErasureReceipt):
        console.print(
            f"[green]✓[/green] DEK destroyed for subject_id={subject_id!r} "
            f"({len(result.capsule_ids_affected)} capsule(s) affected). "
            f"method={result.method}"
        )
    elif isinstance(result, ErasureDeferredReceipt):
        console.print(
            f"[yellow]⚠[/yellow] Erasure deferred for subject_id={subject_id!r}. "
            f"Earliest erasure: {result.earliest_erasure_at.isoformat()} "
            f"(retention_months={retention_months})"
        )
    elif isinstance(result, ErasureHeldReceipt):
        hold_ids = ", ".join(h.hold_id for h in result.holds)
        console.print(
            f"[yellow]⚠[/yellow] Erasure held for subject_id={subject_id!r}: active legal "
            f"hold(s) {hold_ids}. The DEK was NOT destroyed; "
            f"{len(result.held_capsules)} capsule(s) held (GDPR Art.17(3)(e)). "
            "Repeat after the holds are released.",
            soft_wrap=True,
        )

    if output is None:
        sys.stdout.write(receipt_json + "\n")
    if isinstance(result, ErasureHeldReceipt):
        raise typer.Exit(code=EXIT_HELD)


def _audit_erasure(
    subject_id: str,
    result: ErasureReceipt | ErasureDeferredReceipt | ErasureHeldReceipt,
) -> None:
    """Append one hash-only ``erasure.request`` event for this CLI erasure outcome."""
    import hashlib

    from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path

    extra: dict[str, object]
    if isinstance(result, ErasureReceipt):
        state, extra = "COMPLETED", {"capsule_ids": list(result.capsule_ids_affected)}
    elif isinstance(result, ErasureDeferredReceipt):
        state, extra = "DEFERRED", {"earliest_erasure_at": result.earliest_erasure_at.isoformat()}
    else:
        state, extra = "HELD", {
            "hold_ids": [h.hold_id for h in result.holds],
            "held_capsule_ids": [c.capsule_id for c in result.held_capsules],
            "dek_retained": True,
        }
    subject_hash = hashlib.sha256(subject_id.encode("utf-8")).hexdigest()
    AuditLog(resolve_audit_log_path()).append(
        event_type=AuditEventType.ERASURE_REQUEST,
        actor="cli-user",
        resource_id=f"sha256:{subject_hash[:12]}",
        details={
            "via": "nova pii erase",
            "subject_sha256": subject_hash,
            "state": state,
            "receipt_sha256": hashlib.sha256(
                result.model_dump_json().encode("utf-8")
            ).hexdigest(),
            **extra,
        },
    )


_DEK_STATE_STYLE = {"active": "green", "erased": "red", "unknown": "yellow"}


@app.command("status")
def status_cmd(
    capsule: str = typer.Argument(
        ...,
        help="Capsule ID or path to a capsule directory.",
    ),
    capsule_dir: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--capsule-dir",
        exists=False,
        file_okay=False,
        dir_okay=True,
        help=(
            "Directory scanned for the capsule's redaction_manifest.json when a "
            "bare capsule ID is given. Defaults to where `nova capture` writes: "
            "$NOVAFABRIC_CAPSULE_DIR, else $NOVAFABRIC_HOME/capsules/ "
            "(~/.novafabric/capsules/ when NOVAFABRIC_HOME is unset)."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the status report as JSON instead of human-readable text.",
    ),
) -> None:
    """Show PII encryption/erasure status for a capsule (read-only, ADR-0069).

    Reports which fields are encrypted (from the capsule's redaction
    manifest), which subjects have active DEKs, and which have been
    crypto-shredded — correlated against $NOVAFABRIC_HOME/dek.db.
    Subjects appear only as HMACs; no key material or plaintext PII is shown.

    Requires NOVA_PII_PEPPER to correlate subjects to DEKs; without it,
    per-subject DEK state is reported as 'unknown'. Never creates or
    modifies dek.db. Local-first: no server required.

    Scope: single capsule.

    \b
    Examples:
      nova pii status 01HXAMPLECAPSULEID
      nova pii status .novafabric/runs/01HX.../
      nova pii status 01HXAMPLECAPSULEID --json
      NOVA_PII_PEPPER=secret nova pii status 01HXAMPLECAPSULEID
    """
    try:
        report = build_pii_status(capsule, capsule_dir=capsule_dir)
    except PIIStatusError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if json_output:
        sys.stdout.write(report.model_dump_json(indent=2) + "\n")
        return

    console.print(f"[bold]Capsule:[/bold] {report.capsule_id}")
    if report.capsule_dir:
        console.print(f"  dir: {report.capsule_dir}", markup=False, highlight=False)
    if report.manifest_present:
        console.print(
            f"  manifest: present — {report.encrypted_field_count} encrypted PII field(s)"
        )
    else:
        console.print("  manifest: not found — no PII fields recorded for this capsule")
    dek_store_state = "present" if report.dek_store_present else "absent"
    console.print(
        f"  DEK store: {report.dek_store_path} ({dek_store_state})",
        markup=False,
        highlight=False,
    )
    if not report.pepper_available:
        console.print(
            "  [yellow]⚠[/yellow] NOVA_PII_PEPPER not set — "
            "per-subject DEK state is 'unknown'"
        )

    if report.subjects:
        console.print("[bold]Subjects:[/bold]")
        for subject in report.subjects:
            style = _DEK_STATE_STYLE[subject.dek_state]
            created = (
                f" (DEK created {subject.dek_created_at.isoformat()})"
                if subject.dek_created_at is not None
                else ""
            )
            rules = escape(", ".join(subject.detection_rule_ids))
            console.print(
                f"  {escape(subject.subject_id_hmac[:23])}… — "
                f"[{style}]{subject.dek_state}[/{style}]{created} — "
                f"{subject.field_count} field(s): {rules}",
                highlight=False,
            )
    if report.fields:
        console.print("[bold]Encrypted fields:[/bold]")
        for field in report.fields:
            console.print(
                f"  {field.field_path} — {field.detection_rule_id} "
                f"(redacted {field.redacted_at_utc})",
                markup=False,
                highlight=False,
            )
