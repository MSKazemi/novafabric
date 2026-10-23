"""L24: the Evidence Bundle README's step 4 says what each statement's subject really is.

Step 4 used to read "Confirm each statement's subject digest matches
`subject.capsule_hash`". Only the run statement's subject is the capsule root; the
redaction, lineage and energy statements sign other digests (``bundle.py`` spec table),
so a third party following the recipe rejected a correct bundle three times out of four.

The test follows the recipe as a third party would — from the bundle's own files, with
no NovaFabric code — and checks the README names the source of every subject.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.energy._attribution import attest_capsule, write_receipts
from novafabric.evidence.bundle import (
    PREDICATE_BUNDLE_MEMBERS,
    PREDICATE_ENERGY,
    PREDICATE_LINEAGE,
    PREDICATE_REDACTION,
    PREDICATE_RUN,
    VERIFIER_INSTRUCTIONS,
    EvidenceBundleBuilder,
)
from novafabric.evidence.signing import LocalSigner, generate_keypair


def _bare(digest: str) -> str:
    return digest.split(":", 1)[1] if ":" in digest else digest


def _bundle(tmp_path: Path) -> Path:
    cap = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "pass"]
    ).capsule_dir
    write_receipts(cap, attest_capsule(cap, rapl_base=tmp_path / "no-rapl", node_id="t"))
    priv, _ = generate_keypair(tmp_path / "keys")
    out = tmp_path / "bundle.zip"
    EvidenceBundleBuilder(cap, LocalSigner(priv), out).build()
    return out


def _expected_subject(zf: zipfile.ZipFile, manifest: dict, statement: dict) -> str:
    """Steps 4 and 6 of the README, implemented from the bundle's files alone."""
    predicate_type = statement["predicateType"]
    if predicate_type == PREDICATE_BUNDLE_MEMBERS:
        members = statement["predicate"]["members"]
        canonical = json.dumps(members, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()
    if predicate_type == PREDICATE_RUN:
        return _bare(manifest["subject"]["capsule_hash"])
    if predicate_type == PREDICATE_REDACTION:
        proof = json.loads(zf.read("run-capsule/redaction-proof.json"))
        return _bare(proof["chain_hash"])
    if predicate_type == PREDICATE_LINEAGE:
        return hashlib.sha256(zf.read("lineage-subgraph/edges.jsonl")).hexdigest()
    if predicate_type == PREDICATE_ENERGY:
        return hashlib.sha256(zf.read("run-capsule/energy-receipts.jsonl")).hexdigest()
    raise AssertionError(f"README step 4 has no rule for {predicate_type}")


def test_every_statement_subject_follows_the_readme_rule(tmp_path: Path) -> None:
    with zipfile.ZipFile(_bundle(tmp_path)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        readme = zf.read("README.md").decode()
        assert readme == VERIFIER_INSTRUCTIONS == manifest["verifier_instructions"]
        seen = set()
        for att in manifest["attestations"]:
            envelope = json.loads(zf.read(att["path"]))
            statement = json.loads(base64.b64decode(envelope["payload"]))
            predicate_type = statement["predicateType"]
            digests = {_bare(s["digest"]["sha256"]) for s in statement["subject"]}
            assert digests == {_expected_subject(zf, manifest, statement)}, predicate_type
            assert digests == {_bare(h) for h in att["subject_hashes"]}
            seen.add(predicate_type)
    assert seen == {
        PREDICATE_RUN, PREDICATE_REDACTION, PREDICATE_LINEAGE, PREDICATE_ENERGY,
        PREDICATE_BUNDLE_MEMBERS,
    }


def test_readme_step4_names_each_subject_source() -> None:
    step4 = VERIFIER_INSTRUCTIONS.split("  4.", 1)[1].split("  5.", 1)[0]
    for predicate_type, source in (
        (PREDICATE_RUN, "subject.capsule_hash"),
        (PREDICATE_REDACTION, "chain_hash"),
        (PREDICATE_LINEAGE, "edges.jsonl"),
        (PREDICATE_ENERGY, "energy-receipts.jsonl"),
    ):
        short = predicate_type.removeprefix("https://novafabric.io/")
        assert short in step4, f"step 4 does not name {short}"
        assert source in step4, f"step 4 does not name {source}"
    assert "subject_hashes" in step4
    # The old, wrong universal claim is gone.
    assert "each statement's subject digest matches" not in VERIFIER_INSTRUCTIONS.replace("\n", " ")
