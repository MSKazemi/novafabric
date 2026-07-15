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

"""Signed, content-addressed evaluator cards (NF-002, ADR-0099).

An eval card is the *reproducibility key* for an evidence-grade
:class:`~novafabric.eval.scores.Score`: it pins the exact evaluator (judge model,
prompt version, rubric, dataset version, human-agreement calibration) that produced
a score, and carries an Ed25519 signature so the binding is tamper-evident.

Design invariants (``design/spec/features/NF-002-010-signed-eval-cards.md``):

- **Content-addressed (req 5):** ``eval_card_digest`` = ``sha256`` over the card's
  canonical-JSON bytes *excluding* the ``signature`` block. Signing therefore never
  changes the digest — the digest identifies the card, the signature authenticates it.
- **Judge cards are fully specified (req 4):** a ``source: judge`` card MUST carry
  ``judge_model``, ``prompt_version``, ``rubric``, ``dataset_version`` and ``calibration``.
- **No new crypto (spec §4.4):** signing reuses the Evidence-Bundle Ed25519 path in
  ``novafabric.trust.keyring`` — this module adds no cryptographic dependency.
- **No hardcoded external endpoint (req 9, ADR-0024):** a judge model pins an
  ``endpoint_ref`` (e.g. ``env:NOVA_JUDGE_ENDPOINT``), never a literal URL.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from novafabric.eval.scores import ScoreSource
from novafabric.trust.keyring import sign_payload, verify_sig

CARD_SCHEMA_VERSION = "1.0.0"

#: Asset type under which eval cards are stored in the v0.1 Asset Registry.
EVAL_CARD_ASSET_TYPE = "eval-card"

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+([-+].*)?$")


class JudgeModel(BaseModel):
    """Identity + endpoint reference of a judge model (never the weights)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    endpoint_ref: str = Field(min_length=1)


class Calibration(BaseModel):
    """Human-agreement calibration summary for a judge card (req 4)."""

    model_config = ConfigDict(extra="forbid")

    human_agreement: float = Field(ge=-1.0, le=1.0)
    n: int = Field(ge=1)
    metric: str = Field(min_length=1)
    measured_at: str | None = None


class BiasFlags(BaseModel):
    """Calibrated magnitudes of known judge biases (req 6, NF-004 hook)."""

    model_config = ConfigDict(extra="forbid")

    position: float | None = None
    verbosity: float | None = None
    self_enhancement: float | None = None


class CardSignature(BaseModel):
    """Ed25519 signature block over the card's canonical bytes."""

    model_config = ConfigDict(extra="forbid")

    alg: str = "ed25519"
    key_id: str = Field(min_length=1)
    sig: str = Field(min_length=1)


class EvalCard(BaseModel):
    """A signed, content-addressed evaluator card.

    See ``schemas/eval-card-v1.schema.json`` for the wire contract.
    """

    model_config = ConfigDict(extra="forbid")

    card_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str
    source: ScoreSource
    judge_model: JudgeModel | None = None
    prompt_version: str | None = None
    rubric: str | None = None
    dataset_version: str | None = None
    calibration: Calibration | None = None
    bias_flags: BiasFlags | None = None
    signature: CardSignature | None = None

    @model_validator(mode="after")
    def _check(self) -> EvalCard:
        if not _SEMVER_RE.match(self.version):
            raise ValueError(f"version must be semver (X.Y.Z): {self.version!r}")
        if self.source is ScoreSource.JUDGE:
            required = (
                "judge_model",
                "prompt_version",
                "rubric",
                "dataset_version",
                "calibration",
            )
            missing = [field for field in required if getattr(self, field) is None]
            if missing:
                raise ValueError(
                    "judge eval card requires " + ", ".join(missing) + " (NF-002 req 4)"
                )
        return self


@dataclass(frozen=True)
class CardVerification:
    """Result of :func:`verify_card` (drives ``nova eval card verify`` exit code)."""

    signature_ok: bool
    calibration_present: bool
    digest: str

    @property
    def ok(self) -> bool:
        """True iff signature verifies and (for judge cards) calibration is present."""
        return self.signature_ok and self.calibration_present


def canonical_card_bytes(card: EvalCard) -> bytes:
    """Canonical-JSON bytes of *card* excluding its signature block (req 5).

    Deterministic: keys sorted, no insignificant whitespace, ``None`` fields dropped,
    enums rendered as their string value. This is the exact byte stream the digest is
    taken over and the signature is computed against.
    """
    payload = card.model_dump(mode="json", exclude={"signature"}, exclude_none=True)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def card_digest(card: EvalCard) -> str:
    """Return the ``sha256:<hex>`` content digest of *card* (req 5)."""
    return "sha256:" + hashlib.sha256(canonical_card_bytes(card)).hexdigest()


def sign_card(card: EvalCard, private_key: Ed25519PrivateKey, key_id: str) -> EvalCard:
    """Return a copy of *card* with an Ed25519 signature over its canonical bytes.

    Reuses ``trust.keyring.sign_payload``. The signature does not enter the digest, so
    ``card_digest(signed) == card_digest(unsigned)``.
    """
    sig = sign_payload(private_key, canonical_card_bytes(card))
    return card.model_copy(
        update={"signature": CardSignature(alg="ed25519", key_id=key_id, sig=sig)}
    )


def verify_card(card: EvalCard, public_key: Ed25519PublicKey) -> CardVerification:
    """Verify *card*'s signature against *public_key* and check calibration.

    ``calibration_present`` is vacuously True for non-judge cards. A missing signature
    yields ``signature_ok=False``.
    """
    digest = card_digest(card)
    if card.signature is None:
        signature_ok = False
    else:
        signature_ok = verify_sig(public_key, card.signature.sig, canonical_card_bytes(card))
    calibration_present = card.source is not ScoreSource.JUDGE or card.calibration is not None
    return CardVerification(
        signature_ok=signature_ok,
        calibration_present=calibration_present,
        digest=digest,
    )
