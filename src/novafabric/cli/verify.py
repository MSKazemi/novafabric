"""nova verify — verify a capsule's NovaSeal signature, timestamp, and Merkle log."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console

from novafabric import _paths as _nf_paths
from novafabric._capsule_fs import CapsuleEntryError, read_capsule_file

console = Console()
err_console = Console(stderr=True)


def verify_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path to the capsule directory to verify, or to an "
                "export-manifest.json (batch export, ADR-0141)"
            )
        ),
    ],
    seal_config: Annotated[
        str | None,
        typer.Option(
            "--seal-config",
            envvar="NOVAFABRIC_SEAL_CONFIG",
            help=(
                "Path to novaseal.yaml config (default: $NOVAFABRIC_HOME/novaseal.yaml, "
                "i.e. ~/.novafabric/novaseal.yaml only when NOVAFABRIC_HOME is unset)"
            ),
        ),
    ] = None,
    check_redaction: Annotated[
        Optional[Path],
        typer.Option(
            "--check-redaction",
            help=(
                "Path to a redaction_proof_report.seal.json file. Verifies the "
                "NovaSeal DSSE envelope of a nova subject-proof report instead of a "
                "capsule: the positional argument is then the report JSON the seal "
                "must cover. --ca-bundle validates the signer chain."
            ),
            exists=False,
            dir_okay=False,
        ),
    ] = None,
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help=(
                "Verification backend: 'local' (ECDSA P-256 DSSE, default) or "
                "'sigstore' (Sigstore bundle verification). "
                r'Requires pip install "novafabric\[sigstore]" for the sigstore backend.'
            ),
        ),
    ] = "local",
    capsule_id: Annotated[
        str,
        typer.Option(
            "--capsule-id",
            help=(
                "Capsule ID for Sigstore bundle lookup "
                "(required when --backend sigstore is used without a .seal/ dir)."
            ),
        ),
    ] = "",
    home: Annotated[
        str,
        typer.Option(
            "--home",
            help="NovaFabric home dir for Sigstore bundle lookup (default: ~/.novafabric).",
        ),
    ] = "",
    public_key: Annotated[
        Optional[Path],
        typer.Option(
            "--public-key",
            help=(
                "PEM-encoded ed25519 public key of the export signer "
                "(required when verifying an export-manifest.json; for an Evidence "
                "Bundle ZIP, the key every attestation must be signed with)."
            ),
        ),
    ] = None,
    dest: Annotated[
        Optional[str],
        typer.Option(
            "--dest",
            help=(
                "Override the destination to read exported member blobs from "
                "(export-manifest verification only; default: the manifest's "
                "recorded dest, falling back to the manifest's own directory)."
            ),
        ),
    ] = None,
    ca_bundle: Annotated[
        Optional[Path],
        typer.Option(
            "--ca-bundle",
            help=(
                "Operator CA bundle (concatenated PEM) to validate the DSSE signer "
                "certificate chain against, offline (ADR-0055, experimental; local "
                "backend only). Overrides ca_bundle in novaseal.yaml. A chain that "
                "does not reach a bundle anchor fails verification."
            ),
            dir_okay=False,
        ),
    ] = None,
    crl_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--crl-dir",
            help=(
                "Directory of operator-synced CRLs (DER or PEM) to revocation-check the "
                "validated signer chain against, offline — never fetched (ADR-0070 §3, "
                "experimental). Requires --ca-bundle (or ca_bundle in novaseal.yaml); "
                "overrides crl_dir in novaseal.yaml. A revoked certificate always fails; "
                "a missing/stale/invalid CRL is a visible warning unless --crl-strict."
            ),
            file_okay=False,
        ),
    ] = None,
    crl_strict: Annotated[
        bool,
        typer.Option(
            "--crl-strict",
            help=(
                "With --crl-dir: also fail when a chain certificate has no CRL, only a "
                "stale CRL, or only CRLs whose signature does not verify "
                "(ADR-0070 crl_strict). Also enabled by crl_strict: true in novaseal.yaml."
            ),
        ),
    ] = False,
    tsa_ca_bundle: Annotated[
        Optional[Path],
        typer.Option(
            "--tsa-ca-bundle",
            help=(
                "Operator TSA CA bundle (concatenated PEM). When the capsule or Evidence "
                "Bundle carries an RFC 3161 token (manifest.dsse.tsr), also verify its "
                "CMS signature, the TSA certificate's critical id-kp-timeStamping EKU, "
                "the message imprint, and the chain to this bundle at the token's "
                "genTime, offline (ADR-0070 §1, experimental). With TSA anchors set, a "
                "missing or empty token FAILS (it is not covered by the DSSE signature). "
                "Overrides tsa_ca_certs in novaseal.yaml; --crl-dir/--crl-strict also "
                "apply to this chain (revocation as of genTime, CRL freshness judged now)."
            ),
            dir_okay=False,
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option(
            "--json",
            help=(
                "Print one JSON object instead of the text report (capsule directories, "
                "local backend). Carries identity_trust and timestamp_ok (null when the "
                "capsule has no RFC 3161 token) — ADR-0301, timestamp_trust "
                "(anchored|unanchored|absent|invalid), sealer_log and unanchored_claims. "
                "Exit code is unchanged."
            ),
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help=(
                "Also exit 1 when a passing check rests on something no anchor you "
                "trust vouches for (capsule directories, local backend): a "
                "self-asserted signer (no --ca-bundle), a timestamp whose TSA is not "
                "anchored (no --tsa-ca-bundle / tsa_ca_certs), Merkle inclusion shown "
                "only by the proof the capsule carries or not checked, or "
                "sealer_log=unverifiable-other-version. Listed as unanchored_claims."
            ),
        ),
    ] = False,
) -> None:
    """Verify a capsule's cryptographic seal, timestamp, and Merkle log inclusion.

    Checks three layers of integrity in the .seal/ directory (local backend):
      • ECDSA P-256 DSSE signature
      • RFC 3161 timestamp (structural hash)
      • Merkle log inclusion (proof carried in the capsule, and/or the local log)

    With ``--backend sigstore``, verifies the Sigstore bundle stored under
    ``<home>/sigstore/<capsule_id>.bundle.json``.

    Also reports the signer's trust level (ADR-0301): SELF-ASSERTED (the holder of
    the key signed; no anchor you trust vouches for it), LOCAL CA PINNED (chains to a
    `nova seal init` local CA you passed with --ca-bundle) or CA-ANCHORED (chains to
    another CA bundle you passed). A capsule without an RFC 3161 token reports the
    timestamp as NOT PRESENT (timestamp_ok=None), which does not fail verification.

    The timestamp line separates integrity from trust: OK means the token is intact,
    bound to this envelope and its TSA chain reached an anchor you passed
    (--tsa-ca-bundle or tsa_ca_certs) — timestamp_trust=anchored. Without an anchor a
    present token prints UNANCHORED (timestamp_trust=unanchored): anyone can mint a
    TSA certificate and sign any time, so the genTime it shows is a claim, not trusted
    time. It does not fail verification unless --strict.

    Exits 0 if all checks pass, 1 otherwise. Needs only the capsule: no NovaSeal
    config and no Merkle log are required (an independent auditor can verify on a
    fresh machine). With novaseal.yaml (or NOVAFABRIC_SEAL_CONFIG) the entry is also
    checked against the sealer's own Merkle log. A capsule that carries no
    inclusion proof (sealed by <= v0.102.x) and is not in a local log prints
    "Merkle log inclusion: NOT CHECKED" and does not fail. An entry missing from
    the sealer's log (a local log holding this signer's seals) fails. A v1 capsule
    on a host whose log is a newer v2 log (ADR-0313 migration) is UNVERIFIED unless
    the retired v1 log is listed in previous_merkle_dbs
    (sealer_log=unverifiable-other-version; --strict fails on it).

    --strict exits 1 unless every passing claim is anchored (see unanchored_claims
    in --json): signer CA-validated, timestamp absent or anchored, entry found in
    the sealer's log.

    Scope: single capsule.

    \b
    Examples:
      # Verify a capsule (local ECDSA backend)
      nova verify path/to/my-capsule/

      # Machine-readable result, incl. identity_trust
      nova verify --json path/to/my-capsule/

      # Pin the local seal CA written by `nova seal init`
      nova verify --ca-bundle ~/.novafabric/keys/novaseal/ca.crt.pem path/to/my-capsule/

      # Use an explicit seal config path
      nova verify --seal-config ~/configs/novaseal.yaml path/to/my-capsule/

      # Also validate the signer certificate chain against an operator CA bundle
      nova verify --ca-bundle /etc/novaseal/ca-bundle.crt path/to/my-capsule/

      # ... and revocation-check that chain against locally synced CRLs (offline)
      nova verify --ca-bundle /etc/novaseal/ca-bundle.crt \\
          --crl-dir /var/lib/novaseal/crl --crl-strict path/to/my-capsule/

      # Verify the RFC 3161 token's TSA chain against an operator TSA CA (offline)
      nova verify --tsa-ca-bundle /etc/novaseal/tsa-ca.pem path/to/my-capsule/

      # Fail unless signer, timestamp and log inclusion are all anchored
      nova verify --strict --ca-bundle ca.pem --tsa-ca-bundle tsa-ca.pem path/to/my-capsule/

      # Verify a subject-proof report seal: the seal must cover exactly this report
      nova verify --check-redaction proof.seal.json proof.json

      # Verify a Sigstore bundle
      nova verify --backend sigstore --capsule-id <capsule-id> path/to/my-capsule/

      # Verify an Evidence Bundle ZIP (manifest_hash, digests, signatures, subjects)
      nova verify path/to/evidence-bundle.zip

      # ... and pin the key every attestation must be signed with
      nova verify --public-key signer.pub.pem path/to/evidence-bundle.zip
    """
    for flag, given in (("--json", json_out), ("--strict", strict)):
        if given and (
            check_redaction is not None
            or backend != "local"
            or capsule_dir.suffix == ".zip"
            or capsule_dir.is_file()
        ):
            err_console.print(
                f"[red]Error:[/red] {flag} is supported for capsule directories with the "
                "local backend only."
            )
            raise typer.Exit(code=2)

    # Handle --check-redaction standalone check
    if check_redaction is not None:
        _verify_redaction_seal(
            check_redaction, capsule_dir,
            ca_bundle=ca_bundle, crl_dir=crl_dir, crl_strict=crl_strict,
        )
        return

    # Evidence Bundle ZIP: the bundle manifest carries a sha256 for every
    # artifact, which is what makes the bundle tamper-evident — but nothing
    # recomputed them, so the guarantee shipped as written instructions in the
    # bundle README and every check had to be done by hand.
    if capsule_dir.suffix == ".zip":
        if not capsule_dir.is_file():
            console.print(f"[red]Error:[/red] bundle not found: {capsule_dir}")
            raise typer.Exit(code=1)
        _verify_evidence_bundle(
            capsule_dir,
            tsa_ca_bundle=tsa_ca_bundle,
            crl_dir=crl_dir,
            crl_strict=crl_strict,
            public_key=public_key,
        )
        return

    # Batch export manifest (ADR-0141): a JSON file, not a capsule directory.
    if capsule_dir.is_file() and _is_export_manifest(capsule_dir):
        _verify_export_manifest_cli(capsule_dir, public_key=public_key, dest=dest)
        return

    # Handle Sigstore backend path
    if backend == "sigstore":
        _verify_sigstore(capsule_dir, capsule_id=capsule_id, home=home)
        return

    if backend not in ("local", "sigstore"):
        err_console.print(
            f"[red]Error:[/red] Unknown backend {backend!r}. Choose 'local' or 'sigstore'."
        )
        raise typer.Exit(code=1)

    if not capsule_dir.exists():
        if json_out:
            _emit_json({"valid": False, "sealed": None, "error": "capsule directory not found"})
            raise typer.Exit(code=1)
        console.print(f"[red]Error:[/red] capsule directory not found: {capsule_dir}")
        raise typer.Exit(code=1)

    seal_dir = capsule_dir / ".seal"
    if not seal_dir.exists():
        if json_out:
            _emit_json(
                {
                    "capsule": capsule_dir.name,
                    "valid": False,
                    "sealed": False,
                    "identity_trust": "none",
                    "error": "capsule is not sealed (no .seal/ directory)",
                }
            )
            raise typer.Exit(code=1)
        console.print(
            f"[yellow]No .seal/ directory found in {capsule_dir}[/yellow]\n"
            "This capsule was not sealed with NovaSeal. Sealing is opt-in: run "
            "`nova seal init` once to create a local sealing identity (or configure "
            "novaseal.yaml), then capture again."
        )
        raise typer.Exit(code=1)

    previous_quiet = console.quiet
    if json_out:
        console.quiet = True  # the text report is replaced by one JSON object
    try:
        _verify_capsule_dir(
            capsule_dir,
            seal_dir,
            seal_config=seal_config,
            ca_bundle=ca_bundle,
            crl_dir=crl_dir,
            crl_strict=crl_strict,
            tsa_ca_bundle=tsa_ca_bundle,
            json_out=json_out,
            strict=strict,
        )
    finally:
        console.quiet = previous_quiet


def _emit_json(payload: dict[str, Any]) -> None:
    """Print *payload* as JSON on stdout, bypassing the (possibly quiet) rich console."""
    import json

    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _verify_capsule_dir(
    capsule_dir: Path,
    seal_dir: Path,
    *,
    seal_config: str | None,
    ca_bundle: Optional[Path],
    crl_dir: Optional[Path],
    crl_strict: bool,
    tsa_ca_bundle: Optional[Path],
    json_out: bool,
    strict: bool = False,
) -> None:
    """Verify one sealed capsule directory (local backend) and print the report."""

    # Load signing profile for Merkle DB location
    import os

    if seal_config:
        os.environ["NOVAFABRIC_SEAL_CONFIG"] = seal_config

    try:
        from novafabric.trust.novaseal.config import SealConfigError, load_signing_profile

        profile = load_signing_profile()
    except SealConfigError as exc:
        if json_out:
            _emit_json(
                {"capsule": capsule_dir.name, "valid": False, "error": f"NovaSeal config: {exc}"}
            )
        console.print(f"[red]NovaSeal config error:[/red] {exc}")
        raise typer.Exit(code=1)

    configured_tsa = getattr(profile, "tsa_ca_certs", None)
    if (
        crl_dir is not None
        and ca_bundle is None
        and getattr(profile, "ca_bundle", None) is None
        and tsa_ca_bundle is None
        and not (isinstance(configured_tsa, list) and configured_tsa)
    ):
        err_console.print(
            "[red]Error:[/red] --crl-dir needs a CA bundle to build the chain it checks "
            "(pass --ca-bundle / --tsa-ca-bundle or set ca_bundle / tsa_ca_certs in "
            "novaseal.yaml)."
        )
        raise typer.Exit(code=2)

    # Verification is self-contained (audit finding S2): the signature, the
    # timestamp and a carried inclusion proof need nothing but the capsule. The
    # sealer's Merkle log, when this machine has one, is an extra check — never a
    # prerequisite, or an independent auditor could not verify at all.
    if profile is None:
        console.print(
            "[dim]No NovaSeal config on this machine — verifying from the capsule "
            "alone (no local Merkle log).[/dim]"
        )

    # One verification path (audit H1): the trust radar, the dashboard Seal tab
    # and POST /api/runs/{id}/verify call the same function, so they can never
    # call a capsule valid that this command rejects.
    from novafabric.trust.novaseal import unanchored_claims  # noqa: PLC0415
    from novafabric.trust.novaseal.capsule_verify import verify_capsule  # noqa: PLC0415
    from novafabric.trust.novaseal.identity_trust import (  # noqa: PLC0415
        IDENTITY_TRUST_STATEMENTS,
    )

    report = verify_capsule(
        capsule_dir,
        profile,
        ca_bundle=ca_bundle,
        crl_dir=crl_dir,
        crl_strict=crl_strict,
        tsa_ca_bundle=tsa_ca_bundle,
    )
    result = report.result
    binding = report.binding
    capsule_id = report.log_entry_capsule_id
    capsule_id_ok = report.capsule_id_ok
    chain_check = report.chain_check
    chain_ok = report.chain_ok
    tsa_check = report.tsa_check

    # Print results
    console.print(f"\n[bold]NovaSeal verification:[/bold] {capsule_dir.name}")
    _print_check("Signature (DSSE ECDSA P-256)", result.signature_ok)
    if result.pae_encoding == "legacy-le64":
        # Sealed through v0.102.x over a non-standard PAE: still valid evidence,
        # but a stock DSSE verifier (in-toto, cosign, Rekor) will reject it.
        console.print(
            "    [yellow]⚠ legacy envelope:[/yellow] signed over the pre-spec PAE "
            "(NovaFabric ≤ v0.102.x) — verifies with NovaFabric, not with stock "
            "DSSE tooling. Re-seal to obtain a DSSE v1 envelope."
        )
    if result.signing_intent is not None:
        console.print(f"    Intent: {result.signing_intent.value}")
    if result.timestamp_ok is None:
        # Timestamping is opt-in, so a missing token still verifies — but it is
        # reported as absent (timestamp_ok=None), never as a passed check (ADR-0301).
        console.print(
            "  [yellow]⊘[/yellow] Timestamp (RFC 3161): "
            "[yellow]NOT PRESENT[/yellow] (no tsa_url configured, or TSA unavailable) "
            "— no trusted time"
        )
    else:
        _print_timestamp(result)
    _print_log_inclusion(result.log_inclusion, result.log_notes)
    _print_sealer_log(result.sealer_log)
    binding_ok = _print_capsule_binding(binding)
    if chain_check is not None:
        _print_check("Signer certificate chain (CA bundle)", chain_check.ok)
        console.print(f"    {chain_check.reason}")
        for line in _revocation_lines(chain_check.revocation):
            console.print(f"    {line}")
    tsa_ok = _print_tsa_check(_tsa_lines(tsa_check) if tsa_check is not None else None)
    if not capsule_id_ok:
        _print_check("Log-entry capsule_id matches the signed payload", False)
        console.print(
            f"    [red]log-entry.json says[/red] {capsule_id}\n"
            f"    [red]signed payload hashes to[/red] {report.derived_capsule_id}"
        )

    _print_identity(result)

    # A capture that recorded tampering by its workload is not trustworthy
    # evidence, however good its seal: the seal signs that record.
    from novafabric.trust.novaseal.capsule_verify import (  # noqa: PLC0415
        capture_tampered_error,
        special_entry_errors,
    )

    tamper_errors = (
        [capture_tampered_error(report.capture_tampered)]
        if report.capture_tampered is not None
        else []
    )
    if report.capture_tampered is not None:
        _print_check("Capture integrity (no workload tampering recorded)", False)
    # A link or special file in the directory now: planted after the capture's
    # sweep or after sealing. Never followed, and never valid.
    special_errors = special_entry_errors(report.special_entries)
    if report.special_entries:
        _print_check("No links or special files in the capsule", False)
    all_errors = [*result.errors, *report.errors, *tamper_errors, *special_errors]
    if all_errors:
        console.print()
        for err in all_errors:
            console.print(f"  [red]✗[/red] {err}")

    console.print()
    console.print(str(result))

    overall_ok = report.valid
    claims = unanchored_claims(result)
    if strict:
        _print_strict(claims)
    if json_out:
        _emit_json(
            {
                "capsule": capsule_dir.name,
                "sealed": True,
                "valid": overall_ok,
                "strict": strict,
                "unanchored_claims": claims,
                "timestamp_trust": result.timestamp_trust,
                "timestamp_gen_time": result.timestamp_gen_time,
                "tsa_subject": result.tsa_subject,
                "sealer_log": result.sealer_log,
                "signature_ok": result.signature_ok,
                "pae_encoding": result.pae_encoding,
                "timestamp_ok": result.timestamp_ok,
                "timestamp_present": result.timestamp_present,
                "timestamp_strict": result.timestamp_strict,
                "log_integrity_ok": result.log_integrity_ok,
                "log_inclusion": result.log_inclusion,
                "log_notes": list(result.log_notes),
                "capsule_binding_ok": binding_ok,
                # Additive: which manifest the seal binds, and the unsealed
                # substitutes (manifest files, signed refs) that failed it.
                "manifest_file": binding.manifest_name,
                "unsealed_manifests": list(binding.unsealed_manifests),
                "unsealed_refs": list(binding.unsealed_refs),
                "binding_errors": binding.failure_reasons(),
                "capsule_id_ok": capsule_id_ok,
                "ca_chain_checked": chain_check is not None,
                "ca_chain_ok": chain_ok if chain_check is not None else None,
                "tsa_chain_checked": tsa_check is not None,
                "tsa_chain_ok": tsa_ok if tsa_check is not None else None,
                "identity_trust": result.identity_trust,
                "identity_statement": IDENTITY_TRUST_STATEMENTS[result.identity_trust],
                "signer_subject": result.signer_subject,
                "local_seal_identity": result.local_seal_identity,
                "signing_intent": (
                    result.signing_intent.value if result.signing_intent is not None else None
                ),
                "capture_tampered": report.capture_tampered,
                "special_entries": list(report.special_entries),
                "errors": all_errors,
            }
        )

    if strict and overall_ok and claims:
        # Verified, but not to the anchored standard --strict asked for. Not a
        # seal failure, so no ADR-0192 critical alert.
        raise typer.Exit(code=1)
    if not overall_ok:
        # ADR-0192 wired source: the evidence guarantee itself failed, so
        # this is `critical` — the run can no longer be proven.
        from novafabric.events.sources import (  # noqa: PLC0415
            emit_seal_verify_failed_alert,
        )

        emit_seal_verify_failed_alert(
            capsule_id=capsule_id,
            errors=all_errors,
            signature_ok=result.signature_ok,
        )
        raise typer.Exit(code=1)


def _print_identity(result: Any) -> None:
    """Print the signer trust level (ADR-0301) — never stronger than what was checked."""
    from novafabric.trust.novaseal.identity_trust import (  # noqa: PLC0415
        IDENTITY_NONE,
        IDENTITY_SELF_ASSERTED,
        IDENTITY_TRUST_HEADLINES,
        IDENTITY_TRUST_STATEMENTS,
    )

    level = result.identity_trust
    if level == IDENTITY_NONE:
        colour = "red"
    elif level == IDENTITY_SELF_ASSERTED:
        colour = "yellow"
    else:
        colour = "green"
    console.print(
        f"  [bold]Signer identity:[/bold] [{colour}]{IDENTITY_TRUST_HEADLINES[level]}"
        f"[/{colour}] (identity_trust={level})"
    )
    console.print(f"    {IDENTITY_TRUST_STATEMENTS[level]}")
    if result.signer_subject and level == IDENTITY_SELF_ASSERTED:
        if result.local_seal_identity:
            console.print(
                "    certificate: NovaFabric local seal identity (`nova seal init`); pin "
                "its CA with --ca-bundle for continuity across key rotations"
            )
        else:
            console.print(
                f"    certificate subject (unverified claim): {result.signer_subject} "
                "— pass --ca-bundle to check it"
            )


def _tsa_chain_check(
    token: bytes | None,
    expected_digest: bytes,
    anchor_paths: list[Path],
    *,
    crl_dir: Path | None = None,
    crl_strict: bool = False,
) -> tuple[bool, str, list[str]]:
    """TSA chain check (ADR-0070 §1) as ``(ok, reason, lines to print)``.

    The check itself is ``capsule_verify.tsa_chain_check``; this renders it.
    """
    from novafabric.trust.novaseal.capsule_verify import tsa_chain_check  # noqa: PLC0415

    return _tsa_lines(
        tsa_chain_check(
            token, expected_digest, anchor_paths, crl_dir=crl_dir, crl_strict=crl_strict
        )
    )


def _tsa_lines(check: Any) -> tuple[bool, str, list[str]]:
    """Render an ``AnchorCheck`` from a TSA chain check as printable lines."""
    lines: list[str] = []
    if check.gen_time is not None:
        lines.append(f"genTime: {check.gen_time}")
    if check.signer_subject is not None:
        lines.append(f"TSA signer: {check.signer_subject}")
    if check.chain_subjects:
        lines.append("chain: " + " <- ".join(check.chain_subjects))
    lines.extend(_revocation_lines(check.revocation))
    return check.ok, check.reason, lines


def _print_tsa_check(check: tuple[bool, str, list[str]] | None) -> bool:
    """Print the TSA trust-chain block; return ``False`` only on a failed check.

    ``None`` means no TSA anchors were given or configured: nothing is printed (the
    soft ``Timestamp (RFC 3161): NOT PRESENT`` line already covers a missing token).
    """
    if check is None:
        return True
    ok, reason, lines = check
    label = "TSA certificate chain (RFC 3161, TSA CA bundle)"
    _print_check(label, ok)
    console.print(f"    {reason}")
    for line in lines:
        console.print(f"    {line}")
    return ok


def _revocation_lines(revocation: Any) -> list[str]:
    """Render a ``RevocationCheckResult`` as indented, colour-coded CLI lines."""
    if revocation is None:
        return []
    mode = "strict" if revocation.strict else "soft-fail"
    lines = [f"Revocation (CRL, offline, {mode}):"]
    colours = {"good": "green", "revoked": "red"}
    for cert in revocation.certificates:
        status = cert.status.value
        colour = colours.get(status, "red" if revocation.strict else "yellow")
        label = status.upper() if status in colours else f"WARNING {status}"
        if status not in colours and revocation.strict:
            label = f"FAIL {status}"
        lines.append(f"  [{colour}]{label}[/{colour}] {cert.subject}: {cert.detail}")
    for finding in revocation.findings:
        lines.append(f"  [yellow]skipped[/yellow] {finding.source}: {finding.message}")
    return lines


def _print_capsule_binding(binding: Any) -> bool:
    """Print the ADR-0251 binding checks. Returns False if the capsule is unbound."""
    name = binding.manifest_name
    label = f"Manifest binding ({name} == signed payload)"
    if not binding.manifest_present:
        console.print(
            "  [yellow]⊘[/yellow] Manifest binding: "
            "[yellow]NOT PRESENT[/yellow] (no manifest file to compare)"
        )
    elif binding.manifest_ok and not binding.unsealed_manifests:
        _print_check(label, True)
    else:
        _print_check(label, False)
        if binding.differing_keys:
            keys = ", ".join(binding.differing_keys[:8])
            more = len(binding.differing_keys) - 8
            console.print(
                f"    [red]{name} disagrees with the signed payload:[/red] {keys}"
                + (f" (+{more} more)" if more > 0 else "")
            )
    # A manifest-named file or a signed ref's target the seal does not cover is a
    # substitute every reader would trust: a failure, never "not covered" noise.
    for reason in binding.reasons:
        console.print(f"  [red]✗[/red] {reason}")

    if not binding.digests_present:
        console.print(
            "  [yellow]⊘[/yellow] Evidence binding: "
            "[yellow]NOT PRESENT[/yellow] (sealed before evidence_digests)"
        )
    elif binding.digests_ok:
        _print_check("Evidence binding (per-file sha256)", True)
    else:
        _print_check("Evidence binding (per-file sha256)", False)
        for rel in binding.mismatched:
            console.print(f"    [red]modified:[/red] {rel}")
        for rel in binding.missing:
            console.print(f"    [red]missing:[/red] {rel}")

    if binding.unlisted:
        console.print(
            f"    [yellow]not covered by the seal ({len(binding.unlisted)}):[/yellow] "
            + ", ".join(binding.unlisted[:5])
            + (" …" if len(binding.unlisted) > 5 else "")
        )
    for err in binding.errors:
        console.print(f"  [red]✗[/red] {err}")
    return bool(binding.ok)


def _is_export_manifest(path: Path) -> bool:
    """True if *path* looks like an ADR-0141 export-manifest.json."""
    import json

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "export_id" in data and "batch_digest" in data


def _verify_export_manifest_cli(
    manifest_path: Path, *, public_key: Path | None, dest: str | None
) -> None:
    """Verify an export manifest offline: signature, batch digest, every member."""
    from novafabric.export_blob.service import VerifyStatus, verify_export_manifest

    if public_key is None:
        err_console.print(
            "[red]Error:[/red] --public-key <pem> is required to verify an "
            "export manifest (the signer's ed25519 public key, obtained "
            "out-of-band or via `nova export-blob --public-key-out`)."
        )
        raise typer.Exit(code=1)
    try:
        pem = public_key.read_bytes()
    except OSError as exc:
        err_console.print(f"[red]Error:[/red] cannot read --public-key: {exc}")
        raise typer.Exit(code=1)

    report = verify_export_manifest(manifest_path, pem, dest_override=dest)

    console.print(f"\n[bold]Export manifest verification:[/bold] {manifest_path}")
    invalid = report.status is VerifyStatus.INVALID
    _print_check("Signature (DSSE ed25519) + batch digest", not invalid)
    _print_check(
        f"Members at destination ({report.members_ok}/{report.members_total})",
        report.status is VerifyStatus.VALID,
    )
    for problem in report.problems:
        console.print(f"  [red]✗[/red] {problem}")
    color = {"VALID": "green", "INCOMPLETE": "yellow", "INVALID": "red"}[report.status.value]
    console.print(f"\n[{color}]{report.status.value}[/{color}]")
    if report.status is not VerifyStatus.VALID:
        raise typer.Exit(code=1)


def _print_check(label: str, ok: bool) -> None:
    icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
    status = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
    console.print(f"  {icon} {label}: {status}")


def _print_timestamp(result: Any) -> None:
    """Print a present token's line: integrity, and whether its *time* is trusted."""
    from novafabric.trust.novaseal import (  # noqa: PLC0415
        TIMESTAMP_TRUST_ANCHORED,
        TIMESTAMP_TRUST_UNANCHORED,
    )

    if result.timestamp_trust == TIMESTAMP_TRUST_ANCHORED:
        _print_check("Timestamp (RFC 3161)", True)
        console.print("    TSA chain verified against your TSA trust anchors — trusted time")
    elif result.timestamp_trust == TIMESTAMP_TRUST_UNANCHORED:
        console.print(
            "  [yellow]⚠[/yellow] Timestamp (RFC 3161): [yellow]UNANCHORED[/yellow] — token "
            "intact and bound to this envelope; its time is NOT trusted"
        )
        claimed = result.timestamp_gen_time or "(unknown)"
        tsa = result.tsa_subject or "(unknown)"
        console.print(
            f"    claims genTime {claimed} from TSA {tsa!r} — unverified: anyone can mint a "
            "TSA certificate and sign any time. Pass --tsa-ca-bundle (or set tsa_ca_certs) "
            "to anchor it"
        )
    else:
        _print_check("Timestamp (RFC 3161)", bool(result.timestamp_ok))
    if result.timestamp_ok and not result.timestamp_strict:
        console.print(
            "    [yellow]⚠ structural check only:[/yellow] the token could not be "
            "parsed strictly, so its TSA signature and imprint binding were not fully "
            "verified"
        )


def _print_sealer_log(sealer_log: str) -> None:
    """Warn when the sealer's log exists only under a newer root version (ADR-0313)."""
    from novafabric.trust.novaseal import SEALER_LOG_OTHER_VERSION  # noqa: PLC0415

    if sealer_log == SEALER_LOG_OTHER_VERSION:
        console.print(
            "  [yellow]⚠[/yellow] Sealer's log: [yellow]UNVERIFIED[/yellow] "
            "(sealer_log=unverifiable-other-version) — this host's log is a newer root "
            "version and cannot confirm or refute this capsule; list the retired log in "
            "previous_merkle_dbs"
        )


def _print_strict(claims: list[str]) -> None:
    """Report the --strict verdict: the unanchored claims a pass would rest on."""
    if not claims:
        console.print("  [green]✓[/green] Strict: every passing claim is anchored")
        return
    console.print(
        "  [red]✗[/red] Strict: [red]FAIL[/red] — passing checks rest on unanchored "
        f"claims: {', '.join(claims)}"
    )


def _print_log_inclusion(status: str, notes: list[str]) -> None:
    """Print the Merkle inclusion outcome (see ``verify_seal_dir``)."""
    if status == "local-log":
        _print_check("Merkle log inclusion", True)
        console.print("    checked against the local (sealer's) Merkle log")
    elif status == "carried-proof":
        _print_check("Merkle log inclusion", True)
        console.print(
            "    inclusion proof carried in the capsule; the tree head it proves against "
            "is not independently anchored"
        )
    elif status == "not-checked":
        console.print(
            "  [yellow]⊘[/yellow] Merkle log inclusion: [yellow]NOT CHECKED[/yellow] — "
            "log not available, inclusion not checked"
        )
        console.print(
            "    this capsule carries no inclusion proof (sealed by NovaFabric ≤ v0.102.x) "
            "and its entry is not in a local Merkle log; verify with the sealer's log "
            "(--seal-config) for this check"
        )
    else:
        _print_check("Merkle log inclusion", False)


def _verify_sigstore(
    capsule_dir: Path,
    *,
    capsule_id: str,
    home: str,
) -> None:
    """Verify a Sigstore bundle for *capsule_dir*.

    Looks up the bundle at ``<home>/sigstore/<capsule_id>.bundle.json``
    and verifies it against the capsule manifest.  If no bundle is found,
    falls back gracefully with an explanatory message and exits 1.
    """
    # Check sigstore package is installed
    try:
        import sigstore  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        err_console.print(
            "[red]Error:[/red] Sigstore backend requires: "
            r"pip install novafabric\[sigstore]"
        )
        raise typer.Exit(code=1)

    import hashlib

    from novafabric.trust.novaseal.sigstore_signer import (
        SigstoreBundleStore,
        SigstoreSigner,
    )

    home_path = Path(home) if home else _nf_paths.nova_home()

    # Determine capsule_id: use provided or compute from manifest
    effective_capsule_id = capsule_id
    manifest_bytes: bytes | None = None

    if capsule_dir.exists():
        # Try to read manifest from capsule dir
        for candidate in ("manifest.json", "capsule.json"):
            manifest_file = capsule_dir / candidate
            if manifest_file.exists():
                try:
                    manifest_bytes = read_capsule_file(
                        capsule_dir, candidate, operation="nova verify"
                    )
                except CapsuleEntryError:
                    continue  # a link or special file is never followed
                if not effective_capsule_id:
                    effective_capsule_id = hashlib.sha256(manifest_bytes).hexdigest()
                break

    if not effective_capsule_id:
        err_console.print(
            "[red]Error:[/red] Cannot determine capsule ID. "
            "Provide --capsule-id or point to a capsule directory with manifest.json."
        )
        raise typer.Exit(code=1)

    bundle_dict = SigstoreBundleStore.load_bundle(effective_capsule_id, home_path)
    if bundle_dict is None:
        err_console.print(
            f"[red]Error:[/red] No Sigstore bundle found for capsule {effective_capsule_id!r}. "
            f"Run [bold]nova seal sign --backend sigstore[/bold] first."
        )
        raise typer.Exit(code=1)

    # Verify bundle; artifact_bytes defaults to manifest if available
    artifact_bytes = manifest_bytes or effective_capsule_id.encode("utf-8")
    signer = SigstoreSigner()
    result = signer.verify_bundle(bundle_dict, artifact_bytes)

    dir_name = capsule_dir.name if capsule_dir != Path("") else effective_capsule_id
    console.print(f"\n[bold]Sigstore verification:[/bold] {dir_name}")
    _print_check("Sigstore bundle signature + Rekor inclusion", result.valid)
    if result.identity:
        console.print(f"    Identity: {result.identity}")
    if result.rekor_log_index is not None:
        console.print(f"    Rekor log index: {result.rekor_log_index}")

    if result.error:
        console.print()
        console.print(f"  [red]✗[/red] {result.error}")

    console.print()
    console.print(str(result))

    if not result.valid:
        raise typer.Exit(code=1)


def _verify_evidence_bundle(
    bundle_path: Path,
    *,
    tsa_ca_bundle: Path | None = None,
    crl_dir: Path | None = None,
    crl_strict: bool = False,
    public_key: Path | None = None,
) -> None:
    """Run the bundle's verification recipe: digests, manifest_hash, signatures, subjects.

    With ``tsa_ca_bundle`` (ADR-0070 §1, experimental) the bundle's
    ``manifest.dsse.tsr`` is also verified against that TSA CA — signature, EKU,
    chain at genTime, and a message imprint equal to SHA-256 of
    ``attestations/run.intoto.json`` (what ``nova export evidence`` timestamps).

    ``manifest.json`` lists each packaged file with its ``sha256``, and the
    manifest itself carries a ``manifest_hash`` over the artifact list. Checking
    both is what turns "we wrote the hashes down" into a verification: a single
    edited byte anywhere in the bundle changes one digest and is reported by
    name. Every attestation's DSSE signature is verified under the key the bundle
    ships, and its subject is checked against the manifest
    (:func:`novafabric.evidence.bundle_verify.verify_bundle_signatures`); with
    ``public_key`` the signer must also be that key. Exits 1 on any failure.
    """
    import zipfile

    from novafabric.evidence.bundle_verify import BundleArchiveError, verify_bundle_archive

    trusted_pems: list[bytes] = []
    if public_key is not None:
        try:
            trusted_pems.append(public_key.read_bytes())
        except OSError as exc:
            console.print(f"[red]Error:[/red] cannot read --public-key: {exc}")
            raise typer.Exit(code=1) from None
    try:
        zf = zipfile.ZipFile(bundle_path)
    except zipfile.BadZipFile as exc:
        console.print(f"[red]Error:[/red] not a readable ZIP: {bundle_path} ({exc})")
        raise typer.Exit(code=1) from exc

    # One verifier for the CLI and `POST /api/evidence/{bundle_id}/verify`, so the two
    # can never reach different verdicts on the same bundle.
    with zf:
        try:
            outcome = verify_bundle_archive(
                zf,
                trusted_public_pems=trusted_pems,
            )
        except BundleArchiveError as exc:
            console.print(f"[red]Error:[/red] {bundle_path}: {exc}")
            raise typer.Exit(code=1) from exc
        names = set(zf.namelist())
        tsa_check = (
            _bundle_tsa_check(zf, names, tsa_ca_bundle, crl_dir=crl_dir, crl_strict=crl_strict)
            if tsa_ca_bundle is not None
            else None
        )

    signed = outcome.signatures
    console.print(f"\nEvidence Bundle verification: {bundle_path.name}")
    console.print(f"  bundle_id: {outcome.manifest.get('bundle_id', '(none)')}")
    console.print(f"  artifacts: {outcome.artifact_count}")
    _print_check(f"Artifact digests ({outcome.artifact_count} recomputed)", outcome.digests_ok)
    for rel in outcome.modified:
        console.print(f"    [red]✗ modified:[/red] {rel}")
    if outcome.missing:
        _print_check(f"All listed artifacts present ({len(outcome.missing)} missing)", False)
        for rel in outcome.missing:
            console.print(f"    [red]✗ missing:[/red] {rel}")
    else:
        _print_check("All listed artifacts present", True)
    if outcome.unlisted:
        _print_check(f"No unlisted files ({len(outcome.unlisted)} extra)", False)
        for rel in outcome.unlisted:
            console.print(f"    [red]✗ not in manifest:[/red] {rel}")
    else:
        _print_check("No unlisted files", True)
    for rel in outcome.duplicate_members:
        console.print(f"    [red]✗ duplicate archive member:[/red] {rel}")

    _print_check("manifest_hash matches the manifest", signed.manifest_hash_ok)
    _print_check(
        f"Attestation signatures ({signed.attestations_checked} verified)",
        signed.attestations_checked > 0 and not signed.bad_signatures and not signed.unsigned,
    )
    for rel in signed.bad_signatures:
        console.print(f"    [red]✗ bad signature:[/red] {rel}")
    for rel in signed.unsigned:
        console.print(f"    [red]✗ unsigned attestation:[/red] {rel}")
    _print_check("Attestation subjects match the manifest", not signed.bad_subjects)
    for rel in signed.bad_subjects:
        console.print(f"    [red]✗ subject:[/red] {rel}")
    for err in signed.errors:
        console.print(f"    [red]✗[/red] {err}")
    _print_members(outcome)
    if public_key is not None:
        _print_check("Signer is the --public-key", not signed.signer_not_trusted)
    elif signed.signer_fingerprints:
        console.print(
            "  [yellow]![/yellow] signer not pinned: signatures verify under the key shipped "
            f"in the bundle ({', '.join(signed.signer_fingerprints)}); pass --public-key "
            "to require a known signer"
        )
    tsa_ok = _print_tsa_check(tsa_check)
    if tsa_check is None and outcome.has_timestamp_token:
        # Not checked at all without anchors: say so rather than staying silent, so
        # the bundle's token is never read as a verified, trusted time.
        console.print(
            "  [yellow]⚠[/yellow] Timestamp (RFC 3161): [yellow]NOT CHECKED[/yellow] — the "
            "bundle carries manifest.dsse.tsr, but no TSA trust anchor was given; its time "
            "is not trusted (pass --tsa-ca-bundle)"
        )
    ok = outcome.ok and tsa_ok
    console.print(
        f"\nartifacts_ok={outcome.digests_ok}, complete={outcome.complete}, "
        f"no_extras={outcome.no_extras}"
    )
    if not ok:
        console.print("[red]Evidence Bundle verification FAILED[/red]")
        raise typer.Exit(code=1)
    if not outcome.members_signed:
        # A legacy bundle: its signed content verified, but not every member is
        # signed. Never reported as a plain PASSED (ADR-0011 Amendment 7).
        console.print(
            "[yellow]Evidence Bundle verification PASSED — signed content only[/yellow] "
            f"({len(outcome.unsigned_members)} member(s) not covered by any signature)"
        )
        return
    console.print("[green]Evidence Bundle verification PASSED[/green]")


def _print_members(outcome: Any) -> None:
    """Report the members statement check (ADR-0011 Amendment 7, review r8)."""
    if outcome.member_problems:
        _print_check("Every bundle member is covered by the signed members statement", False)
        for problem in outcome.member_problems:
            console.print(f"    [red]✗ members:[/red] {problem}")
        return
    if outcome.members_signed:
        _print_check("Every bundle member is covered by the signed members statement", True)
        return
    unsigned = outcome.unsigned_members
    shown = ", ".join(unsigned[:8]) + (f", … (+{len(unsigned) - 8})" if len(unsigned) > 8 else "")
    console.print(
        "  [yellow]![/yellow] signed content only: this bundle has no members statement "
        "(exported before it existed), so these members are not covered by any signature "
        f"and are not verified: {shown}"
    )


def _bundle_tsa_check(
    zf: Any,
    names: set[str],
    tsa_ca_bundle: Path,
    *,
    crl_dir: Path | None,
    crl_strict: bool,
) -> tuple[bool, str, list[str]]:
    """TSA trust-chain check for an Evidence Bundle ZIP's ``manifest.dsse.tsr``.

    The token timestamps ``attestations/run.intoto.json`` (ADR-0030), so its
    message imprint must equal that entry's SHA-256. Reads are size-bounded.
    """
    from novafabric.trust.novaseal.tsa_token import MAX_TOKEN_BYTES  # noqa: PLC0415

    if "manifest.dsse.tsr" not in names:
        return _tsa_chain_check(None, b"", [tsa_ca_bundle])
    if "attestations/run.intoto.json" not in names:
        return False, "bundle has a timestamp token but no attestations/run.intoto.json", []
    with zf.open("manifest.dsse.tsr") as handle:
        token = handle.read(MAX_TOKEN_BYTES + 1)
    digest = hashlib.sha256()
    with zf.open("attestations/run.intoto.json") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return _tsa_chain_check(
        token, digest.digest(), [tsa_ca_bundle], crl_dir=crl_dir, crl_strict=crl_strict
    )


def _verify_redaction_seal(
    seal_path: Path,
    report_path: Path | None = None,
    *,
    ca_bundle: Path | None = None,
    crl_dir: Path | None = None,
    crl_strict: bool = False,
) -> None:
    """Verify a ``nova subject-proof`` report seal (G-CROSS-004) against its report.

    Three checks, all required (review cli-b-02): the envelope's signature verifies;
    its signed payload is exactly *report_path* bound to intent ``verified`` — the
    form ``nova subject-proof`` seals — so a valid seal over some other document
    does not pass; and the signer's identity is reported: ``SELF-ASSERTED`` without
    *ca_bundle* (any self-made key signs a valid envelope), chain-validated — or
    failed — with it. *report_path* defaults to ``X.json`` for ``X.seal.json``.

    Exits 0 when every check passes, 1 when one fails, 2 when *report_path* is a
    directory or does not exist (a capsule is not what this seal covers).
    """
    if not seal_path.exists():
        console.print(f"[red]Error:[/red] seal file not found: {seal_path}")
        raise typer.Exit(code=1)
    if report_path is None:
        report_path = seal_path.with_name(seal_path.name.removesuffix(".seal.json") + ".json")
    if not report_path.is_file():
        what = "is a directory" if report_path.is_dir() else "does not exist"
        err_console.print(
            f"[red]Error:[/red] {report_path} {what}. With --check-redaction, pass the "
            "report JSON the seal covers (proof.json for proof.seal.json), not a capsule."
        )
        raise typer.Exit(code=2)

    console.print(f"\n[bold]Redaction proof seal:[/bold] {seal_path.name}")
    try:
        from novafabric.trust.novaseal.envelope import (
            EnvelopeError,
            SigningIntent,
            bind_intent,
            extract_payload,
            verify_envelope,
        )

        seal_bytes = seal_path.read_bytes()
        verify_envelope(seal_bytes)
    except Exception as exc:
        _print_check("Signature (DSSE ECDSA P-256)", False)
        console.print(f"\n[red]✗[/red] {exc}")
        console.print("\n[red]Redaction proof seal is INVALID[/red]")
        raise typer.Exit(code=1) from exc
    _print_check("Signature (DSSE ECDSA P-256)", True)

    try:
        expected = json.dumps(
            bind_intent(json.loads(report_path.read_bytes()), SigningIntent.VERIFIED),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        covered = extract_payload(seal_bytes) == expected
    except (OSError, ValueError, TypeError, AttributeError, EnvelopeError):
        covered = False
    _print_check(f"Seal covers the report {report_path.name} (intent: verified)", covered)
    if not covered:
        console.print(
            f"\n[red]✗[/red] the signed payload does not cover {report_path}: the report "
            "was edited, or the seal belongs to another document or intent"
        )
        console.print("\n[red]Redaction proof seal is INVALID[/red]")
        raise typer.Exit(code=1)

    if ca_bundle is None:
        console.print(
            "  Signer: [yellow]SELF-ASSERTED[/yellow] — any key signs a valid envelope; "
            "pass --ca-bundle to validate the signer chain"
        )
    else:
        from novafabric.trust.novaseal.capsule_verify import signer_chain_check

        chain = signer_chain_check(
            seal_bytes, ca_bundle, crl_dir=crl_dir, crl_strict=crl_strict
        )
        _print_check(f"Signer chain ({ca_bundle.name})", chain.ok)
        if not chain.ok:
            console.print(f"\n[red]✗[/red] {chain.reason}")
            console.print("\n[red]Redaction proof seal is INVALID[/red]")
            raise typer.Exit(code=1)
        console.print(f"    {chain.reason}")
    console.print("\n[green]Redaction proof seal is VALID[/green]")
