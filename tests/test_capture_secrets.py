import json
from pathlib import Path

import jsonschema

from novafabric.capture.secrets import PACK_NAME, PACK_VERSION, SecretScannerV0

SCHEMA = json.loads(
    (Path(__file__).parents[1] / "src/novafabric/schemas/secret-redaction.schema.json").read_text()
)
RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def _make_capsule(tmp_path: Path, model_calls: str = "") -> Path:
    (tmp_path / "model-calls.jsonl").write_text(model_calls)
    (tmp_path / "tool-calls.jsonl").write_text("")
    (tmp_path / "trace.jsonl").write_text("")
    (tmp_path / "capsule.yaml").write_text("status: success\n")
    return tmp_path


def test_clean_capsule_zero_findings(tmp_path: Path) -> None:
    _make_capsule(tmp_path, '{"gen_ai.system":"anthropic","content":"hello world"}\n')
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    jsonschema.validate(proof, SCHEMA, format_checker=jsonschema.FormatChecker())
    assert proof["findings_count"]["total"] == 0
    assert proof["findings"] == []
    assert proof["bytes_redacted"] == 0


def test_proof_validates_against_schema(tmp_path: Path) -> None:
    _make_capsule(tmp_path)
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    jsonschema.validate(proof, SCHEMA, format_checker=jsonschema.FormatChecker())


def test_proof_chain_hash_present(tmp_path: Path) -> None:
    _make_capsule(tmp_path)
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    assert proof["chain_hash"].startswith("sha256:")
    assert len(proof["chain_hash"]) == 71  # "sha256:" + 64 hex chars


def test_openai_key_redacted(tmp_path: Path) -> None:
    secret = "sk-abcDEFghijKLMNopqRSTuvwxyz012345678901234567890123456"
    _make_capsule(tmp_path, json.dumps({"api_key": secret}) + "\n")
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    assert proof["findings_count"]["total"] >= 1
    content = (tmp_path / "model-calls.jsonl").read_text()
    assert secret not in content
    assert "[REDACTED:openai-api-key]" in content


def test_anthropic_key_redacted(tmp_path: Path) -> None:
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789abcdef0"
    _make_capsule(tmp_path, json.dumps({"content": f"key={secret}"}) + "\n")
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    assert proof["findings_count"]["total"] >= 1
    assert "[REDACTED:anthropic-api-key]" in (tmp_path / "model-calls.jsonl").read_text()


def test_huggingface_token_redacted(tmp_path: Path) -> None:
    secret = "hf_" + "a" * 36
    _make_capsule(tmp_path, json.dumps({"token": secret}) + "\n")
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    assert proof["findings_count"]["total"] >= 1


def test_proof_capsule_run_id_matches(tmp_path: Path) -> None:
    _make_capsule(tmp_path)
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    assert proof["capsule_run_id"] == RUN_ID


def test_proof_scanner_identity(tmp_path: Path) -> None:
    _make_capsule(tmp_path)
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    assert proof["scanner"]["name"] == "novafabric.secrets"
    assert proof["scanner"]["engine"] == "regex"


def test_proof_pack_has_correct_name(tmp_path: Path) -> None:
    _make_capsule(tmp_path)
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    assert proof["packs"][0]["name"] == PACK_NAME
    assert proof["packs"][0]["version"] == PACK_VERSION
    assert proof["packs"][0]["rules_count"] == 13


def test_findings_dont_store_secret(tmp_path: Path) -> None:
    secret = "sk-abcDEFghijKLMNopqRSTuvwxyz012345678901234567890123456"
    _make_capsule(tmp_path, json.dumps({"api_key": secret}) + "\n")
    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()
    for finding in proof["findings"]:
        assert secret not in json.dumps(finding)
