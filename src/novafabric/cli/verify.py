"""nova verify — verify a capsule's NovaSeal signature, timestamp, and Merkle log."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console

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
) -> None:
    """Verify a capsule's cryptographic seal, timestamp, and Merkle log inclusion.

    Checks three layers of integrity in the .seal/ directory (local backend):
      • ECDSA P-256 DSSE signature
      • RFC 3161 timestamp (structural hash)
      • Merkle log inclusion proof

    With ``--backend sigstore``, verifies the Sigstore bundle stored under
    ``<home>/sigstore/<capsule_id>.bundle.json``.

    Exits 0 if all checks pass, 1 otherwise.
    Requires NovaSeal config (novaseal.yaml or NOVAFABRIC_SEAL_CONFIG env var)
    for the local backend.

    Scope: single capsule.

    \b
    Examples:
      # Verify a capsule (local ECDSA backend)
      nova verify path/to/my-capsule/

      # Use an explicit seal config path
      nova verify --seal-config ~/configs/novaseal.yaml path/to/my-capsule/

      # Verify a redaction proof report seal independently
      nova verify --check-redaction path/to/report.seal.json path/to/my-capsule/

      # Verify a Sigstore bundle
      nova verify --backend sigstore --capsule-id <capsule-id> path/to/my-capsule/

      # Verify an Evidence Bundle ZIP (recomputes every artifact digest)
      nova verify path/to/evidence-bundle.zip
    """
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
        _verify_evidence_bundle(capsule_dir)
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
            f"[red]Error:[/red] Unknown backend {backend!r}. "
            "Choose 'local' or 'sigstore'."
        )
        raise typer.Exit(code=1)

    if not capsule_dir.exists():
        console.print(f"[red]Error:[/red] capsule directory not found: {capsule_dir}")
        raise typer.Exit(code=1)

    seal_dir = capsule_dir / ".seal"
    if not seal_dir.exists():
        console.print(
            f"[yellow]No .seal/ directory found in {capsule_dir}[/yellow]\n"
            "This capsule was not sealed with NovaSeal. "
            "Run `nova capture` with a novaseal.yaml config to produce sealed capsules."
        )
        raise typer.Exit(code=1)

    # Load signing profile for Merkle DB location
    import os
    if seal_config:
        os.environ["NOVAFABRIC_SEAL_CONFIG"] = seal_config

    try:
        from novafabric.trust.novaseal.config import SealConfigError, load_signing_profile
        profile = load_signing_profile()
    except SealConfigError as exc:
        console.print(f"[red]NovaSeal config error:[/red] {exc}")
        raise typer.Exit(code=1)

    if profile is None:
        console.print(
            "[yellow]NovaSeal is not configured.[/yellow]\n"
            "Create ~/.novafabric/novaseal.yaml or set NOVAFABRIC_SEAL_CONFIG "
            "to enable cryptographic verification."
        )
        raise typer.Exit(code=1)

    from novafabric.trust.novaseal import KeyConfig, NovaSeal

    config = KeyConfig(
        profile=profile.profile,
        key_path=str(profile.key_path),
        cert_path=str(profile.cert_path),
    )
    seal = NovaSeal(
        config=config,
        tsa_url=profile.tsa_url,
        db_path=str(profile.merkle_db),
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

    result = seal.verify(capsule_id=capsule_id, seal_dir=str(seal_dir))
    binding = _capsule_binding_report(capsule_dir, dsse_bytes)

    # Print results
    console.print(f"\n[bold]NovaSeal verification:[/bold] {capsule_dir.name}")
    _print_check("Signature (DSSE ECDSA P-256)", result.signature_ok)
    if result.signing_intent is not None:
        console.print(f"    Intent: {result.signing_intent.value}")
    if result.timestamp_ok and not result.timestamp_present:
        # Timestamping is best-effort, so a missing token still verifies — but
        # printing "OK" would claim evidence this capsule does not carry.
        console.print(
            "  [yellow]⊘[/yellow] Timestamp (RFC 3161): "
            "[yellow]NOT PRESENT[/yellow] (TSA skipped or unavailable)"
        )
    else:
        _print_check("Timestamp (RFC 3161)", result.timestamp_ok)
    _print_check("Merkle log inclusion", result.log_integrity_ok)
    binding_ok = _print_capsule_binding(binding)
    if not capsule_id_ok:
        _print_check("Log-entry capsule_id matches the signed payload", False)
        console.print(
            f"    [red]log-entry.json says[/red] {capsule_id}\n"
            f"    [red]signed payload hashes to[/red] {derived_capsule_id}"
        )

    if result.errors:
        console.print()
        for err in result.errors:
            console.print(f"  [red]✗[/red] {err}")

    console.print()
    console.print(str(result))

    if not result.valid or not binding_ok or not capsule_id_ok:
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
    return (
        isinstance(data, dict) and "export_id" in data and "batch_digest" in data
    )


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
    color = {"VALID": "green", "INCOMPLETE": "yellow", "INVALID": "red"}[
        report.status.value
    ]
    console.print(f"\n[{color}]{report.status.value}[/{color}]")
    if report.status is not VerifyStatus.VALID:
        raise typer.Exit(code=1)


def _print_check(label: str, ok: bool) -> None:
    icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
    status = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
    console.print(f"  {icon} {label}: {status}")


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

    home_path = Path(home) if home else Path.home() / ".novafabric"

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


def _verify_evidence_bundle(bundle_path: Path) -> None:
    """Recompute every artifact digest recorded in an Evidence Bundle manifest.

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
        unlisted = sorted(
            names - {a.get("path", "") for a in artifacts} - {"manifest.json"}
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

    ok = not mismatched and not missing and not unlisted
    console.print(
        f"\nartifacts_ok={not mismatched}, complete={not missing}, "
        f"no_extras={not unlisted}"
    )
    if not ok:
        console.print("[red]Evidence Bundle verification FAILED[/red]")
        raise typer.Exit(code=1)
    console.print("[green]Evidence Bundle verification PASSED[/green]")


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
