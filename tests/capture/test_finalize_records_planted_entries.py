"""Finalisation records a link or special file it meets as tampering, not just a log line.

The post-workload sweep (``capture/tamper.py``) removes every link and special file
in the capsule. A process the workload left running can plant another one after the
sweep. ``evidence_digests`` never follows or hashes it, but used to leave it out with
only a log line, so the sealed manifest said nothing and ``nova verify`` passed.
Now ``finalize_capsule`` adds it to ``metadata.capture_tampered`` -- the signed
payload -- before the manifest gate and the seal.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.finalize import finalize_in_process_capsule
from novafabric.capture.tamper import TAMPER_METADATA_KEY, record_planted_entries_in_manifest

RUN_ID = "01HZZZTESTPLANTEDAFTERSWEEP"


def _capsule(tmp_path: Path) -> tuple[CapsuleWriter, Path]:
    writer = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path / "caps")
    writer.open()
    return writer, writer.capsule_dir


def test_finalize_records_links_and_fifos_planted_after_the_sweep(tmp_path: Path) -> None:
    writer, capsule = _capsule(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("TOPSECRET\n")
    (capsule / "inputs" / "deep").mkdir()
    os.symlink(outside, capsule / "inputs" / "deep" / "data.csv")
    os.mkfifo(capsule / "outputs" / "pipe")

    result = finalize_in_process_capsule(
        capsule, {"schema_version": "0.1.0", "run_id": RUN_ID}, run_id=RUN_ID, writer=writer
    )

    tampered = result.manifest["metadata"][TAMPER_METADATA_KEY]
    assert "inputs/deep/data.csv (symlink)" in tampered
    assert "outputs/pipe (fifo)" in tampered
    on_disk = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert on_disk["metadata"][TAMPER_METADATA_KEY] == tampered
    assert "inputs/deep/data.csv" not in on_disk["evidence_digests"]
    assert outside.read_text() == "TOPSECRET\n"


def test_a_clean_capsule_gets_no_tamper_record(tmp_path: Path) -> None:
    writer, capsule = _capsule(tmp_path)
    (capsule / "outputs" / "result.txt").write_text("ok\n")

    result = finalize_in_process_capsule(
        capsule, {"schema_version": "0.1.0", "run_id": RUN_ID}, run_id=RUN_ID, writer=writer
    )

    assert "metadata" not in result.manifest
    assert "metadata" not in yaml.safe_load((capsule / "capsule.yaml").read_text())


def test_planted_entries_extend_an_existing_tamper_summary() -> None:
    manifest: dict[str, object] = {"metadata": {TAMPER_METADATA_KEY: "model-calls.jsonl (symlink)"}}
    record_planted_entries_in_manifest(manifest, [("inputs/x", "symlink")])
    summary = manifest["metadata"][TAMPER_METADATA_KEY]  # type: ignore[index]
    assert summary.startswith("model-calls.jsonl (symlink); planted after the sweep: ")
    assert summary.endswith("inputs/x (symlink)")


def test_planted_entries_summary_is_bounded() -> None:
    manifest: dict[str, object] = {}
    record_planted_entries_in_manifest(manifest, [(f"inputs/l{i:04d}", "symlink") for i in range(500)])
    summary = manifest["metadata"][TAMPER_METADATA_KEY]  # type: ignore[index]
    assert summary.count("(symlink)") == 20
    assert summary.endswith("and 480 more")
