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

"""``nova consent`` — ISO/IEC TS 27560-shaped consent receipts (experimental, ADR-0150 NF-183).

``record`` builds a receipt and writes it into the capsule's
``facets.conversation.consent`` list (atomic replace of ``capsule.yaml``);
``withdraw`` sets ``withdrawn_at`` on one stored receipt the same way;
``show`` and ``verify`` are read-only. Every output carries the record-only
notice: NovaFabric does not assert the consent was legally valid.

``verify`` exit codes: 0 every receipt is intact, 1 at least one is defective
(digest mismatch, dangling turn_ref, duplicate consent_id, malformed), 2
nothing to check. ``record`` and ``withdraw`` exit 1 when refused.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.markup import escape

from novafabric.capsule._manifest_write import (
    FORCE_UNSEAL_FLAG,
    FORCE_UNSEAL_HELP,
    UNSEAL_WARNING,
    ManifestWriteError,
    write_capsule_manifest,
)
from novafabric.cli.hitl import CapsuleLoadError, Manifest, read_manifest
from novafabric.hitl._records import AccountabilityRecordError
from novafabric.hitl.consent import (
    CONSENT_NOTICE,
    ConsentWithdrawalError,
    build_consent_receipt,
    load_consents,
    record_consent,
    verify_consents,
    withdraw_recorded_consent,
)
from novafabric.hitl.conversation import ConversationError

console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_DEFECTIVE = 1
EXIT_NOTHING = 2

#: How the receipt binds to the capsule root, stated rather than implied.
BINDING_NOTE = (
    "transitive: the receipt is stored in capsule.yaml, which is a leaf of the "
    "capsule Merkle root; no dedicated attestation entry"
)

_CAPSULE_HELP = "Capsule directory or run id."
_JSON_HELP = "Emit deterministic JSON instead of text."

app = typer.Typer(
    help=(
        "Consent receipts (ISO/IEC TS 27560-shaped) bound into a capsule — record, "
        "withdraw, show, verify (experimental, ADR-0150 NF-183). Record-only: never asserts "
        "legal validity."
    ),
    no_args_is_help=True,
)


def _fail(message: str) -> typer.Exit:
    err_console.print(f"[red]Error:[/red] {escape(message)}", highlight=False)
    return typer.Exit(EXIT_DEFECTIVE)


def _load(ref: str) -> Manifest:
    try:
        return read_manifest(ref)
    except CapsuleLoadError as exc:
        raise _fail(str(exc)) from exc


def _emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps({**payload, "notice": CONSENT_NOTICE}, sort_keys=True, indent=2))


def _notice() -> None:
    console.print(f"[dim]{CONSENT_NOTICE}[/dim]", highlight=False)


def _default_consent_id(subject: str, purpose: str, scope: list[str], given_at: str) -> str:
    """Deterministic id from the receipt's identifying fields."""
    key = json.dumps([subject, purpose, sorted(scope), given_at], separators=(",", ":"))
    return "consent-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _write_manifest(manifest: Manifest, data: dict[str, Any], *, force_unseal: bool) -> None:
    """Atomically replace ``capsule.yaml``; refuse a sealed capsule unless forced."""
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    try:
        result = write_capsule_manifest(manifest.capsule_dir, text, force_unseal=force_unseal)
    except ManifestWriteError as exc:
        raise _fail(str(exc)) from exc
    if result.was_sealed:
        err_console.print(f"[bold red]{escape(UNSEAL_WARNING)}[/bold red]", highlight=False)


@app.command("record")
def consent_record(
    capsule: str = typer.Option(..., "--capsule", help=_CAPSULE_HELP),
    subject: str = typer.Option(
        ..., "--subject", help="Data subject as a pseudonymous 'human:' ref."
    ),
    purpose: str = typer.Option(
        ..., "--purpose", help="Purpose concept code, e.g. dpv:ServiceProvision."
    ),
    scope: list[str] = typer.Option(
        ..., "--scope", help="Processing concept code (repeatable), e.g. dpv:Store."
    ),
    expiry: str | None = typer.Option(None, "--expiry", help="ISO-8601 consent expiry."),
    given_at: str | None = typer.Option(
        None, "--given-at", help="ISO-8601 time consent was given (default: now, UTC)."
    ),
    consent_id: str | None = typer.Option(
        None, "--consent-id", help="Receipt id (default: derived from the fields)."
    ),
    turn: str | None = typer.Option(
        None, "--turn", help="Optional turn_id the consent was given at (must resolve)."
    ),
    not_withdrawable: bool = typer.Option(
        False,
        "--not-withdrawable",
        help="Record the source system's statement that this consent cannot be withdrawn.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build and print the receipt; do not write the capsule."
    ),
    force_unseal: bool = typer.Option(False, FORCE_UNSEAL_FLAG, help=FORCE_UNSEAL_HELP),
    as_json: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Record a consent receipt into the capsule (NF-183).

    Rewrites capsule.yaml atomically, which changes the capsule Merkle root:
    any seal or signature issued over the capsule before this call no longer
    covers it and must be re-issued. A NovaSeal-sealed capsule (.seal/ present)
    is refused unless --force-unseal is passed; a symlinked capsule.yaml or
    capsule directory is always refused. Exits 1 when the receipt or the write
    is refused (malformed field, dangling --turn, duplicate consent id, sealed).

    \b
    Examples:
      nova consent record --capsule 01KZ... --subject human:fp:9f2c4a1b7e0d5638 \\
          --purpose dpv:ServiceProvision --scope dpv:Store --scope dpv:Analyse
    """
    manifest = _load(capsule)
    when = given_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        receipt = build_consent_receipt(
            consent_id=consent_id or _default_consent_id(subject, purpose, scope, when),
            subject_ref=subject,
            purpose=purpose,
            action=scope,
            given_at=when,
            withdrawable=not not_withdrawable,
            expiry=expiry,
            turn_ref=turn,
        )
    except (ConversationError, AccountabilityRecordError, ValueError) as exc:
        # ValueError covers a pydantic ValidationError; its message is not echoed.
        name = type(exc).__name__
        detail = str(exc) if isinstance(exc, ConversationError) else name
        raise _fail(f"consent receipt refused: {detail}") from exc
    outcome = record_consent(manifest.data, receipt)
    if not outcome.recorded:
        raise _fail(f"consent receipt not recorded: {outcome.reason}")
    if not dry_run:
        _write_manifest(manifest, outcome.capsule, force_unseal=force_unseal)
    body = receipt.model_dump(exclude_none=True)
    if as_json:
        _emit_json({"receipt": body, "written": not dry_run})
    else:
        verb = "Would record" if dry_run else "Recorded"
        console.print(
            f"{verb} consent {escape(receipt.consent_id)} for {escape(receipt.subject_ref)}: "
            f"purpose={escape(receipt.purpose)} action={escape(','.join(receipt.action))}",
            highlight=False,
        )
        console.print(f"  receipt_digest {receipt.receipt_digest}", highlight=False)
        if not dry_run:
            console.print(
                "  capsule.yaml rewritten: re-issue any seal or signature over this capsule.",
                highlight=False,
            )
        _notice()
    raise typer.Exit(EXIT_OK)


@app.command("withdraw")
def consent_withdraw(
    capsule: str = typer.Option(..., "--capsule", help=_CAPSULE_HELP),
    consent_id: str = typer.Option(
        ..., "--consent-id", help="consent_id of the stored receipt to withdraw."
    ),
    withdrawn_at: str | None = typer.Option(
        None,
        "--withdrawn-at",
        help="ISO-8601 time consent was withdrawn (default: now, UTC). Never in the future.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the withdrawn receipt; do not write the capsule."
    ),
    force_unseal: bool = typer.Option(False, FORCE_UNSEAL_FLAG, help=FORCE_UNSEAL_HELP),
    as_json: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Record that a stored consent was withdrawn (sets withdrawn_at; experimental).

    The receipt stays in place — a withdrawal is recorded, never a deletion —
    and its receipt_digest still verifies (the digest excludes withdrawn_at).
    Rewrites capsule.yaml atomically like ``record``: a NovaSeal-sealed capsule
    is refused unless --force-unseal, and any earlier seal must be re-issued.
    Exits 1 when refused: unknown or duplicated consent id, a malformed or
    tampered stored receipt, a non-withdrawable or already-withdrawn receipt,
    or a withdrawn_at that is not ISO-8601, precedes given_at, or is in the
    future.

    \b
    Examples:
      nova consent withdraw --capsule 01KZ... --consent-id consent-0001
      nova consent withdraw --capsule 01KZ... --consent-id consent-0001 \\
          --withdrawn-at 2026-09-30T12:00:00Z --dry-run --json
    """
    manifest = _load(capsule)
    when = withdrawn_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        outcome = withdraw_recorded_consent(manifest.data, consent_id, withdrawn_at=when)
    except ConsentWithdrawalError as exc:
        raise _fail(f"consent withdrawal refused: {exc}") from exc
    if not dry_run:
        _write_manifest(manifest, outcome.capsule, force_unseal=force_unseal)
    receipt = outcome.receipt
    if as_json:
        _emit_json(
            {
                "receipt": receipt.model_dump(exclude_none=True),
                "index": outcome.index,
                "written": not dry_run,
            }
        )
    else:
        verb = "Would withdraw" if dry_run else "Withdrew"
        console.print(
            f"{verb} consent {escape(receipt.consent_id)} for {escape(receipt.subject_ref)} "
            f"at {receipt.withdrawn_at}",
            highlight=False,
        )
        console.print(
            f"  receipt_digest {receipt.receipt_digest} (unchanged; excludes withdrawn_at)",
            highlight=False,
        )
        if not dry_run:
            console.print(
                "  capsule.yaml rewritten: re-issue any seal or signature over this capsule.",
                highlight=False,
            )
        _notice()
    raise typer.Exit(EXIT_OK)


@app.command("show")
def consent_show(
    capsule: str = typer.Option(..., "--capsule", help=_CAPSULE_HELP),
    as_json: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Show stored consent receipts in stored order.

    Exits 1 when a stored receipt is malformed.

    \b
    Examples:
      nova consent show --capsule 01KZ... --json
    """
    manifest = _load(capsule)
    loaded = load_consents(manifest.data)
    rows = [{"index": i, **r.model_dump(exclude_none=True)} for i, r in loaded.records]
    defects = [{"index": d.index, "error": d.error} for d in loaded.defects]
    if as_json:
        _emit_json({"consents": rows, "defects": defects})
    else:
        console.print(f"Consent receipts: {len(rows)}", highlight=False)
        for i, r in loaded.records:
            state = f"withdrawn_at={r.withdrawn_at}" if r.withdrawn_at else "not withdrawn"
            console.print(
                f"  [{i}] {escape(r.consent_id)} subject={escape(r.subject_ref)} "
                f"purpose={escape(r.purpose)} action={escape(','.join(r.action))} "
                f"given_at={r.given_at} expiry={r.expiry or '-'} "
                f"withdrawable={r.withdrawable} {state}",
                highlight=False,
            )
        for d in loaded.defects:
            console.print(f"  [red]DEFECT[/red] consent entry {d.index}: {d.error}")
        _notice()
    raise typer.Exit(EXIT_DEFECTIVE if loaded.defects else EXIT_OK)


@app.command("verify")
def consent_verify(
    capsule: str = typer.Option(..., "--capsule", help=_CAPSULE_HELP),
    as_json: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Re-check every consent receipt offline (fail-closed).

    Recomputes each receipt_digest, resolves any turn_ref, flags duplicate
    consent ids, and reports the capsule.yaml digest the receipts bind through.
    Exit 0 intact; 1 defective; 2 nothing to check. It does not assert that
    any consent was legally valid, current, or sufficient.

    \b
    Examples:
      nova consent verify --capsule 01KZ...
    """
    manifest = _load(capsule)
    result = verify_consents(manifest.data)
    status = result.status
    if as_json:
        _emit_json(
            {
                "status": status,
                "verdicts": [v.to_dict() for v in result.verdicts],
                "defects": [{"index": d.index, "error": d.error} for d in result.defects],
                "manifest_digest": manifest.digest,
                "binding": BINDING_NOTE,
            }
        )
    else:
        if status == "empty":
            console.print("Nothing to check: no consent receipts.")
        for v in result.verdicts:
            label = "[green]OK[/green]" if v.ok else "[red]DEFECTIVE[/red]"
            console.print(
                f"  {label} [{v.index}] {escape(v.consent_id)} digest_matches={v.digest_matches} "
                f"turn_resolves={v.turn_resolves} duplicate_id={v.duplicate_id} "
                f"withdrawn={v.withdrawn}",
                highlight=False,
            )
        for d in result.defects:
            console.print(f"  [red]DEFECT[/red] consent entry {d.index}: {d.error}")
        console.print(f"capsule.yaml {manifest.digest} ({BINDING_NOTE})", highlight=False)
        console.print(f"Status: {status}", highlight=False)
        _notice()
    code = {"ok": EXIT_OK, "defective": EXIT_DEFECTIVE, "empty": EXIT_NOTHING}[status]
    raise typer.Exit(code)
