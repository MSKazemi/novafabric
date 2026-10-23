"""Each layer of capture finalisation refuses a planted link or FIFO on its own.

``tests/capture/test_capture_hostile_workload.py`` drives a real ``nova capture``.
There the post-workload sweep (``capture/tamper.py``) removes a planted entry
before anything else runs, which would hide a regression in any later layer.
So every later layer is tested here directly: the capsule writer, the secret
scanner, the event recorder, ``evidence_digests``, the seal writer, the in-toto
subjects and the capture-health hand-off. A FIFO test runs under an alarm, so a
regression fails instead of hanging the run.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from novafabric._capsule_fs import (
    CapsuleLinkError,
    CapsuleSpecialFileError,
    replace_capsule_file,
)
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import EventRecorder
from novafabric.capture.finalize import _digest_entry, _write_seal_files, evidence_digests
from novafabric.capture.secrets import SecretScannerV0
from novafabric.capture.tamper import (
    TAMPER_METADATA_KEY,
    TamperFinding,
    TamperReport,
    inspect_capsule_after_workload,
    record_tamper_in_manifest,
    record_tamper_in_proof,
)
from novafabric.capture.workload_health import (
    MAX_COUNT,
    WORKLOAD_HEALTH_PREFIX,
    merge_workload_drops,
)
from novafabric.envelopes.intoto import capsule_subjects

_SECRET = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789\n"


@pytest.fixture()
def no_hang() -> Iterator[None]:
    """Fail (SIGALRM) instead of hanging when a FIFO is opened blocking."""

    def _alarm(_signum: int, _frame: object) -> None:
        raise TimeoutError("blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(20)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    writer = CapsuleWriter(run_id="r-untrusted", base_dir=tmp_path / "runs")
    writer.open()
    return writer.capsule_dir


@pytest.fixture()
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "outside.txt"
    path.write_text(_SECRET)
    return path


def _plant_link(capsule: Path, name: str, target: Path) -> None:
    path = capsule / name
    if os.path.lexists(path):
        path.unlink()
    path.symlink_to(target)


def _plant_fifo(capsule: Path, name: str) -> None:
    path = capsule / name
    if os.path.lexists(path):
        path.unlink()
    os.mkfifo(path)


# ── replace_capsule_file ────────────────────────────────────────────────────


def test_replace_replaces_a_link_never_its_target(capsule: Path, outside: Path) -> None:
    _plant_link(capsule, "env.lock", outside)
    replace_capsule_file(capsule, "env.lock", b"real\n")
    assert outside.read_text() == _SECRET
    assert not (capsule / "env.lock").is_symlink()
    assert (capsule / "env.lock").read_bytes() == b"real\n"


def test_replace_replaces_a_fifo_without_blocking(capsule: Path, no_hang: None) -> None:
    _plant_fifo(capsule, "env.lock")
    replace_capsule_file(capsule, "env.lock", b"real\n")
    assert stat.S_ISREG((capsule / "env.lock").lstat().st_mode)


def test_replace_breaks_a_hard_link(capsule: Path, tmp_path: Path) -> None:
    other = tmp_path / "hard-target"
    other.write_text("original\n")
    (capsule / "env.lock").unlink(missing_ok=True)
    os.link(other, capsule / "env.lock")
    replace_capsule_file(capsule, "env.lock", b"new\n")
    assert other.read_text() == "original\n"


def test_replace_refuses_a_linked_parent(capsule: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (capsule / "outputs").rmdir()
    (capsule / "outputs").symlink_to(elsewhere)
    with pytest.raises(CapsuleLinkError):
        replace_capsule_file(capsule, "outputs/stdout.txt", b"x")
    assert list(elsewhere.iterdir()) == []


def test_replace_keeps_a_requested_mode(capsule: Path) -> None:
    replace_capsule_file(capsule, "f.txt", b"x", file_mode=0o600)
    assert stat.S_IMODE((capsule / "f.txt").stat().st_mode) == 0o600
    assert [p.name for p in capsule.iterdir() if ".nf-tmp-" in p.name] == []


# ── CapsuleWriter ───────────────────────────────────────────────────────────


def test_writer_append_refuses_a_link(tmp_path: Path, outside: Path) -> None:
    writer = CapsuleWriter(run_id="r1", base_dir=tmp_path / "runs")
    writer.open()
    _plant_link(writer.capsule_dir, "trace.jsonl", outside)
    with pytest.raises(CapsuleLinkError):
        writer.append_trace_span({"span_id": "s"})
    assert outside.read_text() == _SECRET


def test_writer_append_refuses_a_fifo(tmp_path: Path, no_hang: None) -> None:
    writer = CapsuleWriter(run_id="r1", base_dir=tmp_path / "runs")
    writer.open()
    _plant_fifo(writer.capsule_dir, "trace.jsonl")
    with pytest.raises((CapsuleSpecialFileError, OSError)):
        writer.append_trace_span({"span_id": "s"})


def test_writer_write_text_replaces_a_link(tmp_path: Path, outside: Path) -> None:
    writer = CapsuleWriter(run_id="r1", base_dir=tmp_path / "runs")
    writer.open()
    _plant_link(writer.capsule_dir, "capsule.yaml", outside)
    writer.write_text("capsule.yaml", "run_id: r1\n")
    assert outside.read_text() == _SECRET
    assert (writer.capsule_dir / "capsule.yaml").read_text() == "run_id: r1\n"


def test_writer_records_its_root_identity(tmp_path: Path) -> None:
    writer = CapsuleWriter(run_id="r1", base_dir=tmp_path / "runs")
    assert writer.root_identity is None
    writer.open()
    st = writer.capsule_dir.lstat()
    assert writer.root_identity == (st.st_dev, st.st_ino)


# ── the secret scanner ──────────────────────────────────────────────────────


def test_scanner_never_rewrites_through_a_stream_link(capsule: Path, outside: Path) -> None:
    _plant_link(capsule, "model-calls.jsonl", outside)
    proof = SecretScannerV0(capsule_dir=capsule, run_id="r").scan_and_redact()
    assert outside.read_text() == _SECRET
    target = next(t for t in proof["targets"] if t["ref"] == "model-calls.jsonl")
    assert target["skipped"] is True
    assert target["skip_reason"].startswith("stream_tampered:")
    assert proof["findings_count"]["total"] == 0  # the outside secret was never read


def test_scanner_skips_a_stream_fifo_without_blocking(capsule: Path, no_hang: None) -> None:
    _plant_fifo(capsule, "tool-calls.jsonl")
    proof = SecretScannerV0(capsule_dir=capsule, run_id="r").scan_and_redact()
    target = next(t for t in proof["targets"] if t["ref"] == "tool-calls.jsonl")
    assert target["skipped"] is True


def test_scanner_artifact_link_is_never_read(capsule: Path, outside: Path) -> None:
    scanner = SecretScannerV0(capsule_dir=capsule, run_id="r")
    _plant_link(capsule, "env.lock", outside)
    targets, findings, _ = scanner.scan_and_redact_refs([("env.lock", "env-lock")])
    assert outside.read_text() == _SECRET
    assert findings == []


def test_scanner_still_redacts_a_regular_stream_and_keeps_its_mode(capsule: Path) -> None:
    stream = capsule / "model-calls.jsonl"
    stream.write_text(json.dumps({"content": _SECRET.strip()}) + "\n")
    stream.chmod(0o640)
    proof = SecretScannerV0(capsule_dir=capsule, run_id="r").scan_and_redact()
    assert proof["findings_count"]["total"] == 1
    assert "ghp_" not in stream.read_text()
    assert stat.S_IMODE(stream.stat().st_mode) == 0o640


# ── the event recorder ──────────────────────────────────────────────────────


def test_recorder_append_counts_a_link_as_a_drop(capsule: Path, outside: Path) -> None:
    rec = EventRecorder(capsule_dir=capsule, run_id="r", capsule_id="r")
    _plant_link(capsule, "network_events.jsonl", outside)
    rec._append("network_events.jsonl", {"x": 1})
    assert outside.read_text() == _SECRET
    assert rec.drop_counts == {"network_events.jsonl": 1}


def test_recorder_health_is_never_written_through_a_link(capsule: Path, outside: Path) -> None:
    rec = EventRecorder(capsule_dir=capsule, run_id="r", capsule_id="r")
    rec.note_drop("model-calls.jsonl")
    _plant_link(capsule, "capture-health.json", outside)
    rec.finalize_health()
    assert outside.read_text() == _SECRET
    assert json.loads((capsule / "capture-health.json").read_text())["dropped_events"]


def test_recorder_writes_health_for_tamper_alone(capsule: Path) -> None:
    rec = EventRecorder(capsule_dir=capsule, run_id="r", capsule_id="r")
    rec.record_tampered([{"path": "model-calls.jsonl", "kind": "symlink", "action": "removed"}])
    rec.finalize_health()
    health = json.loads((capsule / "capture-health.json").read_text())
    assert health["dropped_events"] == {}
    assert health["tampered_entries"][0]["path"] == "model-calls.jsonl"


# ── evidence digests and the seal writer ────────────────────────────────────


def test_digest_entry_never_follows_a_link(capsule: Path, outside: Path) -> None:
    _plant_link(capsule, "env.lock", outside)
    assert _digest_entry(capsule, "env.lock") is None


def test_digest_entry_never_blocks_on_a_fifo(capsule: Path, no_hang: None) -> None:
    _plant_fifo(capsule, "env.lock")
    assert _digest_entry(capsule, "env.lock") is None


def test_evidence_digests_leave_out_links_and_fifos(
    capsule: Path, outside: Path, no_hang: None
) -> None:
    _plant_link(capsule, "env.lock", outside)
    _plant_fifo(capsule, "replay.yaml")
    digests = evidence_digests(capsule)
    assert "env.lock" not in digests and "replay.yaml" not in digests
    assert "model-calls.jsonl" in digests


def test_seal_files_are_never_written_through_a_seal_link(capsule: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (capsule / ".seal").symlink_to(elsewhere)
    bundle = SimpleNamespace(dsse_envelope=b"{}", tsr=b"", log_entry={})
    with pytest.raises(CapsuleLinkError):
        _write_seal_files(capsule, bundle)
    assert list(elsewhere.iterdir()) == []


# ── the post-workload sweep ─────────────────────────────────────────────────


def test_sweep_removes_and_records_every_kind(
    capsule: Path, outside: Path, tmp_path: Path, no_hang: None
) -> None:
    writer_root = capsule.lstat()
    _plant_link(capsule, "model-calls.jsonl", outside)
    _plant_fifo(capsule, "tool-calls.jsonl")
    (capsule / "env.lock").mkdir()
    (capsule / ".seal").mkdir()
    (capsule / ".seal" / "manifest.dsse").write_text("{}")
    _plant_link(capsule / "outputs", "stdout.txt", outside)
    report = inspect_capsule_after_workload(
        capsule, expected_root=(writer_root.st_dev, writer_root.st_ino)
    )
    found = {f.path: (f.kind, f.action) for f in report.findings}
    assert found == {
        "model-calls.jsonl": ("symlink", "removed-and-recreated"),
        "tool-calls.jsonl": ("fifo", "removed-and-recreated"),
        "env.lock": ("directory", "removed"),
        ".seal": ("directory", "removed"),
        "outputs/stdout.txt": ("symlink", "removed"),
    }
    link = next(f for f in report.findings if f.path == "model-calls.jsonl")
    assert link.link_target == str(outside)
    assert outside.read_text() == _SECRET
    assert (capsule / "model-calls.jsonl").read_bytes() == b""
    assert not os.path.lexists(capsule / ".seal")


def test_sweep_restores_a_linked_required_directory(capsule: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (capsule / "inputs").rmdir()
    (capsule / "inputs").symlink_to(elsewhere)
    report = inspect_capsule_after_workload(capsule)
    assert [(f.path, f.kind, f.action) for f in report.findings] == [
        ("inputs", "symlink", "removed-and-recreated")
    ]
    assert (capsule / "inputs").is_dir() and not (capsule / "inputs").is_symlink()


def test_sweep_leaves_a_clean_capsule_alone(capsule: Path) -> None:
    before = sorted(p.relative_to(capsule).as_posix() for p in capsule.rglob("*"))
    report = inspect_capsule_after_workload(capsule)
    assert report.findings == () and not report.tampered
    assert sorted(p.relative_to(capsule).as_posix() for p in capsule.rglob("*")) == before


def test_sweep_removes_links_outside_managed_paths_too(capsule: Path, outside: Path) -> None:
    # Superseded 2026-10-23: the sweep used to leave links at unmanaged paths. It
    # now walks the whole tree, since an exporter, a later writer or a reader
    # could follow any of them; a link that stays inside the capsule is no
    # exception (no export format can represent one).
    (capsule / "outputs" / "model.bin").write_bytes(b"weights")
    (capsule / "outputs" / "latest").symlink_to("model.bin")  # not a managed path
    report = inspect_capsule_after_workload(capsule)
    assert [(f.path, f.kind, f.action) for f in report.findings] == [
        ("outputs/latest", "symlink", "removed")
    ]
    assert report.findings[0].link_target == "model.bin"
    assert not os.path.lexists(capsule / "outputs" / "latest")
    assert (capsule / "outputs" / "model.bin").read_bytes() == b"weights"


def test_sweep_descends_every_directory_and_never_opens_what_it_finds(
    capsule: Path, outside: Path, no_hang: None
) -> None:
    import socket

    deep = capsule / "extensions" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "x.json").symlink_to(outside)
    os.mkfifo(capsule / "inputs" / "pipe")
    (capsule / "inputs" / "dirlink").symlink_to(outside.parent, target_is_directory=True)
    expected = {
        ("extensions/a/b/c/x.json", "symlink"),
        ("inputs/pipe", "fifo"),
        ("inputs/dirlink", "symlink"),
    }
    sock = socket.socket(socket.AF_UNIX)
    try:
        try:
            sock.bind(str(capsule / "outputs" / "s.sock"))
            expected.add(("outputs/s.sock", "socket"))
        except OSError:  # AF_UNIX path too long on this runner: skip the socket
            pass
        report = inspect_capsule_after_workload(capsule)
    finally:
        sock.close()
    assert {(f.path, f.kind) for f in report.findings} == expected
    assert all(f.action == "removed" for f in report.findings)
    assert (capsule / "extensions" / "a" / "b" / "c").is_dir()  # directories are kept
    assert outside.read_text() == _SECRET
    assert list(outside.parent.iterdir())  # the linked directory was not emptied


def test_sweep_keeps_a_hard_link_and_records_nothing(capsule: Path, outside: Path) -> None:
    os.link(outside, capsule / "inputs" / "hard.txt")
    report = inspect_capsule_after_workload(capsule)
    assert report.findings == ()
    assert (capsule / "inputs" / "hard.txt").stat().st_nlink == 2


def test_sweep_does_not_descend_past_the_depth_bound(capsule: Path) -> None:
    from novafabric.capture.tamper import MAX_SWEEP_DEPTH

    path = capsule / "outputs"
    for i in range(MAX_SWEEP_DEPTH + 3):
        path = path / f"d{i}"
    path.mkdir(parents=True)
    report = inspect_capsule_after_workload(capsule)
    assert [(f.kind, f.action) for f in report.findings] == [("directory", "not-swept")]
    assert report.findings[0].path.count("/") == MAX_SWEEP_DEPTH - 1


def test_sweep_records_a_bounded_number_of_findings(capsule: Path, outside: Path) -> None:
    for i in range(1005):
        (capsule / "inputs" / f"l{i:04d}").symlink_to(outside)
    report = inspect_capsule_after_workload(capsule)
    assert len(report.findings) == 1000 and report.omitted == 5 and report.total == 1005
    assert not any(os.path.lexists(capsule / "inputs" / f"l{i:04d}") for i in range(1005))
    summary = report.summary()
    assert summary.count("(symlink)") == 20
    assert summary.endswith("and 985 more (1005 in total)")


def test_sweep_detects_a_replaced_root(capsule: Path, tmp_path: Path) -> None:
    st = capsule.lstat()
    identity = (st.st_dev, st.st_ino)
    capsule.rename(tmp_path / "moved")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    capsule.symlink_to(elsewhere)
    report = inspect_capsule_after_workload(capsule, expected_root=identity)
    assert report.root_replaced and report.tampered
    assert list(elsewhere.iterdir()) == []


def test_sweep_detects_a_root_swapped_for_another_real_directory(
    capsule: Path, tmp_path: Path
) -> None:
    # Not a link, so O_NOFOLLOW alone cannot tell: only the identity check can.
    st = capsule.lstat()
    identity = (st.st_dev, st.st_ino)
    capsule.rename(tmp_path / "moved")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    decoy.rename(capsule)
    report = inspect_capsule_after_workload(capsule, expected_root=identity)
    assert report.root_replaced
    assert list(capsule.iterdir()) == []  # nothing written into the decoy


def test_tamper_record_in_proof_and_manifest() -> None:
    report = TamperReport(findings=(TamperFinding("model-calls.jsonl", "symlink", "removed"),))
    proof = record_tamper_in_proof(
        {"targets": [], "findings": [], "bytes_scanned": 0, "bytes_redacted": 0}, report
    )
    (target,) = proof["targets"]
    assert target["ref"] == "model-calls.jsonl#tampered"
    assert target["kind"] == "model-call-messages"
    assert proof["chain_hash"].startswith("sha256:")
    manifest: dict[str, object] = {"metadata": {"team": "a"}}
    record_tamper_in_manifest(manifest, report)
    assert manifest["metadata"] == {
        "team": "a", TAMPER_METADATA_KEY: "model-calls.jsonl (symlink)"
    }


# ── other host-side readers and writers ─────────────────────────────────────


def test_usage_totals_never_read_through_a_link(capsule: Path, tmp_path: Path) -> None:
    from novafabric.cost.usage_types import usage_totals_from_model_calls

    outside = tmp_path / "outside-calls.jsonl"
    outside.write_text(json.dumps({"nova.usage": {"input_tokens": 5}}) + "\n")
    _plant_link(capsule, "model-calls.jsonl", outside)
    assert usage_totals_from_model_calls(capsule / "model-calls.jsonl") is None


def test_lineage_write_replaces_a_link_never_its_target(capsule: Path, outside: Path) -> None:
    from novafabric.lineage._writer import LineageWriter

    _plant_link(capsule, "lineage.jsonl", outside)
    LineageWriter(capsule_dir=capsule, run_id="r").write([])
    assert outside.read_text() == _SECRET
    assert not (capsule / "lineage.jsonl").is_symlink()


# ── in-toto subjects ────────────────────────────────────────────────────────


def test_intoto_subjects_refuse_a_link_to_an_outside_file(tmp_path: Path, outside: Path) -> None:
    cap = tmp_path / "cap"
    (cap / "inputs").mkdir(parents=True)
    (cap / "capsule.yaml").write_text("run_id: r1\n")
    (cap / "inputs" / "leak.txt").symlink_to(outside)
    with pytest.raises(CapsuleLinkError) as exc:
        capsule_subjects(cap)
    assert "inputs/leak.txt" in str(exc.value)


def test_intoto_subjects_refuse_a_linked_directory(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: r1\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text(_SECRET)
    (cap / "inputs").symlink_to(elsewhere)
    with pytest.raises(CapsuleLinkError):
        capsule_subjects(cap)


def test_intoto_subjects_skip_a_fifo_without_blocking(tmp_path: Path, no_hang: None) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: r1\n")
    os.mkfifo(cap / "pipe")
    assert [s["name"] for s in capsule_subjects(cap)] == ["capsule.yaml"]


def test_intoto_subjects_of_a_clean_capsule_are_unchanged(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    (cap / "outputs" / "a").mkdir(parents=True)
    files = {"capsule.yaml": b"run_id: r1\n", "outputs/a/x.bin": b"\x00\x01", "z.txt": b"z"}
    for rel, data in files.items():
        (cap / rel).write_bytes(data)
    subjects = capsule_subjects(cap)
    assert subjects == [
        {"name": rel, "digest": {"sha256": hashlib.sha256(files[rel]).hexdigest()}}
        for rel in ["capsule.yaml", "outputs/a/x.bin", "z.txt"]
    ]


# ── the capture-health hand-off ─────────────────────────────────────────────


def _hand_off(capsule: Path, pid: int, counts: dict[str, object]) -> None:
    (capsule / f"{WORKLOAD_HEALTH_PREFIX}{pid}.json").write_text(
        json.dumps({"dropped_events": counts})
    )


def test_hand_off_with_an_unknown_stream_is_ignored(capsule: Path) -> None:
    rec = EventRecorder(capsule_dir=capsule, run_id="r", capsule_id="r")
    _hand_off(capsule, 1, {"model-calls.jsonl": 1, "FORGED": 5})
    assert merge_workload_drops(capsule, rec) == {}
    assert rec.drop_counts == {}


@pytest.mark.parametrize("count", [2**53, 10**9 + 1])
def test_hand_off_count_above_the_bound_is_ignored(capsule: Path, count: int) -> None:
    assert count > MAX_COUNT
    rec = EventRecorder(capsule_dir=capsule, run_id="r", capsule_id="r")
    _hand_off(capsule, 1, {"model-calls.jsonl": count})
    assert merge_workload_drops(capsule, rec) == {}


def test_hand_off_at_the_bound_is_accepted(capsule: Path) -> None:
    rec = EventRecorder(capsule_dir=capsule, run_id="r", capsule_id="r")
    _hand_off(capsule, 1, {"model-calls.jsonl": 10**9})
    assert merge_workload_drops(capsule, rec) == {"model-calls.jsonl": 10**9}


def test_merged_total_is_capped(capsule: Path) -> None:
    rec = EventRecorder(capsule_dir=capsule, run_id="r", capsule_id="r")
    for pid in range(3):
        _hand_off(capsule, pid, {"tool-calls.jsonl": MAX_COUNT})
    assert merge_workload_drops(capsule, rec) == {"tool-calls.jsonl": MAX_COUNT}


def test_a_hand_off_never_lowers_orchestrator_counts(capsule: Path) -> None:
    rec = EventRecorder(capsule_dir=capsule, run_id="r", capsule_id="r")
    for _ in range(3):
        rec.note_drop("model-calls.jsonl")
    _hand_off(capsule, 1, {"model-calls.jsonl": 0, "tool-calls.jsonl": 2})
    _hand_off(capsule, 2, {"model-calls.jsonl": -5})  # invalid: ignored whole
    merge_workload_drops(capsule, rec)
    rec.finalize_health()
    health = json.loads((capsule / "capture-health.json").read_text())
    assert health["dropped_events"] == {"model-calls.jsonl": 3, "tool-calls.jsonl": 2}
    assert health["sources"] == {
        "orchestrator": {"model-calls.jsonl": 3},
        "workload": {"tool-calls.jsonl": 2},
    }
    assert "not verified" in health["sources_note"]
