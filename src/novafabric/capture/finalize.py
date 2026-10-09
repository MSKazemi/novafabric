"""The one capsule finalization path, shared by every capsule writer.

Everything from "the workload's files are written" to "sealed" lives here:

1. :func:`write_redacted_manifest` -- the manifest is redacted as a data structure
   (built-in rules, then ADR-0135 maskers, then the built-in rules again) before
   ``capsule.yaml`` is first written (ADR-0009).
2. :func:`finalize_capsule` -- ``lineage.jsonl`` is written and indexed
   (best-effort, ADR-0251 §6), scanned late, the ADR-0009 residual pass rescans
   the whole capsule, ``redaction-proof.json`` is written once,
   ``evidence_digests`` binds every file (ADR-0251), the final manifest is gated
   (fail closed: a hit leaves the capsule unsealed), and the capsule is sealed
   when a signing profile exists (opt-in, ADR-0301).

``nova capture`` (:class:`~novafabric.capture.orchestrator.CaptureOrchestrator`)
calls the two steps around its own C2PA marker and PII gate, with the strict
semantics it always had. Framework adapters and the ``@agent`` decorator call
:func:`finalize_in_process_capsule`, which runs the main scan and both steps and
**never raises**: the wrapped call's result or exception belongs to the user, so a
finalization failure degrades to an unsealed capsule whose
``metadata.finalization_error`` says why.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from novafabric.capture.secrets import (
    ResidualSecretError,
    SecretScannerV0,
    merge_scan_results,
    recompute_chain_hash,
    redact_secrets_in_text,
)

logger = logging.getLogger(__name__)

#: The ``metadata`` key an in-process capsule uses to say why it is not sealed.
FINALIZATION_ERROR_KEY = "finalization_error"


@dataclass(frozen=True)
class FinalizeResult:
    """What finalization did.

    ``unsealed_reason`` is set only when the capsule *could* have been sealed and
    was not: the manifest gate refused it, the seal itself failed, or (in-process
    only) finalization raised. A capsule with no signing profile is unsealed by
    design (ADR-0301) and has no reason.
    """

    manifest: dict[str, Any]
    sealed: bool
    unsealed_reason: str | None = None


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def extend_masker_results(
    proof: dict[str, Any],
    findings: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Append ADR-0135 masker results from a later stage to the proof and re-chain it."""
    proof = dict(proof)
    proof["masker_findings"] = list(proof.get("masker_findings", [])) + list(findings)
    proof["masker_errors"] = list(proof.get("masker_errors", [])) + list(errors)
    return recompute_chain_hash(proof)


#: Capsule-relative paths never covered by ``evidence_digests``.
#: ``.seal/`` does not exist yet at digest time, and ``capsule.yaml`` is the
#: carrier of the digest map itself — it is bound instead by comparing it to the
#: signed DSSE payload at verify time (ADR-0251 §2).
_DIGEST_EXCLUDED_TOP: frozenset[str] = frozenset({".seal", "capsule.yaml"})


def evidence_digests(capsule_dir: Path) -> dict[str, Any]:
    """Hash every evidence file in *capsule_dir* for inclusion in the manifest.

    ADR-0251: the signed manifest names its evidence files (``model_calls_ref``,
    ``trace_ref``, …) by filename and nothing else, so a signature over it proves
    only that the *names* are unchanged.  Editing a recorded token count left
    ``nova verify`` fully green.  This map puts the bytes inside the signed
    payload.

    Keys are capsule-relative POSIX paths, sorted, so the payload is stable across
    filesystems.  Entry shape matches the Evidence Bundle's ``ArtifactEntry``
    (``sha256`` in ``sha256:<hex>`` form, ``size_bytes``) so the two tamper-evidence
    layers read alike.
    """
    digests: dict[str, Any] = {}
    for path in sorted(capsule_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(capsule_dir)
        if rel.parts[0] in _DIGEST_EXCLUDED_TOP:
            continue
        data = path.read_bytes()
        digests[rel.as_posix()] = {"sha256": _sha256(data), "size_bytes": len(data)}
    return digests


def seal_capsule(capsule_dir: Path, manifest: dict[str, Any]) -> str | None:
    """Apply NovaSeal signing to *capsule_dir* if NovaSeal is configured.

    Non-blocking: any failure prints a warning and returns its reason instead of
    raising. NovaSeal is skipped entirely if no config is found. Returns ``None``
    when the capsule was sealed or sealing is not configured.
    """
    try:
        from novafabric.trust.novaseal.config import SealConfigError, load_signing_profile
        profile = load_signing_profile()
        if profile is None:
            return None  # NovaSeal not configured — skip silently

        from novafabric.trust.novaseal import KeyConfig, NovaSeal

        config = KeyConfig(
            profile=profile.profile,
            key_path=str(profile.key_path or ""),
            cert_path=str(profile.cert_path),
        )
        # The cloud profiles have no local private key, so they must sign through
        # a SigningBackend. Without this the seal fell through to the local PEM
        # branch of create_envelope() and failed with a misleading
        # "No such file or directory: 'None'" — which meant aws_kms, azure_kv and
        # gcp_kms could never actually seal a capsule.
        backend = None
        if profile.profile != "local":
            from novafabric.trust.novaseal.config import build_signing_backend

            backend = build_signing_backend(profile)
        seal = NovaSeal(
            config=config,
            tsa_url=profile.tsa_url,
            tsa_urls=profile.tsa_urls,
            db_path=str(profile.merkle_db),
            backend=backend,
        )
        bundle = seal.seal(manifest)

        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir(exist_ok=True)
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
        (seal_dir / "log-entry.json").write_text(
            json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
        )
        return None

    except SealConfigError as exc:
        print(f"[novafabric] ⚠ NovaSeal config error: {exc}", file=sys.stderr)
        return f"NovaSeal config error: {exc}"
    except Exception as exc:
        print(f"[novafabric] ⚠ NovaSeal sealing failed (capsule is still valid): {exc}",
              file=sys.stderr)
        return f"NovaSeal sealing failed: {type(exc).__name__}: {exc}"


def write_redacted_manifest(
    manifest: dict[str, Any],
    *,
    scanner: SecretScannerV0,
    proof: dict[str, Any],
    writer: Any,
    run_id: str,
    masking_pipeline: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Redact *manifest* as a data structure and write it as ``capsule.yaml``.

    ADR-0009: the manifest can carry a raw argv or caller-supplied labels, so it
    is redacted before it is ever written (and before it becomes the signed seal
    payload). Values are redacted in the data structure, not in serialized YAML.
    ADR-0135 maskers see it after the built-in redaction; the built-in rules then
    run once more over the masker output (folded into the same ``capsule-yaml``
    target), so they keep the last word. Returns ``(manifest, proof)``.
    """
    manifest, target, findings = scanner.redact_manifest(manifest)
    proof = merge_scan_results(proof, [target], findings)
    if masking_pipeline is not None:
        manifest, mf, me = masking_pipeline.mask_mapping(
            manifest, "capsule.yaml", "capsule-yaml", run_id
        )
        proof = extend_masker_results(proof, mf, me)
        manifest, rt, rf = scanner.redact_manifest(manifest)
        proof = scanner.fold_rescan(proof, rt, rf)
    writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))
    return manifest, proof


def _emit_lineage(capsule_dir: Path, run_id: str) -> None:
    """Write ``lineage.jsonl`` and index the run. Best-effort; never raises.

    ADR-0251 §6: emitted *before* sealing so lineage.jsonl is covered by
    evidence_digests. The writer needs only capsule.yaml, which already exists;
    index_capsule_lineage() writes outside the capsule, so its position is
    immaterial. Always written -- even for no-edge captures, the run node must be
    queryable via `nova lineage provenance <run_id>`.
    """
    try:
        from novafabric.lineage._importer import index_capsule_lineage
        from novafabric.lineage._writer import LineageWriter

        lw = LineageWriter(capsule_dir=capsule_dir, run_id=run_id)
        lineage_edges = lw.infer()
        lw.write(lineage_edges)
        index_capsule_lineage(capsule_dir, run_id=run_id)
    except Exception as _lineage_exc:
        print(
            f"[novafabric] ⚠ Lineage index update failed: {_lineage_exc}\n"
            f"  Run `nova lineage import {capsule_dir}` to repair.",
            file=sys.stderr,
        )


def finalize_capsule(
    capsule_dir: Path,
    manifest: dict[str, Any],
    *,
    run_id: str,
    writer: Any,
    scanner: SecretScannerV0,
    proof: dict[str, Any],
    masking_pipeline: Any = None,
    before_residual: Callable[[], None] | None = None,
    digests: Callable[[Path], dict[str, Any]] | None = None,
    seal: Callable[[Path, dict[str, Any]], str | None] | None = None,
) -> FinalizeResult:
    """Lineage, late scans, residual pass, proof, digests, manifest gate, seal.

    Call after ``capsule.yaml`` was written by :func:`write_redacted_manifest`
    and every other evidence file exists. *before_residual* runs immediately
    before the residual pass, for a file that must be scanned and bound but is
    only known late (``nova capture``'s ``capture-health.json``). *digests* and
    *seal* default to :func:`evidence_digests` and :func:`seal_capsule`.

    Strict: an unexpected exception propagates (``nova capture`` keeps its
    behaviour). The manifest gate fails closed -- a residual secret in the final
    manifest is redacted, written, and the capsule is **not** sealed.
    """
    _emit_lineage(capsule_dir, run_id)

    # ADR-0009: lineage.jsonl is written after the main scan; scan it now
    # (then ADR-0135 maskers over it, same order as every other target).
    late_targets, late_findings, late_removed = scanner.scan_and_redact_refs(
        [("lineage.jsonl", "lineage")]
    )
    proof = merge_scan_results(proof, late_targets, late_findings, late_removed)
    if masking_pipeline is not None:
        lf, le = masking_pipeline.run(
            capsule_dir, run_id, targets=[("lineage.jsonl", "lineage")]
        )
        proof = extend_masker_results(proof, lf, le)

    if before_residual is not None:
        before_residual()

    # ADR-0009 residual pass: the LAST write to any evidence file is done, so
    # rescan the whole finished capsule (late files such as replay.yaml and the
    # C2PA marker, and anything a masker rewrote) with the built-in rules, which
    # therefore have the last word. Residuals are redacted and recorded; the
    # proof's after-hashes are reconciled to the bytes on disk. Only then is
    # the one final proof written, so evidence_digests binds it.
    proof = scanner.residual_scan(proof)
    writer.write_text("redaction-proof.json", json.dumps(proof, indent=2))

    # ADR-0251: bind the seal to the bytes on disk. The manifest names its
    # evidence files by filename with no digest, so hash every evidence file into
    # the manifest *before* it becomes the signed payload, then rewrite
    # capsule.yaml so the file on disk and the signed payload agree.
    manifest["evidence_digests"] = (digests or evidence_digests)(capsule_dir)
    # Last gate: the final manifest (now carrying the digest map) is checked as
    # it will be written. A hit means an earlier stage missed something, so the
    # capsule is NOT sealed — sealing would sign the leak. Fail closed: the
    # manifest is redacted before it is written, and the run is reported.
    try:
        manifest_text = scanner.assert_manifest_clean(manifest)
    except ResidualSecretError as exc:
        logger.error("novafabric.secrets: %s; capsule left unsealed", exc)
        print(
            f"[novafabric] ✗ {exc}. capsule.yaml was redacted and the capsule "
            "was NOT sealed.",
            file=sys.stderr,
        )
        manifest, _, _ = scanner.redact_manifest(manifest)
        writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))
        return FinalizeResult(
            manifest=manifest,
            sealed=False,
            unsealed_reason=f"manifest gate refused to seal: {exc}",
        )

    writer.write_text("capsule.yaml", manifest_text)
    # --- NovaSeal (opt-in, non-blocking) ---
    reason = (seal or seal_capsule)(capsule_dir, manifest)
    # A seal that reported a failure is not a seal, whatever it left on disk.
    sealed = reason is None and (capsule_dir / ".seal" / "manifest.dsse").is_file()
    return FinalizeResult(
        manifest=manifest,
        sealed=sealed,
        unsealed_reason=None if sealed else reason,
    )


def _redact_mapping(value: Any) -> Any:
    """Redact every string in *value* with the built-in rules (scanner-free)."""
    if isinstance(value, str):
        return redact_secrets_in_text(value)
    if isinstance(value, dict):
        return {_redact_mapping(k): _redact_mapping(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_mapping(v) for v in value]
    return value


def _record_unsealed(
    capsule_dir: Path, manifest: dict[str, Any], writer: Any, reason: str
) -> dict[str, Any]:
    """Leave the capsule unsealed and say why in ``metadata.finalization_error``.

    A partial ``.seal/`` is removed: the manifest is rewritten here, so no
    signature in it could match. The reason is redacted with the built-in rules
    (an exception message is not trusted), as is the rest of the manifest.
    """
    shutil.rmtree(capsule_dir / ".seal", ignore_errors=True)
    out = dict(manifest)
    metadata = dict(out.get("metadata") or {})
    metadata[FINALIZATION_ERROR_KEY] = reason
    out["metadata"] = metadata
    redacted: dict[str, Any] = _redact_mapping(out)
    writer.write_text("capsule.yaml", yaml.dump(redacted, allow_unicode=True))
    return redacted


def finalize_in_process_capsule(
    capsule_dir: Path,
    manifest: dict[str, Any],
    *,
    run_id: str,
    writer: Any,
) -> FinalizeResult:
    """Finalize a capsule written inside the user's process. Never raises.

    For the framework adapters and the ``@agent`` decorator. The caller has
    written every workload file plus ``env.lock`` and ``replay.yaml``; this runs
    the main secret scan over them, then the same two steps ``nova capture`` runs
    (:func:`write_redacted_manifest`, :func:`finalize_capsule`), in the same order.

    Any exception is caught: the capsule is left unsealed, the reason is recorded
    in ``metadata.finalization_error`` and logged, and the caller's own result or
    exception is untouched. A gate refusal or a failed seal is recorded the same
    way; a capsule with no signing profile is simply unsealed (ADR-0301).
    """
    proof: dict[str, Any] | None = None
    try:
        scanner = SecretScannerV0(capsule_dir=capsule_dir, run_id=run_id)
        proof = scanner.scan_and_redact()
        manifest, proof = write_redacted_manifest(
            manifest, scanner=scanner, proof=proof, writer=writer, run_id=run_id
        )
        result = finalize_capsule(
            capsule_dir, manifest, run_id=run_id, writer=writer, scanner=scanner,
            proof=proof,
        )
    except Exception as exc:  # noqa: BLE001 — never fail the user's call
        reason = f"finalization failed: {type(exc).__name__}: {exc}"
        logger.warning(
            "novafabric: in-process capsule %s left unsealed: %s",
            run_id, redact_secrets_in_text(reason),
        )
        try:
            if proof is not None and not (capsule_dir / "redaction-proof.json").exists():
                writer.write_text("redaction-proof.json", json.dumps(proof, indent=2))
            manifest = _record_unsealed(capsule_dir, manifest, writer, reason)
        except Exception:  # noqa: BLE001 — the disk itself may be the failure
            logger.exception("novafabric: could not record the finalization failure")
        return FinalizeResult(manifest=manifest, sealed=False, unsealed_reason=reason)

    if result.unsealed_reason is None:
        return result
    try:
        recorded = _record_unsealed(capsule_dir, result.manifest, writer, result.unsealed_reason)
    except Exception:  # noqa: BLE001
        logger.exception("novafabric: could not record why the capsule is unsealed")
        recorded = result.manifest
    return FinalizeResult(
        manifest=recorded, sealed=False, unsealed_reason=result.unsealed_reason
    )
