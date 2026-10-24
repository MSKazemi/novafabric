"""Evidence bindings hash only regular files inside the capsule (review trust-evidence-05).

``_ref_for`` used ``path.exists()`` + ``path.read_bytes()``: a capsule whose
``model-calls.jsonl`` was a link to ``~/anything`` got a signed criterion binding
over a file outside the capsule, and a planted FIFO blocked ``build_bindings``
forever (in ``nova serve``, the event loop). ``verify_binding`` accepted an
absolute or ``..`` ``ref.path``.
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from novafabric._capsule_fs import CapsuleRequiredFileError
from novafabric.evidence.binding import (
    CriterionBinding,
    EvidenceRef,
    build_bindings,
    verify_binding,
)

PROFILE = "nist-ai-rmf"


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: r1\n")
    return cap


def _model_calls_ref(bindings: list[CriterionBinding]) -> EvidenceRef:
    return next(
        ref for b in bindings for ref in b.evidence_refs if ref.path == "model-calls.jsonl"
    )


def test_a_regular_file_is_hashed(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / "model-calls.jsonl").write_text('{"model": "m"}\n')
    ref = _model_calls_ref(build_bindings(cap, PROFILE))
    assert ref.sha256 == hashlib.sha256(b'{"model": "m"}\n').hexdigest()


def test_a_linked_evidence_file_is_refused(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not capsule evidence\n")
    (cap / "model-calls.jsonl").symlink_to(outside)
    with pytest.raises(CapsuleRequiredFileError):
        build_bindings(cap, PROFILE)


def test_a_fifo_evidence_file_is_refused_without_blocking(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    fifo = cap / "model-calls.jsonl"
    os.mkfifo(fifo)
    outcome: list[BaseException | None] = []

    def _run() -> None:
        try:
            build_bindings(cap, PROFILE)
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 — recorded for the assertion
            outcome.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=10)
    try:
        assert not worker.is_alive(), "build_bindings blocked on a FIFO"
        assert isinstance(outcome[0], CapsuleRequiredFileError)
    finally:
        if worker.is_alive():  # release a reader blocked in open(2)
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))


@pytest.mark.parametrize("bad", ["/etc/hostname", "../outside.txt", "a/../../outside.txt"])
def test_verify_binding_refuses_a_path_outside_the_capsule(tmp_path: Path, bad: str) -> None:
    cap = _capsule(tmp_path)
    (tmp_path / "outside.txt").write_text("x")
    target = Path(bad) if bad.startswith("/") else cap / bad
    digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else "0" * 64
    binding = CriterionBinding(
        criterion_id="c",
        evidence_refs=[EvidenceRef(path=bad, sha256=digest)],
        binding_status="satisfied",
    )
    assert verify_binding(cap, binding) is False


def test_verify_binding_still_accepts_an_unchanged_file(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / "model-calls.jsonl").write_text("{}\n")
    bindings = build_bindings(cap, PROFILE)
    assert all(verify_binding(cap, b) for b in bindings)
    (cap / "model-calls.jsonl").write_text("{} \n")
    assert not all(verify_binding(cap, b) for b in bindings)
