"""ADR-0261 — the redaction pack must not destroy digests and identifiers.

Three rules matched a bare token by length alone, so a 40-hex git commit id, a
32-hex MD5 and a bare UUID were redacted as if they were API keys. In an
evidence system that is the more damaging error: those tokens are how capsules
address their own content and identify their own runs.

The guards are structural, not contextual. A SHA-1 is 40 characters of
lowercase hex; a Cohere key draws 40 characters from a 62-character alphabet.
Rejecting the all-hex case therefore costs (16/62)**40 of recall -- far below
any rate these tests could observe -- while removing the false-positive class
entirely. The property tests below assert both halves of that trade.
"""

from __future__ import annotations

import random
import string

import pytest

from novafabric.capture.secrets import _RULES, PACK_VERSION

_BY_ID = {r["id"]: r["pattern"] for r in _RULES}

_ALNUM = string.ascii_letters + string.digits
_HEX = "0123456789abcdef"


def test_pack_version_records_the_guard_change() -> None:
    assert PACK_VERSION >= "0.5.0"


@pytest.mark.parametrize(
    ("rule_id", "sample"),
    [
        # git commit ids -- `git rev-parse HEAD` is an ordinary agent tool call
        ("cohere-api-key", "8f14e45fceea167a5a36dedd4bea2543deadbeef"),
        ("cohere-api-key", "a" * 40),
        # MD5 digests
        ("mistral-api-key", "d41d8cd98f00b204e9800998ecf8427e"),
        ("mistral-api-key", "0" * 32),
        # a NovaFabric run id is a UUID, and so was a legacy Pinecone key:
        # structurally identical, so the rule no longer guesses
        ("pinecone-api-key", "550e8400-e29b-41d4-a716-446655440000"),
        # the ADR-0125 guard this one generalises must keep holding
        ("together-api-key", "sha256:" + "a" * 64),
        ("together-api-key", "outputs/" + "b" * 64),
    ],
)
def test_benign_capsule_tokens_survive(rule_id: str, sample: str) -> None:
    assert _BY_ID[rule_id].search(sample) is None, (
        f"{rule_id} redacted a benign capsule token: {sample!r}"
    )


@pytest.mark.parametrize(
    ("rule_id", "sample"),
    [
        ("pinecone-api-key", "pckey_prodlabel_9f8e7d6c5b4a3210ABCDzz"),
        ("pinecone-api-key", "pcsk_9f8e7d6c5b4a32109f8e7d6c5b4a3210"),
        ("together-api-key", "b" * 64),
        ("cohere-api-key", "A" * 40),
    ],
)
def test_real_key_shapes_are_still_caught(rule_id: str, sample: str) -> None:
    assert _BY_ID[rule_id].search(sample) is not None, (
        f"{rule_id} missed a real key shape: {sample!r}"
    )


@pytest.mark.parametrize(
    ("rule_id", "length"), [("cohere-api-key", 40), ("mistral-api-key", 32)]
)
def test_recall_on_random_keys_is_unaffected(rule_id: str, length: int) -> None:
    """A key drawn from the full alphabet is still matched, every time."""
    rng = random.Random(1234)
    missed = [
        s
        for _ in range(5000)
        if _BY_ID[rule_id].search(s := "".join(rng.choices(_ALNUM, k=length))) is None
    ]
    assert not missed, f"{rule_id} missed {len(missed)} random keys, e.g. {missed[:1]}"


@pytest.mark.parametrize(
    ("rule_id", "length"), [("cohere-api-key", 40), ("mistral-api-key", 32)]
)
def test_no_digest_is_ever_flagged(rule_id: str, length: int) -> None:
    """The false-positive class is closed, not merely narrowed."""
    rng = random.Random(4321)
    hits = [
        s
        for _ in range(5000)
        if _BY_ID[rule_id].search(s := "".join(rng.choices(_HEX, k=length))) is not None
    ]
    assert not hits, f"{rule_id} flagged {len(hits)} digests, e.g. {hits[:1]}"
