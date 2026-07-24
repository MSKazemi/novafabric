from __future__ import annotations

import base64
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from novafabric import __version__ as NF_VERSION
from novafabric._hashutil import sha256_file_prefixed, sha256_prefixed
from novafabric.audit import AUDIT_LOG_PATH, AuditEventType, AuditLog
from novafabric.capture._ulid import new_ulid
from novafabric.evidence.admissibility import Custodian, admissibility_block
from novafabric.evidence.intoto import dsse_sign, make_intoto_statement
from novafabric.evidence.merkle import capsule_merkle_root
from novafabric.evidence.signing import LocalSigner
from novafabric.policy import (
    PolicyDeniedError,
    PolicyInput,
    PolicyResource,
    PolicySubject,
    get_policy_engine,
)

REQUIRED_CAPSULE_FILES = (
    "capsule.yaml",
    "env.lock",
    "redaction-proof.json",
    "trace.jsonl",
    "model-calls.jsonl",
    "tool-calls.jsonl",
    "assets.jsonl",
)
REQUIRED_CAPSULE_DIRS = ("inputs", "outputs")

PREDICATE_RUN = "https://novafabric.io/runcapsule/v0"
PREDICATE_REDACTION = "https://novafabric.io/redaction-proof/v0"
PREDICATE_LINEAGE = "https://novafabric.io/lineage/v0"
PREDICATE_ENERGY = "https://novafabric.io/energy-receipts/v0"

VERIFIER_INSTRUCTIONS = (
    "Verification recipe (no NovaFabric required):\n"
    "  1. Recompute manifest_hash: drop the field, canonicalize the rest as\n"
    "     sorted-key JSON with no whitespace, sha256 it, prefix with 'sha256:'.\n"
    "  2. For each artifact in `artifacts[]`, recompute sha256 of the file at\n"
    "     `path` inside the bundle and compare to `sha256`.\n"
    "  3. For each DSSE envelope in `attestations[]`, decode its payload, build\n"
    "     PAE = 'DSSEv1 LEN(type) SP type SP LEN(payload) SP payload', verify\n"
    "     using the public key in the matching `signatures[*].certificate_path`.\n"
    "  4. Confirm each statement's subject digest matches `subject.capsule_hash`.\n"
)


class CapsuleValidationError(Exception):
    pass


class UnsafeSkipsError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return sha256_prefixed(data)


def _sha256_file(path: Path) -> str:
    return sha256_file_prefixed(path)


def _media_type_for(name: str) -> str:
    if name.endswith(".yaml") or name.endswith(".yml"):
        return "application/yaml"
    if name.endswith(".json") or name.endswith(".intoto.json"):
        return "application/json"
    if name.endswith(".jsonl"):
        return "application/jsonl"
    if name.endswith(".lock"):
        return "text/plain"
    if name.endswith(".pem") or name.endswith(".cert"):
        return "application/x-pem-file"
    if name.endswith(".sig"):
        return "application/octet-stream"
    if name.endswith(".md"):
        return "text/markdown"
    return "application/octet-stream"


def _canonical_manifest_hash(manifest: dict[str, Any]) -> str:
    work = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    canonical = json.dumps(work, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(canonical.encode())


class EvidenceBundleBuilder:
    """Assembles an Evidence Bundle ZIP per ADR-0011 (local-key mode)."""

    def __init__(
        self,
        capsule_dir: Path,
        signer: LocalSigner,
        output_path: Path,
        allow_unsafe_skips: bool = False,
        actor: str = "cli",
        with_custody: bool = False,
        custodian: Custodian | None = None,
        audit_log_path: Path | None = None,
    ) -> None:
        self._capsule_dir = capsule_dir
        self._signer = signer
        self._output_path = output_path
        self._allow_unsafe_skips = allow_unsafe_skips
        self._unsafe_skip_count = 0
        self._actor = actor
        self._with_custody = with_custody
        self._custodian = custodian
        self._audit_log_path = audit_log_path or AUDIT_LOG_PATH

    def _validate_capsule(self) -> None:
        missing: list[str] = []
        for name in REQUIRED_CAPSULE_FILES:
            if not (self._capsule_dir / name).exists():
                missing.append(name)
        for dname in REQUIRED_CAPSULE_DIRS:
            if not (self._capsule_dir / dname).is_dir():
                missing.append(f"{dname}/")
        if missing:
            raise CapsuleValidationError(
                "capsule is missing required files: " + ", ".join(missing)
            )

        proof_data = json.loads(
            (self._capsule_dir / "redaction-proof.json").read_text()
        )
        skips = proof_data.get("unsafe_skips") or []
        self._unsafe_skip_count = len(skips)
        if skips and not self._allow_unsafe_skips:
            raise UnsafeSkipsError(
                f"capsule has {len(skips)} unsafe_skips entries; pass "
                "--allow-unsafe-skips to export anyway."
            )

    def build(self) -> Path:
        self._validate_capsule()

        # Policy gate — checked after structural validation so that the
        # resource_id we pass is always a meaningful capsule identifier.
        capsule_ref = self._capsule_dir.name
        engine = get_policy_engine()
        inp = PolicyInput(
            action="evidence_export",
            subject=PolicySubject(user=self._actor),
            resource=PolicyResource(
                kind="capsule",
                ref=capsule_ref,
                redaction_proof_present=(
                    self._capsule_dir / "redaction-proof.json"
                ).exists(),
                # _validate_capsule already raised unless the count is zero or
                # the operator explicitly waived it (--allow-unsafe-skips); a
                # waived export reports 0 so the default gate honors the waiver.
                unsafe_skips=0 if self._allow_unsafe_skips else self._unsafe_skip_count,
            ),
        )
        decision = engine.evaluate(inp)
        AuditLog(AUDIT_LOG_PATH).append(
            event_type=(
                AuditEventType.POLICY_ALLOW if decision.allow else AuditEventType.POLICY_DENY
            ),
            actor=self._actor,
            resource_id=capsule_ref,
            details={
                "decision_id": decision.decision_id,
                "reason": decision.reason,
                "action": "evidence_export",
            },
        )
        if not decision.allow:
            raise PolicyDeniedError(decision.reason, decision.decision_id)

        with tempfile.TemporaryDirectory() as raw_tmp:
            staging = Path(raw_tmp) / "bundle"
            staging.mkdir()
            self._stage_capsule_copy(staging)
            self._stage_lineage_subgraph(staging)
            self._stage_schemas(staging)
            self._stage_readme(staging)
            attestations, signatures = self._stage_attestations_and_signatures(staging)
            manifest = self._build_manifest(staging, attestations, signatures)
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True)
            )
            self._zip_staging(staging)
        return self._output_path

    def _stage_capsule_copy(self, staging: Path) -> None:
        dest = staging / "run-capsule"
        shutil.copytree(self._capsule_dir, dest)

    def _stage_lineage_subgraph(self, staging: Path) -> None:
        dest = staging / "lineage-subgraph"
        dest.mkdir()
        src = self._capsule_dir / "lineage.jsonl"
        edges = src.read_bytes() if src.exists() else b""
        (dest / "edges.jsonl").write_bytes(edges)

    def _stage_schemas(self, staging: Path) -> None:
        schemas_dir = staging / "schemas"
        schemas_dir.mkdir()
        package_schemas = Path(__file__).parent.parent / "schemas"
        for source in sorted(package_schemas.glob("*.schema.json")):
            (schemas_dir / source.name).write_bytes(source.read_bytes())

    def _stage_readme(self, staging: Path) -> None:
        (staging / "README.md").write_text(VERIFIER_INSTRUCTIONS)

    def _stage_attestations_and_signatures(
        self, staging: Path
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        capsule_hash = capsule_merkle_root(self._capsule_dir)

        manifest_yaml = yaml.safe_load((self._capsule_dir / "capsule.yaml").read_text())
        run_id = manifest_yaml["run_id"]

        proof = json.loads(
            (self._capsule_dir / "redaction-proof.json").read_text()
        )

        lineage_src = staging / "lineage-subgraph" / "edges.jsonl"
        lineage_hash = _sha256_bytes(lineage_src.read_bytes())

        run_predicate = {
            "run_id": run_id,
            "capsule_hash": capsule_hash,
            "captured_by": {"name": "novafabric", "version": NF_VERSION},
        }
        redaction_predicate = {
            "proof_id": proof["proof_id"],
            "chain_hash": proof["chain_hash"],
            "findings_count": proof["findings_count"]["total"],
            "scanner": proof["scanner"]["name"],
        }
        lineage_predicate = {
            "run_id": run_id,
            "lineage_subgraph_hash": lineage_hash,
            "edge_count": _count_lines(lineage_src),
        }

        attestations: list[dict[str, Any]] = []
        signatures: list[dict[str, Any]] = []
        signed_at = _now_iso()

        att_dir = staging / "attestations"
        sig_dir = staging / "signatures"
        att_dir.mkdir()
        sig_dir.mkdir()

        spec_table = [
            ("run", PREDICATE_RUN, run_predicate, capsule_hash, run_id),
            (
                "redaction",
                PREDICATE_REDACTION,
                redaction_predicate,
                proof["chain_hash"],
                f"{run_id}/redaction-proof.json",
            ),
            (
                "lineage",
                PREDICATE_LINEAGE,
                lineage_predicate,
                lineage_hash,
                f"{run_id}/lineage-subgraph",
            ),
        ]

        # Energy-Anchored Receipts attestation (ADR-0093) — only when the
        # capsule carries energy receipts; additive, absent otherwise.
        energy_src = self._capsule_dir / "energy-receipts.jsonl"
        if energy_src.exists():
            energy_bytes = energy_src.read_bytes()
            energy_hash = _sha256_bytes(energy_bytes)
            receipts = [
                json.loads(line)
                for line in energy_bytes.decode().splitlines()
                if line.strip()
            ]
            sources = sorted(
                {str(r.get("measurement_source")) for r in receipts if r}
            )
            energy_predicate = {
                "run_id": run_id,
                "energy_receipts_hash": energy_hash,
                "receipts_total": len(receipts),
                "measurement_sources": sources,
            }
            spec_table.append((
                "energy",
                PREDICATE_ENERGY,
                energy_predicate,
                energy_hash,
                f"{run_id}/energy-receipts.jsonl",
            ))

        for short_name, predicate_type, predicate, subject_digest, subject_name in (
            spec_table
        ):
            statement = make_intoto_statement(
                predicate_type=predicate_type,
                subject_name=subject_name,
                subject_sha256=subject_digest,
                predicate=predicate,
            )
            envelope = dsse_sign(statement, self._signer)

            envelope_rel = f"attestations/{short_name}.intoto.json"
            envelope_path = staging / envelope_rel
            envelope_path.write_text(json.dumps(envelope, indent=2, sort_keys=True))

            sig_rel = f"signatures/{short_name}.sig"
            cert_rel = f"signatures/{short_name}.cert"
            (staging / sig_rel).write_bytes(
                base64.b64decode(envelope["signatures"][0]["sig"])
            )
            (staging / cert_rel).write_bytes(self._signer.public_pem)

            attestations.append({
                "path": envelope_rel,
                "predicate_type": predicate_type,
                "subject_count": 1,
                "subject_hashes": [subject_digest],
                "envelope_format": "DSSE",
                "sha256": _sha256_file(envelope_path),
                "signature_refs": [sig_rel],
            })
            signatures.append({
                "envelope_path": envelope_rel,
                "signature_path": sig_rel,
                "certificate_path": cert_rel,
                "signer_identity": "local-key",
                "signature_algorithm": "ed25519",
                "signed_at": signed_at,
            })

        return attestations, signatures

    def _build_manifest(
        self,
        staging: Path,
        attestations: list[dict[str, Any]],
        signatures: list[dict[str, Any]],
    ) -> dict[str, Any]:
        manifest_yaml = yaml.safe_load((self._capsule_dir / "capsule.yaml").read_text())
        run_id = manifest_yaml["run_id"]
        capsule_hash = capsule_merkle_root(self._capsule_dir)

        artifacts: list[dict[str, Any]] = []
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(staging).as_posix()
            if rel == "manifest.json":
                continue
            covered_by: list[str] = []
            if rel.startswith("run-capsule/"):
                covered_by.append("attestations/run.intoto.json")
            if rel == "run-capsule/redaction-proof.json":
                covered_by.append("attestations/redaction.intoto.json")
            if rel.startswith("lineage-subgraph/"):
                covered_by.append("attestations/lineage.intoto.json")
            entry: dict[str, Any] = {
                "path": rel,
                "size_bytes": path.stat().st_size,
                "media_type": _media_type_for(rel),
                "sha256": _sha256_file(path),
            }
            if covered_by:
                entry["covered_by"] = covered_by
            artifacts.append(entry)

        schemas_meta: list[dict[str, Any]] = []
        for path in sorted((staging / "schemas").iterdir()):
            schemas_meta.append({
                "name": path.name,
                "version": "0.1.0",
                "sha256": _sha256_file(path),
            })

        manifest: dict[str, Any] = {
            "schema_version": "0.1.0",
            "bundle_id": new_ulid(),
            "created_at": _now_iso(),
            "created_by": {"name": "novafabric", "version": NF_VERSION},
            "subject": {
                "kind": "run-capsule",
                "run_id": run_id,
                "capsule_hash": capsule_hash,
                "capsule_path_in_bundle": "run-capsule/",
            },
            "bundle_format": "zip",
            "artifacts": artifacts,
            "attestations": attestations,
            "signatures": signatures,
            "schemas": schemas_meta,
            "verifier_instructions": VERIFIER_INSTRUCTIONS,
        }

        # Court-admissibility binding (ADR-0095) — additive; embedded before the
        # manifest hash so the chain of custody is covered by manifest_hash.
        if self._with_custody:
            manifest.update(
                admissibility_block(
                    self._capsule_dir,
                    audit_log_path=self._audit_log_path,
                    run_id=run_id,
                    custodian=self._custodian,
                    signer=self._signer,
                    timestamp_ok=False,
                )
            )

        manifest["manifest_hash"] = _canonical_manifest_hash(manifest)
        return manifest

    def _zip_staging(self, staging: Path) -> None:
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self._output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(staging).as_posix())


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.read_bytes().splitlines() if _)
