"""ADR-0009 "Scanning targets": every byte written to the capsule is scanned.

Before this, the scanner walked only the structured event streams. A key printed by
the workload stayed verbatim in ``outputs/stdout.txt``, and a key on the command line
stayed verbatim in ``capsule.yaml`` because the manifest was written after the scan.
These tests pin the full ADR-0009 target list, the binary-artifact rule ("flagged for
drop") and the orchestrator ordering, and they search the finished capsule for the
raw secret bytes rather than trusting the proof's own counts.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from novafabric.capture import secrets as secrets_mod
from novafabric.capture.secrets import SecretScannerV0, redact_secrets_in_text

SCHEMA = json.loads(
    (
        Path(__file__).parents[2] / "src/novafabric/schemas/secret-redaction.schema.json"
    ).read_text()
)
RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"

# Real-length shapes: an Anthropic key is ~108 chars, an OpenAI project key ~164+.
ANTHROPIC_KEY = "sk-ant-api03-" + "Ab3dEf6hIj" * 9 + "xyzAA"
OPENAI_PROJECT_KEY = "sk-proj-" + "Ab3dEf6hIj_-" * 12 + "T3BlbkFJ" + "Ab3dEf6hIj" * 2


def _assert_absent(capsule_dir: Path, secret: str) -> None:
    """No file anywhere in the capsule holds the secret — or any 16-char piece of it."""
    needles = {secret.encode()} | {
        secret[i : i + 16].encode() for i in range(0, len(secret) - 16, 8)
    }
    for path in capsule_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            for needle in needles:
                assert needle not in data, f"secret fragment left in {path.relative_to(capsule_dir)}"


def _scan(capsule_dir: Path) -> dict:
    proof = SecretScannerV0(capsule_dir=capsule_dir, run_id=RUN_ID).scan_and_redact()
    jsonschema.validate(proof, SCHEMA, format_checker=jsonschema.FormatChecker())
    return proof


# ── rule pack: real key formats are matched in full ──────────────────────────


@pytest.mark.parametrize("secret", [ANTHROPIC_KEY, OPENAI_PROJECT_KEY])
def test_real_length_keys_are_redacted_completely(secret: str) -> None:
    out = redact_secrets_in_text(f"token={secret} tail")
    assert out.startswith("token=[REDACTED:")
    assert out.endswith("] tail"), f"partial redaction left a suffix: {out!r}"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-svcacct-" + "Ab3dEf6hIj" * 6,
        "sk-admin-" + "Ab3dEf6hIj" * 6,
        "sk-lf-1f2e3d4c-5b6a-4789-8abc-def012345678",
    ],
)
def test_other_current_key_formats_are_detected(secret: str) -> None:
    assert secret not in redact_secrets_in_text(f"key={secret}")


# ── the ADR-0009 artifact targets ────────────────────────────────────────────


def test_artifact_targets_pinned() -> None:
    """Adding or removing an artifact target is a deliberate act (ADR-0009 list)."""
    assert secrets_mod.ARTIFACT_SCAN_TARGETS == [
        ("env.lock", "env-lock"),
        ("assets.jsonl", "assets"),
        ("lineage.jsonl", "lineage"),
    ]
    assert secrets_mod.ARTIFACT_SCAN_DIRS == [
        ("inputs", "input-artifact"),
        ("outputs", "output-artifact"),
    ]
    allowed = set(SCHEMA["$defs"]["ScanTarget"]["properties"]["kind"]["enum"])
    kinds = {k for _, k in secrets_mod.ARTIFACT_SCAN_TARGETS + secrets_mod.ARTIFACT_SCAN_DIRS}
    assert kinds <= allowed


@pytest.mark.parametrize(
    ("relpath", "kind"),
    [
        ("outputs/stdout.txt", "output-artifact"),
        ("outputs/stderr.txt", "output-artifact"),
        ("outputs/nested/report.json", "output-artifact"),
        ("inputs/prompt.txt", "input-artifact"),
        ("env.lock", "env-lock"),
        ("assets.jsonl", "assets"),
        ("lineage.jsonl", "lineage"),
    ],
)
def test_secret_redacted_in_every_adr0009_location(
    tmp_path: Path, relpath: str, kind: str
) -> None:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"before\ntoken={ANTHROPIC_KEY}\nafter\n")

    proof = _scan(tmp_path)

    _assert_absent(tmp_path, ANTHROPIC_KEY)
    assert "[REDACTED:anthropic-api-key]" in target.read_text()
    entry = next(t for t in proof["targets"] if t["ref"] == relpath)
    assert entry["kind"] == kind
    assert entry["findings_count"] == 1
    assert entry["hash_after_redaction"] == "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    assert [f["target_ref"] for f in proof["findings"]] == [relpath]


def test_clean_artifact_is_untouched_and_listed(tmp_path: Path) -> None:
    out = tmp_path / "outputs" / "stdout.txt"
    out.parent.mkdir()
    out.write_bytes(b"hello\n")

    proof = _scan(tmp_path)

    assert out.read_bytes() == b"hello\n"
    entry = next(t for t in proof["targets"] if t["ref"] == "outputs/stdout.txt")
    assert entry["findings_count"] == 0
    assert entry["hash_before_redaction"] == entry["hash_after_redaction"]


def test_binary_without_secret_is_byte_identical(tmp_path: Path) -> None:
    blob = b"\x89PNG\r\n\x1a\n\x00\xff\xfe" + bytes(range(256))
    out = tmp_path / "outputs" / "image.png"
    out.parent.mkdir()
    out.write_bytes(blob)

    proof = _scan(tmp_path)

    assert out.read_bytes() == blob
    entry = next(t for t in proof["targets"] if t["ref"] == "outputs/image.png")
    assert entry["binary"] is True
    assert entry["findings_count"] == 0


def test_binary_with_secret_is_dropped_not_kept(tmp_path: Path) -> None:
    """ADR-0009: binaries cannot be redacted in place and are flagged for drop."""
    out = tmp_path / "outputs" / "dump.bin"
    out.parent.mkdir()
    out.write_bytes(b"\x00\x01\xff" + ANTHROPIC_KEY.encode() + b"\x00\xfe")

    proof = _scan(tmp_path)

    assert not out.exists()
    _assert_absent(tmp_path, ANTHROPIC_KEY)
    entry = next(t for t in proof["targets"] if t["ref"] == "outputs/dump.bin")
    assert entry["binary"] is True
    assert entry["findings_count"] == 1
    assert entry["hash_after_redaction"] == "sha256:" + hashlib.sha256(b"").hexdigest()
    (finding,) = proof["findings"]
    assert finding["redaction_strategy"] == "drop"
    assert finding["replacement"] == ""


def test_binary_is_not_dropped_on_an_unprefixed_generic_match(tmp_path: Path) -> None:
    """A PDF's uppercase-hex /ID matches the prefix-less Mistral rule; dropping the
    file on that would delete ordinary binaries (incl. referenced media blobs)."""
    blob = b"%PDF-1.7\n\x00\xff/ID [<8F3A2B1C9D4E5F60718293A4B5C6D7E8>]\n\x00"
    out = tmp_path / "outputs" / "report.pdf"
    out.parent.mkdir()
    out.write_bytes(blob)

    proof = _scan(tmp_path)

    assert out.read_bytes() == blob
    assert proof["findings"] == []


def test_oversize_artifact_is_recorded_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets_mod, "MAX_ARTIFACT_SCAN_BYTES", 16)
    out = tmp_path / "outputs" / "big.txt"
    out.parent.mkdir()
    out.write_text("x" * 64)

    proof = _scan(tmp_path)

    entry = next(t for t in proof["targets"] if t["ref"] == "outputs/big.txt")
    assert entry["skipped"] is True
    assert "16" in entry["skip_reason"]
    assert out.read_text() == "x" * 64


def test_symlink_in_outputs_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text(f"token={ANTHROPIC_KEY}\n")
    capsule = tmp_path / "capsule"
    (capsule / "outputs").mkdir(parents=True)
    (capsule / "outputs" / "link.txt").symlink_to(outside)

    proof = _scan(capsule)

    assert ANTHROPIC_KEY in outside.read_text()  # never rewritten through the link
    assert all(t["ref"] != "outputs/link.txt" for t in proof["targets"])


# ── manifest redaction (YAML-safe) ───────────────────────────────────────────


def test_redact_manifest_keeps_yaml_structure(tmp_path: Path) -> None:
    """A secret that IS a whole scalar must not turn into a YAML flow sequence."""
    scanner = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID)
    manifest = {"run_id": RUN_ID, "command": ["deploy", ANTHROPIC_KEY, f"--key={ANTHROPIC_KEY}"]}

    redacted, target, findings = scanner.redact_manifest(manifest)

    assert redacted["command"] == [
        "deploy",
        "[REDACTED:anthropic-api-key]",
        "--key=[REDACTED:anthropic-api-key]",
    ]
    assert manifest["command"][1] == ANTHROPIC_KEY  # input not mutated
    assert yaml.safe_load(yaml.dump(redacted))["command"] == redacted["command"]
    assert target["kind"] == "capsule-yaml" and target["ref"] == "capsule.yaml"
    assert target["findings_count"] == 2 == len(findings)
    assert all(f["target_ref"] == "capsule.yaml" for f in findings)


# ── end to end through the orchestrator ──────────────────────────────────────


def test_capture_leaves_no_secret_anywhere_in_the_capsule(tmp_path: Path) -> None:
    from novafabric.capture.orchestrator import CaptureOrchestrator

    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(
        command=[
            sys.executable,
            "-c",
            "import sys; print('token=' + sys.argv[1]); print(sys.argv[2], file=sys.stderr)",
            ANTHROPIC_KEY,
            OPENAI_PROJECT_KEY,
        ]
    )
    capsule = result.capsule_dir

    _assert_absent(capsule, ANTHROPIC_KEY)
    _assert_absent(capsule, OPENAI_PROJECT_KEY)

    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert "[REDACTED:anthropic-api-key]" in manifest["command"]
    proof = json.loads((capsule / "redaction-proof.json").read_text())
    jsonschema.validate(proof, SCHEMA, format_checker=jsonschema.FormatChecker())
    refs = {f["target_ref"] for f in proof["findings"]}
    assert {"capsule.yaml", "outputs/stdout.txt", "outputs/stderr.txt"} <= refs
    # one capsule.yaml target, not a stale one plus a fresh one
    assert [t["ref"] for t in proof["targets"]].count("capsule.yaml") == 1

    # the sealed-digest map binds the proof that is actually on disk
    proof_bytes = (capsule / "redaction-proof.json").read_bytes()
    assert manifest["evidence_digests"]["redaction-proof.json"]["sha256"] == (
        "sha256:" + hashlib.sha256(proof_bytes).hexdigest()
    )
