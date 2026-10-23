"""ADR-0094 §A exit 8 — the ledger verifier checks the checkpoint's Merkle inclusion.

``LedgerExit.MERKLE_FAILURE`` (8) was defined, documented in ``nova ledger verify --help``
and in ADR-0094's table, and never produced: ``verify_ledger`` did not look at the
``merkle`` block at all (ADR-0313 "Known gaps"). A checkpoint whose ``merkle`` block named
another leaf, or whose inclusion proof did not recompute its root, passed with exit 0 —
whose documented meaning includes "Merkle inclusion holds".

When a checkpoint carries a ``merkle`` block it is now verified: the leaf must be the
hash of this checkpoint's signed body, the root is verified only with its ``tree_size``
(ADR-0313 D4), and the inclusion proof must recompute the root. A checkpoint without a
``merkle`` block (the ``nova ledger anchor`` local-finalize path never appends to a log)
makes no inclusion claim and is not failed for it.
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
from novafabric.trust.ledger._verify import LedgerExit, verify_ledger
from novafabric.trust.novaseal.merkle import MerkleLog

runner = CliRunner()


@pytest.fixture()
def signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


def _verify_fn(signer: LocalSigner):  # type: ignore[no-untyped-def]
    def verify_fn(pae: bytes, sig: bytes) -> bool:
        return verify_with_pem(signer.public_pem, pae, sig)

    return verify_fn


def _logged_capsule(
    tmp_path: Path, signer: LocalSigner, *, root_version: int = 2, padding: int = 2
) -> Path:
    """A capsule whose checkpoint was appended (after *padding* other leaves) to a log."""
    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    (capsule / "model-calls.jsonl").write_text(
        "".join(json.dumps({"model": "m", "n": i}) + "\n" for i in range(3))
    )
    write_chain(capsule, "model-calls")
    log = MerkleLog(tmp_path / "merkle.db", root_version=root_version)
    try:
        for i in range(padding):
            log.append({"other": i})
        record = build_checkpoint(capsule, node_id="n1", epoch=3, epoch_pubkey="pk")
        sign_checkpoint(record, signer, capsule_dir=capsule, merkle_log=log)
        log.append({"after": True})  # the log keeps growing after the checkpoint
    finally:
        log.close()
    return capsule


def _edit(capsule: Path, change: Any) -> None:
    path = capsule / ".ledger" / "checkpoint.json"
    checkpoint = json.loads(path.read_text())
    change(checkpoint)
    path.write_text(json.dumps(checkpoint))


@pytest.mark.parametrize("root_version", [1, 2])
@pytest.mark.parametrize("padding", [0, 2, 5])
def test_logged_checkpoint_passes(
    tmp_path: Path, signer: LocalSigner, root_version: int, padding: int
) -> None:
    capsule = _logged_capsule(tmp_path, signer, root_version=root_version, padding=padding)
    verdict = verify_ledger(capsule, None, _verify_fn(signer))
    assert verdict.ok, verdict.reasons
    assert verdict.exit == LedgerExit.OK


def test_checkpoint_without_a_merkle_block_makes_no_inclusion_claim(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule = _logged_capsule(tmp_path, signer)
    _edit(capsule, lambda c: c.pop("merkle"))
    assert verify_ledger(capsule, None, _verify_fn(signer)).ok


def _set(key: str, value: Any):  # type: ignore[no-untyped-def]
    def change(checkpoint: dict[str, Any]) -> None:
        checkpoint["merkle"][key] = value

    return change


def _drop(key: str):  # type: ignore[no-untyped-def]
    def change(checkpoint: dict[str, Any]) -> None:
        checkpoint["merkle"].pop(key)

    return change


def _flip_first_proof_hash(checkpoint: dict[str, Any]) -> None:
    proof = checkpoint["merkle"]["inclusion_proof"]
    proof[0] = "sha256:" + ("0" if proof[0][7] != "0" else "1") + proof[0][8:]


def _edit_body_after_logging(checkpoint: dict[str, Any]) -> None:
    """Change a field the log leaf covers (``kind``), as if re-written after logging."""
    checkpoint["kind"] = "interim"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_set("root", "sha256:" + "b" * 64), id="root-swapped"),
        pytest.param(_set("leaf_hash", "sha256:" + "c" * 64), id="leaf-of-another-entry"),
        pytest.param(_flip_first_proof_hash, id="proof-tampered"),
        pytest.param(_set("log_index", 0), id="wrong-log-index"),
        pytest.param(_set("tree_size", 99), id="inflated-tree-size"),
        pytest.param(_drop("tree_size"), id="root-without-tree-size"),
        pytest.param(_drop("inclusion_proof"), id="no-proof"),
        pytest.param(_drop("log_index"), id="no-log-index"),
        pytest.param(_set("root_version", 7), id="unsupported-root-version"),
        pytest.param(_set("root_version", 1), id="v2-proof-claimed-as-v1"),
        pytest.param(_set("inclusion_proof", ["sha256:zz"]), id="malformed-proof-hash"),
        pytest.param(_set("root", "not-a-digest"), id="malformed-root"),
        pytest.param(lambda c: c.__setitem__("merkle", []), id="merkle-not-an-object"),
        pytest.param(_edit_body_after_logging, id="body-edited-after-logging"),
    ],
)
def test_bad_merkle_block_exits_8(tmp_path: Path, signer: LocalSigner, change: Any) -> None:
    capsule = _logged_capsule(tmp_path, signer)
    _edit(capsule, change)
    # No verify_fn: a body edit would otherwise stop at the signature (exit 6) first.
    verdict = verify_ledger(capsule, None, None)
    assert not verdict.ok
    assert verdict.exit == LedgerExit.MERKLE_FAILURE, verdict.reasons
    assert verdict.reasons


def test_v1_root_without_tree_size_exits_8(tmp_path: Path, signer: LocalSigner) -> None:
    """A v1 checkpoint logged before ADR-0313 carries no tree_size: not verifiable."""
    capsule = _logged_capsule(tmp_path, signer, root_version=1)
    _edit(capsule, _drop("tree_size"))
    verdict = verify_ledger(capsule, None, _verify_fn(signer))
    assert verdict.exit == LedgerExit.MERKLE_FAILURE
    assert "tree_size" in verdict.reasons[0]


def test_v1_tree_size_inflated_past_the_proof_depth_exits_8(
    tmp_path: Path, signer: LocalSigner
) -> None:
    """Leaf 2 of 3 under v1: claiming size 5 needs a 3-hash path; the 2-hash one is refused.

    Without the path-length check the 2-hash path still recomputes the size-3 root,
    so the inflated size would pass.
    """
    capsule = _logged_capsule(tmp_path, signer, root_version=1, padding=2)
    _edit(capsule, _set("tree_size", 5))
    assert verify_ledger(capsule, None, None).exit == LedgerExit.MERKLE_FAILURE


def test_v1_root_cannot_tell_size_3_from_4(tmp_path: Path, signer: LocalSigner) -> None:
    """Documented v1 limitation (ADR-0313): [a, b, c] and [a, b, c, c] share a root.

    A v1 proof for leaf 2 of 3 also proves leaf 2 of 4; only the log itself can refuse
    the larger size. v2 commits to the size (see ``inflated-tree-size`` above).
    """
    capsule = _logged_capsule(tmp_path, signer, root_version=1, padding=2)
    _edit(capsule, _set("tree_size", 4))
    assert verify_ledger(capsule, None, None).ok


def test_merkle_failure_is_reported_before_the_anchor(
    tmp_path: Path, signer: LocalSigner
) -> None:
    """ADR-0094 order: inclusion (8) is checked before the external anchor (9)."""
    capsule = _logged_capsule(tmp_path, signer)

    def change(checkpoint: dict[str, Any]) -> None:
        checkpoint["merkle"]["root"] = "sha256:" + "b" * 64
        checkpoint["anchor"] = {"method": "rfc3161", "anchored_value": "sha256:" + "d" * 64}

    _edit(capsule, change)
    assert verify_ledger(capsule, None, _verify_fn(signer)).exit == LedgerExit.MERKLE_FAILURE


def test_nova_ledger_verify_exits_8(tmp_path: Path, signer: LocalSigner) -> None:
    capsule = _logged_capsule(tmp_path, signer)
    _edit(capsule, _flip_first_proof_hash)
    pub = tmp_path / "pub.pem"
    pub.write_bytes(signer.public_pem)
    result = runner.invoke(app, ["ledger", "verify", str(capsule), "--pubkey", str(pub)])
    assert result.exit_code == 8, result.output
    assert "MERKLE_FAILURE" in result.output


def test_nova_ledger_verify_passes_a_logged_checkpoint(
    tmp_path: Path, signer: LocalSigner
) -> None:
    capsule = _logged_capsule(tmp_path, signer)
    pub = tmp_path / "pub.pem"
    pub.write_bytes(signer.public_pem)
    result = runner.invoke(app, ["ledger", "verify", str(capsule), "--pubkey", str(pub)])
    assert result.exit_code == 0, result.output
