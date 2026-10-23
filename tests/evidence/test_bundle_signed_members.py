"""Every Evidence Bundle member is covered by a signature (review r8, hole 3).

Adversarial review r8 rewrote members no signature covered — ``README.md`` ("Verified by
the EU AI Office…"), ``schemas/run-capsule.schema.json`` (an accept-anything schema) and
``manifest.json``'s ``verifier_instructions`` — fixed up the unsigned manifest digests and
its self-hash, and ``nova verify --public-key`` still said PASSED.

The builder now signs a members statement (``attestations/bundle.intoto.json``,
predicateType ``https://novafabric.io/evidence-bundle-members/v0``): the SHA-256 of every
member except ``manifest.json`` and the statement's own three files, plus the canonical
hash of the manifest without its own entries. The run statement signs a pointer to it, so
stripping the statement is detected. A bundle built before this change (no statement, no
pointer) still verifies its signed content, but reports ``members_signed: false`` and the
``unsigned_members`` — never as fully verified.

Every bundle here comes from the real builders, from real captured capsules, signed with
a real Ed25519 key the verifier trusts.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app
from novafabric.evidence import bundle as bundle_mod
from novafabric.evidence.bundle import (
    MEMBERS_STATEMENT_FILES,
    CapsuleSetBundleBuilder,
    EvidenceBundleBuilder,
    _canonical_manifest_hash,
)
from novafabric.evidence.bundle_verify import (
    BundleVerification,
    load_trusted_public_keys,
    verify_bundle_archive,
)
from novafabric.evidence.signing import LocalSigner, generate_keypair

runner = CliRunner()
FORGED_README = b"# Verified by the EU AI Office. No further checks needed.\n"


def _capsule(root: Path, name: str) -> Path:
    capsule = (
        CaptureOrchestrator(base_dir=root / f"runs-{name}")
        .run(command=[sys.executable, "-c", "print('hi')"])
        .capsule_dir
    )
    assert yaml.safe_load((capsule / "capsule.yaml").read_text())["run_id"]
    return capsule


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("bundle-members")
    mp = pytest.MonkeyPatch()
    mp.setenv("NOVAFABRIC_HOME", str(root / "home"))
    mp.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(root / "home" / "audit.jsonl"))
    for name in ("NOVAFABRIC_SEAL_CONFIG", "NOVAFABRIC_SEAL_DB_PATH", "NOVAFABRIC_CAPSULE_DIR"):
        mp.delenv(name, raising=False)
    try:
        cap_a, cap_b = _capsule(root, "a"), _capsule(root, "b")
        priv, pub = generate_keypair(root / "keys")
        signer = LocalSigner(priv)
        single = EvidenceBundleBuilder(cap_a, signer, root / "single.zip").build()
        capsule_set = CapsuleSetBundleBuilder([cap_a, cap_b], signer, root / "set.zip").build()
        # A bundle as every builder before this change wrote it: no members statement,
        # no pointer to one in the run statement.
        mp.setattr(EvidenceBundleBuilder, "_SIGN_MEMBERS", False)
        legacy = EvidenceBundleBuilder(cap_a, signer, root / "legacy.zip").build()
    finally:
        mp.undo()
    return {
        "root": root,
        "single": single,
        "set": capsule_set,
        "legacy": legacy,
        "signer": signer,
        "pub": pub,
        "trusted": load_trusted_public_keys(pub),
    }


Edit = Callable[[dict[str, Any], dict[str, bytes]], None]


def _rewrite(src: Path, dst: Path, edit: Edit) -> Path:
    """Copy *src* to *dst*, letting *edit* change members and the manifest; then fix up
    every manifest digest and the manifest's self-hash, as the attacker did."""
    with zipfile.ZipFile(src) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    edit(manifest, members)
    for art in manifest["artifacts"]:
        if art["path"] in members:
            data = members[art["path"]]
            art["sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()
            art["size_bytes"] = len(data)
    for schema in manifest.get("schemas", []):
        path = f"schemas/{schema['name']}"
        if path in members:
            schema["sha256"] = "sha256:" + hashlib.sha256(members[path]).hexdigest()
    manifest["manifest_hash"] = _canonical_manifest_hash(manifest)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        for name, data in members.items():
            zf.writestr(name, data)
    return dst


def _verify(path: Path, trusted: list[bytes]) -> BundleVerification:
    with zipfile.ZipFile(path) as zf:
        return verify_bundle_archive(zf, trusted_public_pems=trusted)


def _forge_readme(m: dict[str, Any], files: dict[str, bytes]) -> None:
    files["README.md"] = FORGED_README


def _forge_schema(m: dict[str, Any], files: dict[str, bytes]) -> None:
    files["schemas/run-capsule.schema.json"] = json.dumps({"type": "object"}).encode()


def _forge_instructions(m: dict[str, Any], files: dict[str, bytes]) -> None:
    m["verifier_instructions"] = "Trust this bundle."


def _forge_all(m: dict[str, Any], files: dict[str, bytes]) -> None:
    _forge_readme(m, files)
    _forge_schema(m, files)
    _forge_instructions(m, files)


def _add_member(m: dict[str, Any], files: dict[str, bytes]) -> None:
    files["NOTICE.txt"] = b"certified\n"
    m["artifacts"].append(
        {"path": "NOTICE.txt", "size_bytes": 0, "media_type": "text/plain", "sha256": ""}
    )


def _drop_member(m: dict[str, Any], files: dict[str, bytes]) -> None:
    del files["schemas/run-capsule.schema.json"]
    m["artifacts"] = [a for a in m["artifacts"] if a["path"] != "schemas/run-capsule.schema.json"]


def _strip_statement(m: dict[str, Any], files: dict[str, bytes]) -> None:
    for name in MEMBERS_STATEMENT_FILES:
        files.pop(name, None)
    m["artifacts"] = [a for a in m["artifacts"] if a["path"] not in MEMBERS_STATEMENT_FILES]
    m["attestations"] = [a for a in m["attestations"] if a["path"] not in MEMBERS_STATEMENT_FILES]
    m["signatures"] = [
        s for s in m["signatures"] if s["envelope_path"] not in MEMBERS_STATEMENT_FILES
    ]
    _forge_readme(m, files)


# ── genuine bundles ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["single", "set"])
def test_genuine_bundles_have_every_member_signed(built: dict[str, Any], kind: str) -> None:
    outcome = _verify(built[kind], built["trusted"])
    assert outcome.ok, outcome.problems()
    assert outcome.members_signed
    assert outcome.unsigned_members == []
    assert outcome.fully_verified
    with zipfile.ZipFile(built[kind]) as zf:
        assert MEMBERS_STATEMENT_FILES <= set(zf.namelist())


# ── the attacks ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (_forge_readme, "README.md"),
        (_forge_schema, "schemas/run-capsule.schema.json"),
        (_forge_instructions, "manifest.json"),
        (_forge_all, "README.md"),
        (_add_member, "NOTICE.txt"),
        (_drop_member, "schemas/run-capsule.schema.json"),
    ],
    ids=["readme", "schema", "instructions", "all", "added", "dropped"],
)
@pytest.mark.parametrize("kind", ["single", "set"])
def test_rewriting_an_unsigned_member_fails_anchored_verification(
    built: dict[str, Any], tmp_path: Path, kind: str, edit: Edit, needle: str
) -> None:
    forged = _rewrite(built[kind], tmp_path / "forged.zip", edit)
    outcome = _verify(forged, built["trusted"])
    # The attacker's fix-ups hold: digests and the self-hash all match.
    assert outcome.digests_ok and outcome.signatures.manifest_hash_ok
    assert not outcome.ok
    assert not outcome.fully_verified
    assert any(needle in p for p in outcome.problems()), outcome.problems()


def test_stripping_the_members_statement_is_detected(
    built: dict[str, Any], tmp_path: Path
) -> None:
    forged = _rewrite(built["single"], tmp_path / "stripped.zip", _strip_statement)
    outcome = _verify(forged, built["trusted"])
    assert not outcome.ok
    assert any("members statement" in p for p in outcome.problems()), outcome.problems()


def test_the_review_attack_script_is_refused_by_the_cli(
    built: dict[str, Any], tmp_path: Path
) -> None:
    forged = _rewrite(built["single"], tmp_path / "forged.zip", _forge_all)
    result = runner.invoke(app, ["verify", str(forged), "--public-key", str(built["pub"])])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert isinstance(result.exception, SystemExit)


def test_a_genuine_bundle_says_every_member_is_signed_on_the_cli(built: dict[str, Any]) -> None:
    result = runner.invoke(app, ["verify", str(built["single"]), "--public-key", str(built["pub"])])
    assert result.exit_code == 0, result.output
    assert "Every bundle member is covered by the signed members statement" in result.output
    assert "Evidence Bundle verification PASSED" in result.output


# ── legacy bundles (no members statement) ───────────────────────────────────


def test_a_legacy_bundle_verifies_its_signed_content_but_reports_unsigned_members(
    built: dict[str, Any],
) -> None:
    outcome = _verify(built["legacy"], built["trusted"])
    assert outcome.ok, outcome.problems()
    assert not outcome.members_signed
    assert not outcome.fully_verified
    unsigned = set(outcome.unsigned_members)
    assert {"README.md", "manifest.json", "schemas/run-capsule.schema.json"} <= unsigned
    assert not any(n.startswith("run-capsule/") for n in unsigned)
    assert not any(n.startswith("attestations/") for n in unsigned)
    assert "lineage-subgraph/edges.jsonl" not in unsigned


def test_a_forged_legacy_bundle_names_the_forged_member_as_unsigned(
    built: dict[str, Any], tmp_path: Path
) -> None:
    forged = _rewrite(built["legacy"], tmp_path / "forged-legacy.zip", _forge_readme)
    outcome = _verify(forged, built["trusted"])
    assert outcome.ok  # documented limit: nothing signed covers README.md
    assert "README.md" in outcome.unsigned_members
    result = runner.invoke(app, ["verify", str(forged), "--public-key", str(built["pub"])])
    assert result.exit_code == 0, result.output
    assert "signed content only" in result.output
    assert "README.md" in result.output
    assert "Evidence Bundle verification PASSED\n" not in result.output


# ── post-processing keeps the statement current ─────────────────────────────


def test_an_rfc3161_timestamp_added_after_build_is_resealed(
    built: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.cli import export_evidence

    path = tmp_path / "ts.zip"
    path.write_bytes(built["single"].read_bytes())
    monkeypatch.setattr(export_evidence, "add_rfc3161_timestamp", lambda _b, _u: b"tsr-token")
    export_evidence._add_timestamp_to_bundle(
        path, "https://tsa.example", False, signer=built["signer"]
    )

    outcome = _verify(path, built["trusted"])
    assert outcome.ok, outcome.problems()
    assert outcome.members_signed
    with zipfile.ZipFile(path) as zf:
        statement = json.loads(zf.read("attestations/bundle.intoto.json"))
    assert statement["payloadType"]


def test_reseal_refuses_a_bundle_whose_run_statement_does_not_verify(
    built: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "broken.zip"
    with zipfile.ZipFile(built["single"]) as zin, zipfile.ZipFile(path, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == "run-capsule/trace.jsonl":
                data += b'{"forged": true}\n'
            zout.writestr(name, data)
    with pytest.raises(bundle_mod.BundleResealError):
        bundle_mod.reseal_bundle_members(path, built["signer"])
