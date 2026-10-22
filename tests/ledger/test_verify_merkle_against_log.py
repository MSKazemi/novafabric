"""Exit 8 against a real log — and what exit 0 proves without one (ADR-0094 §A, ADR-0313).

The exit-8 check added in the Compendium 2e defect batch verifies a checkpoint's
``merkle`` block *against itself*: the leaf is the hash of the signed body, the proof
recomputes the root at ``tree_size``. Nothing compared that tree with a log, and the
block sits outside the checkpoint signature, so anyone could replace it with a
fabricated one-leaf tree (root = leaf, empty proof) and pass with exit 0 — a tree that
no log ever held.

Now ``verify_ledger(..., merkle_log=...)`` / ``nova ledger verify --merkle-db`` checks
the block against the log: the log must hold this checkpoint's leaf at ``log_index``
and reproduce the recorded ``(tree_size, root)`` from its own leaves. Without a log,
the verdict says the block is only ``self-consistent`` — the documented limit, pinned
here so nobody reads exit 0 as "this checkpoint is in a log".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.evidence.signing import LocalSigner, generate_keypair, verify_with_pem
from novafabric.trust.ledger._chain import write_chain
from novafabric.trust.ledger._checkpoint import build_checkpoint, sign_checkpoint
from novafabric.trust.ledger._verify import (
    MERKLE_INCLUSION_LOG,
    MERKLE_INCLUSION_NONE,
    MERKLE_INCLUSION_SELF_CONSISTENT,
    LedgerExit,
    _signing_body,
    verify_ledger,
)
from novafabric.trust.novaseal.merkle import MerkleLog, _leaf_hash

runner = CliRunner()


@pytest.fixture()
def signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


def _verify_fn(signer: LocalSigner):  # type: ignore[no-untyped-def]
    def verify_fn(pae: bytes, sig: bytes) -> bool:
        return verify_with_pem(signer.public_pem, pae, sig)

    return verify_fn


def _capsule(tmp_path: Path, signer: LocalSigner, log: MerkleLog | None) -> Path:
    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    (capsule / "model-calls.jsonl").write_text(
        "".join(json.dumps({"model": "m", "n": i}) + "\n" for i in range(3))
    )
    write_chain(capsule, "model-calls")
    record = build_checkpoint(capsule, node_id="n1", epoch=3, epoch_pubkey="pk")
    sign_checkpoint(record, signer, capsule_dir=capsule, merkle_log=log)
    return capsule


def _logged(tmp_path: Path, signer: LocalSigner, root_version: int = 2) -> tuple[Path, Path]:
    """A capsule whose checkpoint is leaf 2 of a log that keeps growing afterwards."""
    db = tmp_path / "merkle.db"
    log = MerkleLog(db, root_version=root_version)
    try:
        log.append({"other": 0})
        log.append({"other": 1})
        capsule = _capsule(tmp_path, signer, log)
        log.append({"after": True})
    finally:
        log.close()
    return capsule, db


def _checkpoint(capsule: Path) -> dict[str, Any]:
    return json.loads((capsule / ".ledger" / "checkpoint.json").read_text())


def _write(capsule: Path, checkpoint: dict[str, Any]) -> None:
    (capsule / ".ledger" / "checkpoint.json").write_text(json.dumps(checkpoint))


def _fabricate_one_leaf_tree(capsule: Path) -> None:
    """Replace the merkle block with a self-consistent tree no log ever held."""
    checkpoint = _checkpoint(capsule)
    body = json.dumps(_signing_body(checkpoint), sort_keys=True, separators=(",", ":"))
    leaf = _leaf_hash(body.encode("utf-8"))
    checkpoint["merkle"] = {
        "root": "sha256:" + leaf,  # a one-leaf RFC 6962 tree's root is its leaf
        "leaf_hash": "sha256:" + leaf,
        "log_index": 0,
        "tree_size": 1,
        "root_version": 2,
        "inclusion_proof": [],
    }
    _write(capsule, checkpoint)


def _verify(capsule: Path, signer: LocalSigner, db: Path | None = None):  # type: ignore[no-untyped-def]
    if db is None:
        return verify_ledger(capsule, None, _verify_fn(signer))
    log = MerkleLog(db)
    try:
        return verify_ledger(capsule, None, _verify_fn(signer), merkle_log=log)
    finally:
        log.close()


# ---------------------------------------------------------------------------
# The documented limit: without a log, exit 0 means "self-consistent", nothing more
# ---------------------------------------------------------------------------


def test_without_a_log_a_fabricated_tree_is_only_self_consistent(
    tmp_path: Path, signer: LocalSigner
) -> None:
    """Pins the limit ``nova ledger verify --help`` documents — not a guarantee."""
    capsule, _db = _logged(tmp_path, signer)
    _fabricate_one_leaf_tree(capsule)
    verdict = _verify(capsule, signer)
    assert verdict.ok, verdict.reasons
    assert verdict.merkle_inclusion == MERKLE_INCLUSION_SELF_CONSISTENT


def test_a_checkpoint_without_a_block_and_without_a_log_claims_nothing(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule = _capsule(tmp_path, signer, None)
    verdict = _verify(capsule, signer)
    assert verdict.ok
    assert verdict.merkle_inclusion == MERKLE_INCLUSION_NONE


# ---------------------------------------------------------------------------
# Against the log
# ---------------------------------------------------------------------------


def test_a_fabricated_tree_fails_against_the_log(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, db = _logged(tmp_path, signer)
    _fabricate_one_leaf_tree(capsule)
    verdict = _verify(capsule, signer, db)
    assert verdict.exit == LedgerExit.MERKLE_FAILURE, verdict.reasons
    assert any("log" in r for r in verdict.reasons), verdict.reasons


@pytest.mark.parametrize("root_version", [1, 2])
def test_a_logged_checkpoint_passes_against_its_log(
    tmp_path: Path, signer: LocalSigner, root_version: int
) -> None:
    capsule, db = _logged(tmp_path, signer, root_version)
    verdict = _verify(capsule, signer, db)
    assert verdict.ok, verdict.reasons
    assert verdict.merkle_inclusion == MERKLE_INCLUSION_LOG


def test_a_checkpoint_absent_from_the_log_fails(tmp_path: Path, signer: LocalSigner) -> None:
    """A consistent block from another log is not inclusion in *this* log."""
    capsule, _db = _logged(tmp_path, signer)
    other = tmp_path / "other.db"
    log = MerkleLog(other)
    for i in range(4):
        log.append({"unrelated": i})
    log.close()
    verdict = _verify(capsule, signer, other)
    assert verdict.exit == LedgerExit.MERKLE_FAILURE, verdict.reasons


def test_a_head_the_log_does_not_reproduce_fails(tmp_path: Path, signer: LocalSigner) -> None:
    """Right leaf at the right index, but a tree head the log never had."""
    capsule, db = _logged(tmp_path, signer)
    checkpoint = _checkpoint(capsule)
    # A v2 tree of 3 leaves recomputed with a different leaf 0 → a self-consistent
    # proof for leaf 2 under a root the log never had.
    from novafabric.trust.novaseal.merkle import (
        _compute_inclusion_proof,
        compute_root,
    )

    log = MerkleLog(db)
    leaves = [log.get_leaf_hash(i) for i in range(3)]
    log.close()
    leaves[0] = _leaf_hash(b'{"forged":true}')
    checkpoint["merkle"].update(
        root="sha256:" + compute_root(leaves, root_version=2),
        tree_size=3,
        inclusion_proof=["sha256:" + h for h in _compute_inclusion_proof(2, leaves, 2)],
    )
    _write(capsule, checkpoint)
    assert _verify(capsule, signer).ok  # self-consistent on its own
    verdict = _verify(capsule, signer, db)
    assert verdict.exit == LedgerExit.MERKLE_FAILURE, verdict.reasons


def test_a_removed_block_is_found_in_the_log(tmp_path: Path, signer: LocalSigner) -> None:
    """The block is unsigned; with the log, inclusion no longer depends on it."""
    capsule, db = _logged(tmp_path, signer)
    checkpoint = _checkpoint(capsule)
    checkpoint.pop("merkle")
    _write(capsule, checkpoint)
    verdict = _verify(capsule, signer, db)
    assert verdict.ok, verdict.reasons
    assert verdict.merkle_inclusion == MERKLE_INCLUSION_LOG


def test_an_unlogged_checkpoint_fails_when_a_log_is_required(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule = _capsule(tmp_path, signer, None)  # the `nova ledger anchor` path
    db = tmp_path / "merkle.db"
    MerkleLog(db).close()
    verdict = _verify(capsule, signer, db)
    assert verdict.exit == LedgerExit.MERKLE_FAILURE, verdict.reasons


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _pubkey(tmp_path: Path, signer: LocalSigner) -> Path:
    path = tmp_path / "pub.pem"
    path.write_bytes(signer.public_pem)
    return path


def test_cli_merkle_db_catches_a_fabricated_tree(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, db = _logged(tmp_path, signer)
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    _fabricate_one_leaf_tree(capsule)
    pub = _pubkey(tmp_path, signer)

    without = runner.invoke(app, ["ledger", "verify", str(capsule), "--pubkey", str(pub)])
    assert without.exit_code == 0, without.output
    assert "self-consistent only" in without.output

    with_log = runner.invoke(
        app, ["ledger", "verify", str(capsule), "--pubkey", str(pub), "--merkle-db", str(db)]
    )
    assert with_log.exit_code == int(LedgerExit.MERKLE_FAILURE), with_log.output


def test_cli_merkle_db_confirms_a_logged_checkpoint(tmp_path: Path, signer: LocalSigner) -> None:
    capsule, db = _logged(tmp_path, signer)
    result = runner.invoke(
        app,
        [
            "ledger", "verify", str(capsule),
            "--pubkey", str(_pubkey(tmp_path, signer)),
            "--merkle-db", str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "checked against the Merkle log" in result.output


def test_cli_merkle_db_that_does_not_exist_is_a_usage_error(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule, _db = _logged(tmp_path, signer)
    missing = tmp_path / "nope.db"
    result = runner.invoke(app, ["ledger", "verify", str(capsule), "--merkle-db", str(missing)])
    assert result.exit_code == 2, result.output
    assert not missing.exists()  # verification never creates a log
