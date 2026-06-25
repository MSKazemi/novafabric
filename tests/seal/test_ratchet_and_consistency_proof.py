"""ADR-0089 ratchet + ADR-0041 v0.2 consistency-proof tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.seal_propose import seal_app
from novafabric.trust.novaseal.merkle import (
    MerkleError,
    MerkleLog,
    _compute_root,
    _decompose_range,
    _leaf_hash,
    _root_from_parts,
    verify_consistency_proof,
)
from novafabric.trust.novaseal.ratchet import (
    EpochMonotonicityError,
    EpochRegistry,
    RatchetError,
    RatchetSigner,
    init_ratchet,
    load_state,
    ratchet_dir,
    rotate,
)

# ---------------------------------------------------------------------------
# Consistency proof — pure primitives
# ---------------------------------------------------------------------------


def _hashes(n: int) -> list[str]:
    return [_leaf_hash(f"entry-{i}".encode()) for i in range(n)]


class TestRootFromParts:
    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 16, 31, 64])
    def test_decomposition_reproduces_padded_root(self, n: int) -> None:
        hashes = _hashes(n)
        parts = _decompose_range(hashes, 0, n)
        assert _root_from_parts(parts) == _compute_root(hashes)


class TestConsistencyProof:
    @pytest.mark.parametrize(
        ("old", "new"), [(1, 2), (2, 4), (3, 7), (4, 6), (5, 13), (8, 9), (7, 7)]
    )
    def test_valid_proof_verifies(self, tmp_path: Path, old: int, new: int) -> None:
        log = MerkleLog(tmp_path / "log.db")
        roots = {}
        for i in range(new):
            result = log.append({"i": i})
            roots[int(result["tree_size"])] = result["root_hash"]  # type: ignore[index]
        proof = log.consistency_proof(old)
        assert verify_consistency_proof(proof, str(roots[old]), str(roots[new]))
        assert log.root_at_size(old) == roots[old]

    def test_forked_log_proof_fails(self, tmp_path: Path) -> None:
        log_a = MerkleLog(tmp_path / "a.db")
        log_b = MerkleLog(tmp_path / "b.db")
        for i in range(4):
            log_a.append({"i": i})
            log_b.append({"i": i})
        root_a4 = log_a.current_root()
        log_a.append({"i": 4})
        # log_b diverges at index 4
        log_b.append({"i": "tampered"})
        proof_b = log_b.consistency_proof(4)
        # proof from the forked log does not connect a's old root... it does
        # (prefix identical), but its new root differs from a's:
        assert root_a4 is not None
        assert not verify_consistency_proof(
            proof_b, root_a4, str(log_a.current_root())
        )

    def test_tampered_prefix_detected(self, tmp_path: Path) -> None:
        log = MerkleLog(tmp_path / "log.db")
        old_root = ""
        for i in range(6):
            result = log.append({"i": i})
            if result["tree_size"] == 3:
                old_root = str(result["root_hash"])
        proof = log.consistency_proof(3)
        # claim a different old root than the proof's prefix commits to
        wrong_old = _leaf_hash(b"wrong")
        assert not verify_consistency_proof(proof, wrong_old, str(log.current_root()))
        assert verify_consistency_proof(proof, old_root, str(log.current_root()))

    def test_invalid_sizes_raise(self, tmp_path: Path) -> None:
        log = MerkleLog(tmp_path / "log.db")
        log.append({"i": 0})
        with pytest.raises(MerkleError):
            log.consistency_proof(0)
        with pytest.raises(MerkleError):
            log.consistency_proof(5)
        with pytest.raises(MerkleError):
            log.consistency_proof(1, 99)

    def test_proof_is_logarithmic(self, tmp_path: Path) -> None:
        log = MerkleLog(tmp_path / "log.db")
        for i in range(100):
            log.append({"i": i})
        proof = log.consistency_proof(37)
        total = len(proof["old_parts"]) + len(proof["tail_parts"])
        assert total <= 14  # O(log old + log new), far below 100


class TestLogVerifyCli:
    def test_consistency_flag_ok_and_failure(self, tmp_path: Path) -> None:
        db = tmp_path / "log.db"
        log = MerkleLog(db)
        for i in range(5):
            log.append({"i": i})
        log.close()
        runner = CliRunner()
        result = runner.invoke(
            seal_app, ["log", "verify", "--db", str(db), "--consistency", "3"]
        )
        assert result.exit_code == 0, result.output
        assert "Append-only proof 3 → 5" in result.output
        bad = runner.invoke(
            seal_app, ["log", "verify", "--db", str(db), "--consistency", "9"]
        )
        assert bad.exit_code == 2


# ---------------------------------------------------------------------------
# Ratchet (ADR-0089)
# ---------------------------------------------------------------------------


@pytest.fixture()
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "ratchet"
    monkeypatch.setenv("NOVAFABRIC_RATCHET_DIR", str(directory))
    return directory


class TestRatchet:
    def test_init_creates_epoch_zero_with_registry(self, state_dir: Path) -> None:
        state = init_ratchet("node-a", state_dir)
        assert state.epoch == 0
        registry = EpochRegistry(state_dir)
        assert registry.latest_epoch("node-a") == 0
        assert (state_dir / "node-a.json").stat().st_mode & 0o777 == 0o600

    def test_double_init_refused(self, state_dir: Path) -> None:
        init_ratchet("node-a", state_dir)
        with pytest.raises(RatchetError, match="already exists"):
            init_ratchet("node-a", state_dir)

    def test_sign_verify_across_three_epochs(self, state_dir: Path) -> None:
        init_ratchet("node-a", state_dir)
        registry = EpochRegistry(state_dir)
        payload = b"seal-me"
        signatures = {}
        for expected_epoch in range(3):
            signer = RatchetSigner("node-a", state_dir)
            assert signer.epoch == expected_epoch
            signatures[expected_epoch] = signer.sign(payload)
            rotate("node-a", state_dir)
        for epoch, sig in signatures.items():
            assert registry.verify("node-a", epoch, payload, sig)
        # cross-epoch signature does not verify
        assert not registry.verify("node-a", 0, payload, signatures[1])

    def test_rotation_erases_old_chain_key(self, state_dir: Path) -> None:
        state0 = init_ratchet("node-a", state_dir)
        old_key_hex = state0.chain_key_hex
        rotate("node-a", state_dir)
        state1 = load_state("node-a", state_dir)
        assert state1.epoch == 1
        assert state1.chain_key_hex != old_key_hex
        # old chain key no longer present anywhere on disk
        on_disk = b"".join(
            p.read_bytes() for p in state_dir.iterdir() if p.is_file()
        )
        assert old_key_hex.encode() not in on_disk

    def test_epoch_monotonicity_violation_detected(self, state_dir: Path) -> None:
        init_ratchet("node-a", state_dir)
        rotate("node-a", state_dir)
        rotate("node-a", state_dir)
        registry = EpochRegistry(state_dir)
        registry.check_monotonic("node-a", 2)  # current epoch ok
        with pytest.raises(EpochMonotonicityError, match="rollback"):
            registry.check_monotonic("node-a", 1)

    def test_keyid_carries_node_and_epoch(self, state_dir: Path) -> None:
        init_ratchet("node-a", state_dir)
        rotate("node-a", state_dir)
        signer = RatchetSigner("node-a", state_dir)
        assert signer.keyid == "ratchet:node-a:1"

    def test_missing_state_raises_named_error(self, state_dir: Path) -> None:
        with pytest.raises(RatchetError, match="no ratchet state"):
            load_state("node-z", state_dir)

    def test_deterministic_derivation_same_seed(self, tmp_path: Path) -> None:
        seed = b"\x01" * 32
        init_ratchet("n1", tmp_path / "a", seed=seed)
        init_ratchet("n1", tmp_path / "b", seed=seed)
        rotate("n1", tmp_path / "a")
        rotate("n1", tmp_path / "b")
        assert (
            load_state("n1", tmp_path / "a").chain_key_hex
            == load_state("n1", tmp_path / "b").chain_key_hex
        )

    def test_ratchet_dir_env_override(self, state_dir: Path) -> None:
        assert ratchet_dir() == state_dir


class TestRatchetCli:
    def test_init_rotate_status_round_trip(self, state_dir: Path) -> None:
        runner = CliRunner()
        init = runner.invoke(seal_app, ["ratchet", "init", "--node-id", "node-a"])
        assert init.exit_code == 0, init.output
        rotated = runner.invoke(
            seal_app, ["ratchet", "rotate", "--node-id", "node-a"]
        )
        assert rotated.exit_code == 0
        assert "epoch 1" in rotated.output
        status = runner.invoke(
            seal_app, ["ratchet", "status", "--node-id", "node-a"]
        )
        assert status.exit_code == 0
        assert "epoch:   1" in status.output
        assert "[0, 1]" in status.output

    def test_rotate_without_init_fails(self, state_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            seal_app, ["ratchet", "rotate", "--node-id", "node-z"]
        )
        assert result.exit_code == 1
