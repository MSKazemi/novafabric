from __future__ import annotations

import base64
import json
import shutil
import tempfile
import zipfile
from collections.abc import Sequence
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

    def _validate_capsule(self, capsule_dir: Path | None = None) -> None:
        """Structural + redaction validation for one capsule.

        Takes the directory explicitly so a capsule-set export can validate
        **every** capsule before anything is written. Defaults to this builder's
        own capsule, so the single-capsule path is unchanged.
        """
        capsule_dir = capsule_dir or self._capsule_dir
        missing: list[str] = []
        for name in REQUIRED_CAPSULE_FILES:
            if not (capsule_dir / name).exists():
                missing.append(name)
        for dname in REQUIRED_CAPSULE_DIRS:
            if not (capsule_dir / dname).is_dir():
                missing.append(f"{dname}/")
        if missing:
            raise CapsuleValidationError(
                f"capsule {capsule_dir.name} is missing required files: "
                + ", ".join(missing)
            )

        proof_data = json.loads((capsule_dir / "redaction-proof.json").read_text())
        skips = proof_data.get("unsafe_skips") or []
        self._unsafe_skip_count += len(skips)
        if skips and not self._allow_unsafe_skips:
            raise UnsafeSkipsError(
                f"capsule {capsule_dir.name} has {len(skips)} unsafe_skips entries; "
                "pass --allow-unsafe-skips to export anyway."
            )

    def build(self) -> Path:
        self._validate_capsule()
        self._run_policy_gate()

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

    def _run_policy_gate(self) -> None:
        """Evaluate the ADR-0019 export policy and record the decision.

        Extracted from :meth:`build` so a capsule-set export runs the *same*
        gate rather than a second copy of it. One export is one decision: the
        gate is evaluated once for the whole bundle, keyed to the primary
        capsule, because the artifact a policy would refuse is the bundle.
        """
        # Checked after structural validation so that the resource_id we pass
        # is always a meaningful capsule identifier.
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

    def _stage_capsule_copy(
        self, staging: Path, capsule_dir: Path | None = None, dest_rel: str = "run-capsule"
    ) -> None:
        dest = staging / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(capsule_dir or self._capsule_dir, dest)

    def _stage_lineage_subgraph(
        self,
        staging: Path,
        capsule_dir: Path | None = None,
        dest_rel: str = "lineage-subgraph",
    ) -> None:
        dest = staging / dest_rel
        dest.mkdir(parents=True, exist_ok=True)
        src = (capsule_dir or self._capsule_dir) / "lineage.jsonl"
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
        self,
        staging: Path,
        capsule_dir: Path | None = None,
        lineage_rel: str = "lineage-subgraph",
        name_prefix: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Sign one capsule's attestations into *staging*.

        ``name_prefix`` namespaces the envelope filenames so a capsule-set
        bundle does not collide four attestations per capsule onto the same four
        paths. Empty for the single-capsule path, which keeps
        ``attestations/run.intoto.json`` exactly where every existing bundle and
        the ADR-0011 layout put it.
        """
        capsule_dir = capsule_dir or self._capsule_dir
        capsule_hash = capsule_merkle_root(capsule_dir)

        manifest_yaml = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
        run_id = manifest_yaml["run_id"]

        proof = json.loads((capsule_dir / "redaction-proof.json").read_text())

        lineage_src = staging / lineage_rel / "edges.jsonl"
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
        if name_prefix:
            att_dir = att_dir / name_prefix
            sig_dir = sig_dir / name_prefix
        att_dir.mkdir(parents=True, exist_ok=True)
        sig_dir.mkdir(parents=True, exist_ok=True)

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
        energy_src = capsule_dir / "energy-receipts.jsonl"
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

            scoped = f"{name_prefix}/" if name_prefix else ""
            envelope_rel = f"attestations/{scoped}{short_name}.intoto.json"
            envelope_path = staging / envelope_rel
            envelope_path.write_text(json.dumps(envelope, indent=2, sort_keys=True))

            sig_rel = f"signatures/{scoped}{short_name}.sig"
            cert_rel = f"signatures/{scoped}{short_name}.cert"
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


# ---------------------------------------------------------------------------
# ADR-0011 Amendment 1 — a bundle may carry a capsule SET
# ---------------------------------------------------------------------------

#: The curation record ADR-0239 D5 requires, written as a bundle file rather
#: than a manifest key. Its digest lands in ``artifacts[]`` and is therefore
#: covered by ``manifest_hash``, so it cannot be stripped without breaking
#: verification — which is what D5 actually asks for. A manifest key would have
#: meant editing three live copies of a schema that is
#: ``additionalProperties: false`` at the root, to gain a property an artifact
#: entry already makes tamper-evident. A file is also visible to a human who
#: opens the ZIP, which a manifest key is not.
CURATION_FILENAME = "curation.json"


class CapsuleSetBundleBuilder(EvidenceBundleBuilder):
    """One Evidence Bundle over **several** capsules (ADR-0011 Amendment 1).

    The bundle format already permitted this before any code did:
    ``evidence-bundle.schema.json`` defines ``subject`` as
    ``oneOf[Subject, array<Subject> minItems:2]``. So this is an implementation
    of the shipped format, not a change to it, and the shipped ``nova verify``
    reads such a bundle unmodified — it works off ``artifacts[]`` and
    ``manifest_hash`` and never consults ``subject`` at all.

    **Why one bundle rather than N.** A cart of twelve items exported as twelve
    bundles hands the recipient twelve things to verify and cross-check, bound
    by a thirteenth artifact describing the set — and *"a third top-level format
    beyond Run Capsule and Evidence Bundle"* is an explicit anti-pattern in this
    project. One signed artifact with N subjects is what in-toto Statement v1
    already models, and what a recipient can check in one step.

    A **one-capsule** set is not a set: the schema's array form starts at two,
    and a single-capsule export *is* an ordinary bundle. Constructing this with
    one capsule therefore produces byte-identical output to
    :class:`EvidenceBundleBuilder`.
    """

    def __init__(
        self,
        capsule_dirs: Sequence[Path],
        signer: LocalSigner,
        output_path: Path,
        allow_unsafe_skips: bool = False,
        actor: str = "cli",
        curation: dict[str, Any] | None = None,
    ) -> None:
        ordered = self._normalize(capsule_dirs)
        super().__init__(
            capsule_dir=ordered[0],
            signer=signer,
            output_path=output_path,
            allow_unsafe_skips=allow_unsafe_skips,
            actor=actor,
        )
        self._capsule_dirs = ordered
        self._curation = curation

    @staticmethod
    def _normalize(capsule_dirs: Sequence[Path]) -> tuple[Path, ...]:
        """Resolve, reject an empty set, and reject duplicates.

        An empty set is refused because a bundle of nothing that verifies is a
        trap: it carries a valid signature over no evidence, and reads as an
        export that succeeded.

        Duplicates are refused rather than de-duplicated. The same capsule under
        two paths would appear twice in ``subject`` with one digest, and a
        recipient counting subjects would over-count the evidence. Silently
        collapsing them would instead hide that the caller's selection was
        wrong.
        """
        resolved = [Path(d).resolve() for d in capsule_dirs]
        if not resolved:
            raise CapsuleValidationError(
                "an evidence bundle needs at least one capsule; a bundle of nothing "
                "still carries a valid signature and reads as a successful export"
            )
        seen: dict[Path, int] = {}
        for index, path in enumerate(resolved):
            if path in seen:
                raise CapsuleValidationError(
                    f"capsule {path.name} appears twice in the set (positions "
                    f"{seen[path]} and {index}); a repeated subject over-counts the "
                    "evidence a recipient thinks they have"
                )
            seen[path] = index
        return tuple(resolved)

    def build(self) -> Path:
        # One capsule is not a set — the schema's array form begins at two, and
        # an ordinary bundle is the correct artifact for a single capsule.
        if len(self._capsule_dirs) == 1 and self._curation is None:
            return super().build()

        # Validate EVERY capsule before writing anything. A half-written
        # evidence bundle is worse than none: it is a signed artifact whose
        # contents do not match what the operator asked to export.
        for capsule_dir in self._capsule_dirs:
            self._validate_capsule(capsule_dir)

        self._run_policy_gate()

        with tempfile.TemporaryDirectory() as raw_tmp:
            staging = Path(raw_tmp) / "bundle"
            staging.mkdir()

            attestations: list[dict[str, Any]] = []
            signatures: list[dict[str, Any]] = []
            subjects: list[dict[str, Any]] = []

            for capsule_dir in self._capsule_dirs:
                run_id = str(
                    yaml.safe_load((capsule_dir / "capsule.yaml").read_text())["run_id"]
                )
                capsule_rel = f"run-capsule/{run_id}"
                lineage_rel = f"lineage-subgraph/{run_id}"
                self._stage_capsule_copy(staging, capsule_dir, capsule_rel)
                self._stage_lineage_subgraph(staging, capsule_dir, lineage_rel)
                att, sig = self._stage_attestations_and_signatures(
                    staging, capsule_dir, lineage_rel, name_prefix=run_id
                )
                attestations.extend(att)
                signatures.extend(sig)
                subjects.append({
                    "kind": "run-capsule",
                    "run_id": run_id,
                    "capsule_hash": capsule_merkle_root(capsule_dir),
                    "capsule_path_in_bundle": f"{capsule_rel}/",
                })

            self._stage_schemas(staging)
            self._stage_readme(staging)
            if self._curation is not None:
                (staging / CURATION_FILENAME).write_text(
                    json.dumps(self._curation, indent=2, sort_keys=True)
                )

            manifest = self._build_set_manifest(staging, subjects, attestations, signatures)
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True)
            )
            self._zip_staging(staging)
        return self._output_path

    def _build_set_manifest(
        self,
        staging: Path,
        subjects: list[dict[str, Any]],
        attestations: list[dict[str, Any]],
        signatures: list[dict[str, Any]],
    ) -> dict[str, Any]:
        artifacts: list[dict[str, Any]] = []
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(staging).as_posix()
            if rel == "manifest.json":
                continue
            entry: dict[str, Any] = {
                "path": rel,
                "size_bytes": path.stat().st_size,
                "media_type": _media_type_for(rel),
                "sha256": _sha256_file(path),
            }
            covered_by = _covered_by_for(rel, subjects)
            if covered_by:
                entry["covered_by"] = covered_by
            artifacts.append(entry)

        schemas_meta = [
            {"name": p.name, "version": "0.1.0", "sha256": _sha256_file(p)}
            for p in sorted((staging / "schemas").iterdir())
        ]

        manifest: dict[str, Any] = {
            "schema_version": "0.1.0",
            "bundle_id": new_ulid(),
            "created_at": _now_iso(),
            "created_by": {"name": "novafabric", "version": NF_VERSION},
            # The array form. `minItems: 2` in the schema is why a one-capsule
            # set never reaches here.
            "subject": subjects,
            "bundle_format": "zip",
            "artifacts": artifacts,
            "attestations": attestations,
            "signatures": signatures,
            "schemas": schemas_meta,
            "verifier_instructions": VERIFIER_INSTRUCTIONS,
        }
        manifest["manifest_hash"] = _canonical_manifest_hash(manifest)
        return manifest


def _covered_by_for(rel: str, subjects: list[dict[str, Any]]) -> list[str]:
    """Which attestation envelopes cover *rel*, for a capsule-set bundle.

    Per-capsule, because in a set the answer depends on *which* capsule the file
    belongs to. Getting this wrong would tell a verifier that a file is attested
    by an envelope over a different capsule — a worse failure than omitting the
    hint, so the mapping is derived from the staged path rather than guessed.
    """
    for subject in subjects:
        run_id = subject["run_id"]
        if rel == f"run-capsule/{run_id}/redaction-proof.json":
            return [
                f"attestations/{run_id}/run.intoto.json",
                f"attestations/{run_id}/redaction.intoto.json",
            ]
        if rel.startswith(f"run-capsule/{run_id}/"):
            return [f"attestations/{run_id}/run.intoto.json"]
        if rel.startswith(f"lineage-subgraph/{run_id}/"):
            return [f"attestations/{run_id}/lineage.intoto.json"]
    return []
