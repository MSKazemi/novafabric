"""ADR-0094 (A) — per-stream sidecar hash-chain tests.

The chain is a sidecar at ``capsule_dir/.ledger/<stream>.chain.json``; the
``.jsonl`` evidence stream is NEVER modified (additive invariant). A content
edit cascades the chain (TAMPER), a deletion creates a seq gap (TRUNCATION),
a swap breaks the prev_hash linkage (REORDER).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from novafabric.trust.ledger._chain import (
    ChainLink,
    ChainTamperClass,
    build_chain,
    verify_chain,
    write_chain,
)


def _capsule(tmp_path: Path) -> Path:
    capsule = tmp_path / "run-1"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: run-1\n")
    (capsule / "model-calls.jsonl").write_text(
        json.dumps({"seq": 0, "model": "m", "prompt": "a"})
        + "\n"
        + json.dumps({"seq": 1, "model": "m", "prompt": "b"})
        + "\n"
        + json.dumps({"seq": 2, "model": "m", "prompt": "c"})
        + "\n"
    )
    return capsule


def test_build_chain_links_and_construction(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    links = build_chain(capsule, "model-calls")
    assert len(links) == 3
    assert [link.seq for link in links] == [0, 1, 2]
    assert links[0].prev_hash == ""
    # link_hash = sha256(str(seq)||":"||prev_hash||":"||sha256(record_bytes))
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    rec0 = hashlib.sha256((lines[0] + "\n").encode()).hexdigest()
    expected0 = hashlib.sha256(f"0:{''}:{rec0}".encode()).hexdigest()
    assert links[0].link_hash == expected0
    # the chain folds: each prev_hash equals the previous link_hash
    assert links[1].prev_hash == links[0].link_hash
    assert links[2].prev_hash == links[1].link_hash


def test_chain_link_is_a_dataclass_round_trip() -> None:
    link = ChainLink(seq=0, prev_hash="", link_hash="ab", record_hash="cd")
    assert link.to_dict() == {
        "seq": 0,
        "prev_hash": "",
        "link_hash": "ab",
        "record_hash": "cd",
    }
    assert ChainLink.from_dict(link.to_dict()) == link
    # from_dict tolerates a legacy entry without record_hash
    legacy = ChainLink.from_dict({"seq": 1, "prev_hash": "x", "link_hash": "y"})
    assert legacy.record_hash == ""


def test_write_chain_creates_sidecar_and_preserves_jsonl_bytes(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    original = (capsule / "model-calls.jsonl").read_bytes()
    path = write_chain(capsule, "model-calls")
    assert path == capsule / ".ledger" / "model-calls.chain.json"
    assert path.exists()
    # additive invariant: jsonl is byte-identical
    assert (capsule / "model-calls.jsonl").read_bytes() == original
    payload = json.loads(path.read_text())
    assert [entry["seq"] for entry in payload] == [0, 1, 2]


def test_verify_clean_chain_passes(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    write_chain(capsule, "model-calls")
    verdict = verify_chain(capsule, "model-calls")
    assert verdict.ok
    assert verdict.tamper_class is ChainTamperClass.NONE
    assert verdict.seq_high == 2
    assert verdict.record_count == 3


def test_verify_detects_content_edit_as_tamper(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    write_chain(capsule, "model-calls")
    # adversary edits a recorded line in place (jsonl), recompute fails
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    lines[1] = json.dumps({"seq": 1, "model": "m", "prompt": "EDITED"})
    (capsule / "model-calls.jsonl").write_text("\n".join(lines) + "\n")
    verdict = verify_chain(capsule, "model-calls")
    assert not verdict.ok
    assert verdict.tamper_class is ChainTamperClass.TAMPER


def test_verify_detects_reorder(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    write_chain(capsule, "model-calls")
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    lines[0], lines[1] = lines[1], lines[0]
    (capsule / "model-calls.jsonl").write_text("\n".join(lines) + "\n")
    verdict = verify_chain(capsule, "model-calls")
    assert not verdict.ok
    assert verdict.tamper_class is ChainTamperClass.REORDER


def test_verify_detects_truncation(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    write_chain(capsule, "model-calls")
    # drop the last record after the chain was written and sealed
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    (capsule / "model-calls.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    verdict = verify_chain(capsule, "model-calls")
    assert not verdict.ok
    assert verdict.tamper_class is ChainTamperClass.TRUNCATION


def test_verify_missing_chain_raises(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    with pytest.raises(FileNotFoundError):
        verify_chain(capsule, "model-calls")


def test_build_chain_missing_stream_raises(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    with pytest.raises(FileNotFoundError):
        build_chain(capsule, "no-such-stream")


def test_blank_lines_and_missing_trailing_newline(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    # a stream with a blank line in the middle and no trailing newline
    (capsule / "odd.jsonl").write_text(
        json.dumps({"n": 0}) + "\n\n" + json.dumps({"n": 1})
    )
    links = build_chain(capsule, "odd")
    assert [link.seq for link in links] == [0, 1]
    write_chain(capsule, "odd")
    assert verify_chain(capsule, "odd").ok


def test_verify_chain_missing_jsonl_but_chain_present_raises(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    write_chain(capsule, "model-calls")
    (capsule / "model-calls.jsonl").unlink()
    with pytest.raises(FileNotFoundError, match="no evidence stream"):
        verify_chain(capsule, "model-calls")


def test_empty_stream_yields_empty_chain(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    (capsule / "empty.jsonl").write_text("")
    links = build_chain(capsule, "empty")
    assert links == []
    write_chain(capsule, "empty")
    verdict = verify_chain(capsule, "empty")
    assert verdict.ok
    assert verdict.seq_high == -1
    assert verdict.record_count == 0
