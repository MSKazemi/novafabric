from __future__ import annotations

import base64
import json
import os
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from novafabric import __version__ as NF_VERSION
from novafabric._capsule_fs import (
    CapsuleLinkError,
    CapsuleSpecialFileError,
    copy_capsule_tree,
    find_capsule_links,
    read_capsule_file,
    refuse_capsule_links,
)
from novafabric._hashutil import sha256_file_prefixed, sha256_prefixed
from novafabric._safe_path import UnsafePathSegmentError, fsync_dir, require_path_segment
from novafabric.audit import AuditEventType, AuditLog, resolve_audit_log_path
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
    deployment_environment_from_capsule,
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
#: ADR-0011 Amendment 7 (review r8): one statement signing every member's digest.
PREDICATE_BUNDLE_MEMBERS = "https://novafabric.io/evidence-bundle-members/v0"

#: The members statement and its two companion files. They are the only members the
#: statement cannot cover (it cannot sign its own envelope or signature); the
#: verifier finds them at exactly these paths, never via the unsigned manifest.
MEMBERS_ENVELOPE = "attestations/bundle.intoto.json"
MEMBERS_SIGNATURE = "signatures/bundle.sig"
MEMBERS_CERTIFICATE = "signatures/bundle.cert"
MEMBERS_STATEMENT_FILES = frozenset({MEMBERS_ENVELOPE, MEMBERS_SIGNATURE, MEMBERS_CERTIFICATE})

VERIFIER_INSTRUCTIONS = (
    "Verification recipe (no NovaFabric required):\n"
    "  1. Recompute manifest_hash: drop the field, canonicalize the rest as\n"
    "     sorted-key JSON with no whitespace, sha256 it, prefix with 'sha256:'.\n"
    "  2. For each artifact in `artifacts[]`, recompute sha256 of the file at\n"
    "     `path` inside the bundle and compare to `sha256`.\n"
    "  3. For each DSSE envelope in `attestations[]`, decode its payload, build\n"
    "     PAE = 'DSSEv1 LEN(type) SP type SP LEN(payload) SP payload', verify\n"
    "     using the public key in the matching `signatures[*].certificate_path`.\n"
    "  4. Check each statement's subject digest. It must equal its attestation's\n"
    "     `subject_hashes`, and the digest of what its predicateType covers:\n"
    "     - runcapsule/v0: `subject.capsule_hash`, which step 5 recomputes;\n"
    "     - redaction-proof/v0: `chain_hash` in run-capsule/redaction-proof.json;\n"
    "     - lineage/v0: sha256 of lineage-subgraph/edges.jsonl;\n"
    "     - energy-receipts/v0 (present only when the run recorded energy):\n"
    "       sha256 of run-capsule/energy-receipts.jsonl.\n"
    "     In a capsule-set bundle `subject` is a list, and each run's files sit\n"
    "     under run-capsule/<run_id>/ and lineage-subgraph/<run_id>/.\n"
    "  5. Recompute the capsule root from the files under `run-capsule/`\n"
    "     (manifest.json is not signed). Order files by path components; leaf = sha256(0x02 ||\n"
    "     len(path) as 4-byte big-endian || path || sha256(content)); node =\n"
    "     sha256(0x01 || left || right), splitting at the largest power of two\n"
    "     below the count (RFC 6962). Bundles exported before v0.106.0 use\n"
    "     leaf = sha256(0x00 || content).\n"
    "  6. attestations/bundle.intoto.json (evidence-bundle-members/v0) signs\n"
    "     `members`: the sha256 of every file except manifest.json and its own\n"
    "     three files (it, signatures/bundle.sig, signatures/bundle.cert). Every\n"
    "     other file must be listed with that digest, and every listed file must\n"
    "     be present. `manifest_core_sha256` is step 1's hash computed after also\n"
    "     removing this statement's entries from artifacts[], attestations[] and\n"
    "     signatures[]. If the run statement's predicate names this statement in\n"
    "     `members_statement`, it must be present. Bundles exported before this\n"
    "     statement existed carry neither: only steps 3-5 cover their files.\n"
)


def _capsule_text(capsule_dir: Path, name: str) -> str:
    """A top-level capsule file's text, opened without following a link (CapsuleLinkError)."""
    return read_capsule_file(
        capsule_dir, name, operation="the evidence export", error=CapsuleLinkError
    ).decode("utf-8")


class CapsuleValidationError(Exception):
    pass


class UnsafeSkipsError(Exception):
    pass


class CapsuleSymlinkError(CapsuleValidationError, CapsuleLinkError):
    """The capsule holds a symbolic link; an evidence export never follows one.

    Following a link would put its *target* — anything the exporting user can
    read, e.g. ``~/.ssh/id_rsa`` — into the bundle as a capsule file, signed and
    past redaction. NovaFabric never writes links into a capsule, and neither a
    ZIP entry nor the capsule Merkle root can represent one, so any link refuses
    the export. A subclass of :class:`CapsuleValidationError`, so every caller
    that already reports a malformed capsule reports this too, and of the shared
    :class:`novafabric._capsule_fs.CapsuleLinkError` every other capsule exporter
    raises.
    """


_OPERATION = "an evidence export"


def _capsule_symlinks(root: Path) -> list[str]:
    """Describe every symlink under *root*, without following any of them."""
    return find_capsule_links(root)


def _refuse_symlinks(root: Path, capsule_name: str) -> None:
    """Raise :class:`CapsuleSymlinkError` when *root* holds any symlink."""
    refuse_capsule_links(
        root, operation=_OPERATION, capsule_name=capsule_name, error=CapsuleSymlinkError
    )


def _capsule_hash(capsule_dir: Path) -> str:
    """The capsule's Merkle root, its refusals raised as :class:`CapsuleValidationError`.

    ``_validate_capsule`` already refused any link, but a link or FIFO planted
    after that check makes the root computation refuse too (it never follows
    one). Raising it as :class:`CapsuleSymlinkError` / :class:`CapsuleValidationError`
    lets every export surface report it as the malformed capsule it is (CLI
    exit 1, serve 422, server 422), never as a crash.
    """
    try:
        return capsule_merkle_root(capsule_dir)
    except CapsuleSymlinkError:
        raise
    except CapsuleLinkError as exc:
        raise CapsuleSymlinkError(str(exc), links=exc.links) from exc
    except CapsuleSpecialFileError as exc:
        raise CapsuleValidationError(f"capsule {capsule_dir.name}: {exc}") from exc


def _read_regular_file(path: Path) -> bytes:
    """Read *path* only if it is a regular file, refusing to follow a symlink."""
    try:
        return read_capsule_file(
            path.parent, path.name, operation=_OPERATION, error=CapsuleSymlinkError
        )
    except CapsuleSpecialFileError as exc:
        raise CapsuleValidationError(f"{path.name} is not a regular file") from exc


class PolicyAuditUnavailableError(Exception):
    """The export-policy decision could not be appended to the audit log.

    Raised before anything is written: an evidence export whose policy decision
    is not on the hash-chained audit log is refused, not performed unaudited.
    """


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


# ---------------------------------------------------------------------------
# ADR-0011 Amendment 7 — the members statement (review r8, hole 3)
# ---------------------------------------------------------------------------


class BundleResealError(Exception):
    """A bundle's members statement was not re-signed: its signed content does not verify."""


def manifest_core(manifest: dict[str, Any]) -> dict[str, Any]:
    """*manifest* without ``manifest_hash`` and without the members statement's entries.

    What the members statement's ``manifest_core_sha256`` signs: every manifest field
    (``verifier_instructions``, ``subject``, ``schemas``, ...) except the entries the
    statement adds about itself, which it cannot sign.
    """
    core = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    for key, field_name in (
        ("artifacts", "path"),
        ("attestations", "path"),
        ("signatures", "envelope_path"),
    ):
        value = core.get(key)
        if isinstance(value, list):
            core[key] = [
                e
                for e in value
                if not (isinstance(e, dict) and e.get(field_name) in MEMBERS_STATEMENT_FILES)
            ]
    return core


def members_digest(members: dict[str, str]) -> str:
    """``sha256:`` of the canonical JSON of a ``{path: sha256}`` members map."""
    return _sha256_bytes(json.dumps(members, sort_keys=True, separators=(",", ":")).encode())


def sign_members_statement(
    manifest: dict[str, Any], member_digests: dict[str, str], signer: LocalSigner
) -> dict[str, bytes]:
    """Sign the members statement into *manifest*; return its three files' bytes.

    *member_digests* maps every bundle member except ``manifest.json`` and
    :data:`MEMBERS_STATEMENT_FILES` to its ``sha256:`` digest. Any earlier members
    statement entries are dropped first, so this also re-signs a post-processed
    bundle. *manifest* (changed in place) gains the three files' ``artifacts[]``
    entries (kept sorted by path), an ``attestations[]`` and a ``signatures[]``
    entry, and a fresh ``manifest_hash``.
    """
    members = {k: member_digests[k] for k in sorted(member_digests)}
    stale = set(members) & (MEMBERS_STATEMENT_FILES | {"manifest.json"})
    if stale:
        raise ValueError(f"the members map may not list {sorted(stale)}")
    core = manifest_core(manifest)
    for key in ("artifacts", "attestations", "signatures"):
        if key in core:
            manifest[key] = core[key]
    predicate = {
        "bundle_id": manifest.get("bundle_id"),
        "members": members,
        "manifest_core_sha256": _canonical_manifest_hash(core),
    }
    statement = make_intoto_statement(
        predicate_type=PREDICATE_BUNDLE_MEMBERS,
        subject_name="evidence-bundle-members",
        subject_sha256=members_digest(members),
        predicate=predicate,
    )
    envelope = dsse_sign(statement, signer)
    files = {
        MEMBERS_ENVELOPE: json.dumps(envelope, indent=2, sort_keys=True).encode(),
        MEMBERS_SIGNATURE: base64.b64decode(envelope["signatures"][0]["sig"]),
        MEMBERS_CERTIFICATE: signer.public_pem,
    }
    artifacts: list[dict[str, Any]] = manifest.setdefault("artifacts", [])
    for rel, data in files.items():
        artifacts.append({
            "path": rel,
            "size_bytes": len(data),
            "media_type": _media_type_for(rel),
            "sha256": _sha256_bytes(data),
        })
    artifacts.sort(key=lambda a: str(a.get("path", "")))
    manifest.setdefault("attestations", []).append({
        "path": MEMBERS_ENVELOPE,
        "predicate_type": PREDICATE_BUNDLE_MEMBERS,
        "subject_count": 1,
        "subject_hashes": [members_digest(members)],
        "envelope_format": "DSSE",
        "sha256": _sha256_bytes(files[MEMBERS_ENVELOPE]),
        "signature_refs": [MEMBERS_SIGNATURE],
    })
    manifest.setdefault("signatures", []).append({
        "envelope_path": MEMBERS_ENVELOPE,
        "signature_path": MEMBERS_SIGNATURE,
        "certificate_path": MEMBERS_CERTIFICATE,
        "signer_identity": "local-key",
        "signature_algorithm": "ed25519",
        "signed_at": _now_iso(),
    })
    manifest["manifest_hash"] = _canonical_manifest_hash(manifest)
    return files


def reseal_bundle_members(bundle_path: Path, signer: LocalSigner) -> None:
    """Re-sign the members statement of a bundle a post-processor just changed.

    For ``nova export-evidence --timestamp``, which adds ``manifest.dsse.tsr`` and a
    README section after the builder signed. The bundle's other attestations must
    verify under *signer*'s key with every recipe step except the members check
    passing, or :class:`BundleResealError` is raised and nothing is written. Every
    other member is re-vouched for as it now is, so call this only on a bundle you
    built. The archive is rewritten through a temporary file and renamed into place.
    """
    from novafabric.evidence.bundle_verify import verify_bundle_archive  # noqa: PLC0415

    with zipfile.ZipFile(bundle_path) as zf:
        outcome = verify_bundle_archive(zf, trusted_public_pems=[signer.public_pem])
        if not outcome.signed_content_ok:
            raise BundleResealError(
                "refusing to re-sign the members statement: "
                + "; ".join(outcome.problems()[:5])
            )
        data = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}
    manifest = json.loads(data.pop("manifest.json"))
    for name in MEMBERS_STATEMENT_FILES:
        data.pop(name, None)
    files = sign_members_statement(
        manifest, {n: _sha256_bytes(b) for n, b in data.items()}, signer
    )
    data.update(files)
    tmp = bundle_path.with_name(f".{bundle_path.name}.reseal.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            zout.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            for name in sorted(data):
                zout.writestr(name, data[name])
        tmp.replace(bundle_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class EvidenceBundleBuilder:
    """Assembles an Evidence Bundle ZIP per ADR-0011 (local-key mode)."""

    #: Sign the members statement (ADR-0011 Amendment 7). Only tests turn this off,
    #: to produce a bundle as builders before the statement existed wrote it.
    _SIGN_MEMBERS: bool = True

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
        self._audit_log_path = audit_log_path or resolve_audit_log_path()

    def _validate_capsule(self, capsule_dir: Path | None = None) -> None:
        """Structural + redaction validation for one capsule.

        Takes the directory explicitly so a capsule-set export can validate
        **every** capsule before anything is written. Defaults to this builder's
        own capsule, so the single-capsule path is unchanged.
        """
        capsule_dir = capsule_dir or self._capsule_dir
        # First: nothing below may read through a link the capsule holds.
        _refuse_symlinks(capsule_dir, capsule_dir.name)
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

        proof_data = json.loads(_capsule_text(capsule_dir, "redaction-proof.json"))
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
            self._seal_members(staging, manifest)
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True)
            )
            self._zip_staging(staging)
        return self._output_path

    def _seal_members(self, staging: Path, manifest: dict[str, Any]) -> None:
        """Sign every staged member's digest into the members statement (Amendment 7).

        Runs after the manifest is built, whose ``artifacts[]`` lists every staged
        file with its digest, and writes the statement's three files into *staging*.
        """
        if not self._SIGN_MEMBERS:
            return
        digests = {str(a["path"]): str(a["sha256"]) for a in manifest["artifacts"]}
        files = sign_members_statement(manifest, digests, self._signer)
        for rel, data in files.items():
            (staging / rel).write_bytes(data)

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
                # ADR-0126 P3: lets a gate condition on the recorded environment
                # (e.g. production-only requirements); None when not recorded.
                deployment_environment=deployment_environment_from_capsule(self._capsule_dir),
            ),
        )
        decision = engine.evaluate(inp)
        # The caller's audit log (resolved at construction when none was given).
        # This used to call resolve_audit_log_path() again, so a caller that passed
        # its own path had the bundle's audit section read one log while the policy
        # decision was appended to another — the global one (review r6).
        audit_path = self._audit_log_path
        try:
            AuditLog(audit_path).append(
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
        except OSError as exc:
            raise PolicyAuditUnavailableError(
                f"cannot record the export-policy decision in the audit log at "
                f"{audit_path}: {exc}"
            ) from exc
        if not decision.allow:
            raise PolicyDeniedError(decision.reason, decision.decision_id)

    def _stage_capsule_copy(
        self, staging: Path, capsule_dir: Path | None = None, dest_rel: str = "run-capsule"
    ) -> None:
        dest = staging / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        source = (capsule_dir or self._capsule_dir).resolve()
        # Never shutil.copytree: even with symlinks=True it re-resolves each path
        # between its link check and the copy, so a file swapped for a link in
        # that window was copied *through* the link. copy_capsule_tree opens every
        # file with O_NOFOLLOW on each path component and re-checks the copy, so
        # a link that appeared after _validate_capsule is refused, never followed.
        copy_capsule_tree(source, dest, operation=_OPERATION, error=CapsuleSymlinkError)

    def _stage_lineage_subgraph(
        self,
        staging: Path,
        capsule_dir: Path | None = None,
        dest_rel: str = "lineage-subgraph",
    ) -> None:
        dest = staging / dest_rel
        dest.mkdir(parents=True, exist_ok=True)
        src = (capsule_dir or self._capsule_dir) / "lineage.jsonl"
        edges = _read_regular_file(src) if src.exists() or src.is_symlink() else b""
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
        capsule_hash = _capsule_hash(capsule_dir)

        manifest_yaml = yaml.safe_load(_capsule_text(capsule_dir, "capsule.yaml"))
        run_id = manifest_yaml["run_id"]

        proof = json.loads(_capsule_text(capsule_dir, "redaction-proof.json"))

        lineage_src = staging / lineage_rel / "edges.jsonl"
        lineage_hash = _sha256_bytes(lineage_src.read_bytes())

        run_predicate: dict[str, Any] = {
            "run_id": run_id,
            "capsule_hash": capsule_hash,
            "captured_by": {"name": "novafabric", "version": NF_VERSION},
        }
        if self._SIGN_MEMBERS:
            # Signed pointer: a verifier that finds it fails a bundle whose members
            # statement was stripped, rather than treating it as a legacy bundle.
            run_predicate["members_statement"] = MEMBERS_ENVELOPE
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
        if energy_src.exists() or energy_src.is_symlink():
            energy_bytes = _read_regular_file(energy_src)
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
        manifest_yaml = yaml.safe_load(_capsule_text(self._capsule_dir, "capsule.yaml"))
        run_id = manifest_yaml["run_id"]
        capsule_hash = _capsule_hash(self._capsule_dir)

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
        """Zip *staging* to the output path: a fresh file renamed into place.

        The archive is written to a new ``O_EXCL | O_NOFOLLOW`` temporary file
        beside the output and renamed over it, so a link or hard link already at
        the output name is replaced, never written through.
        """
        out = self._output_path
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=out.parent, prefix=f".{out.name}.", suffix=".tmp", delete=False
        ) as handle:
            tmp = Path(handle.name)
            try:
                with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as zf:
                    for path in sorted(staging.rglob("*")):
                        if path.is_file():
                            zf.write(path, path.relative_to(staging).as_posix())
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o644)
            except BaseException:
                handle.close()
                tmp.unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp, out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        fsync_dir(out.parent)


def _set_member_run_ids(capsule_dirs: Sequence[Path]) -> list[str]:
    """Each capsule's ``run_id``, refused unless it is one safe, unique path segment.

    A capsule-set bundle stages every member under ``run-capsule/<run_id>/``,
    ``lineage-subgraph/<run_id>/`` and ``attestations/<run_id>/``. The
    ``run_id`` comes from the capsule's own ``capsule.yaml``, which the workload
    wrote, so a ``../`` value once wrote capsule files and signatures outside
    the staging directory (review trust-evidence-02). Two members with one
    ``run_id`` would also overwrite each other.

    Raises:
        CapsuleValidationError: a ``run_id`` is missing, unsafe or repeated.
    """
    run_ids: list[str] = []
    for capsule_dir in capsule_dirs:
        raw = yaml.safe_load(_capsule_text(capsule_dir, "capsule.yaml")).get("run_id")
        try:
            run_id = require_path_segment(str(raw) if raw is not None else "", "run_id")
        except UnsafePathSegmentError as exc:
            raise CapsuleValidationError(f"{capsule_dir}: {exc}") from exc
        if run_id in run_ids:
            raise CapsuleValidationError(
                f"{capsule_dir}: run_id {run_id!r} appears twice in the capsule set"
            )
        run_ids.append(run_id)
    return run_ids


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
        run_ids = _set_member_run_ids(self._capsule_dirs)

        self._run_policy_gate()

        with tempfile.TemporaryDirectory() as raw_tmp:
            staging = Path(raw_tmp) / "bundle"
            staging.mkdir()

            attestations: list[dict[str, Any]] = []
            signatures: list[dict[str, Any]] = []
            subjects: list[dict[str, Any]] = []

            for capsule_dir, run_id in zip(self._capsule_dirs, run_ids, strict=True):
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
                    "capsule_hash": _capsule_hash(capsule_dir),
                    "capsule_path_in_bundle": f"{capsule_rel}/",
                })

            self._stage_schemas(staging)
            self._stage_readme(staging)
            if self._curation is not None:
                (staging / CURATION_FILENAME).write_text(
                    json.dumps(self._curation, indent=2, sort_keys=True)
                )

            manifest = self._build_set_manifest(staging, subjects, attestations, signatures)
            self._seal_members(staging, manifest)
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
            # `subject` is oneOf[Subject, array minItems:2]: two or more take the
            # array form; a curated ONE-capsule bundle (the curation record makes
            # it reach here) takes the single-object form. A 1-element array
            # matches neither arm of the schema.
            "subject": subjects if len(subjects) >= 2 else subjects[0],
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
