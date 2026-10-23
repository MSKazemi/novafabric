"""A sealed capsule's manifest cannot be substituted by an unsealed one.

Adversarial review, 2026-10-23 (attack A1): rename a sealed capsule's
``capsule.yaml`` to ``capsule.json`` and edit it. ``verify_capsule`` treated the
missing ``capsule.yaml`` as NOT PRESENT and the new ``capsule.json`` as merely
"unlisted", so ``nova verify`` exited 0 and the trust radar said ``attested`` —
while the query indexer, the in-toto statement builder and
``GET /api/runs/{id}/children`` read the forged ``capsule.json`` as the manifest.

Every capsule here is sealed for real (NovaSeal, ECDSA, a Merkle log), both through
the shared finalize path (which records the manifest name in the signed payload)
and directly, the way capsules sealed before that record were made.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from novafabric._capsule_manifest import read_capsule_manifest
from novafabric.capture.finalize import evidence_digests, seal_capsule
from novafabric.cli.main import app as cli_app
from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.novaseal.capsule_verify import verify_capsule

RUN_ID = "01HMANIFESTSUBSTITUTION000"
TOKEN = "manifest-substitution-token"
HOST = {"host": "127.0.0.1:4321"}
runner = CliRunner()


def _identity(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "manifest-substitution")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _write_evidence(capsule: Path) -> None:
    (capsule / "outputs").mkdir(parents=True)
    (capsule / "outputs" / "stdout.txt").write_text("hello\n")
    (capsule / "model-calls.jsonl").write_text('{"model":"gpt-4o","tokens_in":7}\n')
    (capsule / "env.lock").write_text("python=3.13\n")


def _manifest(*, digests: bool, capsule: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "run_id": RUN_ID,
        "status": "success",
        "created_at": "2026-10-20T00:00:00Z",
        "model_calls_ref": "model-calls.jsonl",
        "environment_ref": "env.lock",
        # Named by the signed manifest, absent when it was sealed.
        "tool_calls_ref": "tool-calls.jsonl",
    }
    if digests:
        manifest["evidence_digests"] = evidence_digests(capsule)
    return manifest


def _seal_directly(capsule: Path, manifest: dict[str, Any], cfg: dict[str, str]) -> None:
    """Seal the way capsules were sealed before the payload named its manifest."""
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=cfg["key_path"], cert_path=cfg["cert_path"]),
        tsa_url="",
        db_path=cfg["merkle_db"],
    )
    bundle = seal.seal(manifest)
    seal.__dict__["_merkle"].close()
    seal_dir = capsule / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(home / "audit.jsonl"))
    for name in ("NOVAFABRIC_SEAL_DB_PATH", "NOVAFABRIC_CAPSULE_DIR", "NOVAFABRIC_DB_PATH"):
        monkeypatch.delenv(name, raising=False)
    key_path, cert_path = _identity(tmp_path)
    cfg = {
        "key_path": str(key_path),
        "cert_path": str(cert_path),
        "merkle_db": str(tmp_path / "merkle.db"),
    }
    config = tmp_path / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {cfg['merkle_db']}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    runs = tmp_path / "runs"
    runs.mkdir()
    return {"tmp": tmp_path, "runs": runs, "cfg": cfg, "config": config}


@pytest.fixture(params=["finalize", "direct"])
def sealed(request: pytest.FixtureRequest, env: dict[str, Any]) -> Path:
    """A sealed capsule — through ``seal_capsule`` (records the manifest name) or directly."""
    capsule = env["runs"] / RUN_ID
    _write_evidence(capsule)
    manifest = _manifest(digests=True, capsule=capsule)
    (capsule / "capsule.yaml").write_text(yaml.dump(manifest, allow_unicode=True))
    if request.param == "finalize":
        assert seal_capsule(capsule, manifest) is None
    else:
        _seal_directly(capsule, manifest, env["cfg"])
    return capsule


@pytest.fixture
def legacy_sealed(env: dict[str, Any]) -> Path:
    """Sealed before ADR-0251: no ``evidence_digests``, no recorded manifest name."""
    capsule = env["runs"] / RUN_ID
    _write_evidence(capsule)
    manifest = _manifest(digests=False, capsule=capsule)
    (capsule / "capsule.yaml").write_text(yaml.dump(manifest, allow_unicode=True))
    _seal_directly(capsule, manifest, env["cfg"])
    return capsule


def _forge_as_json(capsule: Path, *, keep_yaml: bool = False) -> None:
    forged = yaml.safe_load((capsule / "capsule.yaml").read_text())
    forged["status"] = "FORGED-success"
    forged["asset"] = "prod-credit-model"
    if not keep_yaml:
        (capsule / "capsule.yaml").unlink()
    (capsule / "capsule.json").write_text(json.dumps(forged))


def _cli_verify(capsule: Path, config: Path) -> Any:
    return runner.invoke(cli_app, ["verify", str(capsule), "--seal-config", str(config)])


def _radar(capsule: Path) -> dict[str, Any]:
    from novafabric.trust.capsule_flags import flags_from_capsule

    return flags_from_capsule(capsule)


def _client(env: dict[str, Any]) -> TestClient:
    from novafabric.serve.app import create_app

    return TestClient(
        create_app(token=TOKEN, capsule_dir=env["runs"], db_path=env["tmp"] / "r.db"),
        raise_server_exceptions=False,
    )


# ── genuine capsules keep verifying ─────────────────────────────────────────


def test_a_genuine_sealed_capsule_verifies_on_every_surface(
    sealed: Path, env: dict[str, Any]
) -> None:
    report = verify_capsule(sealed, None)
    assert report.valid, report.all_errors()
    assert report.binding.unsealed_manifests == ()
    assert _cli_verify(sealed, env["config"]).exit_code == 0
    assert _radar(sealed)["valid"] is True
    body = _client(env).post(
        f"/api/runs/{RUN_ID}/verify", params={"token": TOKEN}, headers=HOST
    ).json()
    assert body["valid"] is True, body


def test_the_finalize_path_records_which_manifest_it_sealed(env: dict[str, Any]) -> None:
    from novafabric._capsule_manifest import sealed_manifest_name, signed_payload

    capsule = env["runs"] / RUN_ID
    _write_evidence(capsule)
    manifest = _manifest(digests=True, capsule=capsule)
    (capsule / "capsule.yaml").write_text(yaml.dump(manifest, allow_unicode=True))
    assert seal_capsule(capsule, manifest) is None
    payload = signed_payload((capsule / ".seal" / "manifest.dsse").read_bytes())
    assert payload["_novaseal"]["manifest"] == "capsule.yaml"
    assert sealed_manifest_name(payload) == "capsule.yaml"


def test_a_post_seal_derived_artifact_is_still_reported_not_failed(sealed: Path) -> None:
    (sealed / "c2pa-manifest.json").write_text('{"c2pa.ai.generated": true}')
    report = verify_capsule(sealed, None)
    assert report.valid, report.all_errors()
    assert report.binding.unlisted == ("c2pa-manifest.json",)


def test_an_identical_json_copy_of_the_signed_manifest_is_not_a_failure(sealed: Path) -> None:
    signed = read_capsule_manifest(sealed)
    assert signed is not None
    (sealed / "capsule.json").write_text(json.dumps(signed.data))
    report = verify_capsule(sealed, None)
    assert report.valid, report.all_errors()


# ── the attack ──────────────────────────────────────────────────────────────


def test_renaming_the_manifest_to_capsule_json_and_editing_it_fails(
    sealed: Path, env: dict[str, Any]
) -> None:
    _forge_as_json(sealed)
    report = verify_capsule(sealed, None)
    assert not report.valid
    assert not report.seal_binds_capsule
    assert report.binding.unsealed_manifests == ("capsule.json",)
    errors = "\n".join(report.all_errors())
    assert "capsule.yaml is missing" in errors
    assert "capsule.json" in errors and "status" in errors

    result = _cli_verify(sealed, env["config"])
    assert result.exit_code == 1, result.output
    assert "capsule.json" in result.output

    flags = _radar(sealed)
    assert flags["valid"] is False
    assert flags["signature_ok"] is False
    assert flags["capsule_binding_ok"] is False


def test_a_forged_capsule_json_beside_the_genuine_yaml_fails(sealed: Path) -> None:
    _forge_as_json(sealed, keep_yaml=True)
    report = verify_capsule(sealed, None)
    assert not report.valid
    assert report.binding.manifest_ok  # capsule.yaml itself is untouched
    assert report.binding.unsealed_manifests == ("capsule.json",)


def test_deleting_the_sealed_manifest_fails(sealed: Path) -> None:
    (sealed / "capsule.yaml").unlink()
    report = verify_capsule(sealed, None)
    assert not report.valid
    assert "capsule.yaml is missing" in "\n".join(report.all_errors())


def test_an_unsealed_file_under_a_signed_ref_fails(sealed: Path) -> None:
    """The signed manifest claims ``tool-calls.jsonl``; one added after sealing is not it."""
    (sealed / "tool-calls.jsonl").write_text('{"tool":"wire_transfer","amount":1e6}\n')
    report = verify_capsule(sealed, None)
    assert not report.valid
    assert report.binding.unsealed_refs == ("tool-calls.jsonl",)
    assert "tool_calls_ref" in "\n".join(report.all_errors())


# ── capsules sealed before ADR-0251 ─────────────────────────────────────────


def test_a_pre_adr0251_capsule_without_a_manifest_file_is_not_present_not_failed(
    legacy_sealed: Path,
) -> None:
    (legacy_sealed / "capsule.yaml").unlink()
    report = verify_capsule(legacy_sealed, None)
    assert report.valid, report.all_errors()
    assert not report.binding.manifest_present


def test_a_pre_adr0251_capsule_with_a_forged_capsule_json_fails(legacy_sealed: Path) -> None:
    _forge_as_json(legacy_sealed)
    report = verify_capsule(legacy_sealed, None)
    assert not report.valid
    # The seal names no file, so the one manifest present is the one compared.
    assert report.binding.manifest_name == "capsule.json"
    assert not report.binding.manifest_ok
    assert "status" in report.binding.differing_keys


def test_a_pre_adr0251_capsule_with_both_manifests_fails_on_the_forged_one(
    legacy_sealed: Path,
) -> None:
    _forge_as_json(legacy_sealed, keep_yaml=True)
    report = verify_capsule(legacy_sealed, None)
    assert not report.valid
    assert report.binding.manifest_ok
    assert report.binding.unsealed_manifests == ("capsule.json",)


def test_a_capsule_sealed_over_capsule_json_binds_that_file(env: dict[str, Any]) -> None:
    capsule = env["runs"] / RUN_ID
    _write_evidence(capsule)
    manifest = _manifest(digests=True, capsule=capsule)
    (capsule / "capsule.json").write_text(json.dumps(manifest))
    _seal_directly(capsule, {**manifest, "_novaseal": {"manifest": "capsule.json"}}, env["cfg"])
    report = verify_capsule(capsule, None)
    assert report.valid, report.all_errors()
    assert report.binding.manifest_name == "capsule.json"
    assert report.binding.unlisted == ()
    (capsule / "capsule.json").unlink()
    report = verify_capsule(capsule, None)
    assert not report.valid
    assert "capsule.json is missing" in "\n".join(report.all_errors())


# ── consumers read the sealed manifest, never the substitute ───────────────


def test_the_query_indexer_serves_the_sealed_status(sealed: Path) -> None:
    from novafabric.query.indexer import find_capsule, scan_capsule

    _forge_as_json(sealed)
    scanned = scan_capsule(sealed)
    assert scanned is not None
    calls, _ = scanned
    assert {row.status for row in calls} == {"success"}
    assert {row.asset for row in calls} == {None}
    assert find_capsule(sealed.parent, RUN_ID) == sealed


def test_the_indexer_change_signature_covers_the_sealed_manifest() -> None:
    from novafabric.query.indexer import INDEXED_FILENAMES

    assert ".seal/manifest.dsse" in INDEXED_FILENAMES


def test_the_intoto_statement_refuses_a_capsule_with_an_unsealed_manifest(
    sealed: Path,
) -> None:
    from novafabric.envelopes.intoto import (
        UnsealedManifestError,
        capsule_statement,
        capsule_subjects,
    )

    statement = capsule_statement(sealed)
    assert statement["predicate"]["runId"] == RUN_ID
    _forge_as_json(sealed)
    with pytest.raises(UnsealedManifestError, match="capsule.json"):
        capsule_subjects(sealed)
    with pytest.raises(UnsealedManifestError):
        capsule_statement(sealed)


def test_the_agent_graph_takes_its_run_id_from_the_sealed_manifest(sealed: Path) -> None:
    from novafabric.agent_graph.builder import _capsule_id

    manifest = yaml.safe_load((sealed / "capsule.yaml").read_text())
    manifest["run_id"] = "victim-run"
    (sealed / "capsule.yaml").unlink()
    (sealed / "capsule.json").write_text(json.dumps(manifest))
    assert _capsule_id(sealed) == RUN_ID


def test_serve_children_and_run_verify_do_not_trust_the_substitute(
    sealed: Path, env: dict[str, Any]
) -> None:
    _forge_as_json(sealed)
    client = _client(env)
    children = client.get(
        f"/api/runs/{RUN_ID}/children", params={"token": TOKEN}, headers=HOST
    )
    assert children.status_code == 200, children.text
    assert children.json()["status"] == "success"
    verdict = client.post(
        f"/api/runs/{RUN_ID}/verify", params={"token": TOKEN}, headers=HOST
    ).json()
    assert verdict["valid"] is False
    assert verdict["capsule_binding_ok"] is False
