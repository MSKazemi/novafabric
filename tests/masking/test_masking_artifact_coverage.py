"""ADR-0135 maskers walk the same targets as the ADR-0009 scanner (issue item 6).

Before this, maskers walked only the structured streams. The built-in scanner had
grown to ``env.lock``, ``assets.jsonl``, ``lineage.jsonl``, ``inputs/**``,
``outputs/**`` and the manifest, so a value an operator's masker exists to remove
(an email, a case id) stayed verbatim in ``outputs/stdout.txt`` and ``capsule.yaml``
-- measured: an email masked in ``trace.jsonl`` was still in both.

The ordering invariant that holds after the change, per target:
built-in scan -> maskers -> final built-in residual pass. The built-in rules
therefore have the last word, even over a masker's own output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.capture.secrets import (
    SCAN_TARGETS,
    SecretScannerV0,
    iter_artifact_targets,
    recompute_chain_hash,
)
from novafabric.masking import UNCHANGED, MaskContext, MaskerSpec, MaskField, MaskingPipeline
from novafabric.masking._pipeline import default_targets
from novafabric.masking._registry import LoadedMasker
from novafabric.masking.examples import EmailMasker

EMAIL = "alice.smith@example.com"
RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"
GITHUB_TOKEN = "ghp" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp"


def _email_pipeline() -> MaskingPipeline:
    return MaskingPipeline(
        [LoadedMasker(masker=EmailMasker(), spec=MaskerSpec(id="novafabric-email"))]
    )


class _RecordingMasker:
    """Declines every value, remembering which refs it was offered."""

    masker_id = "recording-masker"
    masker_version = "1"
    pattern_ids = ("none",)

    def __init__(self) -> None:
        self.refs: list[str] = []

    def mask(self, field: MaskField, value: str, context: MaskContext) -> Any:
        self.refs.append(field.target_ref)
        return UNCHANGED


class _TokenEmittingMasker:
    """A buggy masker whose replacement is itself a key-shaped string."""

    masker_id = "token-emitter"
    masker_version = "1"
    pattern_ids = ("bad-replacement",)

    def mask(self, field: MaskField, value: str, context: MaskContext) -> Any:
        return value.replace("REPLACE-ME", GITHUB_TOKEN) if "REPLACE-ME" in value else UNCHANGED


def _files_with(capsule: Path, needle: str) -> list[str]:
    return sorted(
        p.relative_to(capsule).as_posix()
        for p in capsule.rglob("*")
        if p.is_file() and needle.encode() in p.read_bytes()
    )


def test_default_walk_is_the_scanner_walk(tmp_path: Path) -> None:
    (tmp_path / "outputs" / "nested").mkdir(parents=True)
    (tmp_path / "outputs" / "nested" / "b.txt").write_text("b\n")
    (tmp_path / "outputs" / "a.txt").write_text("a\n")
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "prompt.txt").write_text("p\n")
    (tmp_path / "env.lock").write_text("python: '3.12'\n")
    expected = list(SCAN_TARGETS) + [
        (ref, kind) for _p, ref, kind in iter_artifact_targets(tmp_path)
    ]
    assert default_targets(tmp_path) == expected
    assert ("outputs/nested/b.txt", "output-artifact") in expected
    assert ("inputs/prompt.txt", "input-artifact") in expected


def test_capture_masks_every_file_including_manifest_and_stdout(tmp_path: Path) -> None:
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs", masking_pipeline=_email_pipeline())
    result = orch.run(command=[sys.executable, "-c", f"print({EMAIL!r})"])
    capsule = result.capsule_dir

    assert _files_with(capsule, EMAIL) == []
    assert "[MASKED:email]" in (capsule / "outputs" / "stdout.txt").read_text()
    assert "[MASKED:email]" in (capsule / "capsule.yaml").read_text()

    proof = json.loads((capsule / "redaction-proof.json").read_text())
    assert recompute_chain_hash(dict(proof))["chain_hash"] == proof["chain_hash"]
    refs = {f["target_ref"] for f in proof["masker_findings"]}
    assert "outputs/stdout.txt#L1" in refs
    assert any(r.startswith("capsule.yaml ") for r in refs)
    # the masker rewrote stdout after its first scan: the proof reports the final bytes
    assert "outputs/stdout.txt" in proof["residual_check"]["reconciled_refs"]


def test_text_artifact_is_masked_line_by_line_and_keeps_other_bytes(tmp_path: Path) -> None:
    out = tmp_path / "outputs" / "log.txt"
    out.parent.mkdir()
    out.write_bytes(f"first line\r\nmail {EMAIL} now\n\nlast".encode())

    findings, errors = _email_pipeline().run(tmp_path, run_id=RUN_ID)

    assert errors == []
    assert out.read_bytes() == b"first line\r\nmail [MASKED:email] now\n\nlast"
    (finding,) = findings
    assert finding["target_ref"] == "outputs/log.txt#L2"
    assert finding["byte_offset"] == len(b"first line\r\n")


def test_binary_and_oversize_artifacts_are_not_offered_to_maskers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from novafabric.capture import secrets as secrets_mod

    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "blob.bin").write_bytes(b"\x00\xff" + EMAIL.encode())
    (tmp_path / "outputs" / "big.txt").write_text(EMAIL * 4)
    (tmp_path / "outputs" / "small.txt").write_text("hi\n")
    monkeypatch.setattr(secrets_mod, "MAX_ARTIFACT_SCAN_BYTES", 32)
    masker = _RecordingMasker()
    pipeline = MaskingPipeline([LoadedMasker(masker=masker, spec=MaskerSpec(id="rec"))])

    pipeline.run(tmp_path, run_id=RUN_ID)

    assert masker.refs == ["outputs/small.txt#L1"]


def test_builtin_rules_have_the_last_word_over_masker_output(tmp_path: Path) -> None:
    """Ordering invariant: a masker that emits a key-shaped string cannot leak it,
    because the final ADR-0009 residual pass runs after every masker."""
    pipeline = MaskingPipeline(
        [LoadedMasker(masker=_TokenEmittingMasker(), spec=MaskerSpec(id="token-emitter"))]
    )
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs", masking_pipeline=pipeline)
    result = orch.run(command=[sys.executable, "-c", "print('REPLACE-ME')"])
    capsule = result.capsule_dir

    for rel in _files_with(capsule, GITHUB_TOKEN):
        raise AssertionError(f"masker output leaked a token into {rel}")
    proof = json.loads((capsule / "redaction-proof.json").read_text())
    assert proof["residual_check"]["residual_findings"] >= 1
    assert "outputs/stdout.txt" in proof["residual_check"]["residual_refs"]


def test_mask_mapping_masks_values_and_never_mutates_input() -> None:
    manifest = {"command": ["notify", EMAIL], "run_id": RUN_ID}
    new, findings, errors = _email_pipeline().mask_mapping(
        manifest, "capsule.yaml", "capsule-yaml", RUN_ID
    )
    assert new["command"] == ["notify", "[MASKED:email]"]
    assert manifest["command"][1] == EMAIL
    assert errors == [] and findings[0]["target_ref"] == "capsule.yaml command[1]"


def test_absent_pipeline_writes_no_masker_arrays(tmp_path: Path) -> None:
    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "print('ok')"]
    )
    proof = json.loads((result.capsule_dir / "redaction-proof.json").read_text())
    assert "masker_findings" not in proof and "masker_errors" not in proof


def test_scanner_and_masker_share_one_enumeration(tmp_path: Path) -> None:
    """Symlinks are skipped by both walks alike."""
    outside = tmp_path / "outside.txt"
    outside.write_text(EMAIL)
    capsule = tmp_path / "capsule"
    (capsule / "outputs").mkdir(parents=True)
    (capsule / "outputs" / "link.txt").symlink_to(outside)

    _email_pipeline().run(capsule, run_id=RUN_ID)
    SecretScannerV0(capsule_dir=capsule, run_id=RUN_ID).scan_and_redact()

    assert outside.read_text() == EMAIL
