"""nova verify — verify a capsule's NovaSeal signature, timestamp, and Merkle log."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console

from novafabric import _paths as _nf_paths

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
            help="Path to novaseal.yaml config (default: ~/.novafabric/novaseal.yaml)",
        ),
    ] = None,
    check_redaction: Annotated[
        Optional[Path],
        typer.Option(
            "--check-redaction",
            help=(
                "Path to a redaction_proof_report.seal.json file. "
                "Verifies the NovaSeal DSSE envelope of the proof report "
                "independently of the capsule seal check."
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
                "(required when verifying an export-manifest.json)."
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
                "capsule has no RFC 3161 token) — ADR-0301. Exit code is unchanged."
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

    Exits 0 if all checks pass, 1 otherwise. Needs only the capsule: no NovaSeal
    config and no Merkle log are required (an independent auditor can verify on a
    fresh machine). With novaseal.yaml (or NOVAFABRIC_SEAL_CONFIG) the entry is also
    checked against the sealer's own Merkle log. A capsule that carries no
    inclusion proof (sealed by <= v0.102.x) and is not in a local log prints
    "Merkle log inclusion: NOT CHECKED" and does not fail.

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

      # Verify a redaction proof report seal independently
      nova verify --check-redaction path/to/report.seal.json path/to/my-capsule/

      # Verify a Sigstore bundle
      nova verify --backend sigstore --capsule-id <capsule-id> path/to/my-capsule/

      # Verify an Evidence Bundle ZIP (recomputes every artifact digest)
      nova verify path/to/evidence-bundle.zip
    """
    if json_out and (
        check_redaction is not None
        or backend != "local"
        or capsule_dir.suffix == ".zip"
        or capsule_dir.is_file()
    ):
        err_console.print(
            "[red]Error:[/red] --json is supported for capsule directories with the "
            "local backend only."
        )
        raise typer.Exit(code=2)

    # Handle --check-redaction standalone check
    if check_redaction is not None:
        _verify_redaction_seal(check_redaction)
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
            capsule_dir, tsa_ca_bundle=tsa_ca_bundle, crl_dir=crl_dir, crl_strict=crl_strict
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

    # Verification is self-contained (audit finding S2): the signature, the
    # timestamp and a carried inclusion proof need nothing but the capsule. The
    # sealer's Merkle log, when this machine has one, is an extra check — never a
    # prerequisite, or an independent auditor could not verify at all.
    merkle_log = _open_existing_merkle_log(profile)
    if profile is None:
        console.print(
            "[dim]No NovaSeal config on this machine — verifying from the capsule "
            "alone (no local Merkle log).[/dim]"
        )

    # Read capsule_id from log-entry.json
    import json

    log_file = seal_dir / "log-entry.json"
    capsule_id = ""
    if log_file.exists():
        try:
            entry = json.loads(log_file.read_bytes())
            capsule_id = entry.get("entry", {}).get("capsule_id", "")
        except Exception:
            pass

    # ADR-0251 §2: capsule_id above came from log-entry.json, a file inside the
    # capsule an attacker can rewrite. Re-derive it from the signed payload and
    # treat a disagreement as a failure rather than trusting the file.
    dsse_bytes = b""
    dsse_file = seal_dir / "manifest.dsse"
    if dsse_file.exists():
        dsse_bytes = dsse_file.read_bytes()
    capsule_id_ok = True
    derived_capsule_id = _derive_capsule_id(dsse_bytes)
    if derived_capsule_id and capsule_id and derived_capsule_id != capsule_id:
        capsule_id_ok = False

    from novafabric.trust.novaseal import verify_seal_dir  # noqa: PLC0415

    try:
        result = verify_seal_dir(seal_dir, merkle_log=merkle_log)
    finally:
        if merkle_log is not None:
            merkle_log.close()
    binding = _capsule_binding_report(capsule_dir, dsse_bytes)
    chain_bundle = ca_bundle if ca_bundle is not None else getattr(profile, "ca_bundle", None)
    crl_directory = crl_dir if crl_dir is not None else getattr(profile, "crl_dir", None)
    strict_crl = crl_strict or bool(getattr(profile, "crl_strict", False))
    configured_tsa = getattr(profile, "tsa_ca_certs", None)
    tsa_anchor_paths: list[Path] = (
        [tsa_ca_bundle]
        if tsa_ca_bundle is not None
        else list(configured_tsa)
        if isinstance(configured_tsa, list)
        else []
    )
    if crl_dir is not None and chain_bundle is None and not tsa_anchor_paths:
        err_console.print(
            "[red]Error:[/red] --crl-dir needs a CA bundle to build the chain it checks "
            "(pass --ca-bundle / --tsa-ca-bundle or set ca_bundle / tsa_ca_certs in "
            "novaseal.yaml)."
        )
        raise typer.Exit(code=2)
    chain_check = (
        _signer_chain_check(dsse_bytes, chain_bundle, crl_dir=crl_directory, crl_strict=strict_crl)
        if chain_bundle is not None
        else None
    )

    tsa_check = (
        _tsa_chain_check(
            _read_capsule_tsr(seal_dir / "manifest.dsse.tsr"),
            hashlib.sha256(dsse_bytes).digest(),
            tsa_anchor_paths,
            crl_dir=crl_directory,
            crl_strict=strict_crl,
        )
        if tsa_anchor_paths
        else None
    )

    # ADR-0301: the signer's trust level — what *this verifier* established.
    from novafabric.trust.novaseal.identity_trust import (  # noqa: PLC0415
        IDENTITY_TRUST_STATEMENTS,
        anchored_identity_trust,
    )

    chain_ok = True
    chain_anchor: Any = None
    if chain_check is not None:
        chain_ok, _, _, chain_anchor = chain_check
    result.ca_chain_ok = chain_check is not None and chain_ok
    if result.signature_ok and chain_check is not None and chain_ok:
        result.identity_trust = anchored_identity_trust(chain_anchor)

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
        _print_check("Timestamp (RFC 3161)", result.timestamp_ok)
        if result.timestamp_ok and not result.timestamp_strict:
            console.print(
                "    [yellow]⚠ structural check only:[/yellow] the token could not be "
                "parsed strictly, so its TSA signature and imprint binding were not fully "
                "verified"
            )
        elif result.timestamp_ok and not tsa_anchor_paths:
            console.print(
                "    token intact and bound to this envelope; TSA identity not checked "
                "(pass --tsa-ca-bundle)"
            )
    _print_log_inclusion(result.log_inclusion, result.log_notes)
    binding_ok = _print_capsule_binding(binding)
    if chain_check is not None:
        chain_ok, chain_detail, revocation_lines, _ = chain_check
        _print_check("Signer certificate chain (CA bundle)", chain_ok)
        console.print(f"    {chain_detail}")
        for line in revocation_lines:
            console.print(f"    {line}")
    tsa_ok = _print_tsa_check(tsa_check)
    if not capsule_id_ok:
        _print_check("Log-entry capsule_id matches the signed payload", False)
        console.print(
            f"    [red]log-entry.json says[/red] {capsule_id}\n"
            f"    [red]signed payload hashes to[/red] {derived_capsule_id}"
        )

    _print_identity(result)

    if result.errors:
        console.print()
        for err in result.errors:
            console.print(f"  [red]✗[/red] {err}")

    console.print()
    console.print(str(result))

    overall_ok = result.valid and binding_ok and capsule_id_ok and chain_ok and tsa_ok
    if json_out:
        _emit_json(
            {
                "capsule": capsule_dir.name,
                "sealed": True,
                "valid": overall_ok,
                "signature_ok": result.signature_ok,
                "pae_encoding": result.pae_encoding,
                "timestamp_ok": result.timestamp_ok,
                "timestamp_present": result.timestamp_present,
                "timestamp_strict": result.timestamp_strict,
                "log_integrity_ok": result.log_integrity_ok,
                "log_inclusion": result.log_inclusion,
                "log_notes": list(result.log_notes),
                "capsule_binding_ok": binding_ok,
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
                "errors": list(result.errors),
            }
        )

    if not overall_ok:
        # ADR-0192 wired source: the evidence guarantee itself failed, so
        # this is `critical` — the run can no longer be proven.
        from novafabric.events.sources import (  # noqa: PLC0415
            emit_seal_verify_failed_alert,
        )

        emit_seal_verify_failed_alert(
            capsule_id=capsule_id,
            errors=list(result.errors),
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


def _signer_chain_check(
    dsse_bytes: bytes,
    bundle_path: Path,
    *,
    crl_dir: Path | None = None,
    crl_strict: bool = False,
) -> tuple[bool, str, list[str], Any]:
    """Bind the DSSE signature to a CA-validated signer certificate (ADR-0055).

    Offline and fail-closed. Passes only when some signature entry verifies over the
    signed PAE bytes under the public key of its own embedded certificate *and* that
    same certificate chains to the bundle — chain-validating ``signatures[0].cert``
    alone let a forged envelope pair a legitimate leaf with an attacker's ``pubkey``
    and signature. An unreadable bundle, an envelope without an embedded certificate,
    or a chain that does not reach a bundle anchor all return ``(False, reason, …)``.
    Returns ``(True, "chain: leaf <- ... <- anchor", …)`` on success.

    With ``crl_dir`` (ADR-0070 §3), the validated path is also revocation-checked
    against the locally synced CRLs — never fetched. An unusable CRL directory fails
    closed. The third element holds the per-certificate revocation lines and any
    skipped-file findings, printed so soft-fail warnings are always visible. The fourth
    is the bundle certificate the validated chain ended at (``None`` on failure), which
    decides ``local-ca-pinned`` vs ``ca-anchored`` (ADR-0301).
    """
    from novafabric.trust.novaseal.crl import (  # noqa: PLC0415
        CrlStoreError,
        load_crl_directory,
    )
    from novafabric.trust.novaseal.x509_identity import (  # noqa: PLC0415
        X509ChainError,
        load_ca_bundle,
        verify_dsse_signer_chain,
    )

    try:
        anchors = load_ca_bundle(bundle_path.read_bytes())
        store = load_crl_directory(crl_dir) if crl_dir is not None else None
        outcome = verify_dsse_signer_chain(
            dsse_bytes, anchors, crl_store=store, crl_strict=crl_strict
        )
    except OSError as exc:
        return False, f"cannot read CA bundle {bundle_path}: {exc}", [], None
    except (X509ChainError, CrlStoreError) as exc:
        return False, str(exc), [], None
    lines = _revocation_lines(outcome.revocation)
    if not outcome.valid:
        return False, outcome.reason, lines, None
    anchor = _anchor_by_fingerprint(anchors, outcome.trust_anchor_fingerprint)
    return True, "chain: " + " <- ".join(outcome.chain_subjects), lines, anchor


def _anchor_by_fingerprint(anchors: list[Any], fingerprint: str | None) -> Any:
    """The bundle certificate whose ``sha256:`` fingerprint is *fingerprint*, else None."""
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415

    if fingerprint is None:
        return None
    for cert in anchors:
        if "sha256:" + cert.fingerprint(hashes.SHA256()).hex() == fingerprint:
            return cert
    return None


def _read_capsule_tsr(tsr_path: Path) -> bytes | None:
    """Read ``manifest.dsse.tsr`` bounded to one byte past the token size limit.

    Returns ``None`` when the file is absent. An oversize file is returned truncated
    to ``MAX_TOKEN_BYTES + 1`` bytes, which the parser then rejects (fail closed).
    """
    from novafabric.trust.novaseal.tsa_token import MAX_TOKEN_BYTES  # noqa: PLC0415

    if not tsr_path.is_file():
        return None
    with tsr_path.open("rb") as handle:
        return handle.read(MAX_TOKEN_BYTES + 1)


def _tsa_chain_check(
    token: bytes | None,
    expected_digest: bytes,
    anchor_paths: list[Path],
    *,
    crl_dir: Path | None = None,
    crl_strict: bool = False,
) -> tuple[bool, str, list[str]]:
    """Verify an RFC 3161 token's TSA signature, EKU and chain (ADR-0070 §1).

    Offline and fail-closed. Only called when TSA anchors were given
    (``--tsa-ca-bundle``) or configured (``tsa_ca_certs``): the operator then asked
    for a TSA-attested time, so an absent or empty token *fails* — the ``.tsr`` is
    not covered by the DSSE signature, and a key holder backdating a capsule could
    otherwise just delete it. Returns ``(False, reason, lines)`` on any failure — an
    unreadable anchor file, a missing token or a bad token — and
    ``(True, summary, lines)`` when the token verifies. ``lines`` carry the
    genTime / chain / revocation details to print.
    """
    from novafabric.trust.novaseal.tsa_trust import verify_tsa_trust_chain  # noqa: PLC0415

    try:
        anchors_pem = b"\n".join(p.read_bytes() for p in anchor_paths)
    except OSError as exc:
        return False, f"cannot read TSA CA bundle: {exc}", []
    if token is None:
        return (
            False,
            "no RFC 3161 token (manifest.dsse.tsr) although TSA trust anchors are "
            "configured; the token is not covered by the DSSE signature, so a missing "
            "one cannot be told apart from a deleted one",
            [],
        )
    if not token:
        return (
            False,
            "timestamp token (manifest.dsse.tsr) is empty although TSA trust anchors "
            "are configured",
            [],
        )
    outcome = verify_tsa_trust_chain(
        token,
        anchors_pem,
        crl_dir,
        crl_strict=crl_strict,
        expected_digest=expected_digest,
    )
    lines: list[str] = []
    if outcome.gen_time is not None:
        lines.append(f"genTime: {outcome.gen_time.isoformat()}")
    if outcome.signer_subject is not None:
        lines.append(f"TSA signer: {outcome.signer_subject}")
    if outcome.chain_subjects:
        lines.append("chain: " + " <- ".join(outcome.chain_subjects))
    lines.extend(_revocation_lines(outcome.revocation))
    return outcome.valid, outcome.reason, lines


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


def _derive_capsule_id(dsse_bytes: bytes) -> str:
    """SHA-256 of the DSSE payload — the capsule_id, recomputed rather than read.

    ADR-0251 §2: ``cli/verify.py`` read ``capsule_id`` from ``log-entry.json``, a
    file inside the capsule directory. An identifier read from an attacker-writable
    file and never checked is a suggestion, not an identifier.
    """
    import base64
    import json

    if not dsse_bytes:
        return ""
    try:
        payload = base64.urlsafe_b64decode(json.loads(dsse_bytes)["payload"] + "==")
    except (ValueError, KeyError, TypeError):
        return ""
    return hashlib.sha256(payload).hexdigest()


def _capsule_binding_report(capsule_dir: Path, dsse_bytes: bytes) -> dict[str, Any]:
    """Check that *capsule_dir* is the directory the seal was made over (ADR-0251).

    The DSSE signature proves a manifest was signed. It does not prove that
    manifest describes this directory: ``capsule.yaml`` was never opened during
    verification, and the manifest names its evidence files by filename with no
    digest. Both halves were measured green against a forged capsule on
    2026-08-27.

    Returns a report dict; the caller prints and decides the exit code. Never
    raises — an unreadable envelope degrades to ``present=False``, which prints
    NOT PRESENT rather than a check that did not run.
    """
    import base64
    import json

    report: dict[str, Any] = {
        "manifest_present": False,
        "manifest_ok": False,
        "differing_keys": [],
        "digests_present": False,
        "digests_ok": False,
        "mismatched": [],
        "missing": [],
        "unlisted": [],
        "errors": [],
    }
    if not dsse_bytes:
        return report

    try:
        envelope = json.loads(dsse_bytes)
        payload = base64.urlsafe_b64decode(envelope["payload"] + "==")
        signed = json.loads(payload)
    except (ValueError, KeyError, TypeError) as exc:
        report["errors"].append(f"Cannot decode signed payload: {exc}")
        return report
    if not isinstance(signed, dict):
        report["errors"].append("Signed payload is not a manifest object")
        return report

    # --- half 1: does capsule.yaml on disk match the signed payload? ---
    manifest_file = capsule_dir / "capsule.yaml"
    if not manifest_file.exists():
        # Not every sealed directory is a run capsule — the object capsule store
        # seals a single-field manifest with no capsule.yaml at all. §4: absent is
        # reported as absent, not failed.
        pass
    else:
        report["manifest_present"] = True
        try:
            import yaml

            on_disk = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report["errors"].append(f"Cannot read capsule.yaml: {exc}")
            on_disk = None
        if isinstance(on_disk, dict):
            differing = sorted(
                k for k in set(signed) | set(on_disk) if signed.get(k) != on_disk.get(k)
            )
            report["differing_keys"] = differing
            report["manifest_ok"] = not differing
        elif on_disk is not None:
            report["errors"].append("capsule.yaml did not parse as a mapping")

    # --- half 2: do the evidence files still hash to what was signed? ---
    digests = signed.get("evidence_digests")
    if not isinstance(digests, dict):
        return report  # sealed before ADR-0251 — absent, reported as NOT PRESENT
    report["digests_present"] = True

    on_disk_files = {
        path.relative_to(capsule_dir).as_posix()
        for path in capsule_dir.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.relative_to(capsule_dir).parts[0] not in {".seal", "capsule.yaml"}
    }
    for rel, expected in sorted(digests.items()):
        target = capsule_dir / rel
        if not target.is_file():
            report["missing"].append(rel)
            continue
        actual = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        if not isinstance(expected, dict) or actual != expected.get("sha256"):
            report["mismatched"].append(rel)
    # Files added after sealing are legitimate (`nova export-c2pa` writes one).
    # Report them so nothing is silently uncovered; never fail on them.
    report["unlisted"] = sorted(on_disk_files - set(digests))
    report["digests_ok"] = not report["mismatched"] and not report["missing"]
    return report


def _print_capsule_binding(report: dict[str, Any]) -> bool:
    """Print the ADR-0251 binding checks. Returns False if the capsule is unbound."""
    ok = True

    if not report["manifest_present"]:
        console.print(
            "  [yellow]⊘[/yellow] Manifest binding: "
            "[yellow]NOT PRESENT[/yellow] (no capsule.yaml to compare)"
        )
    elif report["manifest_ok"]:
        _print_check("Manifest binding (capsule.yaml == signed payload)", True)
    else:
        _print_check("Manifest binding (capsule.yaml == signed payload)", False)
        keys = ", ".join(report["differing_keys"][:8])
        more = len(report["differing_keys"]) - 8
        console.print(
            f"    [red]capsule.yaml disagrees with the signed payload:[/red] {keys}"
            + (f" (+{more} more)" if more > 0 else "")
        )
        ok = False

    if not report["digests_present"]:
        console.print(
            "  [yellow]⊘[/yellow] Evidence binding: "
            "[yellow]NOT PRESENT[/yellow] (sealed before evidence_digests)"
        )
    elif report["digests_ok"]:
        _print_check("Evidence binding (per-file sha256)", True)
    else:
        _print_check("Evidence binding (per-file sha256)", False)
        for rel in report["mismatched"]:
            console.print(f"    [red]modified:[/red] {rel}")
        for rel in report["missing"]:
            console.print(f"    [red]missing:[/red] {rel}")
        ok = False

    if report["unlisted"]:
        console.print(
            f"    [yellow]not covered by the seal ({len(report['unlisted'])}):[/yellow] "
            + ", ".join(report["unlisted"][:5])
            + (" …" if len(report["unlisted"]) > 5 else "")
        )
    for err in report["errors"]:
        console.print(f"  [red]✗[/red] {err}")
        ok = False
    return ok


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


def _open_existing_merkle_log(profile: Any) -> Any:
    """Open the configured Merkle log only if it already exists; else None.

    Verification must never *create* a log (an empty one proves nothing and would
    litter an auditor's machine), and a missing log is not an error.
    """
    if profile is None:
        return None
    from novafabric.trust.novaseal.merkle import open_merkle_log  # noqa: PLC0415

    location = str(profile.merkle_db)
    if location.startswith(("postgres://", "postgresql://", "postgresql:/", "postgres:/")):
        return open_merkle_log(location)
    path = Path(location).expanduser()
    if not path.is_file():
        return None
    return open_merkle_log(path)


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
                manifest_bytes = manifest_file.read_bytes()
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
) -> None:
    """Recompute every artifact digest recorded in an Evidence Bundle manifest.

    With ``tsa_ca_bundle`` (ADR-0070 §1, experimental) the bundle's
    ``manifest.dsse.tsr`` is also verified against that TSA CA — signature, EKU,
    chain at genTime, and a message imprint equal to SHA-256 of
    ``attestations/run.intoto.json`` (what ``nova export evidence`` timestamps).

    ``manifest.json`` lists each packaged file with its ``sha256``, and the
    manifest itself carries a ``manifest_hash`` over the artifact list. Checking
    both is what turns "we wrote the hashes down" into a verification: a single
    edited byte anywhere in the bundle changes one digest and is reported by
    name. Exits 1 on any mismatch.
    """
    import hashlib
    import json
    import zipfile

    try:
        zf = zipfile.ZipFile(bundle_path)
    except zipfile.BadZipFile as exc:
        console.print(f"[red]Error:[/red] not a readable ZIP: {bundle_path} ({exc})")
        raise typer.Exit(code=1) from exc

    with zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            console.print(
                f"[red]Error:[/red] {bundle_path} has no manifest.json — "
                "this is not a NovaFabric Evidence Bundle."
            )
            raise typer.Exit(code=1)
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except ValueError as exc:
            console.print(f"[red]Error:[/red] manifest.json is not valid JSON: {exc}")
            raise typer.Exit(code=1) from exc

        artifacts = manifest.get("artifacts") or []
        console.print(f"\nEvidence Bundle verification: {bundle_path.name}")
        console.print(f"  bundle_id: {manifest.get('bundle_id', '(none)')}")
        console.print(f"  artifacts: {len(artifacts)}")

        mismatched: list[str] = []
        missing: list[str] = []
        for art in artifacts:
            rel = art.get("path", "")
            want = art.get("sha256", "")
            if rel not in names:
                missing.append(rel)
                continue
            got = "sha256:" + hashlib.sha256(zf.read(rel)).hexdigest()
            if got != want:
                mismatched.append(rel)

        # Files present in the ZIP that the manifest never accounted for: an
        # addition is a modification too, so it must not pass silently.
        unlisted = sorted(names - {a.get("path", "") for a in artifacts} - {"manifest.json"})
        tsa_check = (
            _bundle_tsa_check(zf, names, tsa_ca_bundle, crl_dir=crl_dir, crl_strict=crl_strict)
            if tsa_ca_bundle is not None
            else None
        )

    _print_check(f"Artifact digests ({len(artifacts)} recomputed)", not mismatched)
    for rel in mismatched:
        console.print(f"    [red]✗ modified:[/red] {rel}")
    if missing:
        _print_check(f"All listed artifacts present ({len(missing)} missing)", False)
        for rel in missing:
            console.print(f"    [red]✗ missing:[/red] {rel}")
    else:
        _print_check("All listed artifacts present", True)
    if unlisted:
        _print_check(f"No unlisted files ({len(unlisted)} extra)", False)
        for rel in unlisted:
            console.print(f"    [red]✗ not in manifest:[/red] {rel}")
    else:
        _print_check("No unlisted files", True)

    tsa_ok = _print_tsa_check(tsa_check)
    ok = not mismatched and not missing and not unlisted and tsa_ok
    console.print(
        f"\nartifacts_ok={not mismatched}, complete={not missing}, no_extras={not unlisted}"
    )
    if not ok:
        console.print("[red]Evidence Bundle verification FAILED[/red]")
        raise typer.Exit(code=1)
    console.print("[green]Evidence Bundle verification PASSED[/green]")


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


def _verify_redaction_seal(seal_path: Path) -> None:
    """Verify a redaction proof report DSSE seal (G-CROSS-004).

    Checks the DSSE envelope in *seal_path* and reports the result.
    Exits 0 on success, 1 on failure.
    """
    if not seal_path.exists():
        console.print(f"[red]Error:[/red] seal file not found: {seal_path}")
        raise typer.Exit(code=1)

    try:
        from novafabric.trust.novaseal.envelope import (
            extract_intent,
            verify_envelope,
        )

        seal_bytes = seal_path.read_bytes()
        verify_envelope(seal_bytes)
        intent = extract_intent(seal_bytes)
        console.print(f"\n[bold]Redaction proof seal:[/bold] {seal_path.name}")
        _print_check("Signature (DSSE ECDSA P-256)", True)
        if intent is not None:
            console.print(f"    Intent: {intent.value}")
        console.print("\n[green]Redaction proof seal is VALID[/green]")
    except Exception as exc:
        console.print(f"\n[bold]Redaction proof seal:[/bold] {seal_path.name}")
        _print_check("Signature (DSSE ECDSA P-256)", False)
        console.print(f"\n[red]✗[/red] {exc}")
        console.print("\n[red]Redaction proof seal is INVALID[/red]")
        raise typer.Exit(code=1)
