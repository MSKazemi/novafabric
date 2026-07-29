# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ADR-0097 §NF-042/043 — checkpoint note format + witness cosigning.

The security property under test is **non-equivocation**: a witness cosigns a new
checkpoint only when it is a verifiable append-only extension of the last one it
cosigned for that log origin. A same-size checkpoint with a different root (a
split view) is refused, which is what makes a K-of-M quorum meaningful.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.trust.novaseal.witness import (
    Checkpoint,
    Witness,
    WitnessRefusedError,
    sign_checkpoint,
    verify_quorum,
)

ORIGIN = "novafabric.example/log"


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


class TestCheckpointNote:
    def test_note_body_is_canonical(self) -> None:
        cp = Checkpoint(origin=ORIGIN, tree_size=3, root_hash="ab" * 32)
        body = cp.note_body()
        lines = body.split("\n")
        assert lines[0] == ORIGIN
        assert lines[1] == "3"
        assert lines[2]  # base64 root
        assert body.endswith("\n")

    def test_sign_and_verify_single(self) -> None:
        cp = Checkpoint(origin=ORIGIN, tree_size=3, root_hash="cd" * 32)
        k = _key()
        note = sign_checkpoint(cp, "witness-A", k)
        assert verify_quorum(note, {"witness-A": k.public_key()}, k=1) is True

    def test_tampered_body_fails_verification(self) -> None:
        cp = Checkpoint(origin=ORIGIN, tree_size=3, root_hash="cd" * 32)
        k = _key()
        note = sign_checkpoint(cp, "witness-A", k)
        tampered = note.replace("\n3\n", "\n4\n", 1)
        assert verify_quorum(tampered, {"witness-A": k.public_key()}, k=1) is False


class TestWitnessCosigning:
    def test_witness_cosigns_first_checkpoint(self) -> None:
        w = Witness("W1", _key())
        cp = Checkpoint(origin=ORIGIN, tree_size=2, root_hash="11" * 32)
        cosig = w.cosign(cp, consistency_proof=None, prev_checkpoint=None)
        assert cosig.name == "W1"

    def test_witness_refuses_split_view_same_size_different_root(self) -> None:
        # The anti-split-view core: same tree_size, different root ⇒ refuse.
        w = Witness("W1", _key())
        cp1 = Checkpoint(origin=ORIGIN, tree_size=2, root_hash="11" * 32)
        w.cosign(cp1, consistency_proof=None, prev_checkpoint=None)
        forked = Checkpoint(origin=ORIGIN, tree_size=2, root_hash="22" * 32)
        with pytest.raises(WitnessRefusedError):
            w.cosign(forked, consistency_proof=None, prev_checkpoint=cp1)

    def test_witness_refuses_going_backwards(self) -> None:
        w = Witness("W1", _key())
        cp2 = Checkpoint(origin=ORIGIN, tree_size=5, root_hash="55" * 32)
        w.cosign(cp2, consistency_proof=None, prev_checkpoint=None)
        older = Checkpoint(origin=ORIGIN, tree_size=3, root_hash="33" * 32)
        with pytest.raises(WitnessRefusedError):
            w.cosign(older, consistency_proof=None, prev_checkpoint=cp2)

    def test_witness_requires_valid_consistency_proof_to_extend(self) -> None:
        w = Witness("W1", _key())
        cp2 = Checkpoint(origin=ORIGIN, tree_size=2, root_hash="aa" * 32)
        w.cosign(cp2, consistency_proof=None, prev_checkpoint=None)
        cp4 = Checkpoint(origin=ORIGIN, tree_size=4, root_hash="bb" * 32)
        # A bogus/empty consistency proof must be rejected.
        with pytest.raises(WitnessRefusedError):
            w.cosign(cp4, consistency_proof={"old_parts": [], "tail_parts": []},
                     prev_checkpoint=cp2)


class TestQuorum:
    def test_k_of_m_quorum(self) -> None:
        cp = Checkpoint(origin=ORIGIN, tree_size=7, root_hash="77" * 32)
        ka, kb, kc = _key(), _key(), _key()
        pubs = {"A": ka.public_key(), "B": kb.public_key(), "C": kc.public_key()}
        note = sign_checkpoint(cp, "A", ka)
        note = _append_cosig(note, cp, "B", kb)
        # 2 of 3 present → 2-of-3 passes, 3-of-3 fails.
        assert verify_quorum(note, pubs, k=2) is True
        assert verify_quorum(note, pubs, k=3) is False

    def test_unknown_witness_does_not_count(self) -> None:
        cp = Checkpoint(origin=ORIGIN, tree_size=7, root_hash="77" * 32)
        ka, rogue = _key(), _key()
        note = sign_checkpoint(cp, "A", ka)
        note = _append_cosig(note, cp, "rogue", rogue)
        # Only A is a known witness; rogue's signature must not count toward quorum.
        assert verify_quorum(note, {"A": ka.public_key()}, k=2) is False
        assert verify_quorum(note, {"A": ka.public_key()}, k=1) is True


def _append_cosig(note: str, cp: Checkpoint, name: str, key: Ed25519PrivateKey) -> str:
    from novafabric.trust.novaseal.witness import add_cosignature

    return add_cosignature(note, cp, name, key)


class TestMerkleLogIntegration:
    def test_witness_cosigns_real_log_extension(self, tmp_path: Path) -> None:
        from novafabric.trust.novaseal.merkle import MerkleLog

        log = MerkleLog(db_path=tmp_path / "log.db")
        for i in range(2):
            log.append({"i": i})
        cp_old = Checkpoint(ORIGIN, log.tree_size(), log.current_root())
        w = Witness("W1", _key())
        w.cosign(cp_old, consistency_proof=None, prev_checkpoint=None)

        for i in range(2, 5):
            log.append({"i": i})
        cp_new = Checkpoint(ORIGIN, log.tree_size(), log.current_root())
        proof = log.consistency_proof(cp_old.tree_size, cp_new.tree_size)
        # A genuine append-only extension must be cosigned.
        cosig = w.cosign(cp_new, consistency_proof=proof, prev_checkpoint=cp_old)
        assert cosig.name == "W1"
