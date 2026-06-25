"""CLI commands: nova seal sign / propose / approve / verify / bypass / log verify.

NovaSeal maker-checker signing workflow using linked DSSE envelopes,
and direct capsule signing via ``nova seal sign`` (local or Sigstore backend).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import typer
from rich.console import Console

from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.exceptions import BundleNotFoundError, PolicyNotFoundError
from novafabric.promote.policy_store import PolicyStore
from novafabric.promote.predicates import (
    APPROVAL_PAYLOAD_TYPE,
    PROPOSAL_PAYLOAD_TYPE,
    build_approval_predicate,
    build_proposal_predicate,
    sign_promote_envelope,
    validate_predicate,
    verify_promote_envelope,
)
from novafabric.promote.verifier import verify_sod

console = Console()
err_console = Console(stderr=True)

seal_app = typer.Typer(
    help="NovaSeal cryptographic signing (propose, approve, verify).",
    no_args_is_help=True,
)

_DEFAULT_DB = Path.home() / ".local" / "share" / "novafabric" / "merkle.db"
_DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "novafabric"


@seal_app.command("sign")
def seal_sign_cmd(
    capsule_manifest: str = typer.Argument(
        ...,
        help="Path to a capsule manifest JSON file, or inline JSON string to sign",
    ),
    backend: str = typer.Option(
        "local",
        "--backend",
        help="Signing backend: 'local' (ECDSA P-256, default) or 'sigstore' (keyless).",
        show_default=True,
    ),
    capsule_id: str = typer.Option(
        "",
        "--capsule-id",
        help="Capsule ID for Sigstore bundle storage (defaults to SHA-256 of manifest).",
    ),
    home: str = typer.Option(
        "",
        "--home",
        help="NovaFabric home dir for bundle storage (default: ~/.novafabric).",
    ),
) -> None:
    """Sign a capsule manifest directly (maker step, no checker required).

    With ``--backend local`` (default), produces a NovaSeal DSSE envelope
    using the local ECDSA P-256 key configured in ``novaseal.yaml``.

    With ``--backend sigstore``, performs Sigstore keyless signing via OIDC.
    Requires: pip install novafabric[sigstore].  The resulting Sigstore
    bundle is stored under ``<home>/sigstore/<capsule_id>.bundle.json``.

    Scope: single capsule.

    \b
    Examples:
      # Sign with default local ECDSA backend
      nova seal sign path/to/manifest.json

      # Sign with Sigstore keyless backend
      nova seal sign path/to/manifest.json --backend sigstore

      # Sign with Sigstore and explicit capsule ID
      nova seal sign path/to/manifest.json --backend sigstore \\
        --capsule-id abc123 --home ~/.novafabric
    """
    import hashlib
    import json as _json
    from pathlib import Path as _Path

    # -- Load manifest bytes ------------------------------------------------
    manifest_path = _Path(capsule_manifest)
    if manifest_path.exists():
        manifest_bytes = manifest_path.read_bytes()
    else:
        # Treat as inline JSON
        try:
            _json.loads(capsule_manifest)  # validate JSON
            manifest_bytes = capsule_manifest.encode("utf-8")
        except (_json.JSONDecodeError, ValueError):
            err_console.print(
                f"[red]Error:[/red] {capsule_manifest!r} is neither a valid path "
                "nor valid JSON."
            )
            raise typer.Exit(code=1)

    effective_capsule_id = capsule_id or hashlib.sha256(manifest_bytes).hexdigest()
    home_path = _Path(home) if home else _Path.home() / ".novafabric"

    # -- Backend dispatch ---------------------------------------------------
    if backend == "local":
        console.print(
            "[yellow]nova seal sign --backend local[/yellow] delegates to "
            "[bold]nova capture[/bold] + NovaSeal config.  "
            "Use [bold]nova seal propose[/bold] for the maker-checker workflow."
        )
        raise typer.Exit(code=0)

    elif backend == "sigstore":
        try:
            from novafabric.trust.novaseal.sigstore_signer import (
                SigstoreBundleStore,
                SigstoreSigner,
            )
        except ImportError:  # pragma: no cover — should never happen (same package)
            err_console.print(
                "[red]Error:[/red] Could not import sigstore_signer module."
            )
            raise typer.Exit(code=1)

        # Check sigstore is installed before signing
        try:
            import sigstore  # noqa: F401  # type: ignore[import-not-found]
        except ImportError:
            err_console.print(
                "[red]Error:[/red] Sigstore backend requires: "
                r"pip install novafabric\[sigstore]"
            )
            raise typer.Exit(code=1)

        signer = SigstoreSigner()
        console.print(
            "[bold]Sigstore keyless signing[/bold] — obtaining OIDC token..."
        )
        try:
            bundle_dict = signer.sign_artifact(manifest_bytes)
        except RuntimeError as exc:
            err_console.print(f"[red]Sigstore signing failed:[/red] {exc}")
            raise typer.Exit(code=1)

        bundle_path = SigstoreBundleStore.store_bundle(
            capsule_id=effective_capsule_id,
            bundle_dict=bundle_dict,
            home=home_path,
        )

        from novafabric.trust.novaseal.sigstore_signer import (
            _extract_identity,
            _extract_rekor_log_index,
        )

        identity = _extract_identity(bundle_dict)
        rekor_log_index = _extract_rekor_log_index(bundle_dict)

        console.print(f"[green]Sigstore bundle stored:[/green] {bundle_path}")
        console.print(f"  capsule_id:      {effective_capsule_id}")
        if identity:
            console.print(f"  identity:        {identity}")
        if rekor_log_index is not None:
            console.print(f"  rekor_log_index: {rekor_log_index}")
        console.print(
            f"  Run [bold]nova verify --backend sigstore "
            f"--capsule-id {effective_capsule_id}[/bold] to verify."
        )

    else:
        err_console.print(
            f"[red]Error:[/red] Unknown backend {backend!r}. "
            "Choose 'local' or 'sigstore'."
        )
        raise typer.Exit(code=1)


@seal_app.command("propose")
def seal_propose_cmd(
    capsule_id: str = typer.Argument(..., help="Capsule ID to propose for promotion"),
    justification: str = typer.Option(..., "--justification", "-j", help="Promotion justification"),
    key: Path = typer.Option(..., "--key", help="Path to ECDSA P-256 private key PEM"),
    cert: Path = typer.Option(..., "--cert", help="Path to X.509 certificate PEM"),
    target_env: str = typer.Option("staging", "--target-env", help="Target environment"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", help="SQLite DB path"),
    data_dir: Path = typer.Option(_DEFAULT_DATA_DIR, "--data-dir", help="Data directory"),
) -> None:
    """Propose a NovaSeal signature for a capsule (maker step).

    Creates a signed proposal envelope stored in the bundle store that must be
    countersigned by a separate key-holder using `nova seal approve`.

    Scope: single capsule.

    \b
    Examples:
      nova seal propose <capsule-id> --justification "Ready for production" \\
        --key ~/.novafabric/keys/alice.key --cert ~/.novafabric/keys/alice.crt

      nova seal propose <capsule-id> --justification "Staging sign-off" \\
        --target-env staging \\
        --key ~/.novafabric/keys/alice.key --cert ~/.novafabric/keys/alice.crt
    """
    # Validate justification length before any signing
    if len(justification) < 20:
        err_console.print(
            "[red]Error:[/red] justification must be at least 20 characters "
            f"(got {len(justification)})"
        )
        raise typer.Exit(code=1)

    # Load policy
    try:
        policy_store = PolicyStore(db_path)
        policy_store.get_latest()  # ensure a policy exists
        policy_version = str(policy_store.get_latest_version())
    except PolicyNotFoundError:
        err_console.print(
            "[red]Error:[/red] No promotion policy found. "
            "Run [bold]nova policy sign[/bold] first."
        )
        raise typer.Exit(code=1)

    # Extract proposer subject from cert
    from cryptography.x509 import NameOID, load_pem_x509_certificate
    try:
        cert_obj = load_pem_x509_certificate(cert.read_bytes())
        attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        proposer_subject = str(attrs[0].value) if attrs else cert_obj.subject.rfc4514_string()
    except Exception as exc:  # pragma: no cover
        err_console.print(f"[red]Error:[/red] Failed to read certificate: {exc}")
        raise typer.Exit(code=1)

    # Compute capsule digest stand-in
    capsule_digest_hex = hashlib.sha256(capsule_id.encode()).hexdigest()

    # Build predicate
    predicate = build_proposal_predicate(
        capsule_id=capsule_id,
        capsule_digest_hex=capsule_digest_hex,
        target_env=target_env,
        justification=justification,
        proposer_subject=proposer_subject,
        policy_version=policy_version,
    )

    # Validate against schema
    from novafabric.promote.exceptions import PredicateValidationError
    try:
        validate_predicate("promote_proposal_v1.json", predicate)
    except PredicateValidationError as exc:  # pragma: no cover
        err_console.print(f"[red]Schema validation error:[/red] {exc}")
        raise typer.Exit(code=1)

    # Sign
    payload = json.dumps(predicate).encode()
    try:
        envelope = sign_promote_envelope(payload, PROPOSAL_PAYLOAD_TYPE, key, cert)
    except Exception as exc:  # pragma: no cover
        err_console.print(f"[red]Signing error:[/red] {exc}")
        raise typer.Exit(code=1)

    # Store
    bundle_store = PromoteBundleStore(data_dir)
    proposal_uuid = bundle_store.put_proposal(capsule_id, envelope)

    console.print(f"[green]Proposal created:[/green] {proposal_uuid}")
    console.print(f"  capsule: {capsule_id}")
    console.print(f"  proposer: {proposer_subject}")
    console.print(f"  policy version: {policy_version}")
    console.print(
        f"  Run [bold]nova seal approve {proposal_uuid} --capsule-id {capsule_id}[/bold] "
        "as a different identity to complete."
    )


@seal_app.command("approve")
def seal_approve_cmd(
    proposal_uuid: str = typer.Argument(..., help="Proposal UUID to approve"),
    capsule_id: str = typer.Option(..., "--capsule-id", help="Capsule ID"),
    key: Path = typer.Option(..., "--key", help="Path to ECDSA P-256 private key PEM"),
    cert: Path = typer.Option(..., "--cert", help="Path to X.509 certificate PEM"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", help="SQLite DB path"),
    data_dir: Path = typer.Option(_DEFAULT_DATA_DIR, "--data-dir", help="Data directory"),
) -> None:
    """Approve a pending NovaSeal proposal (checker step).

    Fetches the proposal envelope, displays its contents, prompts for
    confirmation, then countersigns and stores the approval DSSE envelope.
    The seal is considered complete after this step.

    Scope: single capsule.

    \b
    Examples:
      nova seal approve <proposal-uuid> --capsule-id <capsule-id> \\
        --key ~/.novafabric/keys/bob.key --cert ~/.novafabric/keys/bob.crt
    """
    bundle_store = PromoteBundleStore(data_dir)

    # Fetch proposal
    try:
        proposal_envelope = bundle_store.get_proposal(capsule_id, proposal_uuid)
    except BundleNotFoundError:
        err_console.print(
            f"[red]Error:[/red] Proposal not found: capsule={capsule_id!r} uuid={proposal_uuid!r}"
        )
        raise typer.Exit(code=1)

    # Display proposal contents
    try:
        proposal_payload, proposer_subject = verify_promote_envelope(
            proposal_envelope, PROPOSAL_PAYLOAD_TYPE
        )
        proposal_pred = json.loads(proposal_payload)
    except Exception as exc:  # pragma: no cover
        err_console.print(f"[red]Error:[/red] Cannot read proposal envelope: {exc}")
        raise typer.Exit(code=1)

    console.print("\n[bold]Proposal details:[/bold]")
    console.print(f"  capsule_id:   {proposal_pred.get('capsule_id')}")
    console.print(f"  justification: {proposal_pred.get('justification')}")
    console.print(f"  proposer:      {proposer_subject}")
    console.print(f"  timestamp:     {proposal_pred.get('timestamp')}")
    console.print(f"  target_env:    {proposal_pred.get('target_environment')}")
    console.print()

    # Confirm
    if not typer.confirm("Approve this proposal?"):
        console.print("Promotion approval cancelled.")
        raise typer.Exit(code=0)

    # Extract approver subject from cert
    from cryptography.x509 import NameOID, load_pem_x509_certificate
    try:
        cert_obj = load_pem_x509_certificate(cert.read_bytes())
        attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        approver_subject = str(attrs[0].value) if attrs else cert_obj.subject.rfc4514_string()
    except Exception as exc:  # pragma: no cover
        err_console.print(f"[red]Error:[/red] Failed to read certificate: {exc}")
        raise typer.Exit(code=1)

    # Build and validate approval predicate
    approval_pred = build_approval_predicate(proposal_envelope, "approved", approver_subject)

    from novafabric.promote.exceptions import PredicateValidationError
    try:
        validate_predicate("promote_approval_v1.json", approval_pred)
    except PredicateValidationError as exc:  # pragma: no cover
        err_console.print(f"[red]Schema validation error:[/red] {exc}")
        raise typer.Exit(code=1)

    # Sign
    approval_payload = json.dumps(approval_pred).encode()
    try:
        approval_envelope = sign_promote_envelope(
            approval_payload, APPROVAL_PAYLOAD_TYPE, key, cert
        )
    except Exception as exc:  # pragma: no cover
        err_console.print(f"[red]Signing error:[/red] {exc}")
        raise typer.Exit(code=1)

    # Store
    approval_uuid = bundle_store.put_approval(capsule_id, approval_envelope, proposal_uuid)

    # Record human approval event in the active capsule, if one is running (fail-open).
    try:
        from novafabric.capture.event_recorder import get_current_recorder
        _recorder = get_current_recorder()
        if _recorder is not None:
            _recorder.record_human_approval(
                approver_id=approver_subject,
                action="approved",
                target_run_id=capsule_id,
                rationale=proposal_pred.get("justification"),
                policy_version=proposal_pred.get("policy_version"),
                seal_bundle_path=str(data_dir / capsule_id / f"approval_{approval_uuid}.json"),
            )
    except Exception:
        pass  # fail-open: never block the approval workflow

    # Optionally push to Rekor
    from novafabric.promote.rekor_client import maybe_publish
    log_uuid = maybe_publish(approval_envelope)

    console.print(f"[green]Approval recorded:[/green] {approval_uuid}")
    console.print(f"  capsule: {capsule_id}")
    console.print(f"  approver: {approver_subject}")
    if log_uuid:
        console.print(f"  rekor log:  {log_uuid}")
    console.print(
        f"  Run [bold]nova seal verify {capsule_id}[/bold] to check SoD compliance."
    )


def _parse_duration_hours(duration: str) -> int:
    """Parse e.g. '24h' → 24, '7d' → 168. Raises ValueError on bad format."""
    m = re.fullmatch(r"(\d+)(h|d)", duration.strip().lower())
    if not m:
        raise ValueError(f"Invalid duration {duration!r}; expected e.g. '24h' or '7d'")
    n, unit = int(m.group(1)), m.group(2)
    return n if unit == "h" else n * 24


@seal_app.command("verify")
def seal_verify_cmd(
    capsule_id: str = typer.Argument(..., help="Capsule ID to verify"),
    offline: bool = typer.Option(False, "--offline", help="Skip network checks"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", help="SQLite DB path"),
    data_dir: Path = typer.Option(_DEFAULT_DATA_DIR, "--data-dir", help="Data directory"),
) -> None:
    """Verify the full NovaSeal chain including proposer and approver signatures.

    Runs the five-check SoD (Separation of Duties) verification.

    Scope: single capsule.

    \b
    Examples:
      nova seal verify <capsule-id>
      nova seal verify <capsule-id> --offline

    Exit codes:
      0  — all checks passed
      3  — proposer not in policy
      4  — approver not in policy
      5  — proposal_digest mismatch
      6  — self-approval prohibited
      7  — timestamp ordering violation
      8  — missing approval bundle
      9  — missing proposal bundle
    """
    policy_store = PolicyStore(db_path)
    bundle_store = PromoteBundleStore(data_dir)

    result = verify_sod(capsule_id, policy_store, bundle_store, offline=offline)

    if result.passed:
        console.print("[green]SoD verification passed[/green]")
        if result.bypass_used:
            console.print("[yellow]  (bypass active — SoD checks skipped)[/yellow]")
        raise typer.Exit(code=0)
    else:
        err_console.print(f"[red]SoD verification failed:[/red] {result.message}")
        sys.exit(result.exit_code)


@seal_app.command("bypass")
def seal_bypass_cmd(
    capsule_id: str = typer.Argument(..., help="Capsule ID to bypass SoD for"),
    reason: str = typer.Option(..., "--reason", "-r", help="Bypass reason (≥50 chars)"),
    duration: str = typer.Option(
        "24h", "--duration", help="Validity window: e.g. 24h, 7d (max 168h)"
    ),
    key: Path = typer.Option(..., "--key", help="ECDSA P-256 private key PEM"),
    cert: Path = typer.Option(..., "--cert", help="X.509 certificate PEM"),
    target_env: str = typer.Option("production", "--target-env"),
    notify: list[str] = typer.Option([], "--notify", help="Email to notify (repeatable)"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db"),
    data_dir: Path = typer.Option(_DEFAULT_DATA_DIR, "--data-dir"),
) -> None:
    """Create a time-limited bypass of the maker-checker SoD requirement.

    The bypass is signed with the operator's key and stored locally.
    Use sparingly — every bypass creates a permanent audit trail.
    """
    from datetime import UTC, datetime, timedelta

    from cryptography.x509 import NameOID, load_pem_x509_certificate

    from novafabric.promote.exceptions import PredicateValidationError
    from novafabric.promote.predicates import (
        BYPASS_PAYLOAD_TYPE,
        build_bypass_predicate,
        sign_promote_envelope,
        validate_predicate,
    )
    from novafabric.promote.rekor_client import maybe_publish

    # Validate reason length
    if len(reason) < 50:
        err_console.print(
            f"[red]Error:[/red] --reason must be at least 50 characters (got {len(reason)})"
        )
        raise typer.Exit(code=1)

    # Parse and validate duration
    try:
        hours = _parse_duration_hours(duration)
    except ValueError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    if hours > 168:
        err_console.print("[red]Error:[/red] --duration cannot exceed 168h (7 days)")
        raise typer.Exit(code=1)

    # Extract signer subject from cert
    try:
        cert_obj = load_pem_x509_certificate(cert.read_bytes())
        attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        authorized_by = str(attrs[0].value) if attrs else cert_obj.subject.rfc4514_string()
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] Failed to read certificate: {exc}")
        raise typer.Exit(code=1)

    now = datetime.now(UTC)
    valid_until = now + timedelta(hours=hours)
    valid_until_str = valid_until.isoformat()

    capsule_digest_hex = hashlib.sha256(capsule_id.encode()).hexdigest()

    predicate = build_bypass_predicate(
        capsule_id=capsule_id,
        capsule_digest_hex=capsule_digest_hex,
        target_environment=target_env,
        bypass_reason=reason,
        bypass_authorized_by=authorized_by,
        valid_until=valid_until_str,
        notification_sent_to=list(notify),
        notification_status="sent" if notify else "not_configured",
    )

    try:
        validate_predicate("promote_bypass_v1.json", predicate)
    except PredicateValidationError as exc:
        err_console.print(f"[red]Schema validation error:[/red] {exc}")
        raise typer.Exit(code=1)

    payload = json.dumps(predicate).encode()
    try:
        envelope = sign_promote_envelope(payload, BYPASS_PAYLOAD_TYPE, key, cert)
    except Exception as exc:
        err_console.print(f"[red]Signing error:[/red] {exc}")
        raise typer.Exit(code=1)

    bundle_store = PromoteBundleStore(data_dir)
    bypass_uuid = bundle_store.put_bypass(capsule_id, envelope, valid_until_str)

    # Record bypass event in the active capsule, if one is running (fail-open).
    try:
        from novafabric.capture.event_recorder import get_current_recorder
        _bypass_recorder = get_current_recorder()
        if _bypass_recorder is not None:
            _bypass_recorder.record_human_approval(
                approver_id=authorized_by,
                action="bypassed",
                target_run_id=capsule_id,
                rationale=reason,
                policy_version=None,
                seal_bundle_path=str(data_dir / capsule_id / f"bypass_{bypass_uuid}.json"),
            )
    except Exception:
        pass  # fail-open: never block the bypass workflow

    # Optionally push to Rekor
    log_uuid = maybe_publish(envelope)

    console.print(f"[green]Bypass created:[/green] {bypass_uuid}")
    console.print(f"  capsule:     {capsule_id}")
    console.print(f"  authorized:  {authorized_by}")
    console.print(f"  valid until: {valid_until_str}")
    if log_uuid:
        console.print(f"  rekor log:   {log_uuid}")
    console.print(
        "\n[yellow]Warning:[/yellow] Bypasses are permanently audited. "
        f"Run [bold]nova seal verify {capsule_id}[/bold] to confirm bypass is active."
    )


# ---------------------------------------------------------------------------
# log sub-app
# ---------------------------------------------------------------------------

log_app = typer.Typer(help="Merkle log operations", no_args_is_help=True)
seal_app.add_typer(log_app, name="log")


@log_app.command("verify")
def log_verify_cmd(
    db: str | None = typer.Option(
        None,
        "--db",
        help=(
            "Merkle log path (SQLite) or postgresql:// DSN (Postgres, requires [seal-postgres]). "
            "Defaults to NOVAFABRIC_SEAL_DB_PATH env var, then merkle_db in novaseal.yaml, "
            "then ~/.novafabric/novaseal-merkle.db."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show all leaf details"),
    full: bool = typer.Option(
        False, "--full", help="Full re-hash audit (slow at large N; default: sampled)"
    ),
    consistency: int | None = typer.Option(
        None,
        "--consistency",
        help=(
            "Also emit + verify an append-only consistency proof from this older "
            "tree size to the current head (ADR-0041 v0.2)"
        ),
    ),
) -> None:
    """Verify the internal consistency of the Merkle log.

    Supports both SQLite (default) and Postgres backends. With --consistency
    N, additionally proves the current head is an append-only extension of
    the log at size N.

    Exit codes:
      0 — log is consistent (or empty)
      1 — consistency errors found
      2 — consistency proof failed (--consistency)
    """
    from novafabric.trust.novaseal.config import resolve_merkle_db_uri
    from novafabric.trust.novaseal.merkle import (
        PostgresMerkleLog,
        open_merkle_log,
        verify_consistency_proof,
    )

    resolved_db = db if db is not None else resolve_merkle_db_uri()
    merkle_log = open_merkle_log(resolved_db)
    backend = "postgres" if isinstance(merkle_log, PostgresMerkleLog) else "sqlite"
    if isinstance(merkle_log, PostgresMerkleLog):
        result = merkle_log.verify_consistency(full=full)
    else:
        result = merkle_log.verify_consistency()

    console.print(f"Merkle log: {resolved_db}  [backend: {backend}]")
    console.print(f"  Leaves:      {result.leaf_count}")
    if result.root_hash:
        console.print(f"  Root hash:   {result.root_hash[:32]}…")
    else:
        console.print("  Root hash:   (empty)")
    status = (
        "[green]OK[/green]"
        if result.consistent
        else f"[red]FAILED ({len(result.errors)} error(s))[/red]"
    )
    console.print(f"  Consistency: {status}")

    if result.errors:
        console.print("\n[red]Errors:[/red]")
        for e in result.errors:
            console.print(f"  {e}")
        raise typer.Exit(code=1)

    if consistency is not None:
        from novafabric.trust.novaseal.merkle import MerkleError

        try:
            proof = merkle_log.consistency_proof(consistency)
        except MerkleError as exc:
            console.print(f"\n[red]Consistency proof error:[/red] {exc}")
            raise typer.Exit(code=2) from exc
        old_root = merkle_log.root_at_size(consistency) or str(proof["old_root"])
        new_root = result.root_hash
        ok = verify_consistency_proof(proof, old_root, new_root)
        console.print(
            f"\n  Append-only proof {consistency} → {result.leaf_count}: "
            + ("[green]OK[/green]" if ok else "[red]FAILED[/red]")
        )
        console.print(
            f"    scheme:    {proof['scheme']}"
        )
        console.print(f"    old root:  {old_root[:32]}…")
        console.print(
            f"    proof size: {len(proof['old_parts'])} + "
            f"{len(proof['tail_parts'])} subtree hashes"
        )
        if not ok:
            raise typer.Exit(code=2)

    raise typer.Exit(code=0)


ratchet_app = typer.Typer(
    help=(
        "Forward-secure per-node signing key ratchet (experimental, ADR-0089). "
        "Opt-in; the static-key signing path remains the default."
    ),
    no_args_is_help=True,
)
seal_app.add_typer(ratchet_app, name="ratchet")


@ratchet_app.command("init")
def ratchet_init_cmd(
    node_id: str = typer.Option(..., "--node-id", help="Stable node identifier"),
) -> None:
    """Provision epoch-0 ratchet state for this node.

    Scope: local ratchet state under NOVAFABRIC_HOME/seal/ratchet.

    \b
    Examples:
      nova seal ratchet init --node-id node-a
    """
    from novafabric.trust.novaseal.ratchet import RatchetError, init_ratchet

    try:
        state = init_ratchet(node_id)
    except RatchetError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]✓[/green] Ratchet initialised for node {state.node_id} "
        f"(epoch {state.epoch})"
    )


@ratchet_app.command("rotate")
def ratchet_rotate_cmd(
    node_id: str = typer.Option(..., "--node-id", help="Stable node identifier"),
) -> None:
    """Advance to the next signing epoch; erase the previous chain key.

    Best-effort secure erase (overwrite + delete) — see ADR-0089 for the
    honest limits on journaling filesystems and SSDs.

    Scope: local ratchet state under NOVAFABRIC_HOME/seal/ratchet.

    \b
    Examples:
      nova seal ratchet rotate --node-id node-a
    """
    from novafabric.trust.novaseal.ratchet import RatchetError, rotate

    try:
        state = rotate(node_id)
    except RatchetError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]✓[/green] Rotated node {state.node_id} to epoch {state.epoch}; "
        "previous chain key erased (best-effort)"
    )


@ratchet_app.command("status")
def ratchet_status_cmd(
    node_id: str = typer.Option(..., "--node-id", help="Stable node identifier"),
) -> None:
    """Show the node's current epoch and registry history.

    Scope: local ratchet state under NOVAFABRIC_HOME/seal/ratchet.

    \b
    Examples:
      nova seal ratchet status --node-id node-a
    """
    from novafabric.trust.novaseal.ratchet import (
        EpochRegistry,
        RatchetError,
        load_state,
    )

    try:
        state = load_state(node_id)
    except RatchetError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"node:    {state.node_id}")
    console.print(f"epoch:   {state.epoch}")
    console.print(f"rotated: {state.rotated_at}")
    records = EpochRegistry().records(node_id)
    console.print(f"registry epochs: {[r.epoch for r in records]}")
