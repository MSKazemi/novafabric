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
"""Checkpoint + witness cosigning — the structural core of ADR-0097 (§NF-042/043).

This is the first slice of the verifiable, witness-cosigned transparency log: a
C2SP-style ``tlog-checkpoint`` note (§NF-042) and the ``tlog-witness`` cosigning
protocol (§NF-043). It builds directly on the existing
:class:`~novafabric.trust.novaseal.merkle.MerkleLog` — a witness reuses the log's
consistency proofs, adding no new tree machinery.

The security property is **non-equivocation** (anti-split-view): a witness holds
the last checkpoint it cosigned for a log origin and cosigns a new one **only**
when it is a verifiable append-only extension (a valid consistency proof from the
last size to the new size). A same-size checkpoint with a different root — the
signature of a forked/split view — is refused. A head is trusted only with a
**K-of-M** quorum of witness cosignatures.

Note format (a faithful, simplified C2SP note): the body is
``<origin>\n<tree_size>\n<base64(root)>\n``; each cosignature is a
``— <name> <base64(ed25519 sig over the body)>`` line appended after a blank line.
The 4-byte key-hash disambiguation of the full C2SP note is deferred to a later
slice; here a witness name maps 1:1 to a public key supplied by the verifier.

Tiles over WORM (§NF-041), the ``nova monitor`` auditor (§NF-044), COSE receipts
(§NF-045), and the portable bundle profile (§NF-047) are later slices.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from novafabric.trust.novaseal.merkle import verify_consistency_proof


class WitnessError(Exception):
    """Base class for witness/checkpoint failures."""


class WitnessRefusedError(WitnessError):
    """A witness refused to cosign: the checkpoint is not an append-only extension.

    Raised for a split view (same size, different root), a size that goes
    backwards, or a missing/invalid consistency proof for an extension.
    """


@dataclass(frozen=True)
class Checkpoint:
    """A signed-tree-head note (C2SP ``tlog-checkpoint``, §NF-042).

    ``root_hash`` is the log's Merkle root as a hex string (as produced by
    :meth:`MerkleLog.current_root`).
    """

    origin: str
    tree_size: int
    root_hash: str

    def note_body(self) -> str:
        """Return the canonical checkpoint note body (the bytes witnesses sign)."""
        root_b64 = base64.standard_b64encode(bytes.fromhex(self.root_hash)).decode("ascii")
        return f"{self.origin}\n{self.tree_size}\n{root_b64}\n"


@dataclass(frozen=True)
class Cosignature:
    """One witness's signature line over a checkpoint body."""

    name: str
    signature: bytes

    def note_line(self) -> str:
        sig_b64 = base64.standard_b64encode(self.signature).decode("ascii")
        return f"— {self.name} {sig_b64}"


def _cosign_body(body: str, name: str, key: Ed25519PrivateKey) -> Cosignature:
    return Cosignature(name=name, signature=key.sign(body.encode("utf-8")))


def sign_checkpoint(cp: Checkpoint, name: str, key: Ed25519PrivateKey) -> str:
    """Return a signed checkpoint note: the body plus one cosignature line."""
    body = cp.note_body()
    cosig = _cosign_body(body, name, key)
    return body + "\n" + cosig.note_line() + "\n"


def add_cosignature(note: str, cp: Checkpoint, name: str, key: Ed25519PrivateKey) -> str:
    """Append another witness's cosignature line to an existing signed note."""
    cosig = _cosign_body(cp.note_body(), name, key)
    return note.rstrip("\n") + "\n" + cosig.note_line() + "\n"


def split_note(note: str) -> tuple[str, list[Cosignature]]:
    """Split a signed note into its body and its cosignatures.

    The body is the first three lines (origin, tree_size, base64 root), each
    terminated by ``\\n``; cosignature lines follow after a blank separator line.
    """
    lines = note.split("\n")
    if len(lines) < 3:
        raise WitnessError("malformed checkpoint note: too few lines")
    body = "\n".join(lines[:3]) + "\n"
    cosigs: list[Cosignature] = []
    for line in lines[3:]:
        line = line.strip()
        if not line.startswith("— "):
            continue
        parts = line[2:].split(" ")
        if len(parts) != 2:
            continue
        name, sig_b64 = parts
        try:
            cosigs.append(Cosignature(name=name, signature=base64.standard_b64decode(sig_b64)))
        except (ValueError, binascii.Error):
            continue
    return body, cosigs


def verify_quorum(note: str, witness_pubkeys: dict[str, Ed25519PublicKey], k: int) -> bool:
    """Return True iff *note* carries at least *k* valid cosignatures from *witness_pubkeys*.

    A cosignature counts only when its name is a known witness, its signature over
    the note body verifies under that witness's public key, and the witness has not
    already been counted (duplicate names never inflate the quorum).
    """
    body, cosigs = split_note(note)
    counted: set[str] = set()
    for cosig in cosigs:
        if cosig.name not in witness_pubkeys or cosig.name in counted:
            continue
        try:
            witness_pubkeys[cosig.name].verify(cosig.signature, body.encode("utf-8"))
        except InvalidSignature:
            continue
        counted.add(cosig.name)
    return len(counted) >= k


@dataclass
class Witness:
    """A cosigning witness (C2SP ``tlog-witness``, §NF-043).

    Holds the last checkpoint it cosigned per log origin and cosigns a new one only
    when it is a verifiable append-only extension of that last checkpoint.
    """

    name: str
    key: Ed25519PrivateKey
    _last: dict[str, Checkpoint] = field(default_factory=dict)

    def cosign(
        self,
        checkpoint: Checkpoint,
        *,
        consistency_proof: dict[str, Any] | None,
        prev_checkpoint: Checkpoint | None,
    ) -> Cosignature:
        """Cosign *checkpoint* iff it extends the last one cosigned for its origin.

        Args:
            checkpoint: the new checkpoint to cosign.
            consistency_proof: a proof (from :meth:`MerkleLog.consistency_proof`)
                from the previous cosigned size to the new size — required when the
                size grows.
            prev_checkpoint: the previous checkpoint the caller believes this witness
                last cosigned (used to seed the proof's old root); the witness cross-
                checks it against its own remembered state.

        Raises:
            WitnessRefusedError: split view, size regression, or missing/invalid proof.
        """
        last = self._last.get(checkpoint.origin)
        if last is not None:
            if checkpoint.tree_size < last.tree_size:
                raise WitnessRefusedError(
                    f"checkpoint size {checkpoint.tree_size} is behind the last cosigned "
                    f"size {last.tree_size} for origin {checkpoint.origin!r}"
                )
            if checkpoint.tree_size == last.tree_size:
                if checkpoint.root_hash != last.root_hash:
                    raise WitnessRefusedError(
                        "split view: same tree_size with a different root than the last "
                        f"cosigned checkpoint for origin {checkpoint.origin!r}"
                    )
                # Identical to the last cosigned head — re-cosigning is safe.
            else:
                self._verify_extension(last, checkpoint, consistency_proof, prev_checkpoint)
        # First checkpoint for this origin (last is None), or a verified extension.
        self._last[checkpoint.origin] = checkpoint
        return _cosign_body(checkpoint.note_body(), self.name, self.key)

    def _verify_extension(
        self,
        last: Checkpoint,
        new: Checkpoint,
        consistency_proof: dict[str, Any] | None,
        prev_checkpoint: Checkpoint | None,
    ) -> None:
        if consistency_proof is None:
            raise WitnessRefusedError(
                "a consistency proof is required to cosign a larger checkpoint"
            )
        if prev_checkpoint is not None and (
            prev_checkpoint.tree_size != last.tree_size
            or prev_checkpoint.root_hash != last.root_hash
        ):
            raise WitnessRefusedError(
                "prev_checkpoint does not match this witness's last cosigned checkpoint"
            )
        if not verify_consistency_proof(consistency_proof, last.root_hash, new.root_hash):
            raise WitnessRefusedError(
                "consistency proof does not show an append-only extension from the last "
                f"cosigned root to the new root (origin {new.origin!r})"
            )


__all__ = [
    "Checkpoint",
    "Cosignature",
    "Witness",
    "WitnessError",
    "WitnessRefusedError",
    "add_cosignature",
    "sign_checkpoint",
    "split_note",
    "verify_quorum",
]
