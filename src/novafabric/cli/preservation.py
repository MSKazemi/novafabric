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

"""``nova preservation`` — record/verify re-seals and LTV renewals (ADR-0165 P3).

Record-only (NF-333 / NF-334). ``reseal record`` appends a crypto re-seal
event and ``ltv append`` appends an RFC 4998 archive-timestamp renewal to a
preservation facet; ``reseal verify`` / ``ltv verify`` walk the stored records
offline. Nothing here generates a key, calls a Timestamp Authority, or performs
a PQC signature — the operations are recorded by reference. A stored capsule is
**never written**: ``--capsule`` is read-only and the updated facet is emitted
as JSON (stdout, or a new ``--output`` file).

Streams: facet / verdict JSON on stdout; human messages and the
in-mission-boundary line on stderr.

Exit codes: 0 recorded / ok; 1 record broken or refused (including a re-seal
that dropped the original signature, and a stored ``crypto_migration`` /
``ltv_renewal_chain`` that is malformed or tampered — the evidence is broken,
not the command line); 2 bad input (flags, unreadable/unparseable file, no
anchor).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

import typer
import yaml
from pydantic import BaseModel, ValidationError
from rich.console import Console

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
from novafabric.preservation.anchor import PreservationError, PreservationFacet
from novafabric.preservation.reseal import (
    BrokenLtvChainError,
    BrokenResealRecordError,
    OriginalSignatureDroppedError,
    append_crypto_migration,
    append_ltv_renewal,
    crypto_migrations_from_facet,
    ltv_chain_from_facet,
    plan_ltv_renewal,
    plan_reseal,
    verify_crypto_migrations,
    verify_ltv_chain,
)

err_console = Console(stderr=True, soft_wrap=True)

T = TypeVar("T")

#: Spec §3 req. 5: every re-seal / renewal output carries this line.
BOUNDARY_LINE = (
    "NovaFabric records and re-verifies re-seal and timestamp-renewal provenance "
    "only; it generated no key, called no Timestamp Authority, performed no "
    "signature, modified no stored capsule, and makes no claim that the archive "
    "is durable, lawful, or regulator-accepted (ADR-0165 I-4)."
)

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_USAGE = 2

#: A preservation facet is metadata; anything larger is not one (bounded read).
MAX_INPUT_BYTES = 16 * 1024 * 1024

preservation_app = typer.Typer(
    no_args_is_help=True,
    help="Record and verify evidence-longevity events: crypto re-seals and LTV "
    "timestamp renewals (ADR-0165 P3, experimental, record-only).",
)
reseal_app = typer.Typer(
    no_args_is_help=True,
    help="Crypto re-seal event record (NF-333): the original signature is always preserved.",
)
ltv_app = typer.Typer(
    no_args_is_help=True,
    help="LTV archive-timestamp renewal chain (NF-334, RFC 4998 semantics).",
)
preservation_app.add_typer(reseal_app, name="reseal")
preservation_app.add_typer(ltv_app, name="ltv")


class _InputError(Exception):
    """A user-input problem, rendered and mapped to exit code 2."""


class _Refused(Exception):
    """A refusal (exit 1) whose message is already user-facing."""


class _MalformedStoredRecord(Exception):
    """A stored P3 record cannot be parsed: the evidence is broken (exit 1).

    Distinct from :class:`_InputError`: the command line was fine, the record
    it points at is not — a verifier must report that as *broken*, never as a
    usage error a script might retry or ignore.
    """


# ── Shared options ────────────────────────────────────────────────────────

CapsuleOpt = Annotated[
    str | None,
    typer.Option(
        "--capsule",
        help="Capsule directory or run id whose facets.preservation to read "
        "(read-only; the capsule is never modified).",
        show_default=False,
    ),
]
FacetOpt = Annotated[
    Path | None,
    typer.Option(
        "--facet",
        help="A standalone preservation-facet JSON document to read instead of a capsule.",
        show_default=False,
    ),
]
OutputOpt = Annotated[
    Path | None,
    typer.Option(
        "--output",
        "-o",
        help="Write the updated facet JSON to this new file (must not exist). Default: stdout.",
        show_default=False,
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print the verification as JSON.")]


# ── Helpers ───────────────────────────────────────────────────────────────


def _describe(exc: Exception) -> str:
    """Render an error without echoing the rejected input (it may be a secret)."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
    return str(exc)


def _read_bounded(path: Path, what: str) -> str:
    try:
        size = path.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise _InputError(f"{what} {path} is {size} bytes, over the {MAX_INPUT_BYTES} limit")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _InputError(f"cannot read {what} {path}: {exc}") from exc


def _validate_facet(data: object) -> PreservationFacet:
    if not isinstance(data, dict):
        raise _InputError("preservation facet must be a JSON/YAML mapping")
    try:
        return PreservationFacet.model_validate(data)
    except (ValueError, PreservationError) as exc:
        raise _InputError(f"preservation facet is invalid: {_describe(exc)}") from exc


def _load_facet(capsule: str | None, facet_path: Path | None) -> PreservationFacet:
    """Read the facet from exactly one of ``--capsule`` (read-only) or ``--facet``."""
    if (capsule is None) == (facet_path is None):
        raise _InputError("pass exactly one of --capsule or --facet")
    if facet_path is not None:
        try:
            return _validate_facet(json.loads(_read_bounded(facet_path, "facet file")))
        except json.JSONDecodeError as exc:
            raise _InputError(f"facet file {facet_path} is not JSON: {exc}") from exc
    assert capsule is not None  # narrowed by the exactly-one check above
    try:
        capsule_dir = resolve_capsule_ref(capsule)
    except CapsuleRefError as exc:
        raise _InputError(str(exc)) from exc
    try:
        manifest = yaml.safe_load(_read_bounded(capsule_dir / "capsule.yaml", "capsule manifest"))
    except yaml.YAMLError as exc:
        raise _InputError(f"cannot parse capsule.yaml in {capsule_dir}: {exc}") from exc
    facets = manifest.get("facets") if isinstance(manifest, dict) else None
    block = facets.get("preservation") if isinstance(facets, dict) else None
    if not isinstance(block, dict):
        raise _InputError(
            f"capsule {capsule_dir.name} has no facets.preservation anchor (NF-331); "
            "re-seal and renewal records append to an anchor. Pass --facet instead."
        )
    return _validate_facet(block)


def _write_output(facet: PreservationFacet, output: Path | None) -> None:
    text = json.dumps(facet.model_dump(mode="json"), indent=2) + "\n"
    if output is None:
        typer.echo(text, nl=False)
        return
    try:
        # "x" — create only: an existing file may be the earlier record version.
        with output.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise _InputError(f"--output {output} already exists; refusing to overwrite") from exc
    except OSError as exc:
        raise _InputError(f"cannot write --output {output}: {exc}") from exc
    err_console.print(f"Wrote updated preservation facet to {output}", highlight=False)


def _stored_record(
    loader: Callable[[PreservationFacet], list[T]], facet: PreservationFacet
) -> list[T]:
    """Load a stored P3 list, mapping a malformed/tampered record to exit 1.

    :class:`OriginalSignatureDroppedError` passes through untouched: it has its
    own, more specific I-1 message.
    """
    try:
        return loader(facet)
    except OriginalSignatureDroppedError:
        raise
    except PreservationError as exc:
        raise _MalformedStoredRecord(f"stored record is malformed: {exc}") from exc


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit_verification(
    title: str, verification: BaseModel, ok: bool, findings: list[Any], json_out: bool
) -> int:
    if json_out:
        payload = verification.model_dump(mode="json")
        payload["ok"] = ok
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    status = "[green]OK[/green]" if ok else "[red]BROKEN[/red]"
    err_console.print(f"{title} {status}", highlight=False)
    for f in findings:
        index = getattr(f, "index", getattr(f, "renewal_index", "?"))
        err_console.print(f"  [red]#{index}[/red] {f.code}: {f.message}", highlight=False)
    return EXIT_OK if ok else EXIT_BROKEN


def _guarded(body: Callable[[], int], *, json_out: bool = False) -> None:
    """Run a command body, map errors to exit codes, always print the boundary.

    With ``json_out``, a malformed stored record still yields a JSON verdict
    (``{"ok": false, "malformed_record": ...}``) so a pipeline sees a result.
    """
    try:
        code = body()
    except _Refused as exc:
        err_console.print(f"[red]Refused:[/red] {exc}", highlight=False)
        code = EXIT_BROKEN
    except OriginalSignatureDroppedError as exc:
        err_console.print(f"[red]Refused:[/red] {exc}", highlight=False)
        code = EXIT_BROKEN
    except _MalformedStoredRecord as exc:
        if json_out:
            typer.echo(json.dumps({"ok": False, "malformed_record": str(exc)}, sort_keys=True))
        err_console.print(f"[red]BROKEN:[/red] {exc}", highlight=False)
        code = EXIT_BROKEN
    except (BrokenResealRecordError, BrokenLtvChainError) as exc:
        err_console.print(f"[red]Refused:[/red] {exc}", highlight=False)
        for f in exc.verification.findings:
            err_console.print(f"  {f.code}: {f.message}", highlight=False)
        code = EXIT_BROKEN
    except (ValueError, PreservationError) as exc:
        err_console.print(f"[red]Error:[/red] {_describe(exc)}", highlight=False)
        code = EXIT_USAGE
    except _InputError as exc:
        err_console.print(f"[red]Error:[/red] {exc}", highlight=False)
        code = EXIT_USAGE
    err_console.print(f"[dim]{BOUNDARY_LINE}[/dim]", highlight=False)
    raise typer.Exit(code)


# ── nova preservation reseal ──────────────────────────────────────────────


@reseal_app.command("record")
def reseal_record_cmd(
    to_alg: Annotated[
        str,
        typer.Option("--to-alg", help="Successor signature algorithm, e.g. ml-dsa-65."),
    ],
    upgrade_ref: Annotated[
        str,
        typer.Option(
            "--upgrade-ref",
            help="Opaque reference to the NF-192 upgrade-signature operation that "
            "performed the re-seal, e.g. NF-192:upgrade-signature#op-1.",
        ),
    ],
    renewal_timestamp_ref: Annotated[
        str,
        typer.Option(
            "--renewal-timestamp-ref",
            help="Reference (digest or URI) to the fresh timestamp over the re-seal.",
        ),
    ],
    original_sig_preserved: Annotated[
        bool | None,
        typer.Option(
            "--original-sig-preserved/--original-sig-dropped",
            help="Required assertion: the original signature still exists and verifies. "
            "--original-sig-dropped is always refused (ADR-0165 I-1).",
            show_default=False,
        ),
    ] = None,
    capsule: CapsuleOpt = None,
    facet: FacetOpt = None,
    from_alg: Annotated[
        str | None,
        typer.Option(
            "--from-alg",
            help="Algorithm re-sealed from. Defaults to the previous re-seal's "
            "--to-alg; required for the first re-seal.",
            show_default=False,
        ),
    ] = None,
    resealed_at: Annotated[
        str | None,
        typer.Option(
            "--resealed-at",
            help="RFC 3339 time of the re-seal (default: now, UTC).",
            show_default=False,
        ),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="PREMIS agent reference (digest or URI) for the provenance event.",
            show_default=False,
        ),
    ] = None,
    output: OutputOpt = None,
) -> None:
    """Record a crypto re-seal event (NF-333, experimental, record-only).

    Appends {from_alg, to_alg, resealed_at, upgrade_ref,
    original_sig_preserved: true, renewal_timestamp_ref} to
    facets.preservation.crypto_migration and a PREMIS "digital signature
    generation" provenance event. Generates no key and performs no signature.

    \b
    Examples:
      nova preservation reseal record --facet preservation.json \\
          --from-alg ed25519 --to-alg ml-dsa-65 \\
          --upgrade-ref NF-192:upgrade-signature#op-1 \\
          --renewal-timestamp-ref sha256:<tst> --original-sig-preserved -o next.json
    """

    def body() -> int:
        if original_sig_preserved is None:
            raise _InputError(
                "state --original-sig-preserved (the original signature still "
                "exists and verifies); a re-seal record must assert it"
            )
        if not original_sig_preserved:
            raise _Refused(
                "a re-seal that dropped or overwrote the original signature is a "
                "replacement, not a migration, and is never recorded (ADR-0165 I-1)"
            )
        current = _load_facet(capsule, facet)
        _stored_record(crypto_migrations_from_facet, current)
        event = plan_reseal(
            current,
            to_alg=to_alg,
            upgrade_ref=upgrade_ref,
            renewal_timestamp_ref=renewal_timestamp_ref,
            resealed_at=resealed_at or _now(),
            from_alg=from_alg,
        )
        updated = append_crypto_migration(current, event, agent_ref=agent)
        _write_output(updated, output)
        err_console.print(
            f"Recorded re-seal {event.from_alg} → {event.to_alg} at {event.resealed_at}",
            highlight=False,
        )
        return EXIT_OK

    _guarded(body)


@reseal_app.command("verify")
def reseal_verify_cmd(
    capsule: CapsuleOpt = None, facet: FacetOpt = None, json_out: JsonOpt = False
) -> None:
    """Verify the crypto re-seal record offline (NF-333, experimental).

    Checks the original signature is asserted preserved on every event,
    algorithms chain (each from_alg is the prior to_alg), no PQC→classic
    downgrade and no step down in signature strength (SIGNATURE_STRENGTH),
    known algorithm identifiers, monotonic time, and a fresh renewal
    timestamp per re-seal. Exit 1 on any finding or a malformed stored record.
    """

    def body() -> int:
        verification = verify_crypto_migrations(
            _stored_record(crypto_migrations_from_facet, _load_facet(capsule, facet))
        )
        return _emit_verification(
            f"Re-seal record ({verification.event_count} events)",
            verification,
            verification.ok,
            list(verification.findings),
            json_out,
        )

    _guarded(body, json_out=json_out)


# ── nova preservation ltv ─────────────────────────────────────────────────


@ltv_app.command("append")
def ltv_append_cmd(
    renewal_type: Annotated[
        Literal["timestamp_renewal", "hash_tree_renewal"],
        typer.Option(
            "--type", help="RFC 4998 renewal kind: timestamp_renewal or hash_tree_renewal."
        ),
    ],
    new_timestamp_ref: Annotated[
        str,
        typer.Option(
            "--new-timestamp-ref",
            help="<alg>:<hex> digest of the renewed RFC 3161 / PQC timestamp token.",
        ),
    ],
    new_hash_alg: Annotated[
        str, typer.Option("--new-hash-alg", help="Hash algorithm of the renewal, e.g. sha3-256.")
    ],
    renewed_before: Annotated[
        str,
        typer.Option(
            "--renewed-before",
            help="The algorithm-sunset date this renewal beat (YYYY-MM-DD or RFC 3339).",
        ),
    ],
    capsule: CapsuleOpt = None,
    facet: FacetOpt = None,
    covered_digest: Annotated[
        str | None,
        typer.Option(
            "--covered-digest",
            help="<alg>:<hex> digest of the evidence the new timestamp covers. Defaults "
            "to the previous timestamp for a timestamp_renewal; required otherwise.",
            show_default=False,
        ),
    ] = None,
    renewed_at: Annotated[
        str | None,
        typer.Option(
            "--renewed-at",
            help="RFC 3339 time of the renewal (default: now, UTC).",
            show_default=False,
        ),
    ] = None,
    expires_at: Annotated[
        str | None,
        typer.Option(
            "--expires-at",
            help="When the renewed timestamp stops being trustworthy, if known; the "
            "next renewal must precede it.",
            show_default=False,
        ),
    ] = None,
    output: OutputOpt = None,
) -> None:
    """Append an LTV archive-timestamp renewal (NF-334, experimental, record-only).

    Appends {renewal_type, covered_digest, new_timestamp_ref, new_hash_alg,
    renewed_before, parent, renewed_at[, timestamp_expires_at]} to
    facets.preservation.ltv_renewal_chain; parent is derived. Refuses (exit 1)
    to extend a broken chain or to append a renewal that does not cover the
    previous one, downgrades the hash, or misses the prior expiry. Calls no TSA.

    \b
    Examples:
      nova preservation ltv append --facet preservation.json \\
          --type hash_tree_renewal --covered-digest sha3-256:<hex> \\
          --new-timestamp-ref sha3-256:<hex> --new-hash-alg sha3-256 \\
          --renewed-before 2035-01-01 -o next.json
    """

    def body() -> int:
        current = _load_facet(capsule, facet)
        _stored_record(ltv_chain_from_facet, current)
        renewal = plan_ltv_renewal(
            current,
            renewal_type=renewal_type,
            new_timestamp_ref=new_timestamp_ref,
            new_hash_alg=new_hash_alg,
            renewed_before=renewed_before,
            renewed_at=renewed_at or _now(),
            covered_digest=covered_digest,
            timestamp_expires_at=expires_at,
        )
        updated = append_ltv_renewal(current, renewal)
        _write_output(updated, output)
        err_console.print(
            f"Recorded {renewal.renewal_type} under {renewal.new_hash_alg} "
            f"(before {renewal.renewed_before})",
            highlight=False,
        )
        return EXIT_OK

    _guarded(body)


@ltv_app.command("verify")
def ltv_verify_cmd(
    capsule: CapsuleOpt = None, facet: FacetOpt = None, json_out: JsonOpt = False
) -> None:
    """Walk the LTV renewal chain offline (NF-334, experimental).

    Reports covers_previous (parent / covered_digest chain), hash_algs_ok (no
    downgrade in the explicit strength order; unknown algorithm is a finding)
    and renewed_in_time (renewed_before monotonic, before the prior timestamp's
    recorded expiry). Exit 1 on any finding or a malformed stored record.
    Tokens are not dereferenced.
    """

    def body() -> int:
        verification = verify_ltv_chain(
            _stored_record(ltv_chain_from_facet, _load_facet(capsule, facet))
        )
        return _emit_verification(
            f"LTV renewal chain ({verification.renewal_count} renewals)",
            verification,
            verification.ok,
            list(verification.findings),
            json_out,
        )

    _guarded(body, json_out=json_out)
