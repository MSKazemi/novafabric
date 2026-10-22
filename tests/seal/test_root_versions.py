"""ADR-0313 — NovaSeal log roots are versioned; v2 commits to tree size; v1 verifies only
with ``tree_size``.

The v1 root (``_compute_root`` through v0.105) pads an odd level by duplicating its last
node, so ``[a, b, c]`` and ``[a, b, c, c]`` share a root: the root does not commit to the
tree size. Re-sealing the same capsule with the same key produces exactly that duplicated
leaf. v2 is the RFC 6962 / RFC 9162 Merkle Tree Hash (split at the largest power of two,
no duplication), which commits to the size. These tests pin:

(a) v2 distinguishes ``[a,b,c]`` from ``[a,b,c,c]`` (and v1, documented, does not);
(b) a root or proof presented without its size is refused, for both versions;
(c) a v1 root with the wrong ``tree_size`` is refused;
(d) existing v1 data (the v0.102 golden capsule, a pre-ADR-0313 log database) still
    verifies;
(e) v2 inclusion and consistency proofs verify for every size 1..17;
(f) property: no two leaf lists of different lengths share a v2 root.

Imports of names introduced by ADR-0313 sit inside each test so that one missing name
fails one test, not the module.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from novafabric.trust.novaseal.merkle import MerkleError, MerkleLog, _leaf_hash

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "novaseal"
LEGACY = FIXTURES / "legacy-v0.102"


def _leaves(n: int, prefix: str = "e") -> list[str]:
    return [_leaf_hash(f"{prefix}-{i}".encode()) for i in range(n)]


def _entry_leaf(entry: dict[str, object]) -> str:
    return _leaf_hash(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())


def _legacy_entries() -> list[dict[str, object]]:
    entries: list[dict[str, object]] = json.loads(
        (LEGACY / "sealer-log-entries.json").read_text()
    )
    return entries


def _legacy_record() -> dict[str, object]:
    record: dict[str, object] = json.loads(
        (LEGACY / "capsule" / ".seal" / "log-entry.json").read_text()
    )
    return record


def _sealer_log(path: Path) -> MerkleLog:
    log = MerkleLog(path)
    for entry in _legacy_entries():
        log.append(entry)
    return log


# ---------------------------------------------------------------------------
# (a) v2 commits to the tree size
# ---------------------------------------------------------------------------


def test_v1_root_does_not_commit_to_tree_size() -> None:
    """The defect, pinned: kept only so that existing v1 data keeps verifying."""
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V1, compute_root

    a, b, c = _leaves(3)
    assert compute_root([a, b, c], root_version=ROOT_VERSION_V1) == compute_root(
        [a, b, c, c], root_version=ROOT_VERSION_V1
    )


def test_v2_roots_of_abc_and_abcc_differ() -> None:
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V2, compute_root

    a, b, c = _leaves(3)
    assert compute_root([a, b, c], root_version=ROOT_VERSION_V2) != compute_root(
        [a, b, c, c], root_version=ROOT_VERSION_V2
    )


def test_v2_root_equals_an_independent_rfc6962_implementation() -> None:
    """v2 is RFC 6962 MTH: cross-checked against ``evidence/merkle.py`` (read-only use)."""
    from novafabric.evidence.merkle import _merkle_root as rfc6962_root
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V2, compute_root

    for n in range(0, 34):
        leaves = _leaves(n)
        expected = rfc6962_root([bytes.fromhex(h) for h in leaves]).hex()
        assert compute_root(leaves, root_version=ROOT_VERSION_V2) == expected, n


def test_new_log_writes_v2_and_records_the_version(tmp_path: Path) -> None:
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V2, compute_root

    log = MerkleLog(tmp_path / "new.db")
    assert log.root_version == ROOT_VERSION_V2
    a = log.append({"i": 0})
    log.append({"i": 1})
    last = log.append({"i": 1})  # the same entry again: a duplicated last leaf
    assert a["root_version"] == ROOT_VERSION_V2
    assert last["root_version"] == ROOT_VERSION_V2
    assert last["tree_size"] == 3
    hashes = [str(a["leaf_hash"]), str(last["leaf_hash"]), str(last["leaf_hash"])]
    assert last["root_hash"] == compute_root(hashes, root_version=ROOT_VERSION_V2)
    assert log.verify_consistency().consistent
    log.close()


def test_v2_merkle_layers_end_in_the_v2_root() -> None:
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V2, compute_root, merkle_layers

    for n in range(1, 18):
        leaves = _leaves(n)
        layers = merkle_layers(leaves, root_version=ROOT_VERSION_V2)
        assert layers[0] == leaves
        assert layers[-1] == [compute_root(leaves, root_version=ROOT_VERSION_V2)]


# ---------------------------------------------------------------------------
# (b) a root without its size is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", [1, 2])
def test_verify_root_without_size_is_refused(version: int) -> None:
    from novafabric.trust.novaseal.merkle import (
        MerkleSizeRequiredError,
        compute_root,
        verify_root,
    )

    leaves = _leaves(3)
    root = compute_root(leaves, root_version=version)
    with pytest.raises(MerkleSizeRequiredError):
        verify_root(leaves, root, tree_size=None, root_version=version)


def test_consistency_proof_verified_without_sizes_is_refused(tmp_path: Path) -> None:
    """Before ADR-0313 the verifier recomputed roots from the proof's own parts and
    never compared them with the sizes the caller believed — any old/new size passed."""
    from novafabric.trust.novaseal.merkle import verify_consistency_proof

    log = MerkleLog(tmp_path / "m.db")
    for i in range(6):
        log.append({"i": i})
    proof = log.consistency_proof(3, 6)
    with pytest.raises(MerkleError):
        verify_consistency_proof(proof, str(proof["old_root"]), str(proof["new_root"]))
    log.close()


def test_carried_root_without_tree_size_is_refused(tmp_path: Path) -> None:
    from novafabric.trust.novaseal import LOG_INCLUSION_FAILED, verify_seal_dir

    capsule = tmp_path / "capsule"
    shutil.copytree(LEGACY / "capsule", capsule)
    log_file = capsule / ".seal" / "log-entry.json"
    record = json.loads(log_file.read_text())
    del record["tree_size"]
    log_file.write_text(json.dumps(record))
    result = verify_seal_dir(capsule / ".seal", merkle_log=_sealer_log(tmp_path / "s.db"))
    assert result.log_inclusion == LOG_INCLUSION_FAILED
    assert not result.valid
    assert any("tree_size" in e for e in result.errors)


# ---------------------------------------------------------------------------
# (c) a v1 root with the wrong tree_size is refused
# ---------------------------------------------------------------------------


def test_v1_root_with_wrong_tree_size_is_refused() -> None:
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V1, compute_root, verify_root

    a, b, c = _leaves(3)
    root = compute_root([a, b, c], root_version=ROOT_VERSION_V1)
    assert verify_root([a, b, c], root, tree_size=3, root_version=ROOT_VERSION_V1)
    # [a, b, c, c] has the same v1 root; only the size tells them apart.
    assert not verify_root([a, b, c, c], root, tree_size=3, root_version=ROOT_VERSION_V1)
    assert not verify_root([a, b, c], root, tree_size=4, root_version=ROOT_VERSION_V1)


def test_legacy_capsule_with_inflated_tree_size_fails_against_the_sealer_log(
    tmp_path: Path,
) -> None:
    """A v1 carried proof for leaf 2 of 3 is byte-identical to one for leaf 2 of
    [a, b, c, c], so only the log can refuse the inflated size. Before ADR-0313 the
    local-log check ignored the recorded tree head and reported ``local-log``."""
    from novafabric.trust.novaseal import LOG_INCLUSION_FAILED, verify_seal_dir

    capsule = tmp_path / "capsule"
    shutil.copytree(LEGACY / "capsule", capsule)
    log_file = capsule / ".seal" / "log-entry.json"
    record = json.loads(log_file.read_text())
    record["tree_size"] = 4
    log_file.write_text(json.dumps(record))
    result = verify_seal_dir(capsule / ".seal", merkle_log=_sealer_log(tmp_path / "s.db"))
    assert result.log_inclusion == LOG_INCLUSION_FAILED
    assert not result.valid


def test_v1_consistency_proof_with_wrong_sizes_is_refused(tmp_path: Path) -> None:
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V1, verify_consistency_proof

    log = MerkleLog(tmp_path / "v1.db", root_version=ROOT_VERSION_V1)
    for i in range(6):
        log.append({"i": i})
    proof = log.consistency_proof(3, 6)
    old, new = str(proof["old_root"]), str(proof["new_root"])
    assert verify_consistency_proof(proof, old, new, old_size=3, new_size=6)
    assert not verify_consistency_proof(proof, old, new, old_size=4, new_size=6)
    assert not verify_consistency_proof(proof, old, new, old_size=3, new_size=7)
    log.close()


def test_witness_refuses_a_checkpoint_whose_size_the_proof_does_not_establish(
    tmp_path: Path,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from novafabric.trust.novaseal.witness import Checkpoint, Witness, WitnessRefusedError

    log = MerkleLog(tmp_path / "w.db")
    for i in range(3):
        log.append({"i": i})
    cp3 = Checkpoint.from_log("origin", log)
    w = Witness("W", Ed25519PrivateKey.generate())
    w.cosign(cp3, consistency_proof=None, prev_checkpoint=None)
    log.append({"i": 3})
    proof_3_to_4 = log.consistency_proof(3, 4)
    for i in range(4, 6):
        log.append({"i": i})
    # Claims size 6, but carries the size-4 root and a 3 -> 4 proof.
    lying = Checkpoint("origin", 6, str(proof_3_to_4["new_root"]), log.root_version)
    with pytest.raises(WitnessRefusedError):
        w.cosign(lying, consistency_proof=proof_3_to_4, prev_checkpoint=cp3)
    log.close()


def test_witness_refuses_a_checkpoint_mislabelled_with_another_root_version(
    tmp_path: Path,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V1
    from novafabric.trust.novaseal.witness import Checkpoint, Witness, WitnessRefusedError

    log = MerkleLog(tmp_path / "w.db")  # v2
    for i in range(3):
        log.append({"i": i})
    cp3 = Checkpoint.from_log("origin", log)
    w = Witness("W", Ed25519PrivateKey.generate())
    w.cosign(cp3, consistency_proof=None, prev_checkpoint=None)
    for i in range(3, 5):
        log.append({"i": i})
    proof = log.consistency_proof(3, 5)
    mislabelled = Checkpoint("origin", 5, str(log.current_root()), ROOT_VERSION_V1)
    with pytest.raises(WitnessRefusedError):
        w.cosign(mislabelled, consistency_proof=proof, prev_checkpoint=cp3)
    log.close()


# ---------------------------------------------------------------------------
# (d) existing v1 data keeps verifying
# ---------------------------------------------------------------------------


def test_legacy_fixture_root_reproduces_as_v1_with_its_tree_size() -> None:
    from novafabric.trust.novaseal.merkle import (
        ROOT_VERSION_V1,
        ROOT_VERSION_V2,
        compute_root,
        root_version_of,
        verify_root,
    )

    record = _legacy_record()
    leaves = [_entry_leaf(e) for e in _legacy_entries()]
    assert "root_version" not in record  # absent means v1
    assert root_version_of(record) == ROOT_VERSION_V1
    assert record["tree_size"] == len(leaves) == 3
    assert verify_root(leaves, str(record["root_hash"]), tree_size=3, root_version=1)
    # Three leaves is not a power of two, so v2 cannot be confused with the v1 root.
    assert compute_root(leaves, root_version=ROOT_VERSION_V2) != record["root_hash"]


def test_legacy_capsule_verifies_against_an_explicit_v1_sealer_log(tmp_path: Path) -> None:
    from novafabric.trust.novaseal import LOG_INCLUSION_LOCAL, verify_seal_dir
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V1

    log = MerkleLog(tmp_path / "s.db", root_version=ROOT_VERSION_V1)
    for entry in _legacy_entries():
        log.append(entry)
    assert log.current_root() == _legacy_record()["root_hash"]
    capsule = tmp_path / "capsule"
    shutil.copytree(LEGACY / "capsule", capsule)
    result = verify_seal_dir(capsule / ".seal", merkle_log=log)
    assert result.valid, result.errors
    assert result.log_inclusion == LOG_INCLUSION_LOCAL
    log.close()


#: The SQLite schema exactly as shipped through v0.105 (pre-ADR-0313).
_PRE_ADR0313_SCHEMA = """
CREATE TABLE leaves (
    leaf_index  INTEGER PRIMARY KEY,
    leaf_hash   TEXT    NOT NULL,
    entry_json  TEXT    NOT NULL
);
CREATE TABLE tree_heads (
    tree_size   INTEGER PRIMARY KEY,
    root_hash   TEXT    NOT NULL
);
"""


def _pre_adr0313_log(path: Path) -> str:
    """Write a log database in the released pre-ADR-0313 layout; return its v1 root."""
    from novafabric.trust.novaseal.merkle import _compute_root

    conn = sqlite3.connect(path)
    conn.executescript(_PRE_ADR0313_SCHEMA)
    hashes: list[str] = []
    for i, entry in enumerate(_legacy_entries()):
        body = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        hashes.append(_leaf_hash(body.encode()))
        conn.execute("INSERT INTO leaves VALUES (?, ?, ?)", (i, hashes[-1], body))
        conn.execute("INSERT INTO tree_heads VALUES (?, ?)", (i + 1, _compute_root(hashes)))
    conn.commit()
    conn.close()
    return _compute_root(hashes)


def test_pre_adr0313_log_database_stays_v1_and_consistent(tmp_path: Path) -> None:
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V1

    root = _pre_adr0313_log(tmp_path / "old.db")
    assert root == _legacy_record()["root_hash"]
    log = MerkleLog(tmp_path / "old.db")
    assert log.root_version == ROOT_VERSION_V1
    assert log.current_root() == root
    assert log.verify_consistency().consistent
    # Appending keeps the log v1, so every stored head stays comparable.
    entry = log.append({"i": "after-upgrade"})
    assert entry["root_version"] == ROOT_VERSION_V1
    assert log.verify_consistency().consistent
    proof = log.consistency_proof(3, 4)
    from novafabric.trust.novaseal.merkle import verify_consistency_proof

    assert verify_consistency_proof(
        proof, root, str(log.current_root()), old_size=3, new_size=4
    )
    log.close()
    # Re-opening keeps the recorded version.
    assert MerkleLog(tmp_path / "old.db").root_version == ROOT_VERSION_V1


def test_log_refuses_to_reopen_under_another_root_version(tmp_path: Path) -> None:
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V1

    MerkleLog(tmp_path / "v2.db").close()
    with pytest.raises(MerkleError, match="root_version"):
        MerkleLog(tmp_path / "v2.db", root_version=ROOT_VERSION_V1)


def test_a_head_written_by_an_older_version_into_a_v2_log_is_reported(tmp_path: Path) -> None:
    """A pre-ADR-0313 binary appending to a v2 log writes v1 heads with no version."""
    log = MerkleLog(tmp_path / "v2.db")
    for i in range(3):
        log.append({"i": i})
    log._conn.execute("UPDATE tree_heads SET root_version = NULL WHERE tree_size = 2")
    log._conn.commit()
    result = log.verify_consistency()
    assert not result.consistent
    assert any("root_version" in e for e in result.errors)
    log.close()


# ---------------------------------------------------------------------------
# (e) v2 inclusion + consistency proofs, sizes 1..17
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", range(1, 18))
def test_v2_inclusion_proofs_verify_for_every_leaf(n: int) -> None:
    from novafabric.trust.novaseal.merkle import (
        ROOT_VERSION_V2,
        _compute_inclusion_proof,
        compute_root,
        verify_inclusion_proof,
    )

    leaves = _leaves(n)
    root = compute_root(leaves, root_version=ROOT_VERSION_V2)
    for i, leaf in enumerate(leaves):
        proof = _compute_inclusion_proof(i, leaves, root_version=ROOT_VERSION_V2)
        assert verify_inclusion_proof(leaf, i, proof, root, n, root_version=ROOT_VERSION_V2)
        # Out of range, wrong leaf, tampered, truncated or extended proofs fail.
        assert not verify_inclusion_proof(leaf, n, proof, root, n, root_version=2)
        assert not verify_inclusion_proof(_leaf_hash(b"x"), i, proof, root, n, root_version=2)
        if proof:
            bad = [*proof[:-1], _leaf_hash(b"y")]
            assert not verify_inclusion_proof(leaf, i, bad, root, n, root_version=2)
            assert not verify_inclusion_proof(leaf, i, proof[:-1], root, n, root_version=2)
        assert not verify_inclusion_proof(leaf, i, [*proof, leaf], root, n, root_version=2)


@pytest.mark.parametrize("new_size", range(1, 18))
def test_v2_consistency_proofs_verify_for_every_prefix(new_size: int, tmp_path: Path) -> None:
    from novafabric.trust.novaseal.merkle import (
        ROOT_VERSION_V2,
        compute_root,
        verify_consistency_proof,
    )

    log = MerkleLog(tmp_path / "c.db")
    for i in range(new_size):
        log.append({"seq": i})
    hashes = [str(log.get_leaf_hash(i)) for i in range(new_size)]
    new_root = compute_root(hashes, root_version=ROOT_VERSION_V2)
    assert new_root == log.current_root()
    for old_size in range(1, new_size + 1):
        old_root = compute_root(hashes[:old_size], root_version=ROOT_VERSION_V2)
        assert log.root_at_size(old_size) == old_root
        proof = log.consistency_proof(old_size, new_size)
        assert proof["root_version"] == ROOT_VERSION_V2
        assert verify_consistency_proof(
            proof, old_root, new_root, old_size=old_size, new_size=new_size
        ), (old_size, new_size)
        # A different old root, or a tampered path, fails.
        assert not verify_consistency_proof(
            proof, _leaf_hash(b"x"), new_root, old_size=old_size, new_size=new_size
        )
        if proof["path"]:
            tampered = dict(proof, path=[_leaf_hash(b"y"), *proof["path"][1:]])
            assert not verify_consistency_proof(
                tampered, old_root, new_root, old_size=old_size, new_size=new_size
            )
        # The proof records its own sizes; a caller with other sizes is refused.
        if old_size > 1:
            assert not verify_consistency_proof(
                proof, old_root, new_root, old_size=old_size - 1, new_size=new_size
            )
    log.close()


def test_get_leaf_hash_exists_on_sqlite_log(tmp_path: Path) -> None:
    log = MerkleLog(tmp_path / "g.db")
    entry = log.append({"i": 0})
    assert log.get_leaf_hash(0) == entry["leaf_hash"]
    with pytest.raises(MerkleError):
        log.get_leaf_hash(5)
    log.close()


# ---------------------------------------------------------------------------
# (f) property: no two leaf lists of different lengths share a v2 root
# ---------------------------------------------------------------------------

_leaf_lists = st.lists(st.binary(max_size=6), min_size=1, max_size=40).map(
    lambda xs: [_leaf_hash(x) for x in xs]
)


@settings(max_examples=300, deadline=None)
@given(xs=_leaf_lists, ys=_leaf_lists)
def test_v2_lists_of_different_lengths_never_share_a_root(xs: list[str], ys: list[str]) -> None:
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V2, compute_root

    if len(xs) == len(ys):
        ys = [*ys, ys[-1]]
    assert compute_root(xs, root_version=ROOT_VERSION_V2) != compute_root(
        ys, root_version=ROOT_VERSION_V2
    )


@settings(max_examples=300, deadline=None)
@given(xs=_leaf_lists, k=st.integers(min_value=1, max_value=17))
def test_v2_duplicating_the_last_leaf_always_changes_the_root(xs: list[str], k: int) -> None:
    """The exact shape of the v1 defect, for every list and duplication count."""
    from novafabric.trust.novaseal.merkle import ROOT_VERSION_V2, compute_root

    padded = xs + [xs[-1]] * k
    assert compute_root(xs, root_version=ROOT_VERSION_V2) != compute_root(
        padded, root_version=ROOT_VERSION_V2
    )


# ---------------------------------------------------------------------------
# v2 golden fixtures (tests/fixtures/novaseal/root-v2) — valid and invalid
# ---------------------------------------------------------------------------

ROOT_V2 = FIXTURES / "root-v2"


def _v2_capsule(tmp_path: Path, log_entry: Path | None = None) -> Path:
    capsule = tmp_path / "v2-capsule"
    shutil.copytree(ROOT_V2 / "capsule", capsule)
    if log_entry is not None:
        shutil.copyfile(log_entry, capsule / ".seal" / "log-entry.json")
    return capsule


def _v2_sealer_log(path: Path, size: int) -> MerkleLog:
    log = MerkleLog(path)
    entries = json.loads((ROOT_V2 / "sealer-log-entries.json").read_text())
    for entry in entries[:size]:
        log.append(entry)
    return log


def test_v2_vectors_reproduce_and_match_rfc6962() -> None:
    from novafabric.evidence.merkle import _merkle_root as rfc6962_root
    from novafabric.trust.novaseal.merkle import compute_root

    vectors = json.loads((ROOT_V2 / "vectors.json").read_text())
    for row in vectors["roots"]:
        leaves = _leaves(row["tree_size"])
        assert compute_root(leaves, root_version=1) == row["v1"]
        assert compute_root(leaves, root_version=2) == row["v2"]
        assert rfc6962_root([bytes.fromhex(h) for h in leaves]).hex() == row["v2"]
    dup = vectors["duplicated_last_leaf"]
    assert dup["v1_abc"] == dup["v1_abcc"]
    assert dup["v2_abc"] != dup["v2_abcc"]


def test_v2_golden_capsule_verifies_with_no_log(tmp_path: Path) -> None:
    from novafabric.trust.novaseal import LOG_INCLUSION_PROOF, verify_seal_dir

    result = verify_seal_dir(_v2_capsule(tmp_path) / ".seal")
    assert result.valid, result.errors
    assert result.log_inclusion == LOG_INCLUSION_PROOF
    assert not any("does not commit" in n for n in result.log_notes)  # no v1 caveat


@pytest.mark.parametrize("size", [3, 4])
def test_v2_golden_capsule_verifies_against_the_sealer_log(tmp_path: Path, size: int) -> None:
    """Also after the duplicate append: the recorded size-3 head is reproduced from
    the log's first three leaves, not read from its current head."""
    from novafabric.trust.novaseal import LOG_INCLUSION_LOCAL, verify_seal_dir

    log = _v2_sealer_log(tmp_path / "s.db", size)
    result = verify_seal_dir(_v2_capsule(tmp_path) / ".seal", merkle_log=log)
    assert result.valid, result.errors
    assert result.log_inclusion == LOG_INCLUSION_LOCAL
    log.close()


@pytest.mark.parametrize(
    "name",
    sorted(p.name for p in (ROOT_V2 / "invalid").glob("*.json")),
)
@pytest.mark.parametrize("with_log", [False, True], ids=["no-log", "sealer-log"])
def test_v2_invalid_golden_log_entries_fail(tmp_path: Path, name: str, with_log: bool) -> None:
    from novafabric.trust.novaseal import LOG_INCLUSION_FAILED, verify_seal_dir

    capsule = _v2_capsule(tmp_path, ROOT_V2 / "invalid" / name)
    log = _v2_sealer_log(tmp_path / "s.db", 4) if with_log else None
    result = verify_seal_dir(capsule / ".seal", merkle_log=log)
    assert result.log_inclusion == LOG_INCLUSION_FAILED, (name, result.log_notes)
    assert not result.valid
    if log is not None:
        log.close()


def test_invalid_fixture_set_is_complete() -> None:
    assert len(list((ROOT_V2 / "invalid").glob("*.json"))) == 5


# ---------------------------------------------------------------------------
# Every structure that carries a root also carries its size and version
# ---------------------------------------------------------------------------


def test_ledger_checkpoint_merkle_block_carries_size_and_version(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from novafabric.evidence.signing import LocalSigner, generate_keypair
    from novafabric.trust.ledger._chain import write_chain
    from novafabric.trust.ledger._checkpoint import build_checkpoint, sign_checkpoint
    from novafabric.trust.novaseal.merkle import (
        ROOT_VERSION_V2,
        verify_inclusion_proof,
    )

    capsule = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    capsule.mkdir()
    (capsule / "model-calls.jsonl").write_text(json.dumps({"model": "m", "n": 0}) + "\n")
    write_chain(capsule, "model-calls")
    log = MerkleLog(tmp_path / "merkle.db")
    for i in range(2):  # the checkpoint becomes leaf 2 of 3, a non-power-of-two tree
        log.append({"other": i})
    record = build_checkpoint(capsule, node_id="n1", epoch=0, epoch_pubkey="pk")
    priv, _ = generate_keypair(tmp_path / "keys")
    sign_checkpoint(record, LocalSigner(priv), merkle_log=log)
    stored = json.loads((capsule / ".ledger" / "checkpoint.json").read_text())
    merkle = stored["merkle"]
    assert merkle["tree_size"] == 3
    assert merkle["root_version"] == ROOT_VERSION_V2
    schema_path = (
        Path(__file__).resolve().parents[2] / "schemas" / "ledger-checkpoint-v1.schema.json"
    )
    Draft202012Validator(json.loads(schema_path.read_text())).validate(stored)
    strip = len("sha256:")
    assert verify_inclusion_proof(
        merkle["leaf_hash"][strip:],
        merkle["log_index"],
        [h[strip:] for h in merkle["inclusion_proof"]],
        merkle["root"][strip:],
        merkle["tree_size"],
        root_version=merkle["root_version"],
    )
    log.close()


def test_proof_tree_follows_root_version_and_refuses_a_wrong_size() -> None:
    from novafabric.trust.merkle_view import VerifyState, build_proof_tree
    from novafabric.trust.novaseal.merkle import compute_root

    leaves = _leaves(3)
    v2_root = compute_root(leaves, root_version=2)
    tree = build_proof_tree(leaves, sealed_root=v2_root, root_version=2, tree_size=3)
    assert tree.computed_root == v2_root and tree.root_version == 2 and tree.tree_size == 3
    assert all(n.verify_state is VerifyState.verified for n in tree.nodes)
    # Without a root_version the document is v1, so the v2 root is a mismatch.
    legacy = build_proof_tree(leaves, sealed_root=v2_root)
    assert legacy.root_version == 1
    assert any(n.verify_state is VerifyState.mismatch for n in legacy.nodes)
    with pytest.raises(ValueError, match="tree_size"):
        build_proof_tree(leaves, sealed_root=v2_root, root_version=2, tree_size=4)
    with pytest.raises(ValueError):
        build_proof_tree(leaves, root_version=3)
