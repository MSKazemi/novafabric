from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional  # noqa: UP007

import typer
import yaml
from rich.console import Console

from novafabric.evidence.bundle import (
    CapsuleValidationError,
    EvidenceBundleBuilder,
    UnsafeSkipsError,
)
from novafabric.evidence.signing import LocalSigner
from novafabric.trust._rfc3161 import DEFAULT_TSA_URL, TimestampError, add_rfc3161_timestamp

console = Console()
logger = logging.getLogger(__name__)

# Environment variable name for TSA URL
_ENV_TSA_URL = "NOVAFABRIC_TSA_URL"
# Config file location
_CONFIG_FILE = Path.home() / ".novafabric" / "config.yaml"

TIMESTAMP_VERIFIER_SECTION = """\
## Timestamp (RFC 3161)

This bundle includes a trusted timestamp from a Timestamp Authority (TSA).
The raw DER-encoded TimeStampResponse is stored as `manifest.dsse.tsr`.

To verify the timestamp independently (requires OpenSSL 1.1+):

```bash
# Extract the DSSE envelope and TSR from the bundle
unzip evidence.zip attestations/run.intoto.json manifest.dsse.tsr -d bundle/

# Verify the TSR covers the DSSE envelope bytes
openssl ts -verify -in bundle/manifest.dsse.tsr -data bundle/attestations/run.intoto.json \\
  -CAfile tsa-ca-bundle.crt
```

FreeTSA root certificate: https://freetsa.org/files/tsa.crt
For production EU-regulated deployments, configure a QTSP TSA (eIDAS Art. 41(2)).
For US-regulated deployments, any commercially operated RFC 3161 TSA satisfies
SEC 17a-4(f) "reliable timestamp" requirements.
"""


def _resolve_tsa_url(cli_url: Optional[str]) -> str:  # noqa: UP007
    """Resolve TSA URL using precedence: CLI arg > config file > env var > default."""
    if cli_url:
        return cli_url
    # Config file
    if _CONFIG_FILE.exists():
        try:
            config = yaml.safe_load(_CONFIG_FILE.read_text()) or {}
            # Support nested key: evidence.tsa_url
            tsa_url = config.get("evidence", {}).get("tsa_url")
            if tsa_url:
                return str(tsa_url)
        except Exception:
            pass
    # Environment variable
    env_url = os.environ.get(_ENV_TSA_URL)
    if env_url:
        return env_url
    return DEFAULT_TSA_URL


def _sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _rewrite_zip_entry(bundle_path: Path, entry_name: str, new_data: bytes) -> None:
    """Replace a single file entry in a ZIP archive (atomic via temp file)."""
    tmp_path = bundle_path.with_suffix(".tmp.zip")
    try:
        with (
            zipfile.ZipFile(bundle_path, "r") as zf_in,
            zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_out,
        ):
            for item in zf_in.infolist():
                if item.filename == entry_name:
                    zf_out.writestr(item, new_data)
                else:
                    zf_out.writestr(item, zf_in.read(item.filename))
        shutil.move(str(tmp_path), str(bundle_path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _add_timestamp_to_bundle(
    bundle_path: Path,
    tsa_url: str,
    timestamp_optional: bool,
) -> None:
    """Post-process an Evidence Bundle ZIP to add an RFC 3161 timestamp.

    Steps (per ADR-0030):
    1. Read the primary DSSE envelope (attestations/run.intoto.json) bytes.
    2. Call the TSA to get a TSR DER.
    3. Write the TSR as `manifest.dsse.tsr` into the ZIP.
    4. Append OpenSSL verification instructions to README.md inside the ZIP.
    5. Rewrite manifest.json: add manifest_dsse_tsr_sha256, timestamp_status,
       timestamp_tsa_url, and recompute manifest_hash.
    """
    with zipfile.ZipFile(bundle_path, "r") as zf:
        names = zf.namelist()
        dsse_path = "attestations/run.intoto.json"
        if dsse_path not in names:
            console.print(
                "[red]✗[/red] cannot find attestations/run.intoto.json in bundle "
                "— skipping timestamp"
            )
            return
        dsse_bytes = zf.read(dsse_path)
        manifest_json = zf.read("manifest.json")
        readme_bytes = zf.read("README.md") if "README.md" in names else b""

    # Request TSR from TSA
    try:
        tsr_der = add_rfc3161_timestamp(dsse_bytes, tsa_url)
    except TimestampError as exc:
        if timestamp_optional:
            logger.warning("timestamp skipped (--timestamp-optional): %s", exc)
            console.print(
                f"[yellow]⚠[/yellow] RFC 3161 timestamp skipped "
                f"(--timestamp-optional): {exc}"
            )
            # Record failure in manifest.json and recompute manifest_hash
            manifest = json.loads(manifest_json)
            manifest["timestamp_status"] = "failed"
            manifest["timestamp_failure_reason"] = str(exc)
            work = {k: v for k, v in manifest.items() if k != "manifest_hash"}
            canonical = json.dumps(work, sort_keys=True, separators=(",", ":"))
            manifest["manifest_hash"] = _sha256_hex(canonical.encode())
            _rewrite_zip_entry(
                bundle_path,
                "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode(),
            )
            return
        raise

    tsr_sha256 = _sha256_hex(tsr_der)

    # Update manifest.json
    manifest = json.loads(manifest_json)
    manifest["manifest_dsse_tsr_sha256"] = tsr_sha256
    manifest["timestamp_status"] = "ok"
    manifest["timestamp_tsa_url"] = tsa_url
    work = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    canonical = json.dumps(work, sort_keys=True, separators=(",", ":"))
    manifest["manifest_hash"] = _sha256_hex(canonical.encode())
    new_manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()

    # Build updated README
    new_readme_bytes = readme_bytes
    if new_readme_bytes and not new_readme_bytes.endswith(b"\n"):
        new_readme_bytes += b"\n"
    new_readme_bytes += b"\n" + TIMESTAMP_VERIFIER_SECTION.encode()

    # Rewrite bundle atomically
    tmp_path = bundle_path.with_suffix(".tmp.zip")
    try:
        with (
            zipfile.ZipFile(bundle_path, "r") as zf_in,
            zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_out,
        ):
            for item in zf_in.infolist():
                if item.filename == "manifest.json":
                    zf_out.writestr(item, new_manifest_bytes)
                elif item.filename == "README.md":
                    zf_out.writestr(item, new_readme_bytes)
                else:
                    zf_out.writestr(item, zf_in.read(item.filename))
            # Add the TSR as a new entry
            zf_out.writestr("manifest.dsse.tsr", tsr_der)
        shutil.move(str(tmp_path), str(bundle_path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def export_evidence_cmd(
    capsule_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to a Run Capsule directory.",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="Path to the evidence bundle ZIP to write.",
    ),
    key: Path | None = typer.Option(
        None,
        "--key",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to a PEM-encoded ed25519 private key for local-key signing.",
    ),
    sigstore: bool = typer.Option(
        False,
        "--sigstore",
        help=(
            "Publish the bundle's DSSE envelope to a Rekor transparency log. "
            "Requires NOVA_REKOR_URL; skipped with a warning if unset. Note: "
            "this is transparency-log publication, not Sigstore keyless "
            "signing — the bundle is still signed by the configured key."
        ),
    ),
    allow_unsafe_skips: bool = typer.Option(
        False,
        "--allow-unsafe-skips",
        help="Permit export when redaction-proof.json contains unsafe_skips entries.",
    ),
    timestamp: bool = typer.Option(
        False,
        "--timestamp/--no-timestamp",
        help=(
            "Request an RFC 3161 trusted timestamp from a TSA after assembling "
            "the DSSE envelope (ADR-0030). The TSR is stored as manifest.dsse.tsr "
            "inside the bundle."
        ),
    ),
    timestamp_url: Optional[str] = typer.Option(  # noqa: UP007
        None,
        "--timestamp-url",
        help=(
            "TSA URL to use for RFC 3161 timestamping. Overrides "
            f"~/.novafabric/config.yaml evidence.tsa_url, {_ENV_TSA_URL} env var, "
            f"and the default ({DEFAULT_TSA_URL})."
        ),
    ),
    timestamp_optional: bool = typer.Option(
        False,
        "--timestamp-optional",
        help=(
            "If the TSA is unreachable or rejects the request, log a warning and "
            "continue rather than failing. The failure reason is recorded in "
            "manifest.json under 'timestamp_failure_reason'."
        ),
    ),
    with_custody: bool = typer.Option(
        False,
        "--with-custody",
        help=(
            "Embed an FRE-902(14) court-admissibility block (chain-of-custody + "
            "self-authentication) in the manifest, built from the audit log + "
            "capsule hash (ADR-0095). Unwitnessed fields are marked operator_declared."
        ),
    ),
    custodian: Optional[str] = typer.Option(  # noqa: UP007
        None,
        "--custodian",
        help="Custodian identity for --with-custody (e.g. email). Omit → operator_declared.",
    ),
    custodian_provenance: str = typer.Option(
        "operator_declared",
        "--custodian-provenance",
        help="novaseal-identity | oidc | operator_declared (for --with-custody).",
    ),
    dsse: bool = typer.Option(
        False,
        "--dsse",
        help=(
            "Also emit a standard DSSE outer envelope wrapping the bundle manifest "
            "(<bundle>.dsse.json, payloadType application/vnd.novafabric.bundle+json, "
            "NF-029/ADR-0096). Verifiable with `nova verify-envelope` or stock cosign. "
            "Opt-in: without this flag the bundle output is byte-for-byte unchanged."
        ),
    ),
) -> None:
    """Build a signed Evidence Bundle ZIP from a capsule.

    Packages capsule.yaml, event_log.jsonl, and lineage data into a
    tamper-evident ZIP signed with a local Ed25519 key.

    Scope: single capsule.

    \b
    Examples:
      nova export-evidence path/to/my-capsule/
      nova export-evidence path/to/my-capsule/ --output bundle.zip
    """
    if key is None:
        console.print(
            "[red]✗[/red] --key is required in v0.4 (local-key signing). "
            "Generate one with `python -m novafabric.evidence.signing` "
            "or your preferred ed25519 tool."
        )
        raise typer.Exit(code=1)

    try:
        signer = LocalSigner(key)
    except Exception as exc:
        console.print(f"[red]✗[/red] failed to load signing key: {exc}")
        raise typer.Exit(code=1) from exc

    custody_arg = None
    if with_custody and custodian:
        from novafabric.evidence.admissibility import Custodian

        custody_arg = Custodian(
            identity=custodian,
            provenance=custodian_provenance,  # type: ignore[arg-type]
        )
    builder = EvidenceBundleBuilder(
        capsule_dir=capsule_dir,
        signer=signer,
        output_path=output,
        allow_unsafe_skips=allow_unsafe_skips,
        with_custody=with_custody,
        custodian=custody_arg,
    )
    try:
        out_path = builder.build()
    except UnsafeSkipsError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except CapsuleValidationError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if timestamp:
        tsa_url = _resolve_tsa_url(timestamp_url)
        console.print(f"[dim]Requesting RFC 3161 timestamp from {tsa_url} …[/dim]")
        try:
            _add_timestamp_to_bundle(out_path, tsa_url, timestamp_optional)
        except TimestampError as exc:
            console.print(f"[red]✗[/red] RFC 3161 timestamp failed: {exc}")
            raise typer.Exit(code=1) from exc

    if sigstore:
        try:
            with zipfile.ZipFile(out_path) as _zf:
                _envelope_bytes = _zf.read("attestations/run.intoto.json")
        except (KeyError, zipfile.BadZipFile) as _exc:
            console.print(
                f"[yellow]⚠[/yellow] could not extract DSSE envelope for Rekor: {_exc}"
            )
        else:
            from novafabric.promote.rekor_client import maybe_publish  # noqa: PLC0415

            _log_uuid = maybe_publish(_envelope_bytes)
            if _log_uuid:
                console.print(
                    f"[green]✓[/green] Rekor transparency log entry: {_log_uuid}"
                )
            else:
                console.print(
                    "[yellow]⚠[/yellow] Rekor publish skipped "
                    "(set NOVA_REKOR_URL to enable)"
                )

    if dsse:
        from novafabric.envelopes.dsse import wrap_bundle  # noqa: PLC0415

        try:
            with zipfile.ZipFile(out_path) as _zf:
                manifest_bytes = _zf.read("manifest.json")
        except (KeyError, zipfile.BadZipFile) as exc:
            console.print(f"[red]✗[/red] could not read manifest for DSSE wrap: {exc}")
            raise typer.Exit(code=1) from exc
        envelope = wrap_bundle(manifest_bytes, signer)
        dsse_path = Path(str(out_path) + ".dsse.json")
        dsse_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        console.print(f"[green]✓[/green] DSSE envelope written: {dsse_path}")

    console.print(
        f"[green]✓[/green] evidence bundle written: {out_path} "
        f"({out_path.stat().st_size} bytes)"
    )


# ---------------------------------------------------------------------------
# cap-002: EU AI Act Annex IV export
# ---------------------------------------------------------------------------


def export_annex_iv_cmd(
    capsule_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to a Run Capsule directory.",
    ),
    output_dir: Path = typer.Option(
        ...,
        "--output-dir",
        "-o",
        help="Directory to write annex-iv-<deployment_id>.jsonld (and optionally .pdf).",
    ),
    deployment_id: str = typer.Option(
        ...,
        "--deployment-id",
        help="Operator-assigned deployment identifier for the Annex IV document.",
    ),
    pdf: bool = typer.Option(
        False,
        "--pdf/--no-pdf",
        help=(
            "Also render a PDF using WeasyPrint (BSD-2-Clause). "
            "Requires: pip install 'novafabric[compliance]'"
        ),
    ),
) -> None:
    """Export EU AI Act Annex IV technical documentation from a capsule.

    Annex IV requires providers of high-risk AI systems to maintain
    technical documentation demonstrating compliance with Regulation
    EU 2024/1689.

    Scope: single capsule.

    \b
    Examples:
      nova export-annex-iv path/to/my-capsule/
      nova export-annex-iv path/to/my-capsule/ --output annex-iv.json
    """
    try:
        from novafabric.compliance.export.annex_iv import AnnexIVExporter
        from novafabric.compliance.export.renderer import DocumentRenderer
    except ImportError as exc:
        console.print(
            f"[red]✗[/red] compliance module not available: {exc}. "
            "Ensure novafabric is installed with the compliance extra."
        )
        raise typer.Exit(code=1) from exc

    try:
        exporter = AnnexIVExporter()
        document = exporter.build_annex_iv_document(
            deployment_id=deployment_id,
            capsule_dir=capsule_dir,
        )
    except Exception as exc:
        console.print(f"[red]✗[/red] Annex IV export failed: {exc}")
        raise typer.Exit(code=1) from exc

    renderer = DocumentRenderer()
    try:
        json_ld_path, pdf_path = renderer.render(document, output_dir, pdf=pdf)
    except ImportError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]✗[/red] Render failed: {exc}")
        raise typer.Exit(code=1) from exc

    complete = sum(
        1 for e in document.elements if e.completeness_flag == "complete"
    )
    console.print(
        f"[green]✓[/green] Annex IV document written: {json_ld_path} "
        f"({complete}/15 elements complete)"
    )
    if pdf_path:
        console.print(f"[green]✓[/green] PDF written: {pdf_path}")


# ---------------------------------------------------------------------------
# cap-005: NIS2 incident report export
# ---------------------------------------------------------------------------


def export_nis2_cmd(
    capsule_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to the incident Run Capsule directory.",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="Path to write the NIS2 incident report JSON file.",
    ),
    incident_id: str = typer.Option(
        ...,
        "--incident-id",
        help="Operator-assigned incident identifier.",
    ),
    phase: int = typer.Option(
        1,
        "--phase",
        min=1,
        max=3,
        help="NIS2 reporting phase: 1 (initial, ≤24h) | 2 (detailed, ≤72h) | 3 (final, ≤1 month).",
    ),
) -> None:
    """Export a NIS2 incident report from a capsule.

    Generates a structured incident report per NIS2 Directive (EU 2022/2555,
    Art. 23). Use when reporting a cybersecurity incident involving an AI
    system to the relevant national authority.

    Scope: single capsule.

    \b
    Examples:
      nova export-nis2 path/to/my-capsule/
      nova export-nis2 path/to/my-capsule/ --output incident.json
    """
    try:
        from novafabric.compliance.export.nis2 import NIS2Exporter
    except ImportError as exc:
        console.print(
            f"[red]✗[/red] compliance module not available: {exc}"
        )
        raise typer.Exit(code=1) from exc

    exporter = NIS2Exporter(cap006_available=False)
    try:
        report = exporter.build_nis2_report(
            incident_id=incident_id,
            capsule_dir=capsule_dir,
            phase=phase,
        )
    except Exception as exc:
        console.print(f"[red]✗[/red] NIS2 export failed: {exc}")
        raise typer.Exit(code=1) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    missing = sum(
        1 for e in report.completeness_summary if e.status == "missing"
    )
    console.print(
        f"[green]✓[/green] NIS2 Phase {phase} report written: {output} "
        f"({missing} fields missing/blocked)"
    )


# ---------------------------------------------------------------------------
# cap-007: GDPR Art.30 Records of Processing Activities export
# ---------------------------------------------------------------------------


def export_ropa_cmd(
    capsule_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to a Run Capsule directory.",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="Path to write the RoPA JSON-LD file.",
    ),
    controller_name: str = typer.Option(
        "",
        "--controller-name",
        help="GDPR Art.30(1)(a) — name of the data controller.",
    ),
    controller_contact: str = typer.Option(
        "",
        "--controller-contact",
        help="GDPR Art.30(1)(a) — contact details of the data controller.",
    ),
) -> None:
    """Export a GDPR Art.30 Records of Processing Activities entry from a capsule.

    Generates a RoPA entry documenting data processing activities in a run,
    as required by GDPR Article 30.

    Scope: single capsule.

    \b
    Examples:
      nova export-ropa path/to/my-capsule/
      nova export-ropa path/to/my-capsule/ --output ropa.json
    """
    from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

    cfg: dict[str, str] = {}
    if controller_name:
        cfg["controller_name"] = controller_name
    if controller_contact:
        cfg["controller_contact"] = controller_contact

    exporter = GDPRRoPAExporter(operator_config=cfg)
    try:
        entry = exporter.build_ropa_entry(capsule_dir)
    except Exception as exc:
        console.print(f"[red]✗[/red] RoPA export failed: {exc}")
        raise typer.Exit(code=1) from exc

    from novafabric.compliance.export.gdpr_ropa import RoPADocument
    doc = RoPADocument(
        controller_name=entry.controller_name,
        entries=[entry],
        total_entries=1,
        completeness_summary={entry.completeness: 1},
    )
    out_path = exporter.export_json(doc, output)
    console.print(
        f"[green]✓[/green] GDPR RoPA written: {out_path} "
        f"(completeness={entry.completeness}, missing={entry.missing_fields})"
    )


# ---------------------------------------------------------------------------
# cap-008: AI-SBOM / CycloneDX ML-BOM export
# ---------------------------------------------------------------------------


def export_aibom_cmd(
    capsule_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to a Run Capsule directory.",
    ),
    output: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--output",
        "-o",
        help="Path to write the CycloneDX 1.7 ML-BOM JSON file. Default: <capsule_dir>/aibom.json.",
    ),
) -> None:
    """Export a CycloneDX 1.7 AI Bill of Materials from a capsule.

    Generates an AI-SBOM describing all AI components (models, datasets,
    tools) used in the run. Required for EU Cyber Resilience Act (CRA)
    compliance (deadline September 2026).

    Scope: single capsule.

    \b
    Examples:
      nova export-aibom path/to/my-capsule/
      nova export-aibom path/to/my-capsule/ --output aibom.json
    """
    from novafabric.compliance.export.aibom import AIBOMExporter

    out_path = output if output is not None else capsule_dir / "aibom.json"

    exporter = AIBOMExporter()
    try:
        doc = exporter.build_aibom(capsule_dir)
    except Exception as exc:
        console.print(f"[red]✗[/red] AI-SBOM export failed: {exc}")
        raise typer.Exit(code=1) from exc

    written = exporter.export_json(doc, out_path)
    console.print(
        f"[green]✓[/green] AI-SBOM (CycloneDX 1.7) written: {written} "
        f"({len(doc.components)} components)"
    )


# ---------------------------------------------------------------------------
# cap-009: NIST AI RMF 1.0 quantitative risk report export
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# cap-010: HIPAA Safe Harbor proof export
# ---------------------------------------------------------------------------


def export_hipaa_proof_cmd(
    capsule_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to a Run Capsule directory.",
    ),
    output: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--output",
        "-o",
        help=(
            "Path to write the HIPAA Safe Harbor proof JSON file. "
            "Default: <capsule_dir>/hipaa-proof.json."
        ),
    ),
) -> None:
    """Export a HIPAA Safe Harbor de-identification proof for a capsule.

    Generates a technical evidence artifact documenting which of the 18 HIPAA
    Safe Harbor identifier categories (45 CFR §164.514(b)) were absent or
    redacted in available capsule evidence files.

    DISCLAIMER: This is a technical evidence artifact only.  It is NOT legal
    advice, NOT a compliance certification, NOT HHS-approved, and does NOT
    guarantee HIPAA Safe Harbor legal sufficiency.

    Scope: single capsule.

    \b
    Examples:
      nova export-hipaa-proof path/to/my-capsule/
      nova export-hipaa-proof path/to/my-capsule/ --output hipaa-proof.json
    """
    from novafabric.compliance.export.hipaa_safe_harbor import HIPAASafeHarborExporter

    out_path = output if output is not None else capsule_dir / "hipaa-proof.json"

    exporter = HIPAASafeHarborExporter()
    try:
        proof = exporter.build_proof(capsule_dir)
    except Exception as exc:
        console.print(f"[red]✗[/red] HIPAA Safe Harbor proof export failed: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        written = exporter.export_json(proof, out_path)
    except Exception as exc:
        console.print(f"[red]✗[/red] Failed to write proof to {out_path}: {exc}")
        raise typer.Exit(code=1) from exc

    redacted_count = sum(
        1 for a in proof.identifiers if a.status == "redacted"
    )
    console.print(
        f"[green]✓[/green] HIPAA Safe Harbor proof written: {written} "
        f"({written.stat().st_size} bytes, {redacted_count}/18 categories redacted)"
    )


def export_nist_rmf_cmd(
    capsule_dir: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to a Run Capsule directory.",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="Path to write the NIST AI RMF JSON report.",
    ),
) -> None:
    """Export a NIST AI RMF 1.0 risk report from a capsule.

    Generates a quantitative risk report aligned with the NIST AI Risk
    Management Framework 1.0, covering GOVERN, MAP, MEASURE, and MANAGE.

    Scope: single capsule.

    \b
    Examples:
      nova export-nist-rmf path/to/my-capsule/
      nova export-nist-rmf path/to/my-capsule/ --output nist-rmf.json
    """
    from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

    reporter = NISTAIRMFReporter()
    try:
        report = reporter.build_report(capsule_dir)
    except Exception as exc:
        console.print(f"[red]✗[/red] NIST AI RMF export failed: {exc}")
        raise typer.Exit(code=1) from exc

    out_path = reporter.export_json(report, output)
    score_str = (
        f"{report.overall_score:.2f}" if report.overall_score is not None else "n/a"
    )
    console.print(
        f"[green]✓[/green] NIST AI RMF report written: {out_path} "
        f"(overall={score_str}, risk_level={report.risk_level}, "
        f"metrics={len(report.metrics)})"
    )
