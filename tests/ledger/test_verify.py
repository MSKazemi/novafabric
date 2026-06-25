"""ADR-0094 (A) — ledger verifier + exit-code taxonomy.

Exit taxonomy (subset implemented in this slice): 0 ok, 3 tamper, 4 reorder,
5 truncation, 6 bad signature, 7 epoch rollback, 8 merkle inclusion failure,
9 anchor mismatch, 10 no checkpoint.

Includes the assume-compromise tests: an adversary with full file-write access
who edits/reorders/truncates a ``.jsonl`` line — and even recomputes the
sidecar chain — still fails verification, because the checkpoint head was
signed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.evidence.signing import LocalSigner, generate_keypair, verify_with_pem
from novafabric.trust.ledger._chain import write_chain
from novafabric.trust.ledger._checkpoint import build_checkpoint, sign_checkpoint
from novafabric.trust.ledger._verify import (
    LedgerExit,
    exit_code_for,
    verify_ledger,
)


@pytest.fixture()
def signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


def _verify_fn_for(signer: LocalSigner):
    def verify_fn(pae: bytes, sig: bytes) -> bool:
        return verify_with_pem(signer.public_pem, pae, sig)

    return verify_fn


def _sealed_capsule(tmp_path: Path, signer: LocalSigner) -> tuple[Path, dict]:
    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    (capsule / "model-calls.jsonl").write_text(
        json.dumps({"model": "m", "n": 0}) + "\n"
        + json.dumps({"model": "m", "n": 1}) + "\n"
        + json.dumps({"model": "m", "n": 2}) + "\n"
    )
    write_chain(capsule, "model-calls")
    record = build_checkpoint(capsule, node_id="n1", epoch=3, epoch_pubkey="pk")
    envelope = sign_checkpoint(record, signer, capsule_dir=capsule)
    return capsule, envelope


def test_clean_capsule_passes(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, envelope = _sealed_capsule(tmp_path, signer)
    verdict = verify_ledger(capsule, envelope, _verify_fn_for(signer))
    assert verdict.ok
    assert verdict.exit == LedgerExit.OK
    assert exit_code_for(verdict) == 0


def test_no_checkpoint_returns_exit_10(tmp_path: Path, signer: LocalSigner) -> None:
    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    (capsule / "model-calls.jsonl").write_text(json.dumps({"n": 0}) + "\n")
    write_chain(capsule, "model-calls")
    verdict = verify_ledger(capsule)
    assert not verdict.ok
    assert verdict.exit == LedgerExit.NO_CHECKPOINT
    assert exit_code_for(verdict) == 10


def test_content_edit_returns_exit_3(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, envelope = _sealed_capsule(tmp_path, signer)
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    lines[1] = json.dumps({"model": "m", "n": 999})
    (capsule / "model-calls.jsonl").write_text("\n".join(lines) + "\n")
    verdict = verify_ledger(capsule, envelope, _verify_fn_for(signer))
    assert verdict.exit == LedgerExit.TAMPER
    assert exit_code_for(verdict) == 3


def test_reorder_returns_exit_4(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, envelope = _sealed_capsule(tmp_path, signer)
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    lines[0], lines[2] = lines[2], lines[0]
    (capsule / "model-calls.jsonl").write_text("\n".join(lines) + "\n")
    verdict = verify_ledger(capsule, envelope, _verify_fn_for(signer))
    assert verdict.exit == LedgerExit.REORDER
    assert exit_code_for(verdict) == 4


def test_truncation_returns_exit_5(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, envelope = _sealed_capsule(tmp_path, signer)
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    (capsule / "model-calls.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    verdict = verify_ledger(capsule, envelope, _verify_fn_for(signer))
    assert verdict.exit == LedgerExit.TRUNCATION
    assert exit_code_for(verdict) == 5


def test_bad_signature_returns_exit_6(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, envelope = _sealed_capsule(tmp_path, signer)
    # everything intact, but the signature does not verify under verify_fn
    verdict = verify_ledger(capsule, envelope, lambda pae, sig: False)
    assert verdict.exit == LedgerExit.BAD_SIGNATURE
    assert exit_code_for(verdict) == 6


def test_epoch_rollback_returns_exit_7(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, envelope = _sealed_capsule(tmp_path, signer)
    # checkpoint sealed epoch 3; an observed epoch of 5 means a rollback
    verdict = verify_ledger(
        capsule, envelope, _verify_fn_for(signer), observed_epoch=5
    )
    assert verdict.exit == LedgerExit.EPOCH_ROLLBACK
    assert exit_code_for(verdict) == 7


def test_anchor_mismatch_returns_exit_9(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, envelope = _sealed_capsule(tmp_path, signer)
    # inject a tampered anchor block whose anchored_value does not match
    checkpoint = json.loads((capsule / ".ledger" / "checkpoint.json").read_text())
    checkpoint["anchor"] = {
        "method": "rfc3161",
        "anchored_value": "sha256:" + "a" * 64,
    }
    (capsule / ".ledger" / "checkpoint.json").write_text(json.dumps(checkpoint))
    verdict = verify_ledger(capsule, envelope, _verify_fn_for(signer))
    assert verdict.exit == LedgerExit.ANCHOR_MISMATCH
    assert exit_code_for(verdict) == 9


def test_assume_compromise_recomputed_chain_still_fails(
    tmp_path: Path, signer: LocalSigner
) -> None:
    """Adversary edits a jsonl record AND recomputes the sidecar chain.

    The local chain now self-verifies, but the signed checkpoint pinned the
    original head — so verification fails on the head mismatch, the property
    the unkeyed audit/_log.py cannot provide.
    """
    capsule, envelope = _sealed_capsule(tmp_path, signer)
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    lines[1] = json.dumps({"model": "m", "n": 666})
    (capsule / "model-calls.jsonl").write_text("\n".join(lines) + "\n")
    # adversary recomputes the sidecar chain to match the edited content
    write_chain(capsule, "model-calls")
    verdict = verify_ledger(capsule, envelope, _verify_fn_for(signer))
    assert not verdict.ok
    # head no longer matches the signed checkpoint head => tamper
    assert verdict.exit == LedgerExit.TAMPER


def test_assume_compromise_truncate_and_rechain_still_fails(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule, envelope = _sealed_capsule(tmp_path, signer)
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    (capsule / "model-calls.jsonl").write_text("\n".join(lines[:-1]) + "\n")
    write_chain(capsule, "model-calls")  # rechain the truncated stream
    verdict = verify_ledger(capsule, envelope, _verify_fn_for(signer))
    assert not verdict.ok
    assert verdict.exit == LedgerExit.TRUNCATION


def test_verify_without_verify_fn_skips_signature_check(
    tmp_path: Path, signer: LocalSigner
) -> None:
    """A structural-only verification (no verify_fn) still checks the chains."""
    capsule, _ = _sealed_capsule(tmp_path, signer)
    verdict = verify_ledger(capsule)
    assert verdict.ok
    assert verdict.exit == LedgerExit.OK


def test_corrupt_checkpoint_json_treated_as_no_checkpoint(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule, _ = _sealed_capsule(tmp_path, signer)
    (capsule / ".ledger" / "checkpoint.json").write_text("{ not json")
    verdict = verify_ledger(capsule)
    assert verdict.exit == LedgerExit.NO_CHECKPOINT


def test_deleted_stream_file_is_truncation(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, _ = _sealed_capsule(tmp_path, signer)
    (capsule / "model-calls.jsonl").unlink()
    verdict = verify_ledger(capsule)
    assert verdict.exit == LedgerExit.TRUNCATION


def test_signature_block_without_value_fails_sig_check(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule, _ = _sealed_capsule(tmp_path, signer)
    checkpoint = json.loads((capsule / ".ledger" / "checkpoint.json").read_text())
    checkpoint["signature"] = {"algorithm": "ed25519"}
    (capsule / ".ledger" / "checkpoint.json").write_text(json.dumps(checkpoint))
    verdict = verify_ledger(capsule, None, _verify_fn_for(signer))
    assert verdict.exit == LedgerExit.BAD_SIGNATURE


def test_anchor_against_merkle_root_mismatch(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule, _ = _sealed_capsule(tmp_path, signer)
    checkpoint = json.loads((capsule / ".ledger" / "checkpoint.json").read_text())
    checkpoint["merkle"] = {"root": "sha256:" + "b" * 64, "leaf_hash": "sha256:" + "c" * 64}
    checkpoint["anchor"] = {
        "method": "rfc3161",
        "anchored_value": "sha256:" + "d" * 64,  # != merkle root
    }
    (capsule / ".ledger" / "checkpoint.json").write_text(json.dumps(checkpoint))
    verdict = verify_ledger(capsule, None, _verify_fn_for(signer))
    assert verdict.exit == LedgerExit.ANCHOR_MISMATCH


def test_malformed_signature_value_fails_sig_check(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule, _ = _sealed_capsule(tmp_path, signer)
    checkpoint = json.loads((capsule / ".ledger" / "checkpoint.json").read_text())
    checkpoint["signature"]["value"] = "!!!not-base64!!!"
    (capsule / ".ledger" / "checkpoint.json").write_text(json.dumps(checkpoint))
    verdict = verify_ledger(capsule, None, _verify_fn_for(signer))
    assert verdict.exit == LedgerExit.BAD_SIGNATURE


def test_anchor_method_none_passes(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, _ = _sealed_capsule(tmp_path, signer)
    checkpoint = json.loads((capsule / ".ledger" / "checkpoint.json").read_text())
    checkpoint["anchor"] = {"method": "none", "anchored_value": "sha256:" + "0" * 64}
    (capsule / ".ledger" / "checkpoint.json").write_text(json.dumps(checkpoint))
    verdict = verify_ledger(capsule, None, _verify_fn_for(signer))
    assert verdict.ok
