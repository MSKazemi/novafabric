"""Per-stream sidecar hash chain (ADR-0094 A, ``trust/ledger/_chain.py``).

For an evidence stream file ``capsule_dir/<stream>.jsonl`` we compute a sidecar
chain written to ``capsule_dir/.ledger/<stream>.chain.json`` as a list of
``{seq, prev_hash, link_hash, record_hash}`` entries, where::

    record_hash[i] = sha256(record_bytes[i])
    link_hash[i]   = sha256( str(seq[i]) || ":" || prev_hash[i] || ":" || record_hash[i] )
    prev_hash[0]   = "" ;  prev_hash[i] = link_hash[i-1]

``record_bytes`` is the as-written ``.jsonl`` line (no re-canonicalization), so
a byte-level edit at index ``k`` cascades every ``link_hash[>= k]``. The
``record_hash`` is the position-independent content digest of one record; it is
recorded so the verifier can soundly tell an in-place content edit (the record
digest multiset changes) from a reorder (the record digest multiset is
preserved, only the order changes). The ``.jsonl`` file itself is NEVER
modified — the chain is a sidecar (additive invariant).

The verifier recomputes the chain over the current ``.jsonl`` bytes and
classifies any divergence:

- ``TAMPER`` — a record's content was edited (same count, but the record-digest
  multiset diverges from the sealed one);
- ``REORDER`` — records were swapped (same count, same record-digest multiset,
  different order);
- ``TRUNCATION`` — records were removed or appended (count differs from sealed);
- ``NONE`` — clean.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

LEDGER_DIRNAME = ".ledger"


class ChainTamperClass(str, Enum):
    """Classification of a sidecar-chain verification failure."""

    NONE = "none"
    TAMPER = "tamper"
    REORDER = "reorder"
    TRUNCATION = "truncation"


@dataclass(frozen=True)
class ChainLink:
    """One entry in a per-stream sidecar chain."""

    seq: int
    prev_hash: str
    link_hash: str
    record_hash: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "prev_hash": self.prev_hash,
            "link_hash": self.link_hash,
            "record_hash": self.record_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ChainLink:
        seq = data["seq"]
        assert isinstance(seq, int)
        return cls(
            seq=seq,
            prev_hash=str(data["prev_hash"]),
            link_hash=str(data["link_hash"]),
            record_hash=str(data.get("record_hash", "")),
        )


@dataclass(frozen=True)
class ChainVerdict:
    """Result of verifying a sidecar chain against its ``.jsonl`` stream."""

    stream: str
    ok: bool
    tamper_class: ChainTamperClass
    seq_high: int
    record_count: int
    head_link_hash: str
    reasons: list[str]


def _record_hash(record_bytes: bytes) -> str:
    return hashlib.sha256(record_bytes).hexdigest()


def _link_hash(seq: int, prev_hash: str, record_hash: str) -> str:
    payload = f"{seq}:{prev_hash}:{record_hash}".encode()
    return hashlib.sha256(payload).hexdigest()


def _read_record_lines(jsonl_path: Path) -> list[bytes]:
    """Return the as-written byte content of each non-empty line.

    Each returned element includes its trailing newline so the hash binds the
    exact on-disk bytes. A trailing empty final line (no content) is ignored.
    """
    raw = jsonl_path.read_bytes()
    if not raw:
        return []
    lines: list[bytes] = []
    for line in raw.splitlines(keepends=True):
        if line.strip() == b"":
            continue
        if not line.endswith(b"\n"):
            line = line + b"\n"
        lines.append(line)
    return lines


def jsonl_path_for(capsule_dir: Path, stream_name: str) -> Path:
    """Resolve the ``.jsonl`` evidence file for *stream_name*."""
    return capsule_dir / f"{stream_name}.jsonl"


def chain_path_for(capsule_dir: Path, stream_name: str) -> Path:
    """Resolve the sidecar chain file for *stream_name*."""
    return capsule_dir / LEDGER_DIRNAME / f"{stream_name}.chain.json"


def build_chain(capsule_dir: Path, stream_name: str) -> list[ChainLink]:
    """Build the hash chain for ``capsule_dir/<stream_name>.jsonl``.

    Raises :class:`FileNotFoundError` if the stream file does not exist. An
    empty stream yields an empty chain.
    """
    jsonl_path = jsonl_path_for(capsule_dir, stream_name)
    if not jsonl_path.exists():
        raise FileNotFoundError(f"no evidence stream: {jsonl_path}")
    links: list[ChainLink] = []
    prev = ""
    for seq, record_bytes in enumerate(_read_record_lines(jsonl_path)):
        rec = _record_hash(record_bytes)
        link = _link_hash(seq, prev, rec)
        links.append(
            ChainLink(seq=seq, prev_hash=prev, link_hash=link, record_hash=rec)
        )
        prev = link
    return links


def write_chain(capsule_dir: Path, stream_name: str) -> Path:
    """Build and persist the sidecar chain; return its path.

    The ``.jsonl`` stream is never modified.
    """
    links = build_chain(capsule_dir, stream_name)
    chain_path = chain_path_for(capsule_dir, stream_name)
    chain_path.parent.mkdir(parents=True, exist_ok=True)
    chain_path.write_text(
        json.dumps([link.to_dict() for link in links], indent=2) + "\n"
    )
    return chain_path


def _load_chain(chain_path: Path) -> list[ChainLink]:
    data = json.loads(chain_path.read_text())
    return [ChainLink.from_dict(entry) for entry in data]


def verify_chain(capsule_dir: Path, stream_name: str) -> ChainVerdict:
    """Verify the recorded sidecar chain against the current ``.jsonl`` bytes.

    Raises :class:`FileNotFoundError` if either the stream or the sidecar chain
    is absent (a missing chain is "not enrolled", handled one level up by the
    checkpoint verifier).
    """
    jsonl_path = jsonl_path_for(capsule_dir, stream_name)
    chain_path = chain_path_for(capsule_dir, stream_name)
    if not jsonl_path.exists():
        raise FileNotFoundError(f"no evidence stream: {jsonl_path}")
    if not chain_path.exists():
        raise FileNotFoundError(f"no sidecar chain: {chain_path}")

    recorded = _load_chain(chain_path)
    current = build_chain(capsule_dir, stream_name)

    recorded_count = len(recorded)
    current_count = len(current)
    seq_high = current_count - 1
    head = current[-1].link_hash if current else ""

    # Length divergence => records added or removed (truncation/extension).
    if current_count != recorded_count:
        return ChainVerdict(
            stream=stream_name,
            ok=False,
            tamper_class=ChainTamperClass.TRUNCATION,
            seq_high=seq_high,
            record_count=current_count,
            head_link_hash=head,
            reasons=[f"record count {current_count} != sealed {recorded_count}"],
        )

    # Same length, perfect link-for-link match => clean.
    if all(c.link_hash == r.link_hash for c, r in zip(current, recorded)):
        return ChainVerdict(
            stream=stream_name,
            ok=True,
            tamper_class=ChainTamperClass.NONE,
            seq_high=seq_high,
            record_count=current_count,
            head_link_hash=head,
            reasons=[],
        )

    # Divergence at equal length: distinguish a reorder from a content edit by
    # comparing the position-independent multiset of sealed record digests
    # against the current one. A reorder permutes records (multiset preserved);
    # a content edit changes at least one record digest (multiset diverges).
    sealed_digests = Counter(link.record_hash for link in recorded)
    current_digests = Counter(link.record_hash for link in current)
    if sealed_digests == current_digests:
        return ChainVerdict(
            stream=stream_name,
            ok=False,
            tamper_class=ChainTamperClass.REORDER,
            seq_high=seq_high,
            record_count=current_count,
            head_link_hash=head,
            reasons=["record-digest multiset preserved but order changed (reorder)"],
        )

    return ChainVerdict(
        stream=stream_name,
        ok=False,
        tamper_class=ChainTamperClass.TAMPER,
        seq_high=seq_high,
        record_count=current_count,
        head_link_hash=head,
        reasons=["record-digest multiset diverges from sealed chain (content edit)"],
    )
