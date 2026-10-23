"""Ledger verifier + exit-code taxonomy (ADR-0094 A, ``trust/ledger/_verify.py``).

``verify_ledger`` walks each per-stream sidecar chain, recomputes its head, and
compares it to the signed :class:`CheckpointRecord` head — so an adversary who
edits a ``.jsonl`` record *and* recomputes the sidecar chain still fails,
because the checkpoint pinned the original head. It then (optionally) verifies
the checkpoint DSSE signature, checks epoch monotonicity, Merkle inclusion, and
the external anchor.

Exit-code taxonomy (ADR-0094 §A):

==== ===================================================
exit  meaning
==== ===================================================
0     pass
3     content edit (chain cascade mismatch)
4     sequence reorder
5     truncation versus signed ``seq_high``
6     bad checkpoint signature
7     epoch rollback
8     Merkle inclusion failure
9     external-anchor mismatch
10    no checkpoint present
==== ===================================================

Degrade-safe: a missing checkpoint returns exit 10, never a crash.

Merkle inclusion (exit 8) is checked when the checkpoint carries a ``merkle``
block (``sign_checkpoint(..., merkle_log=...)``): the leaf must be the hash of
this checkpoint's signed body, the root is verified only together with its
``tree_size`` (ADR-0313 D4) and under its ``root_version``, and the inclusion
proof must recompute the root. A checkpoint without a ``merkle`` block — the
``nova ledger anchor`` local-finalize path never appends to a log — makes no
inclusion claim, and exit 0 then does not speak to inclusion.

**What that proves without a log — and what it does not.** The block is checked
only against itself, and it sits outside the checkpoint signature (it is written
after signing). Anyone can therefore replace it with a self-consistent tree no log
ever held — a one-leaf tree whose root is the leaf passes — or remove it. Without
a log, exit 0 means the block is *self-consistent* (``merkle_inclusion:
"self-consistent"``), not that the checkpoint is in any log.

**With the log** (``merkle_log=`` / ``nova ledger verify --merkle-db``) the block
is checked against it: the log must hold this checkpoint's leaf at ``log_index``
and reproduce the recorded ``(tree_size, root)`` from its own leaves under the
recorded root version — recomputed, never read from a stored head. A checkpoint
without a block must then be found in the log by its leaf hash, so a removed
block no longer hides anything. Either failure is exit 8 (``merkle_inclusion:
"log"`` on success). This proves inclusion in *the log you supplied*; whether that
log is the one others see is a witness/anchor question (ADR-0097).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeGuard

from novafabric.evidence.intoto import (
    DSSE_PAYLOAD_TYPE,
    dsse_pae,
    make_intoto_statement,
)
from novafabric.trust.ledger._chain import (
    ChainTamperClass,
    verify_chain,
)
from novafabric.trust.ledger._checkpoint import (
    CHECKPOINT_PREDICATE_TYPE,
    checkpoint_path,
)
from novafabric.trust.novaseal.merkle import (
    ROOT_VERSION_V1,
    UnsupportedRootVersionError,
    _leaf_hash,
    inclusion_proof_length,
    root_version_of,
    verify_inclusion_proof,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from novafabric.trust.novaseal.merkle import MerkleLog, PostgresMerkleLog

VerifyFn = Callable[[bytes, bytes], bool]

#: ``LedgerVerdict.merkle_inclusion``: the checkpoint's leaf was found in the
#: supplied log and the recorded tree head was reproduced from that log's leaves.
MERKLE_INCLUSION_LOG = "log"
#: The ``merkle`` block verified against itself only — no log was supplied. It is
#: unsigned, so this is not evidence that any log holds the checkpoint.
MERKLE_INCLUSION_SELF_CONSISTENT = "self-consistent"
#: No ``merkle`` block and no log: the checkpoint makes no inclusion claim.
MERKLE_INCLUSION_NONE = "none"


class LedgerExit(IntEnum):
    """ADR-0094 ledger verifier exit-code taxonomy."""

    OK = 0
    TAMPER = 3
    REORDER = 4
    TRUNCATION = 5
    BAD_SIGNATURE = 6
    EPOCH_ROLLBACK = 7
    MERKLE_FAILURE = 8
    ANCHOR_MISMATCH = 9
    NO_CHECKPOINT = 10


@dataclass
class LedgerVerdict:
    """Structured ledger verification result."""

    ok: bool
    exit: LedgerExit
    reasons: list[str] = field(default_factory=list)
    stream_verdicts: dict[str, str] = field(default_factory=dict)
    # What Merkle inclusion was established — one of the ``MERKLE_INCLUSION_*``
    # values. Meaningful when ``ok``; see the module docstring.
    merkle_inclusion: str = MERKLE_INCLUSION_NONE


def exit_code_for(verdict: LedgerVerdict) -> int:
    """Map a :class:`LedgerVerdict` to its process exit code."""
    return int(verdict.exit)


_TAMPER_TO_EXIT = {
    ChainTamperClass.TAMPER: LedgerExit.TAMPER,
    ChainTamperClass.REORDER: LedgerExit.REORDER,
    ChainTamperClass.TRUNCATION: LedgerExit.TRUNCATION,
}


def _load_checkpoint(capsule_dir: Path) -> dict[str, Any] | None:
    path = checkpoint_path(capsule_dir)
    if not path.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(path.read_text())
        return data
    except (OSError, ValueError):
        return None


def _verify_streams(
    capsule_dir: Path, checkpoint: dict[str, Any]
) -> LedgerVerdict | None:
    """Return a failing verdict on the first stream issue, else None.

    Compares each recomputed head to the signed checkpoint head so a re-chained
    edit (locally consistent chain, head changed) is still caught.
    """
    stream_verdicts: dict[str, str] = {}
    for head in checkpoint.get("streams", []):
        stream = str(head["stream"])
        signed_head = str(head["head_link_hash"])
        signed_count = int(head["record_count"])
        try:
            verdict = verify_chain(capsule_dir, stream)
        except FileNotFoundError as exc:
            return LedgerVerdict(
                ok=False,
                exit=LedgerExit.TRUNCATION,
                reasons=[f"stream {stream}: {exc}"],
                stream_verdicts=stream_verdicts,
            )
        stream_verdicts[stream] = verdict.tamper_class.value

        # Local chain reports a tamper class directly.
        if verdict.tamper_class is not ChainTamperClass.NONE:
            return LedgerVerdict(
                ok=False,
                exit=_TAMPER_TO_EXIT[verdict.tamper_class],
                reasons=[f"stream {stream}: {'; '.join(verdict.reasons)}"],
                stream_verdicts=stream_verdicts,
            )

        # Local chain is self-consistent but may have been re-chained after an
        # edit/truncation: compare against the signed head.
        current_head = "sha256:" + verdict.head_link_hash if verdict.head_link_hash else (
            "sha256:" + "0" * 64
        )
        if current_head != signed_head:
            if verdict.record_count != signed_count:
                exit_class = LedgerExit.TRUNCATION
                reason = (
                    f"stream {stream}: record_count {verdict.record_count} "
                    f"!= signed {signed_count} (re-chained truncation)"
                )
            else:
                exit_class = LedgerExit.TAMPER
                reason = (
                    f"stream {stream}: head {current_head} != signed "
                    f"{signed_head} (re-chained content edit)"
                )
            return LedgerVerdict(
                ok=False,
                exit=exit_class,
                reasons=[reason],
                stream_verdicts=stream_verdicts,
            )
    return None


def _signing_body(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in checkpoint.items()
        if k not in ("signature", "merkle", "anchor")
    }


def _verify_signature(checkpoint: dict[str, Any], verify_fn: VerifyFn) -> bool:
    signature = checkpoint.get("signature")
    if not signature or "value" not in signature:
        return False
    statement = make_intoto_statement(
        predicate_type=CHECKPOINT_PREDICATE_TYPE,
        subject_name=f"run-capsule/{checkpoint['capsule_id']}",
        subject_sha256="sha256:" + "0" * 64,
        predicate=_signing_body(checkpoint),
    )
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()
    pae = dsse_pae(DSSE_PAYLOAD_TYPE, payload)
    try:
        sig = base64.b64decode(signature["value"])
    except (ValueError, TypeError):
        return False
    return verify_fn(pae, sig)


_SHA256_PREFIX = "sha256:"


def _sha256_hex(value: object) -> str | None:
    """``"sha256:<64 hex>"`` → the hex digest; anything else → None."""
    if not isinstance(value, str) or not value.startswith(_SHA256_PREFIX):
        return None
    digest = value[len(_SHA256_PREFIX) :]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None
    return digest


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _merkle_failure(checkpoint: dict[str, Any]) -> str | None:
    """Why the checkpoint's ``merkle`` block does not verify, or None when it does.

    None as well when the block is absent: such a checkpoint was never appended
    to a log and claims no inclusion. See the module docstring for the rules.
    """
    if "merkle" not in checkpoint or checkpoint["merkle"] is None:
        return None
    merkle = checkpoint["merkle"]
    if not isinstance(merkle, dict):
        return "merkle block is not an object"
    root = _sha256_hex(merkle.get("root"))
    leaf_hash = _sha256_hex(merkle.get("leaf_hash"))
    if root is None or leaf_hash is None:
        return "merkle block has no well-formed sha256 root and leaf_hash"
    body = json.dumps(_signing_body(checkpoint), sort_keys=True, separators=(",", ":"))
    if _leaf_hash(body.encode("utf-8")) != leaf_hash:
        return (
            "merkle leaf_hash is not the hash of this checkpoint's signed body "
            "(the block names another log entry, or the body changed after logging)"
        )
    try:
        root_version = root_version_of(merkle)
    except UnsupportedRootVersionError as exc:
        return f"merkle block: {exc}"
    tree_size = merkle.get("tree_size")
    if not _is_int(tree_size) or tree_size < 1:
        return (
            "merkle root recorded without its tree_size; a Merkle root is verified only "
            "together with its tree size (ADR-0313) — checkpoints logged before v0.106.0 "
            "lack it"
        )
    log_index = merkle.get("log_index")
    if not _is_int(log_index) or not 0 <= log_index < tree_size:
        return "merkle log_index is missing or outside the recorded tree"
    proof_field = merkle.get("inclusion_proof")
    if not isinstance(proof_field, list):
        return "merkle block carries no inclusion_proof, so inclusion cannot be verified"
    proof = [_sha256_hex(h) for h in proof_field]
    siblings = [h for h in proof if h is not None]
    if len(siblings) != len(proof) or (
        root_version == ROOT_VERSION_V1 and len(siblings) != inclusion_proof_length(tree_size)
    ):
        return "merkle inclusion_proof is malformed"
    if not verify_inclusion_proof(
        leaf_hash, log_index, siblings, root, tree_size, root_version=root_version
    ):
        return (
            f"Merkle inclusion proof for log_index={log_index} does not recompute root "
            f"{root[:16]}… at tree_size={tree_size} (root v{root_version})"
        )
    return None


def _body_leaf_hash(checkpoint: dict[str, Any]) -> str:
    """The log leaf this checkpoint's signed body hashes to (what ``sign_checkpoint`` appends)."""
    body = json.dumps(_signing_body(checkpoint), sort_keys=True, separators=(",", ":"))
    return _leaf_hash(body.encode("utf-8"))


def _merkle_log_failure(
    checkpoint: dict[str, Any], merkle_log: "MerkleLog | PostgresMerkleLog"
) -> str | None:
    """Why the checkpoint is not included in *merkle_log*, or None when it is.

    Runs after :func:`_merkle_failure`, so a present block is already well formed
    and self-consistent. A block's tree head must be reproduced from the log's own
    leaves; a checkpoint without a block must be in the log by its leaf hash.
    """
    from novafabric.trust.novaseal.merkle import MerkleError  # noqa: PLC0415

    leaf_hash = _body_leaf_hash(checkpoint)
    merkle = checkpoint.get("merkle")
    try:
        if not isinstance(merkle, dict):
            if merkle_log.find_leaf_index(leaf_hash) is None:
                return (
                    "the checkpoint carries no merkle block and its signed body is not a "
                    "leaf of the supplied Merkle log"
                )
            return None
        log_index = merkle["log_index"]
        tree_size = merkle["tree_size"]
        root = str(merkle["root"])[len(_SHA256_PREFIX) :]
        root_version = root_version_of(merkle)
        try:
            held = merkle_log.get_leaf_hash(log_index)
        except MerkleError:
            held = None
        if held != leaf_hash:
            return (
                f"the supplied Merkle log does not hold this checkpoint at "
                f"log_index={log_index} — the merkle block describes a tree that is not "
                "this log"
            )
        reproduced = merkle_log.compute_root_at_size(tree_size, root_version=root_version)
    except MerkleError as exc:
        return f"Merkle log error: {exc}"
    if reproduced != root:
        return (
            f"the supplied Merkle log's leaves do not reproduce the recorded tree head "
            f"(tree_size={tree_size}, root {root[:16]}…, root v{root_version})"
        )
    return None


def _verify_anchor(checkpoint: dict[str, Any]) -> bool:
    """Check the external anchor's ``anchored_value`` against the checkpoint.

    The anchored value is the Merkle root (when present) or the checkpoint
    signature digest (local finalize). A mismatch is anchor tampering.
    """
    anchor = checkpoint.get("anchor")
    if not anchor:
        return True  # no anchor present (offline / deferred) — not a mismatch
    if anchor.get("method") == "none":
        return True
    anchored = str(anchor.get("anchored_value", ""))
    merkle = checkpoint.get("merkle")
    if merkle and merkle.get("root"):
        return anchored == str(merkle["root"])
    # local finalize: anchored_value == sha256 of the signature value
    signature = checkpoint.get("signature") or {}
    value = str(signature.get("value", ""))
    expected = "sha256:" + hashlib.sha256(value.encode()).hexdigest()
    return anchored == expected


def verify_ledger(
    capsule_dir: Path,
    checkpoint_envelope: dict[str, Any] | None = None,
    verify_fn: VerifyFn | None = None,
    *,
    observed_epoch: int | None = None,
    merkle_log: "MerkleLog | PostgresMerkleLog | None" = None,
) -> LedgerVerdict:
    """Verify the Adversary-Anchored Accountability Ledger for *capsule_dir*.

    *checkpoint_envelope* is accepted for API symmetry with the DSSE envelope
    returned by :func:`sign_checkpoint`; the verification reads the persisted
    ``.ledger/checkpoint.json`` for the signed body. *verify_fn* (when given)
    performs the cryptographic signature check; without it the verification is
    structural (chains + epoch + anchor) only. *merkle_log* (when given) is the
    log the checkpoint was appended to: inclusion is then checked against it
    rather than only against the checkpoint's own (unsigned) ``merkle`` block —
    see the module docstring for what each ``merkle_inclusion`` result proves.
    """
    checkpoint = _load_checkpoint(capsule_dir)
    if checkpoint is None:
        return LedgerVerdict(
            ok=False,
            exit=LedgerExit.NO_CHECKPOINT,
            reasons=[f"no checkpoint at {checkpoint_path(capsule_dir)}"],
        )

    stream_failure = _verify_streams(capsule_dir, checkpoint)
    if stream_failure is not None:
        return stream_failure

    if verify_fn is not None and not _verify_signature(checkpoint, verify_fn):
        return LedgerVerdict(
            ok=False,
            exit=LedgerExit.BAD_SIGNATURE,
            reasons=["checkpoint DSSE signature did not verify"],
        )

    epoch = int(checkpoint.get("identity", {}).get("epoch", 0))
    if observed_epoch is not None and epoch < observed_epoch:
        return LedgerVerdict(
            ok=False,
            exit=LedgerExit.EPOCH_ROLLBACK,
            reasons=[
                f"checkpoint epoch {epoch} below observed epoch {observed_epoch}"
            ],
        )

    merkle_failure = _merkle_failure(checkpoint)
    if merkle_failure is None and merkle_log is not None:
        merkle_failure = _merkle_log_failure(checkpoint, merkle_log)
    if merkle_failure is not None:
        return LedgerVerdict(
            ok=False,
            exit=LedgerExit.MERKLE_FAILURE,
            reasons=[merkle_failure],
        )
    if merkle_log is not None:
        merkle_inclusion = MERKLE_INCLUSION_LOG
    elif isinstance(checkpoint.get("merkle"), dict):
        merkle_inclusion = MERKLE_INCLUSION_SELF_CONSISTENT
    else:
        merkle_inclusion = MERKLE_INCLUSION_NONE

    if not _verify_anchor(checkpoint):
        return LedgerVerdict(
            ok=False,
            exit=LedgerExit.ANCHOR_MISMATCH,
            reasons=["external anchor value does not match checkpoint"],
        )

    return LedgerVerdict(ok=True, exit=LedgerExit.OK, merkle_inclusion=merkle_inclusion)
