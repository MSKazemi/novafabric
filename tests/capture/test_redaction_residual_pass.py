"""ADR-0009 final residual pass, the manifest gate, and validate/verify end to end.

The main scan runs before the capsule is finished: ``lineage.jsonl``,
``replay.yaml`` and the C2PA marker are written after it, ADR-0135 maskers rewrite
files after it, and ``capsule.yaml`` gains its ``evidence_digests`` map last. Each of
those was a place a secret could reach a sealed capsule unscanned. These tests plant a
secret at each late point and then search the finished capsule for any fragment of it,
rather than trusting the proof's own counts.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.capture import orchestrator as orch_mod
from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.capture.secrets import (
    OTHER_FILE_KIND,
    ResidualSecretError,
    SecretScannerV0,
    recompute_chain_hash,
)
from novafabric.cli.main import app

SCHEMA = json.loads(
    (
        Path(__file__).parents[2] / "src/novafabric/schemas/secret-redaction.schema.json"
    ).read_text()
)
RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"

# Assembled at runtime: no contiguous provider-shaped token in the source bytes.
GITHUB_TOKEN = "ghp" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp"
AWS_KEY_ID = "AKIA" + "QZ7XK2M4PZT3W6RN"
AWS_SECRET = ("Ab3dEf6hIj/+" * 4)[:40]
ANTHROPIC_KEY = "sk-ant-api03-" + "Ab3dEf6hIj" * 9 + "xyzAA"


def _assert_absent(capsule_dir: Path, secret: str) -> None:
    """No file anywhere in the capsule holds the secret -- or any 16-char piece of it."""
    needles = {secret.encode()} | {
        secret[i : i + 16].encode() for i in range(0, max(1, len(secret) - 16), 4)
    }
    for path in capsule_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            for needle in needles:
                assert needle not in data, f"secret fragment left in {path.relative_to(capsule_dir)}"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate(proof: dict[str, Any]) -> None:
    jsonschema.validate(proof, SCHEMA, format_checker=jsonschema.FormatChecker())
    assert recompute_chain_hash(dict(proof))["chain_hash"] == proof["chain_hash"]


# ── scanner level: residual_scan ─────────────────────────────────────────────


def test_residual_pass_on_a_clean_capsule_changes_nothing_but_records_itself(
    tmp_path: Path,
) -> None:
    (tmp_path / "trace.jsonl").write_text('{"span":"root"}\n')
    (tmp_path / "replay.yaml").write_text("mode: forensic\n")
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)

    proof = scanner.residual_scan(scanner.scan_and_redact())

    _validate(proof)
    assert proof["findings"] == []
    rc = proof["residual_check"]
    assert rc["files_rescanned"] == 2
    assert rc["residual_findings"] == 0 and rc["residual_refs"] == []
    assert rc["reconciled_refs"] == []
    # a file no list names is still scanned -- and named in the proof
    replay = next(t for t in proof["targets"] if t["ref"] == "replay.yaml")
    assert replay["kind"] == OTHER_FILE_KIND
    assert replay["hash_after_redaction"] == _sha((tmp_path / "replay.yaml").read_bytes())


def test_secret_written_after_the_main_scan_is_redacted_and_recorded(tmp_path: Path) -> None:
    out = tmp_path / "outputs" / "stdout.txt"
    out.parent.mkdir()
    out.write_text("hello\n")
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)
    proof = scanner.scan_and_redact()
    first = next(t for t in proof["targets"] if t["ref"] == "outputs/stdout.txt")
    original_hash = first["hash_before_redaction"]

    # a late writer appends a secret, and drops a brand-new file
    out.write_text(f"hello\ntoken={GITHUB_TOKEN}\n")
    (tmp_path / "c2pa-manifest.json").write_text(json.dumps({"note": AWS_KEY_ID}))

    proof = scanner.residual_scan(proof)

    _validate(proof)
    _assert_absent(tmp_path, GITHUB_TOKEN)
    _assert_absent(tmp_path, AWS_KEY_ID)
    entry = next(t for t in proof["targets"] if t["ref"] == "outputs/stdout.txt")
    # before/after semantics preserved: before = the ORIGINAL bytes, after = on disk
    assert entry["hash_before_redaction"] == original_hash
    assert entry["hash_after_redaction"] == _sha(out.read_bytes())
    assert entry["findings_count"] == 1
    c2pa = next(t for t in proof["targets"] if t["ref"] == "c2pa-manifest.json")
    assert c2pa["kind"] == OTHER_FILE_KIND and c2pa["findings_count"] == 1
    rc = proof["residual_check"]
    assert rc["residual_findings"] == 2 == proof["findings_count"]["total"]
    assert sorted(rc["residual_refs"]) == ["c2pa-manifest.json", "outputs/stdout.txt"]
    rule_ids = {f["rule_id"] for f in proof["findings"]}
    assert rule_ids == {"github-token", "aws-access-key-id"}
    gh = next(f for f in proof["findings"] if f["rule_id"] == "github-token")
    assert gh["match_hash"] == _sha(GITHUB_TOKEN.encode())  # ADR-0009 semantics unchanged


def test_clean_rewrite_after_the_scan_is_reconciled_not_hidden(tmp_path: Path) -> None:
    """A file rewritten after its scan (an ADR-0135 masker does this) must not leave a
    stale after-hash in the proof: evidence_digests would then disagree with it."""
    out = tmp_path / "outputs" / "stdout.txt"
    out.parent.mkdir()
    out.write_text("alice@example.com\n")
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)
    proof = scanner.scan_and_redact()
    out.write_text("[MASKED:email]\n")

    proof = scanner.residual_scan(proof)

    _validate(proof)
    assert proof["residual_check"]["reconciled_refs"] == ["outputs/stdout.txt"]
    entry = next(t for t in proof["targets"] if t["ref"] == "outputs/stdout.txt")
    assert entry["hash_after_redaction"] == _sha(out.read_bytes())
    assert entry["findings_count"] == 0


def test_residual_pass_skips_the_manifest_the_proof_and_the_seal(tmp_path: Path) -> None:
    (tmp_path / "capsule.yaml").write_text(f"command: [{ANTHROPIC_KEY}]\n")
    (tmp_path / "redaction-proof.json").write_text("{}")
    (tmp_path / ".seal").mkdir()
    (tmp_path / ".seal" / "log-entry.json").write_text(json.dumps({"k": ANTHROPIC_KEY}))
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)

    proof = scanner.residual_scan(scanner.scan_and_redact())

    assert proof["residual_check"]["files_rescanned"] == 0
    refs = {t["ref"] for t in proof["targets"]}
    assert not refs & {"redaction-proof.json", ".seal/log-entry.json"}


def test_secret_in_a_file_name_drops_the_file_and_never_reaches_the_proof(
    tmp_path: Path,
) -> None:
    named = tmp_path / "outputs" / f"dump-{GITHUB_TOKEN}.txt"
    named.parent.mkdir()
    named.write_text("harmless body\n")
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)

    proof = scanner.residual_scan(scanner.scan_and_redact())

    _validate(proof)
    assert not named.exists()
    assert GITHUB_TOKEN not in json.dumps(proof)
    entry = next(t for t in proof["targets"] if "dump-" in t["ref"])
    assert entry["ref"] == "outputs/dump-[REDACTED:github-token].txt"
    assert entry["hash_after_redaction"] == _sha(b"")
    (finding,) = proof["findings"]
    assert finding["redaction_strategy"] == "drop"
    assert finding["target_ref"] == entry["ref"]
    assert finding["match_hash"] == _sha(GITHUB_TOKEN.encode())


def test_content_addressed_file_name_is_not_a_secret(tmp_path: Path) -> None:
    """Only anchored rules judge a name: `outputs/media/<64-hex>` must survive."""
    blob = tmp_path / "outputs" / "media" / ("9f" * 32 + ".png")
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)

    proof = scanner.residual_scan(scanner.scan_and_redact())

    assert blob.exists()
    assert proof["findings"] == []


def test_hex_file_name_is_not_masked_in_the_proof(tmp_path: Path) -> None:
    """The proof must keep naming the file evidence_digests names -- a generic rule
    (bare 64-hex) must not rewrite a ref, only the anchored rules may."""
    name = "outputs/cache/" + "ab" * 32 + ".txt"
    (tmp_path / name).parent.mkdir(parents=True)
    (tmp_path / name).write_text("ok\n")
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)

    proof = scanner.residual_scan(scanner.scan_and_redact())

    assert name in {t["ref"] for t in proof["targets"]}
    assert proof["residual_check"]["proof_strings_redacted"] == 0


def test_secret_in_the_proofs_own_fields_is_masked(tmp_path: Path) -> None:
    """The proof is written into the capsule too; an ADR-0135 masker replacement
    recorded in it gets the same rules."""
    (tmp_path / "trace.jsonl").write_text("{}\n")
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)
    proof = scanner.scan_and_redact()
    proof["masker_findings"] = [{"replacement": f"x {GITHUB_TOKEN}"}]

    proof = scanner.residual_scan(proof)

    assert GITHUB_TOKEN not in json.dumps(proof)
    assert proof["masker_findings"][0]["replacement"] == "x [REDACTED:github-token]"
    assert proof["residual_check"]["proof_strings_redacted"] == 1


# ── the manifest gate ────────────────────────────────────────────────────────


def test_manifest_gate_returns_the_exact_text_it_checked(tmp_path: Path) -> None:
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)
    manifest = {"run_id": RUN_ID, "evidence_digests": {"trace.jsonl": {"sha256": _sha(b"x")}}}
    assert scanner.assert_manifest_clean(manifest) == yaml.dump(manifest, allow_unicode=True)


@pytest.mark.parametrize(
    "manifest",
    [
        {"evidence_digests": {f"outputs/{GITHUB_TOKEN}.txt": {"size_bytes": 1}}},  # a key
        {"command": ["deploy", AWS_KEY_ID]},  # a value
        {"note": "x" * 70 + " " + ANTHROPIC_KEY},  # long scalar YAML may fold
    ],
)
def test_manifest_gate_refuses_a_residual_and_never_echoes_it(
    tmp_path: Path, manifest: dict[str, Any]
) -> None:
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)
    with pytest.raises(ResidualSecretError) as exc:
        scanner.assert_manifest_clean(manifest)
    message = str(exc.value)
    assert exc.value.ref == "capsule.yaml" and exc.value.rule_ids
    for secret in (GITHUB_TOKEN, AWS_KEY_ID, ANTHROPIC_KEY):
        assert secret not in message


def test_redact_manifest_redacts_keys_it_reports(tmp_path: Path) -> None:
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)
    redacted, target, findings = scanner.redact_manifest({GITHUB_TOKEN: {"a": 1}})
    assert list(redacted) == ["[REDACTED:github-token]"]
    assert target["findings_count"] == 1 == len(findings)


# ── end to end through the orchestrator ──────────────────────────────────────


def _digest_map_matches_disk(capsule: Path, manifest: dict[str, Any]) -> None:
    for rel, entry in manifest["evidence_digests"].items():
        assert entry["sha256"] == _sha((capsule / rel).read_bytes()), rel


def test_late_written_file_is_caught_before_digests_bind_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """replay.yaml is written after the main scan; a secret in it must not survive."""
    real = orch_mod.minimal_replay_policy

    def _leaky_policy() -> dict[str, Any]:
        policy = dict(real())
        policy["note"] = f"token {GITHUB_TOKEN}"
        return policy

    monkeypatch.setattr(orch_mod, "minimal_replay_policy", _leaky_policy)

    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "print('ok')"]
    )
    capsule = result.capsule_dir

    _assert_absent(capsule, GITHUB_TOKEN)
    proof = json.loads((capsule / "redaction-proof.json").read_text())
    _validate(proof)
    assert proof["residual_check"]["residual_refs"] == ["replay.yaml"]
    assert any(
        f["target_ref"] == "replay.yaml" and f["rule_id"] == "github-token"
        for f in proof["findings"]
    )
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    _digest_map_matches_disk(capsule, manifest)


def test_every_bound_file_is_a_scanned_target_with_its_final_hash(tmp_path: Path) -> None:
    """evidence_digests binds the exact post-redaction bytes, and the proof names every
    one of those files with the same after-hash -- the two records cannot disagree."""
    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[
            sys.executable,
            "-c",
            "import sys; print('id=' + sys.argv[1]); print(sys.argv[2])",
            AWS_KEY_ID,
            # anchored: a bare 40-char AWS secret is not detectable on its own
            "AWS_SECRET_ACCESS_KEY=" + AWS_SECRET,
        ]
    )
    capsule = result.capsule_dir
    _assert_absent(capsule, AWS_KEY_ID)
    _assert_absent(capsule, AWS_SECRET)

    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    proof = json.loads((capsule / "redaction-proof.json").read_text())
    _validate(proof)
    _digest_map_matches_disk(capsule, manifest)
    after = {t["ref"]: t["hash_after_redaction"] for t in proof["targets"]}
    for rel, entry in manifest["evidence_digests"].items():
        if rel == "redaction-proof.json":
            continue
        assert rel in after, f"{rel} is bound by the seal but absent from the proof"
        assert after[rel] == entry["sha256"], f"proof after-hash for {rel} is stale"
    assert proof["residual_check"]["files_rescanned"] == len(manifest["evidence_digests"]) - 1


def test_capture_health_report_is_scanned_and_bound_by_evidence_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """capture-health.json is evidence (it records fail-open event loss). It must be
    written before the residual pass and the digest map, never after the seal --
    otherwise it is the one capsule file that is neither scanned nor bound."""
    from novafabric.capture.event_recorder import EventRecorder

    monkeypatch.setattr(
        EventRecorder, "drop_counts", property(lambda self: {"trace.jsonl": 2})
    )
    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "print('ok')"]
    )
    capsule = result.capsule_dir
    health = capsule / "capture-health.json"
    assert health.is_file()
    assert json.loads(health.read_text())["dropped_events"] == {"trace.jsonl": 2}

    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert "capture-health.json" in manifest["evidence_digests"]
    _digest_map_matches_disk(capsule, manifest)
    proof = json.loads((capsule / "redaction-proof.json").read_text())
    _validate(proof)
    after = {t["ref"]: t["hash_after_redaction"] for t in proof["targets"]}
    assert after["capture-health.json"] == _sha(health.read_bytes())


def test_drop_after_the_digests_is_logged_not_lost_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A drop after the digest map cannot enter the bound report without breaking the
    seal, so it is reported as a warning rather than vanishing."""
    from novafabric.capture.event_recorder import EventRecorder

    calls: list[int] = []

    def _drops(self: EventRecorder) -> dict[str, int]:
        calls.append(1)
        return {} if len(calls) == 1 else {"trace.jsonl": 1}

    monkeypatch.setattr(EventRecorder, "drop_counts", property(_drops))
    with caplog.at_level("WARNING", logger="novafabric.capture.orchestrator"):
        result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
            command=[sys.executable, "-c", "print('ok')"]
        )
    assert "after the evidence digests were computed" in caplog.text
    manifest = yaml.safe_load((result.capsule_dir / "capsule.yaml").read_text())
    _digest_map_matches_disk(result.capsule_dir, manifest)


def test_secret_reaching_the_final_manifest_fails_closed_and_is_not_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A planted secret introduced into the final manifest cannot survive finalization."""
    real = orch_mod._evidence_digests

    def _leaky_digests(capsule_dir: Path) -> dict[str, Any]:
        digests = real(capsule_dir)
        digests[f"outputs/{GITHUB_TOKEN}.txt"] = {"sha256": _sha(b""), "size_bytes": 0}
        return digests

    sealed: list[Path] = []
    monkeypatch.setattr(orch_mod, "_evidence_digests", _leaky_digests)
    monkeypatch.setattr(orch_mod, "_seal_capsule", lambda d, m: sealed.append(d))

    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "print('ok')"]
    )

    assert result.exit_code == 0  # the workload's own result is untouched
    assert sealed == [], "a capsule whose manifest carried a secret was sealed"
    _assert_absent(result.capsule_dir, GITHUB_TOKEN)
    assert "NOT sealed" in capsys.readouterr().err


def test_clean_capture_is_sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sealed: list[Path] = []
    monkeypatch.setattr(orch_mod, "_seal_capsule", lambda d, m: sealed.append(d))
    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "print('ok')"]
    )
    assert sealed == [result.capsule_dir]


# ── nova validate / nova verify on a redacted capture ────────────────────────


@pytest.fixture()
def seal_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ADR-0009-Residual-Test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {tmp_path / 'merkle.db'}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config_path))
    return config_path


def test_redacted_capture_validates_and_verifies(tmp_path: Path, seal_config: Path) -> None:
    """End to end: a capture whose argv, stdout and stderr carried secrets is redacted,
    passes `nova validate`, is sealed, and passes `nova verify` -- and verify still
    catches a later edit to the redaction proof, so the ordering binds it."""
    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[
            sys.executable,
            "-c",
            "import sys; print('gh=' + sys.argv[1]); print(sys.argv[2], file=sys.stderr)",
            GITHUB_TOKEN,
            ANTHROPIC_KEY,
        ]
    )
    capsule = result.capsule_dir
    _assert_absent(capsule, GITHUB_TOKEN)
    _assert_absent(capsule, ANTHROPIC_KEY)
    assert (capsule / ".seal" / "manifest.dsse").exists()

    runner = CliRunner()
    validated = runner.invoke(app, ["validate", str(capsule)])
    assert validated.exit_code == 0, validated.output
    verified = runner.invoke(app, ["verify", str(capsule), "--seal-config", str(seal_config)])
    assert verified.exit_code == 0, verified.output

    # The proof is bound by the seal: hiding a finding after the fact is detected.
    proof_path = capsule / "redaction-proof.json"
    proof = json.loads(proof_path.read_text())
    assert proof["findings_count"]["total"] >= 3
    proof["findings"] = []
    proof_path.write_text(json.dumps(recompute_chain_hash(proof), indent=2))
    tampered = runner.invoke(app, ["verify", str(capsule), "--seal-config", str(seal_config)])
    assert tampered.exit_code == 1, tampered.output
